#!/usr/bin/python3
# coding=utf-8

import sys
import requests
import urllib3
import uvicorn

# 禁用SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List


class PodInfo(BaseModel):
    ns: str
    pod_name: str


class PodDeleteRequest(BaseModel):
    pods: List[PodInfo]


from aiohttp.http import WSMsgType
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.rest import ApiException
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import (
    ERROR_CHANNEL,
    STDERR_CHANNEL,
    STDOUT_CHANNEL,
)
from loguru import logger
import utils
import uuid
from k8s_node_scheduler import K8sNodeScheduler
from k8s_client_manager import K8sClientManager, load_incluster_config

logger.remove()
logger.add(
    sys.stderr,
    format='<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> [<level>{level}</level>] <level>{message}</level>',
    level='INFO',
)
TASK_RESULTS = {}
POD_CONTAINER_CACHE = {}
app = FastAPI()


async def get_k8s_client():
    """统一获取CoreV1Api客户端的函数，避免重复代码"""
    try:
        logger.info("🔧 正在获取 K8s CoreV1Api 客户端...")
        load_incluster_config()
        client_instance = client.CoreV1Api()
        if client_instance is None:
            logger.error("❌ CoreV1Api 客户端创建失败，返回 None")
            raise Exception("CoreV1Api 客户端创建失败")
        logger.info("✅ K8s CoreV1Api 客户端获取成功")
        return client_instance
    except Exception as e:
        logger.error(f"❌ 获取 K8s 客户端失败: {e}")
        raise


async def get_k8s_clients():
    """统一获取K8S客户端的函数，返回CoreV1Api和AppsV1Api"""
    try:
        logger.info("🔧 正在获取 K8s CoreV1Api 和 AppsV1Api 客户端...")
        load_incluster_config()
        core_v1_api = client.CoreV1Api()
        apps_v1_api = client.AppsV1Api()

        if core_v1_api is None:
            logger.error("❌ CoreV1Api 客户端创建失败，返回 None")
            raise Exception("CoreV1Api 客户端创建失败")
        if apps_v1_api is None:
            logger.error("❌ AppsV1Api 客户端创建失败，返回 None")
            raise Exception("AppsV1Api 客户端创建失败")

        logger.info("✅ K8s CoreV1Api 和 AppsV1Api 客户端获取成功")
        return core_v1_api, apps_v1_api
    except Exception as e:
        logger.error(f"❌ 获取 K8s 客户端失败: {e}")
        raise


def get_pod_isolate_label(pod_name: str):
    return 'app'


async def jfr_upload(env, ns, pod_name, file_name, task_id):
    try:
        logger.info("【JFR-TASK】等待文件生成中...")
        TASK_RESULTS[task_id] = {"status": "等待中"}
        total_wait_time = 310
        interval = 10
        for i in range(0, total_wait_time, interval):
            progress = min(100, int((i / total_wait_time) * 100))
            TASK_RESULTS[task_id] = {"status": f"等待中 - {progress}% 完成"}
            await asyncio.sleep(interval)
            if i + interval >= total_wait_time:
                break
        TASK_RESULTS[task_id] = {"status": "上传中"}
        dlurl = f'{utils.OSS_URL}/{env}/jfr/{file_name}'
        command = f'curl -s -T /{file_name} {dlurl}'

        async with K8sClientManager() as k8s_manager:
            v1 = k8s_manager.core_v1_api
            container_name = await get_pod_container_name(v1, ns, pod_name)
            status, message = await execute_command(command, v1, pod_name, ns, container_name)
            if status:
                message = f"jfr文件上传成功，下载地址：\n{dlurl}"
                TASK_RESULTS[task_id] = {"status": "已完成", "message": message}
                await execute_command(f"rm -rf /{file_name}", v1, pod_name, ns, container_name)
            else:
                message = message + '\n' + f"jfr成功, 文件上传失败"
                TASK_RESULTS[task_id] = {"status": "失败", "message": message}
        send_md(message, env, ns, pod_name)
    except Exception as e:
        logger.exception(f"任务失败: {e}")
        TASK_RESULTS[task_id] = {"status": "失败", "error": str(e)}


