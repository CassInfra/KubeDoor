#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K8S客户端管理器模块
提供统一的Kubernetes客户端管理，确保客户端正确关闭
"""

import asyncio
import json
import re

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import rest
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger

# 响应体达到这个大小才放到线程里反序列化(64KB 在事件循环里反序列化也就几毫秒,不值得切线程)
OFFLOAD_MIN_BYTES = 64 * 1024
LIST_PAGE_SIZE = 500


async def list_all_raw(list_fn, **kwargs):
    """分页 list,返回原始 JSON 的 items(dict 列表),不反序列化成模型对象

    模型对象一个 Pod 约 1.7ms,原始 JSON 快 10 倍左右;json.loads 放线程里做,
    分页让每次解析持有 GIL 的时间短(json.loads 是 C 实现,解析期间不会让出 GIL)。
    非 2xx 抛 ApiException,status / reason / body 和直接调 API 方法时一致。
    """
    items = []
    cont = None
    while True:
        page_kwargs = dict(kwargs, limit=LIST_PAGE_SIZE, _preload_content=False)
        if cont:
            page_kwargs["_continue"] = cont
        resp = await list_fn(**page_kwargs)
        try:
            body = await resp.read()
        finally:
            resp.release()
        if not 200 <= resp.status <= 299:
            exc = ApiException(http_resp=rest.RESTResponse(resp, body))
            exc.body = body.decode("utf-8", "replace")
            raise exc
        page = await asyncio.to_thread(json.loads, body)
        items.extend(page.get("items") or [])
        cont = (page.get("metadata") or {}).get("continue")
        if not cont:
            return items


class OffloadApiClient(client.ApiClient):
    """响应体较大时,把 JSON → 模型对象的反序列化放到线程池里做,不阻塞事件循环

    kubernetes_asyncio 原版在事件循环里同步反序列化,一个 Pod 约 1.7ms:list 几千个 Pod 就会卡住
    事件循环好几秒,期间 /api/health 探活、master 心跳、K8S 事件 watch、admission webhook 全部停摆。
    这里只接管"读响应体 + 反序列化"这一步,处理逻辑和原版 __call_api 一致;拼请求、鉴权、超时仍走原版。
    _preload_content=False(watch / 日志流 / 自己解析原始 JSON 的调用)原样透传。
    """

    def call_api(self, *args, **kwargs):
        types_map = kwargs.get("response_types_map")
        # 生成的 API 方法都用关键字传这几个参数;对不上的调用方式一律走原版
        if kwargs.get("async_req") or kwargs.get("_preload_content", True) is False or not isinstance(types_map, dict):
            return super().call_api(*args, **kwargs)
        return self._call_api_offloaded(args, kwargs, types_map)

    async def _call_api_offloaded(self, args, kwargs, types_map):
        kwargs["_preload_content"] = False
        raw = await super().call_api(*args, **kwargs)
        try:
            data = await raw.read()
        finally:
            raw.release()
        response = rest.RESTResponse(raw, data)
        self.last_response = response
        if not 200 <= response.status <= 299:
            exc = ApiException(http_resp=response)
            exc.body = data.decode("utf-8", "replace")
            raise exc

        result = None
        response_type = types_map.get(response.status)
        if response_type:
            if response_type not in ("file", "bytes"):
                match = re.search(r"charset=([a-zA-Z\-\d]+)[\s;]?", response.getheader("content-type") or "")
                response.data = data.decode(match.group(1) if match else "utf-8")
            if len(data) >= OFFLOAD_MIN_BYTES:
                result = await asyncio.to_thread(self.deserialize, response, response_type)
            else:
                result = self.deserialize(response, response_type)
        if kwargs.get("_return_http_data_only"):
            return result
        return result, response.status, response.getheaders()


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
            self.core_v1_api = client.CoreV1Api(OffloadApiClient())
            self.apps_v1_api = client.AppsV1Api(OffloadApiClient())
            
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