#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
镜像标签获取器
支持阿里云ACR、华为云SWR和自建Harbor仓库
根据K8S名称和完整镜像地址获取最新的20个标签名称和推送时间
"""

import os
import json
import requests
import base64
from datetime import datetime
import datetime as dt
from typing import List, Tuple, Dict, Any
from urllib.parse import urlparse
from aiohttp import web
import utils, sys
from promql import deployment_image, deployment_image_min
from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkswr.v2.swr_client import SwrClient
from huaweicloudsdkswr.v2.model.list_repository_tags_request import ListRepositoryTagsRequest
from huaweicloudsdkswr.v2.region.swr_region import SwrRegion

from aliyunsdkcore.client import AcsClient
from aliyunsdkcr.request.v20160607 import GetRepoTagsRequest
from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    format='<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> [<level>{level}</level>] <level>{message}</level>',
    level='INFO',
)


class ImageTagsFetcher:
    """镜像标签获取器"""

    def __init__(self):
        self.registry_secret = self._load_registry_secret()

    def _load_registry_secret(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """从环境变量加载仓库认证信息"""
        secret_str = os.getenv('REGISTRY_SECRET', '{}')
        try:
            return json.loads(secret_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"REGISTRY_SECRET环境变量格式错误: {e}")

    def _get_credentials(self, k8s_name: str, registry_domain: str) -> Dict[str, str]:
        """获取指定K8S名称和仓库域名的认证信息"""
        # 新的数据结构: {域名: {k8s集群: {ak, sk}}}
        if registry_domain not in self.registry_secret:
            logger.error(f"未找到域名 '{registry_domain}' 的认证配置")
            return {}

        domain_config = self.registry_secret[registry_domain]

        # 优先查找指定的k8s集群配置
        if k8s_name in domain_config:
            creds = domain_config[k8s_name]
        # 如果找不到指定集群，使用default配置
        elif 'default' in domain_config:
            creds = domain_config['default']
        else:
            logger.error(f"未找到K8S集群 '{k8s_name}' 或 'default' 的认证配置")
            return {}
        if 'ak' not in creds or 'sk' not in creds:
            logger.error(f"认证配置缺少ak或sk字段")
            return {}
        return creds

    def _determine_registry_type(self, image_url: str) -> str:
        """判断镜像仓库类型"""
        if 'aliyuncs.com' in image_url:
            return 'acr'
        elif 'myhuaweicloud.com' in image_url:
            return 'swr'
        else:
            return 'harbor'

    def _parse_image_url(self, image_url: str) -> Dict[str, str]:
        """解析镜像URL

        支持的格式:
        - registry.com/namespace/repo
        - registry.com/namespace/repo:tag
        - https://registry.com/namespace/repo:tag
        """
        # 移除协议前缀
        if image_url.startswith('http://') or image_url.startswith('https://'):
            parsed = urlparse(image_url)
            domain = parsed.netloc
            path = parsed.path.lstrip('/')
        else:
            # 处理没有协议的URL
            # 先移除标签部分（如果存在）
            url_without_tag = image_url
            if ':' in image_url:
                # 找到最后一个冒号，可能是标签分隔符
                colon_index = image_url.rfind(':')
                # 检查冒号后面是否是标签（不包含斜杠）
                potential_tag = image_url[colon_index + 1 :]
                if '/' not in potential_tag:
                    url_without_tag = image_url[:colon_index]

            # 分割域名和路径
            parts = url_without_tag.split('/', 1)
            domain = parts[0]
            path = parts[1] if len(parts) > 1 else ''

        # 分离命名空间和仓库名
        if '/' in path:
            parts = path.split('/')
            if len(parts) >= 2:
                namespace = parts[0]
                repository = '/'.join(parts[1:])  # 支持多级路径
            else:
                namespace = ''
                repository = parts[0]
        else:
            namespace = ''
            repository = path

        return {
            'domain': domain,
            'namespace': namespace,
            'repository': repository,
            'full_path': f"{namespace}/{repository}" if namespace else repository,
        }

    def get_image_tags(self, k8s_name: str, image_url: str) -> Dict[str, Any]:
        """获取镜像标签列表

        Args:
            k8s_name: K8S名称
            image_url: 完整镜像地址

        Returns:
            Dict[str, Any]: {
                'tags': [[推送时间, 镜像标签], ...],  # 20个镜像标签的列表
                'current_tag_info': [...]  # 新列表，根据传入标签是否存在决定格式
            }
        """
        registry_type = self._determine_registry_type(image_url)
        image_info = self._parse_image_url(image_url)
        credentials = self._get_credentials(k8s_name, image_info['domain'])

        # 获取20个镜像标签列表
        try:
            if registry_type == 'acr':
                tags_list = self._get_acr_tags(image_info, credentials)
            elif registry_type == 'swr':
                tags_list = self._get_swr_tags(image_info, credentials)
            else:
                tags_list = self._get_harbor_tags(image_info, credentials)
        except Exception:
            # 获取镜像标签失败时返回空列表
            tags_list = []

        # 提取传入image_url中的标签
        input_tag = self._extract_tag_from_url(image_url)

        # 构建不带标签的image_url
        image_url_without_tag = self._remove_tag_from_url(image_url)

        # 检查传入的标签是否在返回的20个标签中
        current_tag_info = []
        if input_tag:
            # 查找匹配的标签和推送时间
            found_tag = None
            for push_time, tag in tags_list:
                if tag == input_tag:
                    found_tag = (push_time, tag)
                    break

            if found_tag:
                # 标签存在，返回 [image_url(不带标签), 单独标签部分, 推送时间]
                current_tag_info = [image_url_without_tag, input_tag, found_tag[0]]
            else:
                # 标签不存在，返回 [image_url(不带标签), 单独标签部分]
                current_tag_info = [image_url_without_tag, input_tag]

        return {'tags': tags_list, 'current_tag_info': current_tag_info}

    def _get_acr_tags(self, image_info: Dict[str, str], credentials: Dict[str, str]) -> List[Tuple[str, str]]:
        """获取阿里云ACR镜像标签"""
        try:
            # 提取区域信息
            region = self._extract_region_from_domain(image_info['domain'])
            namespace = image_info['namespace']
            repository = image_info['repository']

            # 创建阿里云客户端
            client = AcsClient(credentials['ak'], credentials['sk'], region)

            # 构造请求
            request = GetRepoTagsRequest.GetRepoTagsRequest()
            request.set_RepoNamespace(namespace)
            request.set_RepoName(repository)
            request.set_PageSize("20")

            # 设置请求地址
            request.set_endpoint(f"cr.{region}.aliyuncs.com")

            # 发起请求
            response = json.loads(client.do_action_with_exception(request))

            # 解析响应数据
            tags = []
            if 'data' in response and 'tags' in response['data']:
                tags_list = response['data']['tags']
                for tag_info in tags_list:
                    # 获取更新时间（毫秒时间戳）
                    image_update = tag_info.get('imageUpdate', 0)
                    if image_update:
                        # 转换时间戳为本地时间
                        update_time = datetime.fromtimestamp(image_update / 1000)
                        update_time_str = update_time.strftime('%y-%m-%d %H:%M')
                    else:
                        update_time_str = '未知时间'

                    # 获取标签名
                    tag = tag_info.get('tag', '')
                    if tag:
                        tags.append((update_time_str, tag))

            return tags
        except Exception as e:
            return []

    def _get_swr_tags(self, image_info: Dict[str, str], credentials: Dict[str, str]) -> List[Tuple[str, str]]:
        """获取华为云SWR镜像标签"""
        try:
            # 提取区域信息
            region = self._extract_region_from_domain(image_info['domain'])
            namespace = image_info['namespace']
            repository = image_info['repository']

            # 创建认证凭据
            basic_credentials = BasicCredentials(credentials['ak'], credentials['sk'])

            # 创建SWR客户端
            client = (
                SwrClient.new_builder()
                .with_credentials(basic_credentials)
                .with_region(SwrRegion.value_of(region))
                .build()
            )

            # 创建请求对象
            request = ListRepositoryTagsRequest()
            request.namespace = namespace
            request.repository = repository
            request.offset = "0"
            request.limit = "20"
            request.order_column = "updated_at"
            request.order_type = "desc"

            # 发送请求
            response = client.list_repository_tags(request)

            # 解析响应数据
            tags = []
            if response.body:
                for tag_info in response.body:
                    # 获取更新时间
                    update_time_str = getattr(tag_info, 'updated', '') or getattr(tag_info, 'update_time', '')
                    if update_time_str:
                        try:
                            # 转换UTC时间为北京时间（UTC+8）
                            update_time = datetime.fromisoformat(update_time_str.replace('Z', '+00:00'))
                            # 添加8小时时区偏移
                            update_time_beijing = update_time + dt.timedelta(hours=8)
                            update_time_local = update_time_beijing.strftime('%y-%m-%d %H:%M')
                        except ValueError:
                            update_time_local = update_time_str
                    else:
                        update_time_local = '未知时间'

                    # 获取标签名
                    tag = getattr(tag_info, 'tag', '') or getattr(tag_info, 'Tag', '')
                    if tag:
                        tags.append((update_time_local, tag))

            return tags
        except Exception as e:
            return []

    def _get_harbor_tags(self, image_info: Dict[str, str], credentials: Dict[str, str]) -> List[Tuple[str, str]]:
        """获取Harbor镜像标签"""
        # 使用Harbor v2.0 API
        registry_url = f"https://{image_info['domain']}"
        namespace = image_info['namespace']  # project
        repository = image_info['repository']  # repository

        # 构建认证头（Basic Auth）
        auth_string = f"{credentials['ak']}:{credentials['sk']}"
        auth_bytes = auth_string.encode('ascii')
        auth_b64 = base64.b64encode(auth_bytes).decode('ascii')

        headers = {
            'Authorization': f'Basic {auth_b64}',
            'Accept': 'application/json',
            'X-Accept-Vulnerabilities': 'application/vnd.security.vulnerability.report; version=1.1, application/vnd.scanner.adapter.vuln.report.harbor+json; version=1.0',
        }

        # 使用Harbor v2.0 API获取artifacts
        artifacts_url = f"{registry_url}/api/v2.0/projects/{namespace}/repositories/{repository}/artifacts"
        params = {
            'page': 1,
            'page_size': 20,
            'with_tag': 'true',
            'with_label': 'false',
            'with_scan_overview': 'false',
            'with_signature': 'false',
            'with_immutable_status': 'false',
            'with_accessory': 'false',
        }

        try:
            response = requests.get(artifacts_url, headers=headers, params=params, timeout=30)
            response.raise_for_status()

            artifacts = response.json()
            tags_with_time = []

            # 遍历artifacts获取标签信息
            for artifact in artifacts:
                if 'tags' in artifact and artifact['tags']:
                    # 获取第一个标签的信息
                    tag_info = artifact['tags'][0]
                    tag_name = tag_info.get('name', '')

                    # 获取推送时间
                    push_time_str = '未知时间'
                    if 'push_time' in artifact:
                        try:
                            # 解析ISO格式时间并添加8小时转换为北京时间
                            push_time = datetime.fromisoformat(artifact['push_time'].replace('Z', '+00:00'))
                            push_time_beijing = push_time + dt.timedelta(hours=8)
                            push_time_str = push_time_beijing.strftime('%y-%m-%d %H:%M')
                        except ValueError:
                            push_time_str = artifact['push_time']

                    if tag_name:
                        tags_with_time.append((push_time_str, tag_name))

            # 按时间排序（新到旧）
            tags_with_time.sort(key=lambda x: x[0], reverse=True)
            return tags_with_time[:20]

        except Exception as e:
            return []

    def _extract_region_from_domain(self, domain: str) -> str:
        """从域名提取区域信息"""
        parts = domain.split('.')
        return parts[1]

    def _extract_tag_from_url(self, image_url: str) -> str:
        """从镜像URL中提取标签部分"""
        # 移除协议前缀
        if image_url.startswith('http://') or image_url.startswith('https://'):
            parsed = urlparse(image_url)
            url_path = parsed.path.lstrip('/')
        else:
            url_path = image_url

        # 查找最后一个冒号，可能是标签分隔符
        if ':' in url_path:
            colon_index = url_path.rfind(':')
            potential_tag = url_path[colon_index + 1 :]
            # 检查冒号后面是否是标签（不包含斜杠）
            if '/' not in potential_tag:
                return potential_tag

        return ''

    def _remove_tag_from_url(self, image_url: str) -> str:
        """从镜像URL中移除标签部分，返回不带标签的URL"""
        # 移除协议前缀
        protocol = ''
        if image_url.startswith('http://'):
            protocol = 'http://'
            url_without_protocol = image_url[7:]
        elif image_url.startswith('https://'):
            protocol = 'https://'
            url_without_protocol = image_url[8:]
        else:
            url_without_protocol = image_url

        # 查找最后一个冒号，可能是标签分隔符
        if ':' in url_without_protocol:
            colon_index = url_without_protocol.rfind(':')
            potential_tag = url_without_protocol[colon_index + 1 :]
            # 检查冒号后面是否是标签（不包含斜杠）
            if '/' not in potential_tag:
                url_without_protocol = url_without_protocol[:colon_index]

        return protocol + url_without_protocol


def get_image_tags(k8s_name: str, image_url: str) -> Dict[str, Any]:
    fetcher = ImageTagsFetcher()
    return fetcher.get_image_tags(k8s_name, image_url)


# ==================== Web API 处理器 ====================
async def get_image_tags_handler(request):
    """获取镜像标签列表的POST接口处理器

    Args:
        request: aiohttp请求对象，期望的JSON格式:
        {
            "k8s": "集群名称",
            "namespace": "命名空间",
            "deployment": "部署名称"
        }

    Returns:
        JSON响应: {
            "success": true,
            "data": {
                "tags": [[推送时间, 镜像标签], ...],
                "current_tag_info": [...]
            }
        }
    """
    try:
        # 获取请求体数据
        body = await request.json()
        k8s = body.get('k8s')
        namespace = body.get('namespace')
        deployment = body.get('deployment')

        # 调用get_deployment_image函数获取镜像信息
        k8s_name, image_url = await utils.get_deployment_image(deployment_image, k8s, namespace, deployment)
        if image_url == 'retry':
            k8s_name, image_url = await utils.get_deployment_image(deployment_image_min, k8s, namespace, deployment)

        # 调用镜像标签获取函数(镜像仓库 SDK / HTTP 都是阻塞调用,放进线程池)
        result = await utils.run_blocking(get_image_tags, k8s_name, image_url)

        return web.json_response({"success": True, "data": result})

    except json.JSONDecodeError:
        return web.json_response({"success": False, "error": "请求体必须是有效的JSON格式"}, status=400)
    except ValueError as e:
        return web.json_response({"success": False, "error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


if __name__ == '__main__':
    # 测试示例
    import sys

    if len(sys.argv) != 3:
        print("使用方法: python image_tags_fetcher.py <k8s_name> <image_url>")
        print("")
        print("示例:")
        print("  阿里云ACR: python image_tags_fetcher.py prod registry.cn-hangzhou.aliyuncs.com/namespace/repo:tag")
        print("  华为云SWR: python image_tags_fetcher.py prod swr.cn-south-1.myhuaweicloud.com/namespace/repo:tag")
        print("  Harbor: python image_tags_fetcher.py prod harbor.example.com/namespace/repo:latest")
        print("")
        print("注意: 镜像URL支持带标签格式，程序会自动提取仓库信息")
        print("需要设置环境变量 REGISTRY_SECRET")
        print('格式: {"镜像域名":{"k8s名称":{"ak":"xxx","sk":"xxx"}}}')
        print("")
        print("环境变量设置示例:")
        print('$env:REGISTRY_SECRET={"swr.cn-south-1.myhuaweicloud.com":{"prod":{"ak":"your_ak","sk":"your_sk"}}}')
        sys.exit(1)

    k8s_name = sys.argv[1]
    image_url = sys.argv[2]

    print(f"K8S名称: {k8s_name}")
    print(f"镜像地址: {image_url}")
    print("-" * 60)

    try:
        # 检查环境变量
        registry_secret = os.getenv('REGISTRY_SECRET')
        if not registry_secret:
            print("❌ 错误: 未设置环境变量 REGISTRY_SECRET")
            print('请设置格式: {"镜像域名":{"k8s名称":{"ak":"xxx","sk":"xxx"}}}')
            print('支持default配置: {"镜像域名":{"default":{"ak":"xxx","sk":"xxx"}}}')
            print(
                '示例: {"swr.cn-south-1.myhuaweicloud.com":{"prod":{"ak":"ak123","sk":"sk456"},"default":{"ak":"default_ak","sk":"default_sk"}}}'
            )
            sys.exit(1)

        result = get_image_tags(k8s_name, image_url)
        tags = result['tags']
        current_tag_info = result['current_tag_info']

        print(f"✅ 成功获取到 {len(tags)} 个镜像标签:")
        print("")
        for i, (push_time, tag) in enumerate(tags, 1):
            print(f"{i:2d}. {push_time} - {tag}")

        print("")
        print("当前标签信息:")
        if current_tag_info:
            if len(current_tag_info) == 3:
                print(f"  镜像地址(不带标签): {current_tag_info[0]}")
                print(f"  标签: {current_tag_info[1]}")
                print(f"  推送时间: {current_tag_info[2]}")
                print(f"  ✅ 标签存在于返回列表中")
            else:
                print(f"  镜像地址(不带标签): {current_tag_info[0]}")
                print(f"  标签: {current_tag_info[1]}")
                print(f"  ❌ 标签不存在于返回列表中")
        else:
            print("  未检测到标签信息")
    except Exception as e:
        print(f"❌ 错误: {e}")
        sys.exit(1)