async def get_deployment_info(ns: str, pod_name: str):
    """
    根据pod名和命名空间找到对应的deployment名称和当前副本数
    """
    try:
        async with K8sClientManager() as k8s_manager:
            v1 = k8s_manager.core_v1_api
            apps_v1 = k8s_manager.apps_v1_api

            # 获取pod信息
            pod_data = await v1.read_namespaced_pod(name=pod_name, namespace=ns, _request_timeout=30)

            # 从pod的ownerReferences中找到ReplicaSet
            owner_refs = pod_data.metadata.owner_references or []
            replicaset_name = None

            for owner in owner_refs:
                if owner.kind == "ReplicaSet":
                    replicaset_name = owner.name
                    break

            if not replicaset_name:
                return False, None, 0, "Pod没有找到对应的ReplicaSet"

            # 获取ReplicaSet信息
            rs_data = await apps_v1.read_namespaced_replica_set(name=replicaset_name, namespace=ns, _request_timeout=30)

            # 从ReplicaSet的ownerReferences中找到Deployment
            rs_owner_refs = rs_data.metadata.owner_references or []
            deployment_name = None

            for owner in rs_owner_refs:
                if owner.kind == "Deployment":
                    deployment_name = owner.name
                    break

            if not deployment_name:
                return False, None, 0, "ReplicaSet没有找到对应的Deployment"

            # 获取Deployment当前副本数
            deployment_data = await apps_v1.read_namespaced_deployment(
                name=deployment_name, namespace=ns, _request_timeout=30
            )
            current_replicas = deployment_data.spec.replicas or 0

            return True, deployment_name, current_replicas, ""

    except ApiException as e:
        logger.exception(f"获取deployment信息时发生异常: {e}")
        return False, None, 0, f"获取deployment信息失败: {str(e)}"


async def scale_deployment_via_api(
    ns: str, deployment_name: str, new_replicas: int, add_label: bool = False, body_data: list = []
):
    """
    通过调用kubedoor-agent的scale接口来扩容deployment

    Args:
        ns: 命名空间
        deployment_name: deployment名称
        new_replicas: 新的副本数
        add_label: 是否在扩容时给节点添加标签，默认为False

    Returns:
        tuple: (成功标志, 错误信息)
    """
    try:
        # 构造请求数据，格式与kubedoor-agent.py中scale函数期望的格式一致
        request_data = [
            {
                "namespace": ns,
                "deployment_name": deployment_name,
                "num": new_replicas,
                "node_cpu_list": body_data,
            }
        ]

        # 调用kubedoor-agent的scale接口
        # kubedoor-agent运行在443端口（HTTPS）
        # 添加query参数
        scale_url = (
            f"https://localhost:443/api/scale?add_label={'true' if add_label else 'false'}&temp=true&isolate=true"
        )

        headers = {"Content-Type": "application/json"}

        # 使用requests发送POST请求
        # 由于是HTTPS且可能使用自签名证书，禁用SSL验证
        response = requests.post(
            scale_url,
            json=request_data,
            headers=headers,
            timeout=30,
            verify=False,  # 禁用SSL证书验证
        )

        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                logger.info(f"通过API成功将Deployment {deployment_name} 临时扩容到 {new_replicas} 个副本")
                return True, ""
            else:
                error_msg = result.get("message", "扩容失败")
                logger.error(f"扩容API返回错误: {error_msg}")
                return False, error_msg
        else:
            try:
                error_detail = response.text or response.json().get('message', '未知错误')
            except:
                error_detail = '无法解析错误详情'
            error_msg = f"扩容API返回状态码: {response.status_code}, 错误详情: {error_detail}"
            logger.error(error_msg)
            return False, error_msg

    except requests.exceptions.RequestException as e:
        logger.exception(f"调用扩容API时发生异常: {e}")
        return False, f"调用扩容API失败: {str(e)}"
    except Exception as e:
        logger.exception(f"扩容deployment时发生意外异常: {e}")
        return False, f"扩容deployment失败: {str(e)}"


