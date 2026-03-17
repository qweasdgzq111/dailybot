#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
飞书文档管理器
负责对接飞书 Docx API，提供与 NoteManager 兼容的统一接口。
"""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import requests
from loguru import logger


class FeishuDocsManager:
    """飞书文档管理器。"""

    def __init__(self, config: Dict[str, Any]):
        """初始化飞书文档管理器。"""
        self.config = config
        self.base_url = config.get('base_url', 'https://open.feishu.cn/open-apis').rstrip('/')
        self.app_id = config.get('app_id')
        self.app_secret = config.get('app_secret')
        self.llm_service = None

        # 访问令牌缓存，避免每次请求都重新获取。
        self._tenant_access_token: Optional[str] = None
        self._token_expire_at: Optional[datetime] = None

        if not self.app_id or not self.app_secret:
            raise ValueError("飞书配置缺少 app_id/app_secret，无法初始化 FeishuDocsManager。")

        logger.info("Feishu Docs管理器初始化成功")

    def set_llm_service(self, llm_service: Any):
        """注入LLM服务实例（保持与其他后端接口一致）。"""
        self.llm_service = llm_service

    async def _ensure_token(self) -> str:
        """确保有可用的 tenant_access_token。"""
        now = datetime.utcnow()
        if self._tenant_access_token and self._token_expire_at and now < self._token_expire_at:
            return self._tenant_access_token

        def _fetch_token() -> Dict[str, Any]:
            url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
            resp = requests.post(
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()

        data = await asyncio.to_thread(_fetch_token)
        if data.get('code') != 0:
            raise RuntimeError(f"获取飞书tenant_access_token失败: {data}")

        expire_seconds = int(data.get('expire', 7200))
        self._tenant_access_token = data['tenant_access_token']
        # 预留60秒缓冲，减少边界时刻因过期导致的失败。
        self._token_expire_at = now + timedelta(seconds=max(60, expire_seconds - 60))
        return self._tenant_access_token

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """统一的飞书API请求封装。"""
        token = await self._ensure_token()
        headers = kwargs.pop('headers', {})
        headers['Authorization'] = f"Bearer {token}"
        headers['Content-Type'] = 'application/json; charset=utf-8'

        def _do_request() -> Dict[str, Any]:
            url = f"{self.base_url}{path}"
            resp = requests.request(method=method, url=url, headers=headers, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp.json()

        data = await asyncio.to_thread(_do_request)
        if data.get('code') != 0:
            raise RuntimeError(f"飞书API调用失败: {path}, 响应: {data}")
        return data

    async def get_document_content(self, document_id: str) -> Optional[Dict[str, Any]]:
        """获取文档基础信息。"""
        try:
            return await self._request('GET', f'/docx/v1/documents/{document_id}')
        except Exception as e:
            logger.error(f"获取飞书文档 {document_id} 失败: {e}")
            return None

    async def _get_document_blocks(self, document_id: str) -> List[Dict[str, Any]]:
        """分页获取文档所有块。"""
        all_items: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            params = {'page_size': 500}
            if page_token:
                params['page_token'] = page_token

            data = await self._request('GET', f'/docx/v1/documents/{document_id}/blocks', params=params)
            items = data.get('data', {}).get('items', [])
            all_items.extend(items)

            if not data.get('data', {}).get('has_more'):
                break
            page_token = data.get('data', {}).get('page_token')
            if not page_token:
                break

        return all_items

    def _extract_text_from_block(self, block: Dict[str, Any]) -> str:
        """从飞书 block 中提取可读文本。"""
        paragraph = block.get('paragraph', {})
        elements = paragraph.get('elements', [])
        text_parts: List[str] = []
        for element in elements:
            text_run = element.get('text_run', {})
            content = text_run.get('content', '')
            if content:
                text_parts.append(content)
        return ''.join(text_parts).strip()

    async def get_document_structure(self, document_id: str) -> Optional[Dict[str, Any]]:
        """提取文档标题结构。"""
        try:
            blocks = await self._get_document_blocks(document_id)
        except Exception as e:
            logger.error(f"获取飞书文档结构失败 {document_id}: {e}")
            return None

        # Feishu docx block_type 的标题枚举在不同版本SDK中存在差异。
        # 这里采用保守映射：3->H1, 4->H2, 5->H3, 6->H4，无法识别时跳过。
        heading_level_map = {3: 1, 4: 2, 5: 3, 6: 4}
        headings: List[Dict[str, Any]] = []

        for idx, block in enumerate(blocks):
            block_type = block.get('block_type')
            level = heading_level_map.get(block_type)
            if not level:
                continue

            text = self._extract_text_from_block(block)
            if not text:
                continue

            headings.append(
                {
                    'text': text,
                    'level': level,
                    # NoteManager 对“类Google文档后端”主要依赖 startIndex 的相对顺序。
                    # 飞书并非基于字符索引，因此这里使用顺序索引作为兼容占位。
                    'startIndex': idx,
                    'endIndex': idx + 1,
                }
            )

        return {
            'headings': headings,
            'end_of_document': max(1, len(blocks)),
            'raw_document': {'blocks': blocks},
        }

    async def _get_root_block_id(self, document_id: str) -> Optional[str]:
        """获取文档根块ID，用于追加子块。"""
        blocks = await self._get_document_blocks(document_id)
        if not blocks:
            return None

        # 常见情况下，根节点没有 parent_id；这里按该特征优先识别。
        for block in blocks:
            if not block.get('parent_id'):
                return block.get('block_id')

        # 回退到第一个block，保证在API字段变化时仍可尝试写入。
        return blocks[0].get('block_id')

    def _format_structured_content(self, note_data: Dict[str, Any], url: str) -> str:
        """将结构化笔记格式化为多行文本。"""
        date = note_data.get('date', '')
        title = note_data.get('title', '（无标题）')
        link_title = note_data.get('link_title', '（无链接标题）')
        summary = note_data.get('gist', '（无摘要）')
        return f"[自动导入]\n{date} {title}\n{link_title}\n{url}\n{summary}\n"

    async def execute_save(
        self,
        document_id: str,
        content_data: Dict[str, Any],
        insert_location: Dict[str, Any],
        document: Optional[Dict[str, Any]] = None,
    ):
        """将内容追加写入飞书文档。"""
        _ = insert_location  # 当前先保留接口，后续可扩展“按标题精准插入”。

        note_data = content_data.get('structured_note', {})
        url = content_data.get('url', '')
        if not note_data:
            logger.warning("structured_note为空，跳过飞书保存。")
            return

        # 先做单文档查重，避免重复写入。
        if await self.is_duplicate_in_document({'document_id': document_id}, content_data):
            logger.info(f"内容 '{note_data.get('title', '未知标题')}' 已存在于飞书文档，跳过保存。")
            return

        try:
            root_block_id = await self._get_root_block_id(document_id)
            if not root_block_id:
                logger.error(f"飞书文档 {document_id} 未找到可写入的根块，取消保存。")
                return

            content_text = self._format_structured_content(note_data, url)

            payload = {
                # Feishu Docx段落块: block_type=2。
                'children': [
                    {
                        'block_type': 2,
                        'paragraph': {
                            'elements': [
                                {
                                    'text_run': {
                                        'content': content_text,
                                    }
                                }
                            ]
                        },
                    }
                ],
                'index': -1,
            }

            await self._request(
                'POST',
                f'/docx/v1/documents/{document_id}/blocks/{root_block_id}/children',
                json=payload,
            )
            logger.info(f"飞书文档写入成功: {note_data.get('title', '未知标题')}")
        except Exception as e:
            logger.error(f"保存内容到飞书文档失败 ({document_id}): {e}", exc_info=True)

    async def get_document_text(self, doc_config: Dict[str, Any]) -> Optional[str]:
        """获取飞书文档纯文本（用于检索/查重）。"""
        document_id = doc_config.get('document_id')
        if not document_id:
            return None

        try:
            data = await self._request('GET', f'/docx/v1/documents/{document_id}/raw_content')
            return data.get('data', {}).get('content', '')
        except Exception as e:
            logger.error(f"获取飞书文档文本失败 ({document_id}): {e}")
            return None

    async def is_duplicate_in_document(self, doc_config: Dict[str, Any], content_data: Dict[str, Any]) -> bool:
        """在单个飞书文档中执行简单查重。"""
        text = await self.get_document_text(doc_config)
        if not text:
            return False

        note_data = content_data.get('structured_note', {})
        title = note_data.get('title', '')
        url = content_data.get('url', '')

        if title and title in text:
            return True
        if url and url in text:
            return True
        return False
