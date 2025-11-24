"""
K8S资源管理器 - 支持create、apply、replace操作
整合自test-apply.py，适配kubedoor-agent环境
"""

import yaml
import codecs
from typing import Any


class _LiteralSafeDumper(yaml.SafeDumper):
    pass


def _normalize_scalar(value: str) -> str:
    normalized = value.replace("\r\n", "\n")
    try:
        normalized = codecs.decode(normalized.encode("latin1", "backslashreplace"), "unicode_escape")
    except Exception:
        normalized = normalized.replace("\\n", "\n").replace("\\t", "\t")
    normalized = normalized.replace("\r\n", "\n")
    if "\n" in normalized:
        normalized = "\n".join(line.rstrip(" ") for line in normalized.split("\n"))
    return normalized


def _literal_str_representer(dumper: yaml.Dumper, data: str):
    normalized = _normalize_scalar(data)
    if "\n" in normalized:
        return dumper.represent_scalar("tag:yaml.org,2002:str", normalized, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", normalized)


_LiteralSafeDumper.add_representer(str, _literal_str_representer)


def _convert_multiline_strings(value: Any):
    if isinstance(value, str):
        return _normalize_scalar(value)
    if isinstance(value, list):
        return [_convert_multiline_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _convert_multiline_strings(item) for key, item in value.items()}
    return value
import json
import copy
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.utils import create_from_dict
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger


class ThreeWayMerge:
    """三方合并算法实现"""

    @staticmethod
    def merge(
        current: Dict[str, Any], last_applied: Optional[Dict[str, Any]], server_state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        三方合并主函数

        Args:
            current: 用户当前期望的配置
            last_applied: 上次应用的配置
            server_state: 服务器当前实际状态

        Returns:
            Strategic merge patch
        """
        if last_applied is None:
            # 如果没有历史配置，直接返回current
            # 这会让K8S保留服务器上的其他字段
            return current

        # 执行三方合并
        patch = {}

        # 合并metadata
        if 'metadata' in current:
            patch['metadata'] = ThreeWayMerge._merge_metadata(
                current.get('metadata', {}), last_applied.get('metadata', {}), server_state.get('metadata', {})
            )

        # 合并spec
        if 'spec' in current:
            patch['spec'] = ThreeWayMerge._merge_object(
                current.get('spec', {}),
                last_applied.get('spec', {}),
                server_state.get('spec', {}),
                merge_strategy='merge',
            )

        # 合并data (ConfigMap, Secret)
        if 'data' in current:
            patch['data'] = ThreeWayMerge._merge_object(
                current.get('data', {}),
                last_applied.get('data', {}),
                server_state.get('data', {}),
                merge_strategy='merge',
            )

        # 合并stringData (Secret)
        if 'stringData' in current:
            patch['stringData'] = current['stringData']

        # 保留apiVersion和kind
        patch['apiVersion'] = current['apiVersion']
        patch['kind'] = current['kind']

        return patch

    @staticmethod
    def _merge_metadata(current: Dict[str, Any], last: Dict[str, Any], server: Dict[str, Any]) -> Dict[str, Any]:
        """
        合并metadata字段
        """
        metadata = {}

        # 必须字段
        metadata['name'] = current.get('name')
        if 'namespace' in current:
            metadata['namespace'] = current['namespace']

        # 合并labels
        if 'labels' in current or 'labels' in last:
            metadata['labels'] = ThreeWayMerge._merge_map(
                current.get('labels', {}), last.get('labels', {}), server.get('labels', {})
            )

        # 合并annotations
        if 'annotations' in current or 'annotations' in last:
            metadata['annotations'] = ThreeWayMerge._merge_map(
                current.get('annotations', {}), last.get('annotations', {}), server.get('annotations', {})
            )

        return metadata

    @staticmethod
    def _merge_map(current: Dict[str, Any], last: Dict[str, Any], server: Dict[str, Any]) -> Dict[str, Any]:
        """
        合并map类型 (labels, annotations等)

        规则：
        1. current中的键 -> 保留
        2. last中有但current中无 -> 删除（设为None）
        3. server中有但last和current都无 -> 保留（其他控制器管理的）
        """
        result = {}

        # 1. 添加current中的所有键
        result.update(current)

        # 2. 标记删除：在last中但不在current中
        for key in last:
            if key not in current:
                result[key] = None

        # 3. 保留server中的键（如果不在last和current中）
        # 这些是其他控制器或工具添加的
        for key, value in server.items():
            if key not in current and key not in last:
                result[key] = value

        return result

    @staticmethod
    def _merge_object(
        current: Dict[str, Any], last: Dict[str, Any], server: Dict[str, Any], merge_strategy: str = 'merge'
    ) -> Dict[str, Any]:
        """
        递归合并对象

        Args:
            merge_strategy: 'merge' 或 'replace'
        """
        result = {}

        # 收集所有键
        all_keys = set(current.keys()) | set(last.keys()) | set(server.keys())

        for key in all_keys:
            current_val = current.get(key)
            last_val = last.get(key)
            server_val = server.get(key)

            if current_val is not None:
                # current中有这个字段
                if isinstance(current_val, dict):
                    if isinstance(last_val, dict):
                        # 递归合并嵌套对象
                        result[key] = ThreeWayMerge._merge_object(
                            current_val, last_val, server_val if isinstance(server_val, dict) else {}, merge_strategy
                        )
                    else:
                        # last中没有或类型不同，直接使用current
                        result[key] = current_val
                elif isinstance(current_val, list):
                    # 列表处理
                    result[key] = ThreeWayMerge._merge_list(
                        current_val,
                        last_val if isinstance(last_val, list) else None,
                        server_val if isinstance(server_val, list) else None,
                        key,
                    )
                else:
                    # 简单值，直接使用current
                    result[key] = current_val

            elif last_val is not None:
                # current中没有但last中有 -> 删除
                result[key] = None

            elif server_val is not None:
                # current和last都没有，但server有 -> 保留（其他工具管理的）
                result[key] = server_val

        return result

    @staticmethod
    def _merge_list(
        current: List[Any], last: Optional[List[Any]], server: Optional[List[Any]], field_name: str
    ) -> List[Any]:
        """
        合并列表

        对于特定字段（如containers, volumes），使用name作为merge key
        其他列表直接替换
        """
        # 需要按name合并的字段
        merge_by_name_fields = {'containers', 'initContainers', 'volumes', 'volumeMounts', 'ports', 'env', 'envFrom'}

        if field_name in merge_by_name_fields and current:
            # 检查列表项是否有name字段
            if isinstance(current[0], dict) and 'name' in current[0]:
                return ThreeWayMerge._merge_list_by_key(current, last or [], server or [], 'name')

        # 其他列表直接使用current替换
        return current

    @staticmethod
    def _merge_list_by_key(current: List[Dict], last: List[Dict], server: List[Dict], key: str) -> List[Dict]:
        """
        按指定key合并列表
        """
        result = []

        # 构建映射
        current_map = {item.get(key): item for item in current if key in item}
        last_map = {item.get(key): item for item in last if key in item}
        server_map = {item.get(key): item for item in server if key in item}

        # 处理current中的项
        for name, item in current_map.items():
            if name in last_map:
                # 在last中也存在，进行合并
                merged_item = ThreeWayMerge._merge_object(
                    item, last_map[name], server_map.get(name, {}), merge_strategy='merge'
                )
            else:
                # last中不存在，直接使用current
                merged_item = item
            result.append(merged_item)

        # 保留server中的项（如果不在current和last中）
        for name, item in server_map.items():
            if name not in current_map and name not in last_map:
                result.append(item)

        return result


class K8sResourceManager:
    """K8S资源异步管理器 - 适配kubedoor-agent环境"""

    LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"

    def __init__(self, api_client: client.ApiClient):
        """
        初始化资源管理器

        Args:
            api_client: 已初始化的kubernetes API客户端
        """
        self.api_client = api_client

    def parse_yaml_content(self, yaml_content: str) -> List[Dict[str, Any]]:
        """
        解析YAML内容，支持多文档
        """
        resources = []
        for doc in yaml.safe_load_all(yaml_content):
            if doc:  # 过滤空文档
                if not isinstance(doc, dict):
                    raise ValueError(f"YAML document is not a dictionary, but a {type(doc).__name__}")
                resources.append(doc)
        return resources

    def get_api_instance(self, api_version: str, kind: str) -> Tuple[Any, str]:
        """
        根据资源类型获取API实例
        """
        # 定义资源映射
        resource_map = {
            # Core v1
            'v1': {
                'Pod': (client.CoreV1Api, 'pod'),
                'Service': (client.CoreV1Api, 'service'),
                'ConfigMap': (client.CoreV1Api, 'config_map'),
                'Secret': (client.CoreV1Api, 'secret'),
                'Namespace': (client.CoreV1Api, 'namespace'),
                'PersistentVolume': (client.CoreV1Api, 'persistent_volume'),
                'PersistentVolumeClaim': (client.CoreV1Api, 'persistent_volume_claim'),
                'ServiceAccount': (client.CoreV1Api, 'service_account'),
                'Node': (client.CoreV1Api, 'node'),
                'ResourceQuota': (client.CoreV1Api, 'resource_quota'),
                'LimitRange': (client.CoreV1Api, 'limit_range'),
            },
            # Apps v1
            'apps/v1': {
                'Deployment': (client.AppsV1Api, 'deployment'),
                'StatefulSet': (client.AppsV1Api, 'stateful_set'),
                'DaemonSet': (client.AppsV1Api, 'daemon_set'),
                'ReplicaSet': (client.AppsV1Api, 'replica_set'),
            },
            # Batch v1
            'batch/v1': {
                'Job': (client.BatchV1Api, 'job'),
                'CronJob': (client.BatchV1Api, 'cron_job'),
            },
            # Networking v1
            'networking.k8s.io/v1': {
                'Ingress': (client.NetworkingV1Api, 'ingress'),
                'NetworkPolicy': (client.NetworkingV1Api, 'network_policy'),
                'IngressClass': (client.NetworkingV1Api, 'ingress_class'),
            },
            # RBAC v1
            'rbac.authorization.k8s.io/v1': {
                'Role': (client.RbacAuthorizationV1Api, 'role'),
                'RoleBinding': (client.RbacAuthorizationV1Api, 'role_binding'),
                'ClusterRole': (client.RbacAuthorizationV1Api, 'cluster_role'),
                'ClusterRoleBinding': (client.RbacAuthorizationV1Api, 'cluster_role_binding'),
            },
            # Storage v1
            'storage.k8s.io/v1': {
                'StorageClass': (client.StorageV1Api, 'storage_class'),
                'VolumeAttachment': (client.StorageV1Api, 'volume_attachment'),
            },
        }

        if api_version in resource_map and kind in resource_map[api_version]:
            api_class, resource_name = resource_map[api_version][kind]
            return api_class(self.api_client), resource_name

        raise ValueError(f"Unsupported resource: {api_version}/{kind}")

    def is_namespaced(self, kind: str) -> bool:
        """判断资源是否是namespace级别"""
        cluster_scoped = {
            'Namespace',
            'Node',
            'PersistentVolume',
            'ClusterRole',
            'ClusterRoleBinding',
            'StorageClass',
            'IngressClass',
        }
        return kind not in cluster_scoped

    # ==================== CREATE ====================

    async def create_resources(self, yaml_content: str) -> List[Dict[str, Any]]:
        """
        创建资源 (kubectl create -f)
        """
        resources = self.parse_yaml_content(yaml_content)
        results = []

        for resource in resources:
            try:
                # 清理资源配置，移除创建时不应该存在的字段
                clean_resource = self._clean_config_for_create(resource)
                await create_from_dict(self.api_client, clean_resource, verbose=True)

                results.append(
                    {
                        'status': 'success',
                        'operation': 'create',
                        'kind': resource.get('kind'),
                        'name': resource.get('metadata', {}).get('name'),
                        'namespace': resource.get('metadata', {}).get('namespace'),
                        'message': 'Resource created successfully',
                    }
                )

                logger.info(f"Created {resource.get('kind')}/{resource.get('metadata', {}).get('name')}")

            except ApiException as e:
                results.append(
                    {
                        'status': 'error',
                        'operation': 'create',
                        'kind': resource.get('kind'),
                        'name': resource.get('metadata', {}).get('name'),
                        'error': str(e.reason),
                        'message': e.body,
                    }
                )
                logger.error(f"Failed to create resource: {e}")

        return results

    # ==================== APPLY ====================

    async def apply_resources(self, yaml_content: str) -> List[Dict[str, Any]]:
        """
        应用资源 (kubectl apply -f)
        优先使用Server-Side Apply，降级到Client-Side Apply
        """
        resources = self.parse_yaml_content(yaml_content)
        results = []

        for resource in resources:
            try:
                # 尝试Server-Side Apply
                result = await self._server_side_apply(resource)
                results.append(result)
            except (ApiException, AttributeError) as e:
                # 降级到Client-Side Apply
                logger.warning(f"SSA failed, using client-side apply: {e}")
                try:
                    result = await self._client_side_apply(resource)
                    results.append(result)
                except Exception as e:
                    results.append(
                        {
                            'status': 'error',
                            'operation': 'apply',
                            'kind': resource.get('kind'),
                            'name': resource.get('metadata', {}).get('name'),
                            'error': str(e),
                            'message': 'Apply failed',
                        }
                    )
                    logger.error(f"Failed to apply resource: {e}")

        return results

    async def _server_side_apply(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        """
        Server-Side Apply实现
        """
        kind = resource.get('kind')
        name = resource.get('metadata', {}).get('name')
        namespace = resource.get('metadata', {}).get('namespace', 'default')

        api_instance, resource_name = self.get_api_instance(resource.get('apiVersion'), kind)

        is_namespaced = self.is_namespaced(kind)

        # 构建方法名
        method_name = f"patch_namespaced_{resource_name}" if is_namespaced else f"patch_{resource_name}"
        patch_method = getattr(api_instance, method_name, None)

        if not patch_method:
            raise AttributeError(f"Method {method_name} not found")

        # 清理资源对象，移除 SSA 不允许的字段
        clean_resource = self._clean_config_for_ssa(resource)

        # 执行SSA
        kwargs = {
            'name': name,
            'body': clean_resource,
            'field_manager': 'KubeDoor',
            'force': True,
            '_content_type': 'application/apply-patch+yaml',
        }

        if is_namespaced:
            kwargs['namespace'] = namespace

        await patch_method(**kwargs)

        logger.info(f"Applied {kind}/{name} via SSA")

        return {
            'status': 'success',
            'operation': 'apply',
            'method': 'server-side-apply',
            'kind': kind,
            'name': name,
            'namespace': namespace if is_namespaced else None,
            'message': 'Resource applied successfully (SSA)',
        }

    async def _client_side_apply(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        """
        Client-Side Apply实现（三方合并）
        """
        kind = resource.get('kind')
        name = resource.get('metadata', {}).get('name')
        namespace = resource.get('metadata', {}).get('namespace', 'default')

        api_instance, resource_name = self.get_api_instance(resource.get('apiVersion'), kind)

        is_namespaced = self.is_namespaced(kind)

        # 准备新配置（添加last-applied注解）
        new_config = self._add_last_applied_annotation(resource)

        # 尝试读取现有资源
        try:
            existing = await self._read_resource(api_instance, resource_name, name, namespace, is_namespaced)
            existing_dict = self._k8s_object_to_dict(existing)

            # 获取last-applied配置
            last_applied = self._get_last_applied_config(existing_dict)

            # 三方合并
            patch = ThreeWayMerge.merge(new_config, last_applied, existing_dict)

            # 应用patch
            await self._patch_resource(api_instance, resource_name, name, namespace, is_namespaced, patch)

            action = 'configured'

        except ApiException as e:
            if e.status == 404:
                # 资源不存在，创建
                # 清理配置，移除创建时不应该存在的字段
                clean_config = self._clean_config_for_create(new_config)
                await create_from_dict(self.api_client, clean_config)
                action = 'created'
            else:
                raise

        logger.info(f"Applied {kind}/{name} via client-side apply ({action})")

        return {
            'status': 'success',
            'operation': 'apply',
            'method': 'client-side-apply',
            'action': action,
            'kind': kind,
            'name': name,
            'namespace': namespace if is_namespaced else None,
            'message': f'Resource {action} (client-side apply with three-way merge)',
        }

    def _add_last_applied_annotation(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        """
        添加last-applied-configuration注解
        """
        config = copy.deepcopy(resource)

        if 'metadata' not in config:
            config['metadata'] = {}
        if 'annotations' not in config['metadata']:
            config['metadata']['annotations'] = {}

        # 创建一个干净的配置副本用于注解
        clean_config = self._clean_config(resource)
        config['metadata']['annotations'][self.LAST_APPLIED_ANNOTATION] = json.dumps(
            clean_config, sort_keys=True, separators=(',', ':')
        )

        return config

    def _clean_config(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        """
        清理配置，移除运行时字段
        """
        config = copy.deepcopy(resource)

        # 移除metadata中的运行时字段
        if 'metadata' in config:
            metadata = config['metadata']
            runtime_fields = [
                'uid',
                'resourceVersion',
                'generation',
                'creationTimestamp',
                'deletionTimestamp',
                'deletionGracePeriodSeconds',
                'finalizers',
                'managedFields',
                'selfLink',
            ]
            for field in runtime_fields:
                metadata.pop(field, None)

        # 移除status字段
        config.pop('status', None)

        return config

    def _clean_config_for_ssa(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        """
        为Server-Side Apply清理配置
        """
        config = self._clean_config(resource)

        # SSA不需要last-applied注解
        if 'metadata' in config and 'annotations' in config['metadata']:
            config['metadata']['annotations'].pop(self.LAST_APPLIED_ANNOTATION, None)
            # 如果annotations为空，移除它
            if not config['metadata']['annotations']:
                del config['metadata']['annotations']

        return config

    def _clean_config_for_create(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        """
        为create操作清理配置
        """
        config = self._clean_config(resource)

        # 移除可能导致创建失败的字段
        if 'metadata' in config:
            metadata = config['metadata']
            # 移除这些字段，它们会由K8s自动生成
            create_time_fields = ['creationTimestamp', 'uid', 'resourceVersion', 'generation']
            for field in create_time_fields:
                metadata.pop(field, None)

        return config

    def _get_last_applied_config(self, existing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        从现有资源中获取last-applied配置
        """
        annotations = existing.get('metadata', {}).get('annotations', {})
        last_applied_str = annotations.get(self.LAST_APPLIED_ANNOTATION)

        if last_applied_str:
            try:
                return json.loads(last_applied_str)
            except json.JSONDecodeError:
                logger.warning("Failed to parse last-applied-configuration annotation")

        return None

    # ==================== REPLACE ====================

    async def replace_resources(self, yaml_content: str) -> List[Dict[str, Any]]:
        """
        替换资源 (kubectl replace -f)
        """
        resources = self.parse_yaml_content(yaml_content)
        results = []

        for resource in resources:
            try:
                result = await self._replace_resource(resource)
                results.append(result)
            except Exception as e:
                results.append(
                    {
                        'status': 'error',
                        'operation': 'replace',
                        'kind': resource.get('kind'),
                        'name': resource.get('metadata', {}).get('name'),
                        'error': str(e),
                        'message': 'Replace failed',
                    }
                )
                logger.error(f"Failed to replace resource: {e}")

        return results

    async def _replace_resource(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        """
        替换单个资源
        """
        kind = resource.get('kind')
        name = resource.get('metadata', {}).get('name')
        namespace = resource.get('metadata', {}).get('namespace', 'default')

        api_instance, resource_name = self.get_api_instance(resource.get('apiVersion'), kind)

        is_namespaced = self.is_namespaced(kind)

        # 构建方法名
        method_name = f"replace_namespaced_{resource_name}" if is_namespaced else f"replace_{resource_name}"
        replace_method = getattr(api_instance, method_name)

        # 清理资源配置
        clean_resource = self._clean_config_for_create(resource)

        # 执行replace
        kwargs = {'name': name, 'body': clean_resource}
        if is_namespaced:
            kwargs['namespace'] = namespace

        await replace_method(**kwargs)

        logger.info(f"Replaced {kind}/{name}")

        return {
            'status': 'success',
            'operation': 'replace',
            'kind': kind,
            'name': name,
            'namespace': namespace if is_namespaced else None,
            'message': 'Resource replaced successfully',
        }

    # ==================== 辅助方法 ====================

    async def _read_resource(self, api_instance, resource_name: str, name: str, namespace: str, is_namespaced: bool):
        """读取资源"""
        method_name = f"read_namespaced_{resource_name}" if is_namespaced else f"read_{resource_name}"
        read_method = getattr(api_instance, method_name)

        kwargs = {'name': name}
        if is_namespaced:
            kwargs['namespace'] = namespace

        return await read_method(**kwargs)

    async def _patch_resource(
        self, api_instance, resource_name: str, name: str, namespace: str, is_namespaced: bool, patch: Dict[str, Any]
    ):
        """patch资源"""
        method_name = f"patch_namespaced_{resource_name}" if is_namespaced else f"patch_{resource_name}"
        patch_method = getattr(api_instance, method_name)

        kwargs = {'name': name, 'body': patch}
        if is_namespaced:
            kwargs['namespace'] = namespace

        return await patch_method(**kwargs)

    def _k8s_object_to_dict(self, k8s_obj) -> Dict[str, Any]:
        """将K8s对象转换为字典"""
        if hasattr(k8s_obj, 'to_dict'):
            return k8s_obj.to_dict()
        else:
            # 如果没有to_dict方法，使用API client的sanitize_for_serialization
            return self.api_client.sanitize_for_serialization(k8s_obj)

    async def execute_operation(self, method: str, yaml_content: str) -> List[Dict[str, Any]]:
        """
        执行K8S操作
        """
        if method == 'create':
            return await self.create_resources(yaml_content)
        elif method == 'apply':
            return await self.apply_resources(yaml_content)
        elif method == 'replace':
            return await self.replace_resources(yaml_content)
        else:
            raise ValueError(f"Unsupported method: {method}")

    # ==================== GET RESOURCE CONTENT ====================

    async def get_resource_content(
        self, api_version: str, kind: str, name: str, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取指定资源的YAML内容

        Args:
            api_version: 资源的API版本 (如: v1, apps/v1)
            kind: 资源类型 (如: Pod, Service, Deployment)
            name: 资源名称
            namespace: 命名空间 (对于namespace级别的资源)

        Returns:
            包含资源YAML内容的字典
        """
        try:
            # 获取API实例
            api_instance, resource_name = self.get_api_instance(api_version, kind)
            is_namespaced = self.is_namespaced(kind)

            # 读取资源
            resource_obj = await self._read_resource(
                api_instance, resource_name, name, namespace or 'default', is_namespaced
            )

            # 使用async with确保ApiClient正确关闭，与service_manager.py保持一致
            async with client.ApiClient() as api_client:
                resource_dict = api_client.sanitize_for_serialization(resource_obj)

            # 清理不需要的字段
            cleaned_dict = self._clean_resource_for_display(resource_dict)

            # 转换为YAML字符串
            converted_dict = _convert_multiline_strings(cleaned_dict)
            yaml_content = yaml.dump(
                converted_dict,
                default_flow_style=False,
                allow_unicode=True,
                Dumper=_LiteralSafeDumper,
            )

            logger.info(f"Successfully retrieved {kind}/{name} content")

            return {
                'status': 'success',
                'kind': kind,
                'name': name,
                'namespace': namespace if is_namespaced else None,
                'yaml_content': yaml_content,
            }

        except ApiException as e:
            logger.error(f"Failed to get {kind}/{name} content: {e}")
            if e.status == 404:
                error_msg = f"{kind} '{name}'"
                if namespace and self.is_namespaced(kind):
                    error_msg += f" 在命名空间 '{namespace}'"
                error_msg += " 中不存在"
                return {'status': 'error', 'kind': kind, 'name': name, 'error': error_msg, 'error_code': 404}
            return {
                'status': 'error',
                'kind': kind,
                'name': name,
                'error': f"Kubernetes API错误: {e.reason}",
                'error_code': e.status,
            }
        except Exception as e:
            logger.error(f"Failed to get {kind}/{name} content: {e}")
            return {
                'status': 'error',
                'kind': kind,
                'name': name,
                'error': f"服务器内部错误: {str(e)}",
                'error_code': 500,
            }

    def _clean_resource_for_display(self, resource_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        清理资源字典，移除显示时不需要的字段
        与service_manager.py中的get_service_content函数保持一致的清理逻辑
        """
        cleaned = copy.deepcopy(resource_dict)

        # 清理不需要的字段，与原get_service_content函数保持一致
        if 'metadata' in cleaned and 'managedFields' in cleaned['metadata']:
            del cleaned['metadata']['managedFields']
        if 'status' in cleaned:
            del cleaned['status']

        # 清理kubectl注解
        annotations = cleaned.get('metadata', {}).get('annotations', {})
        if 'kubectl.kubernetes.io/last-applied-configuration' in annotations:
            del annotations['kubectl.kubernetes.io/last-applied-configuration']

        return cleaned

    # ==================== DELETE RESOURCE ====================

    async def delete_resource(
        self, api_version: str, kind: str, name: str, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        删除指定资源

        Args:
            api_version: 资源的API版本 (如: v1, apps/v1)
            kind: 资源类型 (如: Pod, Service, Deployment)
            name: 资源名称
            namespace: 命名空间 (对于namespace级别的资源)

        Returns:
            删除操作结果
        """
        try:
            # 获取API实例
            api_instance, resource_name = self.get_api_instance(api_version, kind)
            is_namespaced = self.is_namespaced(kind)

            # 构建删除方法名
            if is_namespaced:
                delete_method_name = f"delete_namespaced_{resource_name}"
            else:
                delete_method_name = f"delete_{resource_name}"

            delete_method = getattr(api_instance, delete_method_name, None)
            if not delete_method:
                raise AttributeError(f"Delete method {delete_method_name} not found")

            # 执行删除
            kwargs = {'name': name}
            if is_namespaced:
                kwargs['namespace'] = namespace or 'default'

            await delete_method(**kwargs)

            logger.info(f"Successfully deleted {kind}/{name}")

            return {
                'status': 'success',
                'operation': 'delete',
                'kind': kind,
                'name': name,
                'namespace': namespace if is_namespaced else None,
                'message': f'{kind} "{name}" 删除成功',
            }

        except ApiException as e:
            logger.error(f"Failed to delete {kind}/{name}: {e}")
            if e.status == 404:
                error_msg = f"{kind} '{name}'"
                if namespace and self.is_namespaced(kind):
                    error_msg += f" 在命名空间 '{namespace}'"
                error_msg += " 中不存在"
                return {
                    'status': 'error',
                    'operation': 'delete',
                    'kind': kind,
                    'name': name,
                    'error': error_msg,
                    'error_code': 404,
                }
            return {
                'status': 'error',
                'operation': 'delete',
                'kind': kind,
                'name': name,
                'error': f"Kubernetes API错误: {e.reason}",
                'error_code': e.status,
            }
        except Exception as e:
            logger.error(f"Failed to delete {kind}/{name}: {e}")
            return {
                'status': 'error',
                'operation': 'delete',
                'kind': kind,
                'name': name,
                'error': f"服务器内部错误: {str(e)}",
                'error_code': 500,
            }


async def handle_k8s_operation(request: web.Request) -> web.Response:
    """
    处理K8S操作请求

    POST /api/agent/res/ops?method=<create|apply|replace>
    Body: {"yaml_content": "..."}
    """
    try:
        # 获取method参数
        method = request.query.get('method')
        if not method:
            return web.json_response({'success': False, 'error': 'Missing method parameter. Use: create, apply, or replace'}, status=400)

        if method not in ['create', 'apply', 'replace']:
            return web.json_response({'success': False, 'error': f'Invalid method: {method}'}, status=400)

        # 获取YAML内容
        try:
            body = await request.json()
            yaml_content = body.get('yaml_content')
        except Exception as e:
            return web.json_response({'success': False, 'error': f'Invalid JSON body: {str(e)}'}, status=400)

        if not yaml_content:
            return web.json_response({'success': False, 'error': 'Missing yaml_content in request body'}, status=400)

        # 获取kubedoor-agent.py中已初始化的API客户端
        # 通过导入模块获取全局变量
        import sys

        kubedoor_agent = sys.modules.get('__main__')
        if kubedoor_agent and hasattr(kubedoor_agent, 'core_v1'):
            # 使用已存在的API客户端
            api_client = kubedoor_agent.core_v1.api_client
        else:
            # 降级：创建新的API客户端
            from kubernetes_asyncio import client

            api_client = client.ApiClient()

        # 创建资源管理器实例
        k8s_manager = K8sResourceManager(api_client)

        # 执行操作
        results = await k8s_manager.execute_operation(method, yaml_content)

        # 统计结果
        success_count = sum(1 for r in results if r.get('status') == 'success')
        error_count = sum(1 for r in results if r.get('status') == 'error')

        status_code = 207 if error_count > 0 else 200

        return web.json_response(
            {
                'success': error_count == 0,
                'method': method,
                'results': results,
                'summary': {'total': len(results), 'success': success_count, 'failed': error_count},
            },
            status=status_code,
        )
    except Exception as e:
        logger.error(f"Error handling K8S operation request: {e}", exc_info=True)
        return web.json_response({'success': False, 'error': str(e)}, status=500)


async def handle_get_resource_content(request: web.Request) -> web.Response:
    """
    处理获取K8S资源内容请求

    GET /api/agent/res/content?namespace=<namespace>&resource_type=<resource_type>&resource_name=<resource_name>
    """
    try:
        # 获取查询参数

        namespace = request.query.get('namespace')
        resource_type = request.query.get('resource_type')
        resource_name = request.query.get('resource_name')

        # 验证必需参数
        if not all([namespace, resource_type, resource_name]):
            return web.json_response({'error': '缺少必要参数:  namespace, resource_type, resource_name'}, status=400)

        # 获取kubedoor-agent.py中已初始化的API客户端
        import sys

        kubedoor_agent = sys.modules.get('__main__')
        if kubedoor_agent and hasattr(kubedoor_agent, 'core_v1'):
            api_client = kubedoor_agent.core_v1.api_client
        else:
            # 降级：创建新的API客户端
            from kubernetes_asyncio import client

            api_client = client.ApiClient()

        # 创建资源管理器实例
        k8s_manager = K8sResourceManager(api_client)

        # 根据资源类型确定API版本和Kind
        resource_mapping = {
            'service': ('v1', 'Service'),
            'deployment': ('apps/v1', 'Deployment'),
            'node': ('v1', 'Node'),
            'pod': ('v1', 'Pod'),
            'configmap': ('v1', 'ConfigMap'),
            'secret': ('v1', 'Secret'),
            'ingress': ('networking.k8s.io/v1', 'Ingress'),
            'persistentvolumeclaim': ('v1', 'PersistentVolumeClaim'),
            'daemonset': ('apps/v1', 'DaemonSet'),
            'statefulset': ('apps/v1', 'StatefulSet'),
            'job': ('batch/v1', 'Job'),
            'cronjob': ('batch/v1', 'CronJob'),
        }

        resource_type_lower = resource_type.lower()
        if resource_type_lower not in resource_mapping:
            return web.json_response({'error': f'不支持的资源类型: {resource_type}'}, status=400)

        api_version, kind = resource_mapping[resource_type_lower]

        # 获取资源内容
        result = await k8s_manager.get_resource_content(api_version, kind, resource_name, namespace)

        if result['status'] == 'success':
            return web.json_response({'success': True, 'data': result['yaml_content']})
        else:
            return web.json_response({'error': result['error']}, status=result.get('error_code', 500))

    except Exception as e:
        logger.error(f"Error handling get resource content request: {e}", exc_info=True)
        return web.json_response({'error': f'服务器内部错误: {str(e)}'}, status=500)


async def handle_delete_resource(request: web.Request) -> web.Response:
    """
    处理删除K8S资源请求

    DELETE /api/agent/res/delete?namespace=<namespace>&resource_type=<resource_type>&resource_name=<resource_name>
    """
    try:
        # 获取查询参数
        namespace = request.query.get('namespace')
        resource_type = request.query.get('resource_type')
        resource_name = request.query.get('resource_name')

        # 验证必需参数
        if not all([namespace, resource_type, resource_name]):
            return web.json_response({'error': '缺少必要参数: namespace, resource_type, resource_name'}, status=400)

        # 资源类型映射
        resource_mapping = {
            'deployment': ('apps/v1', 'Deployment'),
            'node': ('v1', 'Node'),
            'service': ('v1', 'Service'),
            'configmap': ('v1', 'ConfigMap'),
            'secret': ('v1', 'Secret'),
            'pod': ('v1', 'Pod'),
            'ingress': ('networking.k8s.io/v1', 'Ingress'),
            'pvc': ('v1', 'PersistentVolumeClaim'),
            'statefulset': ('apps/v1', 'StatefulSet'),
            'daemonset': ('apps/v1', 'DaemonSet'),
            'job': ('batch/v1', 'Job'),
            'cronjob': ('batch/v1', 'CronJob'),
            'hpa': ('autoscaling/v2', 'HorizontalPodAutoscaler'),
        }

        if resource_type not in resource_mapping:
            return web.json_response({'error': f'不支持的资源类型: {resource_type}'}, status=400)

        api_version, kind = resource_mapping[resource_type]

        # 获取kubedoor-agent.py中已初始化的API客户端
        import sys

        kubedoor_agent = sys.modules.get('__main__')
        if kubedoor_agent and hasattr(kubedoor_agent, 'core_v1'):
            api_client = kubedoor_agent.core_v1.api_client
        else:
            # 降级：创建新的API客户端
            from kubernetes_asyncio import client

            api_client = client.ApiClient()

        # 创建资源管理器实例
        k8s_manager = K8sResourceManager(api_client)

        # 删除资源
        result = await k8s_manager.delete_resource(api_version, kind, resource_name, namespace)

        if result['status'] == 'success':
            return web.json_response({'success': True, 'data': result['message']})
        else:
            return web.json_response({'success': False, 'error': result['error']}, status=result.get('error_code', 500))

    except Exception as e:
        logger.error(f"Error handling delete resource request: {e}", exc_info=True)
        return web.json_response({'success': False, 'error': f'服务器内部错误: {str(e)}'}, status=500)