async def modify_pod_label(ns: str, pod_name: str):
    try:
        logger.info(f"===开始修改标签 {ns} {pod_name}")
        async with K8sClientManager() as k8s_manager:
            v1 = k8s_manager.core_v1_api

            # Get the current pod
            pod_data = await v1.read_namespaced_pod(name=pod_name, namespace=ns, _request_timeout=30)
            current_labels = pod_data.metadata.labels or {}

            # Modify the label
            isolate_label = get_pod_isolate_label(pod_name)
            labels_app = current_labels.get(isolate_label, False)
            if not labels_app:
                return False, '===未找到app标签'
            new_label_value = labels_app + '-ALERT'
            current_labels[isolate_label] = new_label_value

            # Update the pod with the new label
            pod_data.metadata.labels = current_labels
            await v1.patch_namespaced_pod(name=pod_name, namespace=ns, body=pod_data, _request_timeout=30)
            return True, ''
    except ApiException as e:
        logger.exception(f"修改pod标签时发生异常: {e}")
        return False, '===修改标签失败'


async def delete_pod_fun(ns: str, pod_name: str):
    # await asyncio.sleep(300)
    try:
        async with K8sClientManager() as k8s_manager:
            v1 = k8s_manager.core_v1_api
            await v1.delete_namespaced_pod(name=pod_name, namespace=ns, _request_timeout=30)
            logger.info(f"Pod {pod_name} 删除成功")
            return True
    except ApiException as e:
        logger.exception(f"删除pod时发生异常: {e}")
        return False


