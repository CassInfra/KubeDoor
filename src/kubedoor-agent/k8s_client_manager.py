#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S客户端管理器模块
提供统一的Kubernetes客户端管理，确保客户端正确关闭
"""

from kubernetes_asyncio import client, config
from loguru import logger


def load_incluster_config():
    """加载集群内配置"""
    try:
        config.load_incluster_config()
        logger.info("✅ 成功加载集群内配置")
    except Exception as e:
        logger.error(f"❌ 加载集群内配置失败: {e}")
        raise


class K8sClientManager:
    """K8s客户端管理器，确保客户端正确关闭"""
    
    def __init__(self):
        self.core_v1_api = None
        self.apps_v1_api = None
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        try:
            logger.info("🔧 正在获取 K8s 客户端...")
            load_incluster_config()
            self.core_v1_api = client.CoreV1Api()
            self.apps_v1_api = client.AppsV1Api()
            
            if self.core_v1_api is None:
                logger.error("❌ CoreV1Api 客户端创建失败，返回 None")
                raise Exception("CoreV1Api 客户端创建失败")
            if self.apps_v1_api is None:
                logger.error("❌ AppsV1Api 客户端创建失败，返回 None")
                raise Exception("AppsV1Api 客户端创建失败")
                
            logger.info("✅ K8s 客户端获取成功")
            return self
        except Exception as e:
            logger.error(f"❌ 获取 K8s 客户端失败: {e}")
            await self.__aexit__(None, None, None)
            raise
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口，确保客户端正确关闭"""
        try:
            if self.core_v1_api:
                await self.core_v1_api.api_client.close()
                logger.debug("✅ CoreV1Api 客户端已关闭")
        except Exception as e:
            logger.warning(f"⚠️ 关闭 CoreV1Api 客户端时出错: {e}")
        
        try:
            if self.apps_v1_api:
                await self.apps_v1_api.api_client.close()
                logger.debug("✅ AppsV1Api 客户端已关闭")
        except Exception as e:
            logger.warning(f"⚠️ 关闭 AppsV1Api 客户端时出错: {e}")

    @property
    def core_v1(self):
        """获取 CoreV1Api 客户端"""
        return self.core_v1_api
    
    @property
    def apps_v1(self):
        """获取 AppsV1Api 客户端"""
        return self.apps_v1_api