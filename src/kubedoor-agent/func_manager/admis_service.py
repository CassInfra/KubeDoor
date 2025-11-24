import asyncio
import base64
import json
from datetime import datetime, timedelta

from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

import utils


class AdmisService:
    def __init__(self, apps_v1_api, core_v1_api, admission_api, request_futures):
        self.apps_v1 = apps_v1_api
        self.core_v1 = core_v1_api
        self.admission_api = admission_api
        self.request_futures = request_futures
        self.ws_conn = None

    def set_ws_conn(self, ws_conn):
        self.ws_conn = ws_conn

    async def admis_switch(self, request):
        if not self.admission_api:
            return web.json_response({"message": "未初始化 Admission API", "success": False}, status=500)

        action = request.query.get("action")
        res = await self._get_mutating_webhook()

        if action == "get":
            return web.json_response(res)
        if action == "on":
            if res.get("is_on"):
                return web.json_response({"message": "Webhook is already opened!", "success": True})
            create_res = await self._create_mutating_webhook()
            if create_res:
                return web.json_response(create_res, status=500)
        elif action == "off":
            if not res.get("is_on"):
                return web.json_response({"message": "Webhook is already closed!", "success": True})
            delete_res = await self._delete_mutating_webhook()
            if delete_res:
                return web.json_response(delete_res, status=500)

        return web.json_response({"message": "执行成功", "success": True})

    async def admis_mutate(self, request):
        request_info = await request.json()
        obj = request_info['request']['object']
        old_object = request_info['request']['oldObject']
        kind = request_info['request']['kind']['kind']
        operation = request_info['request']['operation']
        uid = request_info['request']['uid']
        namespace = obj['metadata']['namespace']
        deployment_name = obj['metadata']['name']
        logger.info(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到请求{obj}")

        annotations = obj.get('metadata', {}).get('annotations', {})
        scale_temp = annotations.get('scale.temp')
        if scale_temp:
            try:
                time_part = scale_temp.split('@')[0]
                temp_time = datetime.strptime(time_part, "%Y-%m-%d %H:%M:%S")
                current_time = datetime.now()
                time_diff = current_time - temp_time

                if time_diff <= timedelta(minutes=5):
                    if (kind == 'Scale' and operation == 'UPDATE') or (
                        kind == 'Deployment'
                        and operation == 'UPDATE'
                        and obj['spec']['template'] == old_object['spec']['template']
                        and old_object['spec']['replicas'] != obj['spec']['replicas']
                    ):
                        logger.info(
                            f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】临时扩缩容在5分钟内，直接放行: {scale_temp}"
                        )
                        return web.json_response(self._admis_pass(uid))
            except Exception as exc:
                logger.warning(f"解析scale.temp时间失败: {exc}")

        if self.ws_conn is None or self.ws_conn.closed:
            utils.send_msg(
                f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】连接 kubedoor-master 失败"
            )
            return web.json_response(self._admis_fail(uid, 503, "连接 kubedoor-master 失败"))

        response_future = asyncio.get_event_loop().create_future()
        self.request_futures[uid] = response_future
        await self.ws_conn.send_json(
            {"type": "admis", "request_id": uid, "namespace": namespace, "deployment": deployment_name}
        )
        try:
            result = await asyncio.wait_for(response_future, timeout=30)
            logger.info(f"response_future 收到 admis 响应：{uid} {result}")
        except asyncio.TimeoutError:
            if uid in self.request_futures:
                del self.request_futures[uid]
            utils.send_msg(
                f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】连接 kubedoor-master 响应超时"
            )
            return web.json_response(self._admis_fail(uid, 504, "等待 kubedoor-master 响应超时"))

        if len(result) == 2:
            utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】{result[1]}")
            if result[0] == 200:
                return web.json_response(self._admis_pass(uid))
            return web.json_response(self._admis_fail(uid, result[0], result[1]))

        (
            pod_count,
            pod_count_ai,
            pod_count_manual,
            request_cpu_m,
            request_mem_mb,
            limit_cpu_m,
            limit_mem_mb,
            scheduler,
        ) = result
        replicas = pod_count_manual if pod_count_manual >= 0 else (pod_count_ai if pod_count_ai >= 0 else pod_count)
        request_cpu_m = 10 if 0 <= request_cpu_m < 10 else request_cpu_m
        request_mem_mb = 1 if request_mem_mb == 0 else request_mem_mb
        deploy_baseinfo = (
            f"副本数:{replicas}, 请求CPU:{request_cpu_m}m, 请求内存:{request_mem_mb}MB, 限制CPU:{limit_cpu_m}m, 限制内存:{limit_mem_mb}MB"
        )
        logger.info(deploy_baseinfo)

        try:
            if kind == 'Scale' and operation == 'UPDATE':
                admis_msg = (
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到scale请求，仅修改replicas为: {replicas}"
                )
                logger.info(admis_msg)
                utils.send_msg(admis_msg)
                return web.json_response(self._scale_only(uid, replicas))
            if kind == 'Deployment' and operation == 'CREATE':
                admis_msg = (
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到 create 请求，修改所有参数"
                )
                logger.info(admis_msg)
                utils.send_msg(f'{admis_msg}\n{deploy_baseinfo}\n固定节点均衡: {scheduler}')
                resources_dict = obj['spec']['template']['spec']['containers'][0].get('resources', {}) or {}
                resources_dict.setdefault('requests', {})
                resources_dict.setdefault('limits', {})
                return web.json_response(
                    await self._update_all(
                        replicas,
                        namespace,
                        deployment_name,
                        request_cpu_m,
                        request_mem_mb,
                        limit_cpu_m,
                        limit_mem_mb,
                        resources_dict,
                        uid,
                        scheduler,
                    )
                )
            if (
                kind == 'Deployment'
                and operation == 'UPDATE'
                and obj['spec']['template'] != old_object['spec']['template']
            ):
                admis_msg = (
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到 update 请求，修改所有参数"
                )
                logger.info(admis_msg)
                utils.send_msg(f'{admis_msg}\n{deploy_baseinfo}\n固定节点均衡: {scheduler}')
                resources_dict = obj['spec']['template']['spec']['containers'][0].get('resources', {}) or {}
                resources_dict.setdefault('requests', {})
                resources_dict.setdefault('limits', {})
                return web.json_response(
                    await self._update_all(
                        replicas,
                        namespace,
                        deployment_name,
                        request_cpu_m,
                        request_mem_mb,
                        limit_cpu_m,
                        limit_mem_mb,
                        resources_dict,
                        uid,
                        scheduler,
                    )
                )
            if (
                kind == 'Scale'
                and operation == 'CREATE'
                and obj['spec']['replicas'] == old_object['spec']['replicas']
                and obj['spec']['template'] == old_object['spec']['template']
            ):
                logger.info(
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】object 和 oldObject 相等，跳过"
                )
                return web.json_response(self._admis_pass(uid))
            if (
                kind == 'Scale'
                and operation == 'UPDATE'
                and old_object['spec']['replicas'] != obj['spec']['replicas']
            ):
                admis_msg = (
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】收到 update 请求，仅修改replicas为: {replicas}"
                )
                logger.info(admis_msg)
                utils.send_msg(admis_msg)
                return web.json_response(self._scale_only(uid, replicas))
            if (
                kind == 'Deployment'
                and operation == 'UPDATE'
                and obj['spec']['template'] == old_object['spec']['template']
                and obj['spec']['replicas'] == old_object['spec']['replicas']
            ):
                logger.info(
                    f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】template 和 replicas 没变，不处理"
                )
                return web.json_response(self._admis_pass(uid))

            admis_msg = (
                f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】不符合预设判断条件: {kind} {operation}，直接放行"
            )
            logger.error(admis_msg)
            utils.send_msg(admis_msg)
            return web.json_response(self._admis_pass(uid))
        except Exception as exc:
            logger.exception(f"【{namespace}】【{deployment_name}】Webhook 处理错误：{exc}")
            utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】处理错误：{exc}")
            return web.json_response({"error": str(exc)}, status=500)

    async def _update_namespace_label(self, namespace_name, action):
        if not self.core_v1:
            raise ApiException("CoreV1Api 未初始化")

        label_key = "kubedoor-ignore"
        label_value = 'true' if action == "add" else None
        patch_body = {"metadata": {"labels": {label_key: label_value}}}

        try:
            response = await self.core_v1.patch_namespace(name=namespace_name, body=patch_body)
            logger.info(
                f"Label '{label_key}: {label_value}' {'added to' if action == 'add' else 'removed from'} namespace '{namespace_name}' successfully."
            )
            logger.info(f"Updated namespace labels: {response.metadata.labels}")
        except ApiException as exc:
            logger.error(f"Exception when patching namespace '{namespace_name}': {exc}")
            raise

    async def _get_mutating_webhook(self):
        webhook_name = "kubedoor-admis-configuration"
        try:
            await self.admission_api.read_mutating_webhook_configuration(name=webhook_name)
            logger.info(f"MutatingWebhookConfiguration '{webhook_name}' exists.")
            return {"is_on": True}
        except ApiException as exc:
            if exc.status == 404:
                logger.error(f"MutatingWebhookConfiguration '{webhook_name}' does not exist.")
                return {"is_on": False}
            error_message = json.loads(exc.body).get("message") if hasattr(exc, 'body') and exc.body else "执行失败"
            logger.error(f"Exception when reading MutatingWebhookConfiguration: {exc}")
            return {"message": error_message}

    async def _create_mutating_webhook(self):
        webhook_name = "kubedoor-admis-configuration"
        namespace = "kubedoor"
        webhook_config = client.V1MutatingWebhookConfiguration(
            metadata=client.V1ObjectMeta(name=webhook_name),
            webhooks=[
                client.V1MutatingWebhook(
                    name="kubedoor-admis.mutating.webhook",
                    client_config=client.AdmissionregistrationV1WebhookClientConfig(
                        service=client.AdmissionregistrationV1ServiceReference(
                            namespace=namespace, name="kubedoor-agent", path="/api/admis", port=443
                        ),
                        ca_bundle=utils.BASE64CA,
                    ),
                    rules=[
                        client.V1RuleWithOperations(
                            operations=["CREATE", "UPDATE"],
                            api_groups=["apps"],
                            api_versions=["v1"],
                            resources=["deployments", "deployments/scale"],
                            scope="*",
                        )
                    ],
                    failure_policy="Fail",
                    match_policy="Equivalent",
                    namespace_selector=client.V1LabelSelector(
                        match_expressions=[
                            client.V1LabelSelectorRequirement(key="kubedoor-ignore", operator="DoesNotExist")
                        ]
                    ),
                    side_effects="None",
                    timeout_seconds=30,
                    admission_review_versions=["v1"],
                    reinvocation_policy="Never",
                )
            ],
        )

        try:
            response = await self.admission_api.create_mutating_webhook_configuration(body=webhook_config)
            logger.info(f"MutatingWebhookConfiguration created: {response.metadata.name}")
            await self._update_namespace_label("kube-system", "add")
            await self._update_namespace_label("kubedoor", "add")
        except ApiException as exc:
            error_message = json.loads(exc.body).get("message") if hasattr(exc, 'body') and exc.body else "执行失败"
            logger.error(f"Exception when creating MutatingWebhookConfiguration: {exc}")
            return {"message": error_message}

    async def _delete_mutating_webhook(self):
        webhook_name = "kubedoor-admis-configuration"
        try:
            await self.admission_api.delete_mutating_webhook_configuration(name=webhook_name)
            logger.info(f"MutatingWebhookConfiguration '{webhook_name}' deleted successfully.")
            await self._update_namespace_label("kube-system", "delete")
            await self._update_namespace_label("kubedoor", "delete")
        except ApiException as exc:
            error_message = json.loads(exc.body).get("message") if hasattr(exc, 'body') and exc.body else "执行失败"
            logger.error(f"Exception when deleting MutatingWebhookConfiguration: {exc}")
            return {"message": error_message}

    async def _update_all(
        self,
        replicas,
        namespace,
        deployment_name,
        request_cpu_m,
        request_mem_mb,
        limit_cpu_m,
        limit_mem_mb,
        resources_dict,
        uid,
        scheduler,
    ):
        change_list = []
        scheduler_enabled = bool(scheduler)
        if scheduler_enabled:
            info_dict = await self._get_pod_label_and_max_unavailable(namespace, deployment_name)
            if not info_dict:
                raise web.HTTPInternalServerError(text=f"未查到【{namespace}】【{deployment_name}】pod标签")
            pod_label = info_dict.get("app_label_value")
            value = self._get_deployment_affinity(namespace, deployment_name, pod_label)
            logger.info("配置affinity（选择节点和反亲和性）")
            affinity = {"op": "replace", "path": "/spec/template/spec/affinity", "value": value}
            change_list.append(affinity)
            max_unavailable_value = info_dict.get("maxUnavailable_value")
            max_unavailable_ratio = self._process_max_unavailable(max_unavailable_value)
            if int(replicas) * max_unavailable_ratio < 1:
                logger.info(f"maxUnavailable_value原值为{max_unavailable_value}，改为1")
                max_unavailable_value = 1
            restart_strategy = {
                "op": "replace",
                "path": "/spec/strategy/rollingUpdate/maxUnavailable",
                "value": max_unavailable_value,
            }
            change_list.append(restart_strategy)
        else:
            has_scheduler = await self._get_deployment_affinity_old(namespace, deployment_name)
            if has_scheduler:
                remove_node_affinity = {"op": "remove", "path": "/spec/template/spec/affinity/nodeAffinity"}
                change_list.append(remove_node_affinity)
                logger.info(
                    f"检查到【{namespace}】【{deployment_name}】已配置节点选择，并且调度开关已关闭，删除nodeAffinity字段"
                )

        change_list.append({"op": "replace", "path": "/spec/replicas", "value": replicas})
        logger.info(f"改前：{resources_dict}")
        if request_cpu_m > 0:
            resources_dict["requests"]["cpu"] = f"{request_cpu_m}m"
        else:
            utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 request_cpu_m")
        if request_mem_mb > 0:
            resources_dict["requests"]["memory"] = f"{request_mem_mb}Mi"
        else:
            utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 request_mem_mb")
        if limit_cpu_m > 0:
            resources_dict["limits"]["cpu"] = f"{limit_cpu_m}m"
        else:
            utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 limit_cpu_m")
        if limit_mem_mb > 0:
            resources_dict["limits"]["memory"] = f"{limit_mem_mb}Mi"
        else:
            utils.send_msg(f"admis:【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】未配置 limit_mem_mb")
        logger.info(f"改后：{resources_dict}")
        change_list.append(
            {
                "op": "add",
                "path": "/spec/template/spec/containers/0/resources",
                "value": resources_dict,
            }
        )
        code = base64.b64encode(json.dumps(change_list).encode()).decode()
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True, "patchType": "JSONPatch", "patch": code},
        }

    async def _get_pod_label_and_max_unavailable(self, namespace, deployment_name):
        try:
            deployment = await self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
            app_label_value = deployment.spec.template.metadata.labels.get('app')
            max_unavailable_value = deployment.spec.strategy.rolling_update.max_unavailable
            return {"app_label_value": app_label_value, "maxUnavailable_value": max_unavailable_value}
        except ApiException as exc:
            logger.error(f"Kubernetes API 错误: {exc}")
        except Exception as exc:
            logger.error(f"发生未知错误: {exc}")
        return None

    async def _get_deployment_affinity_old(self, namespace, deployment_name):
        try:
            deployment = await self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
            affinity = deployment.spec.template.spec.affinity
            if affinity and affinity.node_affinity:
                node_affinity = affinity.node_affinity
                required = node_affinity.required_during_scheduling_ignored_during_execution
                if required:
                    for term in required.node_selector_terms:
                        for expression in term.match_expressions:
                            if 'kubedoor-scheduler' in expression.values:
                                return True
            return False
        except Exception as exc:
            logger.error(f"检查deployment affinity配置失败: {exc}")
            return False

    @staticmethod
    def _get_deployment_affinity(namespace, deployment_name, pod_label):
        affinity = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": f"{namespace}.{deployment_name}",
                                    "operator": "In",
                                    "values": [f"{utils.NODE_LABLE_VALUE}"],
                                }
                            ]
                        }
                    ]
                }
            },
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {
                            "matchExpressions": [{"key": "app", "operator": "In", "values": [pod_label]}]
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            },
        }
        return affinity

    @staticmethod
    def _process_max_unavailable(max_unavailable):
        if isinstance(max_unavailable, (int, float)):
            return max_unavailable
        if isinstance(max_unavailable, str) and '%' in max_unavailable:
            return float(max_unavailable.strip('%')) / 100
        if isinstance(max_unavailable, str) and '.' in max_unavailable:
            return float(max_unavailable)
        return int(max_unavailable)

    @staticmethod
    def _admis_pass(uid):
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True},
        }

    @staticmethod
    def _admis_fail(uid, code, message):
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": False, "status": {"code": code, "message": message}},
        }

    @staticmethod
    def _scale_only(uid, replicas):
        patch_replicas = {"op": "replace", "path": "/spec/replicas", "value": replicas}
        code = base64.b64encode(json.dumps([patch_replicas]).encode()).decode()
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True, "patchType": "JSONPatch", "patch": code},
        }