@app.post("/api/pod/modify_pod")
async def modify_pod(
    request: Request,
    env: str,
    ns: str,
    pod_name: str,
    scale_pod: bool = False,
    add_label: bool = False,
    scheduler: bool = False,
):
    # 参数验证：add_label和scheduler不能同时为True
    if add_label and scheduler:
        return JSONResponse(status_code=400, content={"message": "add_label和scheduler参数不能同时为True"})

    deployment_name = None
    current_replicas = 0
    new_replicas = 0
    node_scheduler_list = []
    k8s_scheduler = None

    # 如果启用scheduler，解析body获取node_scheduler列表
    if scheduler:
        try:
            logger.info(f"开始处理scheduler参数，env={env}, ns={ns}, pod_name={pod_name}")

            # 解析请求body
            logger.info("正在解析请求body...")
            body_data = await request.json()
            logger.info(f"成功解析body数据: {body_data}")

            if not isinstance(body_data, dict) or "node_scheduler" not in body_data:
                logger.error(f"body数据格式错误: {body_data}")
                return JSONResponse(
                    status_code=400, content={"message": "当scheduler为True时，body必须包含node_scheduler字段"}
                )

            node_scheduler_list = body_data.get("node_scheduler", [])
            logger.info(f"获取到node_scheduler列表: {node_scheduler_list}")

            if not isinstance(node_scheduler_list, list):
                logger.error(f"node_scheduler不是列表类型: {type(node_scheduler_list)}")
                return JSONResponse(status_code=400, content={"message": "node_scheduler必须是一个列表"})

            # 使用客户端管理器确保客户端正确关闭
            async with K8sClientManager() as k8s_manager:
                logger.info(f"成功获取K8s客户端: {type(k8s_manager.core_v1_api)}")

                # 初始化K8s节点调度器
                logger.info("正在初始化K8s节点调度器...")
                k8s_scheduler = K8sNodeScheduler(k8s_manager.core_v1_api)
                logger.info(f"成功初始化K8s节点调度器: {type(k8s_scheduler)}")

                # 执行禁止调度操作
                logger.info(f"开始执行禁止调度操作，排除节点: {node_scheduler_list}")
                cordon_result = await k8s_scheduler.cordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                logger.info(f"禁止调度操作完成: {cordon_result}")

                # 检查 cordon 操作是否有错误
                if cordon_result.get("error_count", 0) > 0:
                    error_details = []
                    for result in cordon_result.get("results", []):
                        if result.get("status") == "error":
                            error_details.append(f"节点 {result.get('node_name')}: {result.get('message')}")

                    error_message = f"禁止节点调度操作失败，错误详情: {'; '.join(error_details)}"
                    logger.error(error_message)

                    # 执行恢复操作：取消所有节点的禁止调度状态
                    try:
                        logger.warning("⚠️ cordon操作失败，开始执行uncordon恢复操作以确保节点状态一致性...")
                        uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                        logger.info(f"uncordon恢复操作完成: {uncordon_result}")

                        if uncordon_result.get("error_count", 0) > 0:
                            logger.error(f"⚠️ uncordon恢复操作也出现错误: {uncordon_result}")
                            error_message += f"；恢复操作也失败: {uncordon_result.get('error_count', 0)}个节点恢复失败"
                        else:
                            logger.info("✅ uncordon恢复操作成功，所有节点调度状态已恢复")
                            error_message += "；已执行恢复操作确保节点状态一致性"

                    except Exception as uncordon_e:
                        logger.error(
                            f"❌ 执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}"
                        )
                        error_message += f"；恢复操作异常: {str(uncordon_e)}"

                    return JSONResponse(status_code=500, content={"message": error_message})

        except Exception as e:
            logger.error(f"处理scheduler参数时发生异常: {type(e).__name__}: {str(e)}")
            import traceback

            logger.error(f"异常堆栈: {traceback.format_exc()}")

            # 在异常情况下也执行恢复操作
            if (
                'node_scheduler_list' in locals()
                and node_scheduler_list
                and 'k8s_scheduler' in locals()
                and k8s_scheduler
            ):
                try:
                    logger.warning("⚠️ 处理scheduler参数时发生异常，开始执行uncordon恢复操作以确保节点状态一致性...")
                    uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                    logger.info(f"异常情况下的uncordon恢复操作完成: {uncordon_result}")

                    if uncordon_result.get("error_count", 0) > 0:
                        logger.error(f"⚠️ 异常情况下的uncordon恢复操作也出现错误: {uncordon_result}")
                    else:
                        logger.info("✅ 异常情况下的uncordon恢复操作成功，所有节点调度状态已恢复")

                except Exception as uncordon_e:
                    logger.error(
                        f"❌ 异常情况下执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}"
                    )

            return JSONResponse(status_code=500, content={"message": f"处理scheduler参数失败: {str(e)}"})

    # 是否扩容--->是否固定节点均衡模式--->临时扩容(开启管控模式)
    # 1. 如果需要扩容，先获取deployment信息并执行扩容
    if scale_pod:
        success, deployment_name, current_replicas, error_msg = await get_deployment_info(ns, pod_name)
        if not success:
            return JSONResponse(status_code=500, content={"message": error_msg})

        # 获取body数据（如果add_label为True）
        if add_label:
            try:
                body_data = await request.json()
                if not isinstance(body_data, list):
                    return JSONResponse(
                        status_code=400,
                        content={"message": "当add_label为True时，body必须是一个list"},
                    )
            except Exception as e:
                return JSONResponse(status_code=400, content={"message": f"解析body失败: {str(e)}"})
        else:
            body_data = []

        # 2. 扩容deployment（增加一个pod）
        new_replicas = current_replicas + 1
        scale_success, scale_error = await scale_deployment_via_api(
            ns, deployment_name, new_replicas, add_label, body_data
        )
        if not scale_success:
            return JSONResponse(status_code=500, content={"message": scale_error})

        logger.info(f"Deployment {deployment_name} 从 {current_replicas} 个副本临时扩容到 {new_replicas} 个副本")

    # 3. 修改pod标签
    success, status = await modify_pod_label(ns, pod_name)
    if not success:
        return JSONResponse(status_code=500, content={"message": status})
        # raise HTTPException(status_code=500, detail=status)

    # 如果启用了scheduler，在标签修改完成后执行取消禁止调度操作（延迟10秒）
    if scheduler and k8s_scheduler:
        try:
            logger.info(f"标签修改完成，准备执行取消禁止调度操作，排除节点: {node_scheduler_list}")
            logger.info("正在调用uncordon_nodes_exclude方法...")

            # 定义错误回调函数
            def uncordon_error_callback(error_message):
                logger.error(f"取消禁止调度操作失败通知: {error_message}")
                send_md(f"⚠️ 取消禁止调度操作失败: {error_message}", env, ns, pod_name)

            # 为 uncordon 操作创建新的客户端管理器
            async with K8sClientManager() as uncordon_k8s_manager:
                uncordon_scheduler = K8sNodeScheduler(uncordon_k8s_manager.core_v1_api)
                uncordon_result = await uncordon_scheduler.uncordon_nodes_exclude(
                    exclude_nodes=node_scheduler_list, delay_seconds=10, error_callback=uncordon_error_callback
                )
                logger.info(f"取消禁止调度操作已安排: {uncordon_result}")
        except Exception as e:
            logger.error(f"执行取消禁止调度操作失败: {type(e).__name__}: {str(e)}")
            import traceback

            logger.error(f"uncordon异常堆栈: {traceback.format_exc()}")
            # 不影响主流程，只记录错误

    # await asyncio.sleep(300)  # Wait for 5 minutes
    # Schedule the pod deletion after 5 minutes without blocking the request
    # asyncio.create_task(delete_pod(ns, pod_name))

    if scale_pod:
        success_msg = f"Deployment {deployment_name} 临时扩容到 {new_replicas} 个副本并成功修改app标签"
    else:
        success_msg = "app标签修改成功"

    if scheduler:
        success_msg += f"，已执行节点调度管理（排除节点: {node_scheduler_list}）"

    send_md(success_msg, env, ns, pod_name)
    return {"message": f"【{ns}】【{pod_name}】{success_msg}", "success": True}


