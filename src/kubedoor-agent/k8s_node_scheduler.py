#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S节点调度管理模块
提供节点禁止调度和取消禁止调度的并发操作功能
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger
from k8s_client_manager import K8sClientManager


class K8sNodeScheduler:
    """K8S节点调度管理器"""

    def __init__(self, core_v1_api, max_concurrent_operations=20, operation_timeout=30):
        if core_v1_api is None:
            raise ValueError("core_v1_api 不能为 None")
        self.core_v1 = core_v1_api
        self.operation_timeout = operation_timeout  # 单个操作超时时间（秒）
        self.max_concurrent_operations = max_concurrent_operations  # 最大并发操作数

    async def get_all_nodes(self) -> List[str]:
        """获取集群中所有节点的名称列表"""
        try:
            logger.info("🔍 正在获取集群节点列表...")
            nodes = await self.core_v1.list_node()
            node_names = [node.metadata.name for node in nodes.items]
            logger.info(f"📋 发现 {len(node_names)} 个节点: {', '.join(node_names)}")
            return node_names
        except ApiException as e:
            logger.error(f"获取节点列表失败 - K8s API异常: {e}")
            raise
        except Exception as e:
            logger.error(f"获取节点列表失败: {e}")
            raise

    def _filter_nodes_to_operate(self, all_nodes: List[str], exclude_nodes: List[str]) -> List[str]:
        """过滤出需要操作的节点列表"""
        if not exclude_nodes:
            exclude_nodes = []

        # 排除指定的节点
        nodes_to_operate = [node for node in all_nodes if node not in exclude_nodes]

        logger.info(f"🎯 排除节点: {exclude_nodes if exclude_nodes else '无'}")
        logger.info(f"🎯 需要操作的节点: {nodes_to_operate if nodes_to_operate else '无'}")

        return nodes_to_operate

    async def _cordon_single_node(self, node_name: str) -> Dict[str, Any]:
        """禁止单个节点调度"""
        max_retries = 1  # 最多重试1次
        retry_delay = 2  # 重试延迟2秒
        
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    logger.debug(f"🔄 重试禁止节点 {node_name} 调度 (第{attempt}次重试)...")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.debug(f"🚫 正在禁止节点 {node_name} 调度...")

                # 每次尝试都重新获取节点信息
                node = await self.core_v1.read_node(name=node_name)

                # 设置节点为不可调度
                node.spec.unschedulable = True

                # 更新节点
                await self.core_v1.patch_node(name=node_name, body=node)

                result = {
                    "node_name": node_name,
                    "operation": "cordon",
                    "status": "success",
                    "message": f"节点 {node_name} 已禁止调度" + (f" (第{attempt}次重试成功)" if attempt > 0 else ""),
                    "timestamp": datetime.now().isoformat(),
                }
                logger.info(f"✅ {result['message']}")
                return result

            except (ApiException, Exception) as e:
                error_msg = f"禁止节点 {node_name} 调度失败"
                if isinstance(e, ApiException):
                    error_msg += f" - K8s API异常: {e}"
                else:
                    error_msg += f": {e}"
                
                if attempt < max_retries:
                    logger.warning(f"⚠️ {error_msg}，将在{retry_delay}秒后重试...")
                else:
                    logger.error(f"❌ {error_msg}，已达到最大重试次数")
                    return {
                        "node_name": node_name,
                        "operation": "cordon",
                        "status": "error",
                        "message": error_msg,
                        "timestamp": datetime.now().isoformat(),
                    }

    async def _uncordon_single_node(self, node_name: str) -> Dict[str, Any]:
        """取消禁止单个节点调度"""
        max_retries = 1  # 最多重试1次
        retry_delay = 2  # 重试延迟2秒
        
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    logger.debug(f"🔄 重试取消节点 {node_name} 调度禁止 (第{attempt}次重试)...")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.debug(f"✅ 正在取消节点 {node_name} 调度禁止...")

                # 每次尝试都重新获取节点信息
                node = await self.core_v1.read_node(name=node_name)

                # 设置节点为可调度
                node.spec.unschedulable = False

                # 更新节点
                await self.core_v1.patch_node(name=node_name, body=node)

                result = {
                    "node_name": node_name,
                    "operation": "uncordon",
                    "status": "success",
                    "message": f"节点 {node_name} 已取消调度禁止" + (f" (第{attempt}次重试成功)" if attempt > 0 else ""),
                    "timestamp": datetime.now().isoformat(),
                }
                logger.info(f"✅ {result['message']}")
                return result

            except (ApiException, Exception) as e:
                error_msg = f"取消节点 {node_name} 调度禁止失败"
                if isinstance(e, ApiException):
                    error_msg += f" - K8s API异常: {e}"
                else:
                    error_msg += f": {e}"
                
                if attempt < max_retries:
                    logger.warning(f"⚠️ {error_msg}，将在{retry_delay}秒后重试...")
                else:
                    logger.error(f"❌ {error_msg}，已达到最大重试次数")
                    return {
                        "node_name": node_name,
                        "operation": "uncordon",
                        "status": "error",
                        "message": error_msg,
                        "timestamp": datetime.now().isoformat(),
                    }

    async def cordon_nodes_exclude(self, exclude_nodes: List[str] = None) -> Dict[str, Any]:
        """
        禁止节点调度（排除指定节点）

        Args:
            exclude_nodes: 不需要禁止调度的节点名称列表

        Returns:
            操作结果字典，包含成功和失败的节点信息
        """
        start_time = datetime.now()
        logger.info(f"🚀 开始批量禁止节点调度操作...")

        try:
            # 获取所有节点
            all_nodes = await self.get_all_nodes()

            # 过滤出需要操作的节点
            nodes_to_cordon = self._filter_nodes_to_operate(all_nodes, exclude_nodes or [])

            if not nodes_to_cordon:
                logger.warning("⚠️ 没有需要禁止调度的节点")
                return {
                    "operation": "cordon_nodes_exclude",
                    "total_nodes": len(all_nodes),
                    "excluded_nodes": exclude_nodes or [],
                    "target_nodes": [],
                    "success_count": 0,
                    "error_count": 0,
                    "results": [],
                    "duration_seconds": (datetime.now() - start_time).total_seconds(),
                    "timestamp": start_time.isoformat(),
                }

            # 创建信号量限制并发数
            semaphore = asyncio.Semaphore(self.max_concurrent_operations)

            async def cordon_with_semaphore(node_name):
                async with semaphore:
                    return await asyncio.wait_for(self._cordon_single_node(node_name), timeout=self.operation_timeout)

            # 并发执行禁止调度操作
            logger.info(f"🔄 开始并发禁止 {len(nodes_to_cordon)} 个节点的调度...")
            tasks = [cordon_with_semaphore(node) for node in nodes_to_cordon]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果
            success_results = []
            error_results = []

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    error_result = {
                        "node_name": nodes_to_cordon[i],
                        "operation": "cordon",
                        "status": "error",
                        "message": f"操作超时或异常: {str(result)}",
                        "timestamp": datetime.now().isoformat(),
                    }
                    error_results.append(error_result)
                    logger.error(f"❌ 节点 {nodes_to_cordon[i]} 禁止调度失败: {str(result)}")
                elif result["status"] == "success":
                    success_results.append(result)
                else:
                    error_results.append(result)

            # 汇总结果
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            summary = {
                "operation": "cordon_nodes_exclude",
                "total_nodes": len(all_nodes),
                "excluded_nodes": exclude_nodes or [],
                "target_nodes": nodes_to_cordon,
                "success_count": len(success_results),
                "error_count": len(error_results),
                "results": success_results + error_results,
                "duration_seconds": duration,
                "timestamp": start_time.isoformat(),
            }

            logger.info(
                f"🏁 批量禁止调度操作完成 - 成功: {len(success_results)}, 失败: {len(error_results)}, 耗时: {duration:.2f}秒"
            )
            return summary

        except Exception as e:
            logger.error(f"批量禁止调度操作失败: {e}")
            raise

    async def _delayed_uncordon_execution(self, nodes_to_uncordon: List[str], delay_seconds: int = 10, error_callback=None):
        """延迟执行取消禁止调度的内部函数"""
        try:
            logger.info(f"⏰ 等待 {delay_seconds} 秒后开始执行取消禁止调度操作...")
            await asyncio.sleep(delay_seconds)

            # 创建自己的客户端管理器，确保延迟任务有独立的客户端会话
            async with K8sClientManager() as k8s_manager:
                # 临时保存原始客户端，并使用新的客户端
                original_core_v1 = self.core_v1
                self.core_v1 = k8s_manager.core_v1
                
                try:
                    # 创建信号量限制并发数
                    semaphore = asyncio.Semaphore(self.max_concurrent_operations)

                    async def uncordon_with_semaphore(node_name):
                        async with semaphore:
                            return await asyncio.wait_for(self._uncordon_single_node(node_name), timeout=self.operation_timeout)

                    # 并发执行取消禁止调度操作
                    logger.info(f"🔄 开始并发取消 {len(nodes_to_uncordon)} 个节点的调度禁止...")
                    tasks = [uncordon_with_semaphore(node) for node in nodes_to_uncordon]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # 处理结果
                    success_count = 0
                    error_count = 0
                    failed_nodes = []

                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            error_count += 1
                            node_name = nodes_to_uncordon[i]
                            error_msg = str(result)
                            failed_nodes.append(f"{node_name}: {error_msg}")
                            logger.error(f"❌ 节点 {node_name} 取消调度禁止失败: {error_msg}")
                        elif result.get("status") == "success":
                            success_count += 1
                        else:
                            error_count += 1
                            node_name = nodes_to_uncordon[i]
                            error_msg = result.get("message", "未知错误")
                            failed_nodes.append(f"{node_name}: {error_msg}")
                            logger.error(f"❌ 节点 {node_name} 取消调度禁止失败: {error_msg}")

                    logger.info(f"🏁 延迟取消禁止调度操作完成 - 成功: {success_count}, 失败: {error_count}")

                    # 如果有失败的节点且提供了错误回调，则调用回调函数
                    if error_count > 0 and error_callback:
                        error_message = f"取消禁止调度操作部分失败 - 成功: {success_count}, 失败: {error_count}。失败详情: {'; '.join(failed_nodes)}"
                        try:
                            if asyncio.iscoroutinefunction(error_callback):
                                await error_callback(error_message)
                            else:
                                error_callback(error_message)
                        except Exception as callback_error:
                            logger.error(f"调用错误回调函数失败: {callback_error}")
                
                finally:
                    # 恢复原始客户端
                    self.core_v1 = original_core_v1

        except Exception as e:
            logger.error(f"延迟取消禁止调度操作失败: {e}")
            # 如果提供了错误回调，也要通知这个异常
            if error_callback:
                error_message = f"延迟取消禁止调度操作完全失败: {str(e)}"
                try:
                    if asyncio.iscoroutinefunction(error_callback):
                        await error_callback(error_message)
                    else:
                        error_callback(error_message)
                except Exception as callback_error:
                    logger.error(f"调用错误回调函数失败: {callback_error}")

    async def uncordon_nodes_exclude(self, exclude_nodes: List[str] = None, delay_seconds: int = 10, error_callback=None) -> Dict[str, Any]:
        """
        取消禁止节点调度（排除指定节点）- 延迟执行版本

        Args:
            exclude_nodes: 不需要取消禁止调度的节点名称列表
            delay_seconds: 延迟执行时间（秒），默认10秒
            error_callback: 错误回调函数，当操作失败时调用

        Returns:
            操作结果字典，立即返回，实际操作会延迟执行
        """
        start_time = datetime.now()
        logger.info(f"🚀 准备批量取消禁止节点调度操作（延迟 {delay_seconds} 秒执行）...")

        try:
            # 获取所有节点
            all_nodes = await self.get_all_nodes()

            # 过滤出需要操作的节点
            nodes_to_uncordon = self._filter_nodes_to_operate(all_nodes, exclude_nodes or [])

            if not nodes_to_uncordon:
                logger.warning("⚠️ 没有需要取消禁止调度的节点")
                return {
                    "operation": "uncordon_nodes_exclude_delayed",
                    "total_nodes": len(all_nodes),
                    "excluded_nodes": exclude_nodes or [],
                    "target_nodes": [],
                    "scheduled_count": 0,
                    "delay_seconds": delay_seconds,
                    "status": "no_nodes_to_process",
                    "timestamp": start_time.isoformat(),
                }

            # 创建延迟执行的异步任务（不等待完成）
            asyncio.create_task(self._delayed_uncordon_execution(nodes_to_uncordon, delay_seconds, error_callback))

            # 立即返回结果
            summary = {
                "operation": "uncordon_nodes_exclude_delayed",
                "total_nodes": len(all_nodes),
                "excluded_nodes": exclude_nodes or [],
                "target_nodes": nodes_to_uncordon,
                "scheduled_count": len(nodes_to_uncordon),
                "delay_seconds": delay_seconds,
                "status": "scheduled",
                "message": f"已安排 {len(nodes_to_uncordon)} 个节点在 {delay_seconds} 秒后取消调度禁止",
                "execution_time": (start_time + timedelta(seconds=delay_seconds)).isoformat(),
                "timestamp": start_time.isoformat(),
            }

            logger.info(f"📅 已安排延迟取消禁止调度操作 - {len(nodes_to_uncordon)} 个节点将在 {delay_seconds} 秒后执行")
            return summary

        except Exception as e:
            logger.error(f"安排延迟取消禁止调度操作失败: {e}")
            raise

    async def get_nodes_scheduling_status(self) -> Dict[str, Any]:
        """获取所有节点的调度状态"""
        try:
            logger.info("📊 正在获取节点调度状态...")
            nodes = await self.core_v1.list_node()

            schedulable_nodes = []
            unschedulable_nodes = []

            for node in nodes.items:
                node_name = node.metadata.name
                is_unschedulable = getattr(node.spec, 'unschedulable', False)

                if is_unschedulable:
                    unschedulable_nodes.append(node_name)
                else:
                    schedulable_nodes.append(node_name)

            status_info = {
                "total_nodes": len(nodes.items),
                "schedulable_nodes": schedulable_nodes,
                "unschedulable_nodes": unschedulable_nodes,
                "schedulable_count": len(schedulable_nodes),
                "unschedulable_count": len(unschedulable_nodes),
                "timestamp": datetime.now().isoformat(),
            }

            logger.info(f"📊 节点调度状态 - 可调度: {len(schedulable_nodes)}, 禁止调度: {len(unschedulable_nodes)}")
            return status_info

        except Exception as e:
            logger.error(f"获取节点调度状态失败: {e}")
            raise
