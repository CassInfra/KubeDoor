import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from aiohttp import web
from kubernetes_asyncio.client import AppsV1Api
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

import utils


class DeploymentMonitor:
    def __init__(self, v1, core_v1):
        self.v1 = v1
        self.core_v1 = core_v1
        self.monitoring_tasks = {}
    
    async def monitor_deployment_update(self, namespace, deployment_name, new_image):
        """监控deployment更新状态"""
        task_key = f"{namespace}/{deployment_name}"
        
        # 如果已经有监控任务在运行，先取消它
        if task_key in self.monitoring_tasks:
            self.monitoring_tasks[task_key].cancel()
        
        # 创建新的监控任务
        task = asyncio.create_task(
            self._monitor_update_process(namespace, deployment_name, new_image)
        )
        self.monitoring_tasks[task_key] = task
        
        try:
            await task
        finally:
            # 清理任务
            if task_key in self.monitoring_tasks:
                del self.monitoring_tasks[task_key]
    
    async def _monitor_update_process(self, namespace, deployment_name, new_image):
        """实际的监控更新过程"""
        try:
            # 发送开始更新消息
            utils.send_msg(
                f"【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】开始更新镜像【{new_image}】"
            )
            
            # 获取初始状态
            deployment = await self.v1.read_namespaced_deployment(deployment_name, namespace)
            target_replicas = deployment.spec.replicas
            
            # 记录初始pod状态
            initial_pods = await self._get_deployment_pods(namespace, deployment_name)
            pod_restart_counts = {pod.metadata.name: self._get_pod_restart_count(pod) for pod in initial_pods}
            pod_pending_start_times = {}  # 记录Pod开始Pending的时间
            
            start_time = datetime.now()
            timeout = timedelta(minutes=20)
            last_report_time = start_time
            report_interval = timedelta(seconds=10)
            
            while datetime.now() - start_time < timeout:
                try:
                    # 获取当前deployment状态
                    deployment = await self.v1.read_namespaced_deployment(deployment_name, namespace)
                    status = deployment.status
                    
                    # 获取当前pods
                    current_pods = await self._get_deployment_pods(namespace, deployment_name)
                    
                    # 检查pod重启情况
                    await self._check_pod_restarts(current_pods, pod_restart_counts, namespace, deployment_name)
                    
                    # 检查pod Pending状态
                    await self._check_pod_pending(current_pods, pod_pending_start_times, namespace, deployment_name)
                    
                    # 计算更新状态
                    ready_replicas = status.ready_replicas or 0
                    updated_replicas = status.updated_replicas or 0
                    unavailable_replicas = status.unavailable_replicas or 0
                    
                    # 检查是否更新完成
                    if (ready_replicas == target_replicas and 
                        updated_replicas == target_replicas and 
                        unavailable_replicas == 0):
                        
                        # 验证所有pod都使用了新镜像
                        all_updated = await self._verify_all_pods_updated(current_pods, new_image)
                        if all_updated:
                            utils.send_msg(
                                f"【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】镜像更新成功！"
                                f"所有{target_replicas}个Pod已成功更新到新镜像"
                            )
                            return
                    
                    # 每10秒发送一次状态报告
                    if datetime.now() - last_report_time >= report_interval:
                        await self._send_status_report(
                            namespace, deployment_name, target_replicas, 
                            ready_replicas, updated_replicas, unavailable_replicas
                        )
                        last_report_time = datetime.now()
                    
                    await asyncio.sleep(2)  # 每2秒检查一次状态
                    
                except ApiException as e:
                    logger.error(f"监控deployment状态时出错: {e}")
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"监控过程中出现未知错误: {e}")
                    await asyncio.sleep(5)

            # 超时处理
            utils.send_msg(
                f"【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】镜像更新超时（20分钟），停止监控"
            )
            
        except asyncio.CancelledError:
            logger.info(f"监控任务被取消: {namespace}/{deployment_name}")
        except Exception as e:
            logger.error(f"监控deployment更新时出现错误: {e}")
            utils.send_msg(
                f"【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】监控过程出现错误: {str(e)}"
            )
    

    async def _get_deployment_pods(self, namespace, deployment_name):
        """获取deployment的所有pods"""
        try:
            # 获取deployment的selector
            deployment = await self.v1.read_namespaced_deployment(deployment_name, namespace)
            selector = deployment.spec.selector.match_labels
            
            # 构建label selector字符串
            label_selector = ','.join([f"{k}={v}" for k, v in selector.items()])
            
            # 获取pods
            pods = await self.core_v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector
            )
            return pods.items
        except Exception as e:
            logger.error(f"获取deployment pods时出错: {e}")
            return []
    
    def _get_pod_restart_count(self, pod):
        """获取pod的重启次数"""
        if not pod.status or not pod.status.container_statuses:
            return 0
        return sum(container.restart_count for container in pod.status.container_statuses)
    
    async def _check_pod_restarts(self, current_pods, pod_restart_counts, namespace, deployment_name):
        """检查pod重启情况"""
        for pod in current_pods:
            pod_name = pod.metadata.name
            current_restart_count = self._get_pod_restart_count(pod)
            
            if pod_name in pod_restart_counts:
                previous_count = pod_restart_counts[pod_name]
                if current_restart_count > previous_count:
                    restart_diff = current_restart_count - previous_count
                    utils.send_msg(
                        f"【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】"
                        f"Pod【{pod_name}】发生重启，重启次数: {current_restart_count} (+{restart_diff})"
                    )
            
            # 更新重启次数记录
            pod_restart_counts[pod_name] = current_restart_count
    
    async def _verify_all_pods_updated(self, pods, new_image):
        """验证所有pod都使用了新镜像"""
        for pod in pods:
            if not pod.spec or not pod.spec.containers:
                continue
            
            for container in pod.spec.containers:
                if container.image != new_image:
                    return False
        return True
    
    async def _send_status_report(self, namespace, deployment_name, target_replicas, 
                                ready_replicas, updated_replicas, unavailable_replicas):
        """发送状态报告"""
        pending_replicas = target_replicas - updated_replicas
        
        status_msg = (
            f"【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】更新状态: "
            f"总共{target_replicas}个Pod, "
            f"已更新{updated_replicas}个, "
            f"就绪{ready_replicas}个, "
            f"待更新{pending_replicas}个"
        )
        
        if unavailable_replicas > 0:
            status_msg += f", 不可用{unavailable_replicas}个"
        
        utils.send_msg(status_msg)
    
    async def _check_pod_pending(self, current_pods, pod_pending_start_times, namespace, deployment_name):
        """检查Pod Pending状态"""
        current_time = datetime.now()
        pending_threshold = timedelta(minutes=2)
        
        for pod in current_pods:
            pod_name = pod.metadata.name
            
            # 检查Pod是否处于Pending状态
            if pod.status and pod.status.phase == 'Pending':
                # 如果是第一次发现该Pod Pending，记录开始时间
                if pod_name not in pod_pending_start_times:
                    pod_pending_start_times[pod_name] = current_time
                    logger.info(f"Pod {pod_name} 开始处于Pending状态")
                else:
                    # 检查Pending时间是否超过阈值
                    pending_duration = current_time - pod_pending_start_times[pod_name]
                    if pending_duration >= pending_threshold:
                        # 获取Pending原因
                        pending_reason = self._get_pod_pending_reason(pod)
                        
                        utils.send_msg(
                            f"【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】"
                            f"Pod【{pod_name}】Pending超过2分钟！原因: {pending_reason}"
                        )
                        
                        # 更新时间，避免重复发送（每2分钟发送一次）
                        pod_pending_start_times[pod_name] = current_time
            else:
                # Pod不再Pending，清除记录
                if pod_name in pod_pending_start_times:
                    del pod_pending_start_times[pod_name]
    
    def _get_pod_pending_reason(self, pod):
        """获取Pod Pending的原因"""
        reasons = []
        
        # 检查Pod的conditions
        if pod.status and pod.status.conditions:
            for condition in pod.status.conditions:
                if condition.status == 'False' and condition.reason:
                    reasons.append(f"{condition.type}: {condition.reason} - {condition.message or ''}")
        
        # 检查容器状态
        if pod.status and pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                if container_status.state and container_status.state.waiting:
                    waiting_state = container_status.state.waiting
                    reasons.append(f"容器 {container_status.name}: {waiting_state.reason} - {waiting_state.message or ''}")
        
        # 检查初始化容器状态
        if pod.status and pod.status.init_container_statuses:
            for init_container_status in pod.status.init_container_statuses:
                if init_container_status.state and init_container_status.state.waiting:
                    waiting_state = init_container_status.state.waiting
                    reasons.append(f"初始化容器 {init_container_status.name}: {waiting_state.reason} - {waiting_state.message or ''}")
        
        # 检查Pod events（需要额外的API调用）
        if not reasons:
            reasons.append("未知原因，建议检查Pod events")
        
        return '; '.join(reasons) if reasons else "未知原因"


async def update_image(request, apps_v1: AppsV1Api, deployment_monitor: Optional["DeploymentMonitor"]):
    """更新 Deployment 镜像并触发监控"""
    try:
        data = await request.json()
        new_image_tag = data.get('image_tag')
        deployment_name = data.get('deployment')
        namespace = data.get('namespace')

        if not all([new_image_tag, deployment_name, namespace]):
            return web.json_response({"message": "缺少必要参数", "success": False}, status=400)

        deployment = await apps_v1.read_namespaced_deployment(deployment_name, namespace)
        current_image = deployment.spec.template.spec.containers[0].image
        image_name = current_image.split(':')[0]
        new_image = f"{image_name}:{new_image_tag}"
        deployment.spec.template.spec.containers[0].image = new_image

        await apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)

        if deployment_monitor:
            asyncio.create_task(deployment_monitor.monitor_deployment_update(namespace, deployment_name, new_image))

        return web.json_response(
            {
                "success": True,
                "message": f"{namespace} {deployment_name} updated with image {new_image}",
            }
        )
    except Exception as exc:
        logger.exception(f"更新镜像失败: {exc}")
        return web.json_response({"message": str(exc)}, status=500)