@app.get("/api/pod/delete_pod")
async def delete_pod(env: str, ns: str, pod_name: str):
    # Delete the pod label
    success = await delete_pod_fun(ns, pod_name)
    if not success:
        return {"message": "删除pod失败", "success": False}
    send_md("pod删除成功", env, ns, pod_name)
    return {"message": f"【{ns}】【{pod_name}】pod删除成功", "success": True}


@app.delete("/api/pod/delete_pods")
async def delete_pods(item: PodDeleteRequest, env: str):
    all_success = True
    all_messages = []
    for pod_info in item.pods:
        success = await delete_pod_fun(pod_info.ns, pod_info.pod_name)
        if not success:
            all_success = False
            all_messages.append(f"【{pod_info.ns}】{pod_info.pod_name} 删除失败")
        else:
            all_messages.append(f"【{pod_info.ns}】{pod_info.pod_name} 删除成功")

    if all_messages:
        messages = '\n'.join(all_messages)
        send_md(messages, env, '', '')

    if not all_success:
        return {"message": "\n".join(all_messages), "success": False}

    return {"message": "批量删除pod完成！", "success": True}


async def get_pod_info(ns, pod_name, v1, type, tail):
    # 返回pod信息
    try:
        await v1.read_namespaced_pod(name=pod_name, namespace=ns, _request_timeout=30)
        now = datetime.now()
        formatted_time = now.strftime("%Y%m%d%H%M")
        file_name = f"{type}-{pod_name}-{formatted_time}.{tail}"
        logger.info(f"文件名{file_name}")
        return file_name, None
    except Exception as e:
        logger.error(f"在命名空间 [{ns}] 中未找到pod [{pod_name}]")
        logger.exception(str(e))
        return "error", f"在命名空间 [{ns}] 中未找到pod [{pod_name}]"


def select_preferred_container(pod):
    containers = getattr(getattr(pod, "spec", None), "containers", None) or []
    if not containers:
        return None
    labels = getattr(getattr(pod, "metadata", None), "labels", None) or {}
    preferred_label_keys = [
        "kubedoor/target-container",
        "kubedoor_target_container",
        "app",
        "app.kubernetes.io/name",
        "k8s-app",
    ]
    for key in preferred_label_keys:
        label_value = labels.get(key)
        if not label_value:
            continue
        for container in containers:
            if container.name == label_value or container.name.startswith(label_value):
                return container.name
    for container in containers:
        name = container.name or ""
        if any(sidecar in name for sidecar in ["sidecar", "proxy", "istio", "envoy", "metrics"]):
            continue
        return container.name
    return containers[0].name


async def get_pod_container_name(v1, ns, pod_name):
    cache_key = f"{ns}/{pod_name}"
    cached = POD_CONTAINER_CACHE.get(cache_key)
    if cached:
        return cached
    pod = await v1.read_namespaced_pod(name=pod_name, namespace=ns, _request_timeout=30)
    container_name = select_preferred_container(pod)
    if not container_name:
        raise RuntimeError(f"未在Pod [{pod_name}] 中找到可用容器")
    POD_CONTAINER_CACHE[cache_key] = container_name
    return container_name


