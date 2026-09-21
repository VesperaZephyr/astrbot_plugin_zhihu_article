# -*- coding: utf-8 -*-
"""
知乎文章与回答数据获取模块
调用知乎 API 获取回答/专栏/问题高赞内容，并精准提取 data-tex 数学公式与图片
"""

import re
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

ZHIHU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.zhihu.com",
    "Origin": "https://www.zhihu.com",
}

_LAST_REQ_TIME = 0.0
_RATE_LOCK = asyncio.Lock()

async def _rate_limit():
    global _LAST_REQ_TIME
    async with _RATE_LOCK:
        now = asyncio.get_event_loop().time()
        elapsed = now - _LAST_REQ_TIME
        if elapsed < 0.35:
            await asyncio.sleep(0.35 - elapsed)
        _LAST_REQ_TIME = asyncio.get_event_loop().time()


class ZhihuFetcher:
    """知乎数据获取器"""

    @classmethod
    async def fetch(cls, content_type: str, target_id: str, cookie_str: str = "") -> Optional[Dict[str, Any]]:
        """根据内容类型获取详情"""
        if content_type == "answer":
            return await cls._fetch_answer(target_id, cookie_str)
        elif content_type == "article":
            return await cls._fetch_article(target_id, cookie_str)
        elif content_type == "question":
            return await cls._fetch_question_top_answer(target_id, cookie_str)
        return None

    @classmethod
    async def _do_request(cls, url: str, cookie_str: str, params: Optional[dict] = None) -> Optional[dict]:
        await _rate_limit()
        headers = dict(ZHIHU_HEADERS)
        if cookie_str:
            headers["Cookie"] = cookie_str

        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"[ZhihuFetcher] 请求失败: HTTP {resp.status} ({url})")
                    return None
        except Exception as e:
            logger.error(f"[ZhihuFetcher] 网络请求异常: {e}", exc_info=True)
            return None

    @classmethod
    async def _fetch_answer(cls, answer_id: str, cookie_str: str) -> Optional[Dict[str, Any]]:
        url = f"https://www.zhihu.com/api/v4/answers/{answer_id}"
        params = {
            "include": (
                "content,voteup_count,comment_count,"
                "author.name,author.avatar_url,author.headline,"
                "question.id,question.title,question.detail"
            )
        }
        data = await cls._do_request(url, cookie_str, params)
        if not data:
            return None

        question = data.get("question", {})
        author = data.get("author", {})
        qid = str(question.get("id", ""))
        raw_html = data.get("content", "")

        full_text, formula_count, image_count = cls._parse_html(raw_html)

        return {
            "type": "answer",
            "id": answer_id,
            "title": question.get("title", "知乎问题"),
            "author": author.get("name", "匿名知友"),
            "voteup_count": data.get("voteup_count", 0),
            "comment_count": data.get("comment_count", 0),
            "content_html": raw_html,
            "full_text": full_text,
            "formula_count": formula_count,
            "image_count": image_count,
            "source_url": f"https://www.zhihu.com/question/{qid}/answer/{answer_id}",
        }

    @classmethod
    async def _fetch_article(cls, article_id: str, cookie_str: str) -> Optional[Dict[str, Any]]:
        url = f"https://api.zhihu.com/articles/{article_id}"
        data = await cls._do_request(url, cookie_str)
        if not data:
            url = f"https://www.zhihu.com/api/v4/articles/{article_id}"
            data = await cls._do_request(url, cookie_str)
            if not data:
                return None

        author = data.get("author", {})
        raw_html = data.get("content", "")
        full_text, formula_count, image_count = cls._parse_html(raw_html)

        return {
            "type": "article",
            "id": article_id,
            "title": data.get("title", "知乎专栏文章"),
            "author": author.get("name", "匿名知友"),
            "voteup_count": data.get("voteup_count", 0),
            "comment_count": data.get("comment_count", 0),
            "content_html": raw_html,
            "full_text": full_text,
            "formula_count": formula_count,
            "image_count": image_count,
            "source_url": f"https://zhuanlan.zhihu.com/p/{article_id}",
        }

    @classmethod
    async def _fetch_question_top_answer(cls, question_id: str, cookie_str: str) -> Optional[Dict[str, Any]]:
        url = f"https://www.zhihu.com/api/v4/questions/{question_id}/answers"
        params = {
            "limit": 1,
            "sort_by": "default",
            "include": (
                "content,voteup_count,comment_count,"
                "author.name,author.avatar_url,author.headline,"
                "question.id,question.title"
            )
        }
        data = await cls._do_request(url, cookie_str, params)
        if not data:
            return None

        items = data.get("data", [])
        if not items:
            return None

        answer = items[0]
        author = answer.get("author", {})
        question = answer.get("question", {})
        aid = str(answer.get("id", ""))
        raw_html = answer.get("content", "")

        full_text, formula_count, image_count = cls._parse_html(raw_html)

        return {
            "type": "question",
            "id": aid,
            "title": question.get("title", "知乎问题"),
            "author": author.get("name", "高赞知友"),
            "voteup_count": answer.get("voteup_count", 0),
            "comment_count": answer.get("comment_count", 0),
            "content_html": raw_html,
            "full_text": full_text,
            "formula_count": formula_count,
            "image_count": image_count,
            "source_url": f"https://www.zhihu.com/question/{question_id}/answer/{aid}",
        }

    @classmethod
    def _parse_html(cls, html_content: str) -> Tuple[str, int, int]:
        """
        深度解析知乎正文 HTML：
        1. 精准提取 <span class="ztext-math" data-tex="..."> 为 $...$ 和 $$...$$
        2. 提取图片说明文字
        3. 统计公式数与真实插图数
        """
        if not html_content:
            return "", 0, 0

        soup = BeautifulSoup(html_content, "html.parser")

        # 统计图片
        images = soup.find_all("img")
        image_count = len([img for img in images if not img.get("class") or "Avatar" not in img.get("class", [])])

        # 提取并保护数学公式
        math_spans = soup.find_all(class_=lambda c: c and "ztext-math" in c)
        formula_count = len(math_spans)

        for span in math_spans:
            tex = span.get("data-tex", "").strip()
            if not tex:
                script_tag = span.find("script", type=lambda t: t and "math/tex" in t)
                if script_tag and script_tag.string:
                    tex = script_tag.string.strip()
            if not tex:
                tex = span.get_text(strip=True)

            if not tex:
                continue

            # 判断是独立块级还是行内
            is_block = False
            parent = span.parent
            if parent and parent.name in ["p", "div"] and len(parent.get_text(strip=True)) == len(span.get_text(strip=True)):
                is_block = True

            if is_block:
                span.replace_with(f"\n\n$${tex}$$\n\n")
            else:
                span.replace_with(f" ${tex}$ ")

        # 提取图片说明文字
        for img in soup.find_all("img"):
            desc_parts = []
            alt = img.get("alt", "").strip()
            if alt and alt != "图片":
                desc_parts.append(alt)
            title = img.get("title", "").strip()
            if title:
                desc_parts.append(title)
            desc = " - ".join(desc_parts) if desc_parts else "插图"
            img.replace_with(f"\n[配图: {desc}]\n")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text, formula_count, image_count
