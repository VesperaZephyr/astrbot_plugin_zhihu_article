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
    def parse_content_html(cls, html_content: str) -> Tuple[str, int, int]:
        """
        深度解析知乎正文 HTML：
        1. 精准提取 <span class="ztext-math" data-tex="..."> 为 $...$ 和 $$...$$
        2. 提取图片说明文字
        3. 统计公式数与真实插图数
        返回 (纯文本, 公式数, 图片数)
        """
        if not html_content:
            return "", 0, 0

        soup = BeautifulSoup(html_content, "html.parser")

        # 统计真实插图（排除头像等小图标）
        images = soup.find_all("img")
        image_count = 0
        for img in images:
            classes = img.get("class") or []
            if isinstance(classes, str):
                classes = [classes]
            if any("Avatar" in c or "Icon" in c or "Emoji" in c for c in classes):
                continue
            if img.get("data-thumbnail") or img.get("data-original") or img.get("src"):
                image_count += 1

        # 提取并转换数学公式
        math_spans = soup.find_all(class_=lambda c: c and "ztext-math" in (c if isinstance(c, list) else [c]))
        formula_count = len(math_spans)

        for span in math_spans:
            tex = (span.get("data-tex") or "").strip()
            if not tex:
                script_tag = span.find("script", type=lambda t: t and "math/tex" in t)
                if script_tag and script_tag.string:
                    tex = script_tag.string.strip()
            if not tex:
                tex = span.get_text(strip=True)
            if not tex:
                span.decompose()
                continue

            # 判断是否为独立块级公式：父节点仅包含该公式
            is_block = False
            parent = span.parent
            if parent is not None and parent.name in ("p", "div"):
                sibling_text = parent.get_text(strip=True)
                own_text = span.get_text(strip=True)
                if len(sibling_text) <= len(own_text) + 2:
                    is_block = True

            if is_block:
                span.replace_with(f"\n\n$${tex}$$\n\n")
            else:
                span.replace_with(f" ${tex}$ ")

        # 提取图片说明文字
        for img in soup.find_all("img"):
            desc_parts = []
            alt = (img.get("alt") or "").strip()
            if alt and alt != "图片":
                desc_parts.append(alt)
            title = (img.get("title") or "").strip()
            if title:
                desc_parts.append(title)
            desc = " - ".join(desc_parts) if desc_parts else "插图"
            img.replace_with(f"\n[配图: {desc}]\n")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text, formula_count, image_count

    @staticmethod
    def parse_vote_text(vote_text: str) -> int:
        """解析知乎赞同文本，如 '赞同 5.9 万' / '赞同 648' -> 整数"""
        if not vote_text:
            return 0
        m = re.search(r"([\d.]+)\s*万", vote_text)
        if m:
            try:
                return int(float(m.group(1)) * 10000)
            except ValueError:
                return 0
        m = re.search(r"([\d]+)", vote_text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return 0
        return 0

    @classmethod
    def build_from_browser_html(
        cls, content_html: str, title: str, author: str, vote_text: str, source_url: str
    ) -> Dict[str, Any]:
        """将浏览器提取的 HTML 组装为与 API 一致的数据结构"""
        full_text, formula_count, image_count = cls.parse_content_html(content_html)
        return {
            "type": "browser",
            "title": title or "知乎内容",
            "author": author or "知乎用户",
            "voteup_count": cls.parse_vote_text(vote_text),
            "comment_count": 0,
            "content_html": content_html,
            "full_text": full_text,
            "formula_count": formula_count,
            "image_count": image_count,
            "source_url": source_url,
        }

    # 向后兼容旧内部调用
    _parse_html = parse_content_html