async def execute_command(command, v1, pod_name, ns, container=None):
    cache_key = f"{ns}/{pod_name}"
    try:
        target_container = container or await get_pod_container_name(v1, ns, pod_name)
    except Exception as e:
        POD_CONTAINER_CACHE.pop(cache_key, None)
        logger.exception(f"获取容器失败: {e}")
        return False, f"获取容器失败: {e}"

    logger.info(f"执行命令：{command} | namespace={ns} | pod={pod_name} | container={target_container}")
    exec_command = ['/bin/sh', '-c', f"{command}; echo $?"]
    try:
        async with WsApiClient() as ws_api:
            v1_ws = client.CoreV1Api(api_client=ws_api)
            ws_connect = await v1_ws.connect_get_namespaced_pod_exec(
                pod_name,
                ns,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
                container=target_container,
            )

            # 收集输出
            output_lines = []
            error_lines = []

            async with ws_connect as websocket:
                async for msg in websocket:
                    if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                        data = msg.data
                        if isinstance(data, str):
                            data_bytes = data.encode('utf-8')
                        else:
                            data_bytes = data
                        if len(data_bytes) > 1:
                            channel = data_bytes[0]
                            content = data_bytes[1:].decode('utf-8', errors='ignore')
                            if channel == STDOUT_CHANNEL:
                                output_lines.append(content)
                            elif channel == STDERR_CHANNEL:
                                error_lines.append(content)
                            elif channel == ERROR_CHANNEL:
                                logger.error(f"WebSocket error channel: {content}")
                    elif msg.type == WSMsgType.ERROR:
                        logger.error(f"WebSocket error: {msg.data}")
                        break
                    elif msg.type == WSMsgType.CLOSE:
                        break

            # 合并输出
            output = ''.join(output_lines).strip()
            error_output = ''.join(error_lines).strip()

            # 分割输出，最后一行是状态码
            if output:
                lines = output.split('\n')
                status_code = lines[-1] if lines else '1'
                command_output = '\n'.join(lines[:-1]) if len(lines) > 1 else ''
            else:
                status_code = '1'
                command_output = ''

            if status_code != '0':
                message = f"命令 {command} 执行失败，状态码: {status_code}，输出信息: {command_output}，错误信息: {error_output}"
                logger.error(message)
                return False, message
            return True, command_output
    except Exception as e:
        POD_CONTAINER_CACHE.pop(cache_key, None)
        logger.exception(str(e))
        return False, str(e)


async def execute_in_pod(env, ns, v1, pod_name, type, file_name="not_found", container_name=None):
    try:
        container_name = container_name or await get_pod_container_name(v1, ns, pod_name)
    except Exception as e:
        logger.exception(f"获取容器失败: {e}")
        return False, f"获取容器失败: {e}"

    status, message = await execute_command(
        "curl -V || (sed -i 's/dl-cdn.alpinelinux.org/repo.huaweicloud.com/g' /etc/apk/repositories && apk add -q curl)",
        v1,
        pod_name,
        ns,
        container_name,
    )
    if not status:
        return status, message
    if type == "dump":
        command = f"env -u JAVA_TOOL_OPTIONS jmap -dump:format=b,file=/{file_name} `pidof -s java`"
        status, message = await execute_command(command, v1, pod_name, ns, container_name)
        if status:
            dlurl = f'{utils.OSS_URL}/{env}/dump/{file_name}'
            command = f'curl -s -T /{file_name} {dlurl}'
            status2, message = await execute_command(command, v1, pod_name, ns, container_name)
            if status2:
                message = f"dump文件上传成功，下载地址：\n{dlurl}"
                await execute_command(f"rm -rf /{file_name}", v1, pod_name, ns, container_name)
            else:
                message = f"dump成功, 文件上传失败"
        else:
            message = f"dump失败"
    if type == "jfr":
        # 解锁JFR功能
        command_unlock = f"env -u JAVA_TOOL_OPTIONS jcmd `pidof -s java` VM.unlock_commercial_features"
        status, message = await execute_command(command_unlock, v1, pod_name, ns, container_name)
        if not status:
            return status, message + '\n' + "jfr解锁失败"
        command = f"env -u JAVA_TOOL_OPTIONS jcmd `pidof -s java` JFR.start duration=5m filename=/{file_name}"
        status, message = await execute_command(command, v1, pod_name, ns, container_name)
        if not status:
            return status, message + '\n' + "开启jfr飞行记录失败"
    if type == "jstack":
        command = f"env -u JAVA_TOOL_OPTIONS jstack -l `pidof -s java` |tee /{file_name}"
        status, jstack_msg = await execute_command(command, v1, pod_name, ns, container_name)
        if status:
            dlurl = f'{utils.OSS_URL}/{env}/jstack/{file_name}'
            command = f'curl -s -T /{file_name} {dlurl}'
            status2, message = await execute_command(command, v1, pod_name, ns, container_name)
            if status2:
                dlmsg = f"jstack文件上传成功，下载地址：\n{dlurl}"
                await execute_command(f"rm -rf /{file_name}", v1, pod_name, ns, container_name)
            else:
                dlmsg = "jstack成功, 文件上传失败"
            message = jstack_msg + '\n' + dlmsg
            send_md(dlmsg, env, ns, pod_name)
        else:
            message = f"jstack失败"
    if type == "jvm_mem":
        # 查询jvm内存
        command = "env -u JAVA_TOOL_OPTIONS jmap -heap `pidof -s java`"
        # command = "ls arthas-boot.jar || curl -s -O https://arthas.aliyun.com/arthas-boot.jar && env -u JAVA_TOOL_OPTIONS java -jar arthas-boot.jar 1 -c 'memory;stop'|sed -n '/memory | plaintext/,/stop | plaintext/{/memory | plaintext/b;/stop | plaintext/b;p}'"
        status, message = await execute_command(command, v1, pod_name, ns, container_name)
    return status, message


def send_md(msg, env, ns, pod_name):
    text = f"# 【<font color=\"#5bcc85\">{env}</font>】{ns}\n## {pod_name}\n"
    text += f"{msg}\n"
    utils.send_msg(text)


@app.get("/api/pod/auto_dump")
async def auto_dump(env: str, ns: str, pod_name: str):
    async with K8sClientManager() as k8s_manager:
        v1 = k8s_manager.core_v1_api
        file_name, err_msg = await get_pod_info(ns, pod_name, v1, "dump", "hprof")
        if file_name == "error":
            return JSONResponse(status_code=500, content={"message": err_msg})
        try:
            container_name = await get_pod_container_name(v1, ns, pod_name)
        except Exception as e:
            logger.exception(f"获取容器失败: {e}")
            return JSONResponse(status_code=500, content={"message": f"获取容器失败: {e}"})
        # 生成 Java 进程对象统计信息直方图
        status, message = await execute_command(
            "env -u JAVA_TOOL_OPTIONS jmap -histo `pidof -s java` |head -n 30", v1, pod_name, ns, container_name
        )
        if status:
            all_msg = "Java 进程对象统计信息直方图:" + '\n' + message
        else:
            all_msg = message + '\n' + "生成 Java 进程对象统计信息直方图失败"
        status, message = await execute_in_pod(env, ns, v1, pod_name, "dump", file_name, container_name)
        all_msg = all_msg + '\n' + message
        if status:
            dlurl = f'{utils.OSS_URL}/{env}/dump/{file_name}'
            send_md(all_msg, env, ns, pod_name)
            return {"message": all_msg, "success": True, "link": dlurl}
        return JSONResponse(status_code=500, content={"message": all_msg})


@app.get("/api/pod/auto_jstack")
async def auto_jstack(env: str, ns: str, pod_name: str):
    async with K8sClientManager() as k8s_manager:
        v1 = k8s_manager.core_v1_api
        file_name, err_msg = await get_pod_info(ns, pod_name, v1, "jstack", "jstack")
        if file_name == "error":
            return JSONResponse(status_code=500, content={"message": err_msg})
        status, message = await execute_in_pod(env, ns, v1, pod_name, "jstack", file_name)
        if status:
            return {"message": message, "success": True}
        else:
            return JSONResponse(status_code=500, content={"message": message})


@app.get("/api/pod/auto_jfr")
async def auto_jfr(env: str, ns: str, pod_name: str, background_tasks: BackgroundTasks):
    async with K8sClientManager() as k8s_manager:
        v1 = k8s_manager.core_v1_api
        file_name, err_msg = await get_pod_info(ns, pod_name, v1, "jfr", "jfr")
        if file_name == "error":
            return JSONResponse(status_code=500, content={"message": err_msg})
        status, message = await execute_in_pod(env, ns, v1, pod_name, "jfr", file_name)
        if status:
            task_id = str(uuid.uuid4())
            TASK_RESULTS[task_id] = {"status": "处理中"}
            background_tasks.add_task(jfr_upload, env, ns, pod_name, file_name, task_id)
            now = datetime.now()
            finish_time = now + timedelta(minutes=6)
            formatted_now = now.strftime("%H:%M:%S")
            formatted_finish = finish_time.strftime("%H:%M:%S")
            link = f'{utils.OSS_URL}/{env}/jfr/{file_name}'
            message = f"飞行记录后台执行需要5分钟，任务ID：{task_id}\n（/api/pod/task_status/{task_id}?env={env}）\n请于{formatted_finish}后，访问以下链接下载:\n{link}"
            send_md(message, env, ns, pod_name)
            return {"message": message, "success": True, 'link': link}
        return JSONResponse(status_code=500, content={"message": message})


@app.get("/api/pod/auto_jvm_mem")
async def auto_jvm_mem(env: str, ns: str, pod_name: str):
    async with K8sClientManager() as k8s_manager:
        v1 = k8s_manager.core_v1
        status, message = await execute_in_pod(env, ns, v1, pod_name, "jvm_mem")
        if status:
            send_md(message, env, ns, pod_name)
            return {"message": message, "success": True}
        return JSONResponse(status_code=500, content={"message": message})


@app.get("/api/pod/task_status/{task_id}")
async def get_task_status(task_id: str):
    if task_id in TASK_RESULTS:
        return TASK_RESULTS[task_id]
    else:
        return {"status": "未找到"}


@app.get("/api/pod/get_logs")
async def get_pod_logs(env: str, ns: str, pod: str, lines: int = 100):
    try:
        async with K8sClientManager() as k8s_manager:
            v1 = k8s_manager.core_v1

            # 检查pod是否存在
            try:
                await v1.read_namespaced_pod(name=pod, namespace=ns, _request_timeout=30)
            except Exception as e:
                error_msg = f"在命名空间 [{ns}] 中未找到pod [{pod}]"
                logger.error(error_msg)
                return JSONResponse(status_code=500, content={"message": error_msg})

            # 获取pod日志
            logs = await v1.read_namespaced_pod_log(name=pod, namespace=ns, tail_lines=lines, _request_timeout=30)
            return {"message": logs, "success": True}
    except ApiException as e:
        logger.exception(f"获取Pod日志时出现异常: {e}")
        return JSONResponse(status_code=500, content={"message": f"获取Pod日志失败: {str(e)}"})


@app.get("/api/pod/get_previous_logs")
async def get_pod_previous_logs(env: str, ns: str, pod: str, lines: int = 100):
    """
    获取pod重启前的日志（previous container logs）
    等同于命令: kubectl logs --tail=100 pod_name --previous
    """
    try:
        async with K8sClientManager() as k8s_manager:
            v1 = k8s_manager.core_v1

            # 检查pod是否存在
            try:
                await v1.read_namespaced_pod(name=pod, namespace=ns, _request_timeout=30)
            except Exception as e:
                error_msg = f"在命名空间 [{ns}] 中未找到pod [{pod}]"
                logger.error(error_msg)
                return JSONResponse(status_code=500, content={"message": error_msg})

            # 获取pod重启前的日志
            try:
                logs = await v1.read_namespaced_pod_log(
                    name=pod,
                    namespace=ns,
                    tail_lines=lines,
                    previous=True,  # 关键参数：获取前一个容器的日志
                    _request_timeout=30,
                )
                # send_md(f"获取pod重启前日志成功，共{lines}行", env, ns, pod)
                return {"message": logs, "success": True}
            except ApiException as api_e:
                # 如果没有previous容器或者previous容器没有日志
                if api_e.status == 400 or "previous terminated container" in str(api_e).lower():
                    error_msg = f"Pod [{pod}] 没有重启前的日志记录，可能该pod从未重启过"
                    logger.warning(error_msg)
                    return JSONResponse(status_code=404, content={"message": error_msg})
                else:
                    raise api_e

    except ApiException as e:
        logger.exception(f"获取Pod重启前日志时出现异常: {e}")
        return JSONResponse(status_code=500, content={"message": f"获取Pod重启前日志失败: {str(e)}"})
    except Exception as e:
        logger.exception(f"获取Pod重启前日志时出现未知异常: {e}")
        return JSONResponse(status_code=500, content={"message": f"获取Pod重启前日志失败: {str(e)}"})


if __name__ == "__main__":
    uvicorn.run("pod-mgr:app", host="0.0.0.0", workers=1, port=81)
