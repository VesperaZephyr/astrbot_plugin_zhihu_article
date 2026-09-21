# -*- coding: utf-8 -*-
"""
知乎回答与专栏文章全文读取、高保真公式排版与 AI 深度总结插件
支持：
  1. 指令触发：/zh <链接>、/知乎 <链接>、/zhihu <链接>
  2. 群聊/私聊链接自动识别
  3. LLM Function Calling (Agent Tool)：@机器人 并发送知乎链接或要求精读时，大模型自主调用
  4. 智能双轨合并转发模式：
     - 节点 1：以下是对知乎 (链接) 内容的解析和总结：
     - 节点 2：AI 深度总结（有公式为 MathJax 渲染卡片图，无公式为清晰纯文本）
     - 后续节点：知乎官方原版极清长图切片（公式 1:1 真实呈现零浮动，插图 100% 真实），或无图无公式时的极速纯文本
"""

import os
import json
import asyncio
import logging
from typing import Optional, List, AsyncGenerator
from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain, Image, Node, Nodes
from astrbot.api.all import Context, AstrBotConfig

from .url_parser import ZhihuUrlParser
from .fetcher import ZhihuFetcher
from .summarizer import ZhihuSummarizer
from .renderer import ZhihuRenderer

logger = logging.getLogger(__name__)

@register(
    name="astrbot_plugin_zhihu_article",
    author="VesperaZephyr",
    desc="自动读取知乎回答与专栏文章全部内容，保留数学公式，合并转发知乎原版高清长图与 Markdown 深度总结导读",
    version="1.1.1",
    repo="https://github.com/VesperaZephyr/astrbot_plugin_zhihu_article"
)
class ZhihuArticlePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._handling_ids = set()
        logger.info("[ZhihuArticlePlugin] 知乎回答与专栏文章全文精读与公式总结插件 v1.1.1 已加载。")

    def _get_cookie(self) -> str:
        """获取知乎 Cookie（若未配置，尝试从其他知乎插件自动继承）"""
        cookie = str(self.config.get("zhihu_cookie", "")).strip()
        if cookie:
            return cookie

        # 尝试继承已有的 zhihuSummary 配置
        candidate_paths = [
            "/AstrBot/data/config/astrbot_plugin_zhihuSummary_config.json",
            "/AstrBot/data/config/astrbot_plugin_multi_parser_config.json",
        ]
        for cp in candidate_paths:
            if os.path.exists(cp):
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    c = data.get("zhihu_cookie") or data.get("cookies", {}).get("zhihu_cookies", "")
                    if c and str(c).strip():
                        return str(c).strip()
                except Exception:
                    pass
        return ""

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        if not group_id:
            return True

        access_mode = self.config.get("access_mode", "blacklist")
        raw_list = str(self.config.get("group_list", "")).strip()
        groups = [g.strip() for g in raw_list.split(",") if g.strip()]

        if access_mode == "whitelist":
            return str(group_id) in groups
        else:
            return str(group_id) not in groups

    @filter.command("zh", alias={"知乎", "zhihu", "读知乎", "知乎总结"})
    async def cmd_zhihu(self, event: AstrMessageEvent, url: str = ""):
        """读取知乎回答或文章全部内容，保留数学公式并以合并转发形式输出"""
        if not self._is_group_allowed(event):
            return

        text = url or event.message_str
        parsed = ZhihuUrlParser.detect_zhihu_url(text)
        if not parsed:
            yield event.plain_result("请在命令后附带知乎回答或文章链接，例如：\n/zh https://www.zhihu.com/question/123/answer/456")
            return

        content_type, target_id, normalized_url = parsed
        async for res in self._process_zhihu(event, content_type, target_id, normalized_url):
            yield res

    @filter.llm_tool(name="read_zhihu_article")
    async def tool_read_zhihu(self, event: AstrMessageEvent, url: str) -> AsyncGenerator[MessageEventResult, None]:
        """读取知乎回答或专栏文章全部内容，保留数学公式，生成知乎原版高清长图和AI总结导读，并以合并转发消息发送。当用户发送知乎链接(zhihu.com)或要求读取、解析、总结知乎内容时调用此工具。

        Args:
            url(string): 必填。知乎回答或专栏文章链接，例如 https://www.zhihu.com/question/123/answer/456 或 https://zhuanlan.zhihu.com/p/123
        """
        parsed = ZhihuUrlParser.detect_zhihu_url(url or event.message_str)
        if not parsed:
            yield event.plain_result(f"抱歉，未能在提供的参数中识别到合法的知乎链接：{url}")
            return

        content_type, target_id, normalized_url = parsed
        async for res in self._process_zhihu(event, content_type, target_id, normalized_url):
            yield res

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_detect_zhihu(self, event: AstrMessageEvent):
        """自动检测群聊/私聊中的知乎链接"""
        raw_text = event.message_str or ""
        if raw_text.strip().startswith("/"):
            return

        if not self.config.get("enable_auto_detect", True):
            return

        if not self._is_group_allowed(event):
            return

        parsed = ZhihuUrlParser.detect_zhihu_url(raw_text)
        if not parsed:
            return

        content_type, target_id, normalized_url = parsed
        dedup_key = f"{content_type}_{target_id}"
        if dedup_key in self._handling_ids:
            return

        self._handling_ids.add(dedup_key)
        try:
            logger.info(f"[ZhihuArticlePlugin] 自动检测到知乎链接: {normalized_url}")
            async for res in self._process_zhihu(event, content_type, target_id, normalized_url):
                yield res
        finally:
            self._handling_ids.discard(dedup_key)

    @staticmethod
    def _split_text_to_chunks(text: str, max_chars: int = 1800) -> List[str]:
        paragraphs = text.split("\n\n")
        chunks = []
        current = []
        current_len = 0

        for p in paragraphs:
            p_clean = p.strip()
            if not p_clean:
                continue
            if current_len + len(p_clean) > max_chars and current:
                chunks.append("\n\n".join(current))
                current = [p_clean]
                current_len = len(p_clean)
            else:
                current.append(p_clean)
                current_len += len(p_clean)

        if current:
            chunks.append("\n\n".join(current))
        return chunks or [text]

    async def _process_zhihu(self, event: AstrMessageEvent, content_type: str, target_id: str, url: str):
        """核心业务逻辑：知乎数据拉取 -> 公式探测 -> AI深度总结 -> 原版极清截图 -> 合并转发"""
        yield event.plain_result("📖 正在读取知乎内容并解析数学公式，请稍候...")

        try:
            cookie_str = self._get_cookie()
            fast_text_mode = self.config.get("fast_text_when_no_formula", True)
            max_slice_h = int(self.config.get("max_slice_height", 12000))
            content_width = int(self.config.get("content_width", 760))
            content_padding = int(self.config.get("content_padding", 26))
            wait_timeout = float(self.config.get("content_wait_timeout", 25))

            # 1. 优先使用知乎 API 快速抓取（回答 / 问题通道，无需浏览器）
            content_data = await ZhihuFetcher.fetch(content_type, target_id, cookie_str)
            article_img_paths: List[str] = []

            # 2. API 不可用时（专栏文章接口需签名会返回 403），回退到 Playwright 浏览器渲染通道
            #    该通道在同一次导航中完成「正文提取 + 原版极清截图」，速度最优
            if not content_data:
                logger.info(f"[ZhihuArticlePlugin] API 抓取失败，切换至浏览器渲染通道: {url}")
                content_data, article_img_paths = await ZhihuRenderer.extract_and_screenshot_via_browser(
                    url=url,
                    cookie_str=cookie_str,
                    max_slice_height=max_slice_h,
                    need_screenshot=True,
                    content_width=content_width,
                    content_padding=content_padding,
                    wait_timeout=wait_timeout,
                )

            if not content_data:
                yield event.plain_result("❌ 抓取知乎内容失败，请检查链接是否有效或 Cookie 是否需要更新。")
                return

            title = content_data.get("title", "知乎内容")
            author = content_data.get("author", "知友")
            voteup = content_data.get("voteup_count", 0)
            formula_count = content_data.get("formula_count", 0)
            image_count = content_data.get("image_count", 0)
            full_text = content_data.get("full_text", "")
            source_url = content_data.get("source_url", url)

            # 2. 生成 AI 深度总结
            summary_style = self.config.get("summary_style", "academic_math")
            md_summary_text = await ZhihuSummarizer.generate_markdown_summary(
                context=self.context,
                content_data=content_data,
                style=summary_style,
            )

            bot_uin = str(event.get_self_id() or "10000")
            bot_name = "知乎深度解析"
            forward_nodes = []

            # 节点 1：引导语与来源
            first_msg_text = f"以下是对知乎 {source_url} 内容的解析和总结："
            forward_nodes.append(
                Node(content=[Plain(first_msg_text)], name=bot_name, uin=bot_uin)
            )

            vote_info = f" · {voteup} 赞同" if voteup and voteup > 0 else ""
            has_media = (formula_count > 0 or image_count > 0)

            if not has_media and fast_text_mode:
                logger.info(f"[ZhihuArticlePlugin] 内容无插图且无公式，走【纯文本极速合并转发】")

                # 节点 2：纯文本总结
                summary_node_text = (
                    f"📑【AI 深度导读】《{title}》\n"
                    f"👤 答主/作者：{author}{vote_info}\n"
                    f"{'-' * 35}\n"
                    f"{md_summary_text}"
                )
                forward_nodes.append(
                    Node(content=[Plain(summary_node_text)], name=f"AI 深度总结 · {author}", uin=bot_uin)
                )

                # 后续节点：纯文本分段
                chunks = self._split_text_to_chunks(full_text, max_chars=1800)
                total_chunks = len(chunks)
                for idx, chunk in enumerate(chunks, 1):
                    node_label = f"知乎原文 ({idx}/{total_chunks})" if total_chunks > 1 else "知乎原文"
                    forward_nodes.append(
                        Node(content=[Plain(chunk)], name=node_label, uin=bot_uin)
                    )

            else:
                logger.info(f"[ZhihuArticlePlugin] 检测到公式数={formula_count}, 插图数={image_count}，走【原版高保真截图模式】")

                # 节点 2：AI 总结
                if formula_count > 0:
                    summary_card_path = await ZhihuRenderer.render_markdown_summary_card(
                        summary_text=md_summary_text,
                        title=title,
                        author=author,
                        voteup=voteup,
                        formula_count=formula_count
                    )
                    if summary_card_path and os.path.exists(summary_card_path):
                        forward_nodes.append(
                            Node(content=[Image.fromFileSystem(summary_card_path)], name=f"AI 深度总结导读 · {author}", uin=bot_uin)
                        )
                else:
                    summary_node_text = (
                        f"📑【AI 深度导读】《{title}》\n"
                        f"👤 答主/作者：{author}{vote_info}\n"
                        f"{'-' * 35}\n"
                        f"{md_summary_text}"
                    )
                    forward_nodes.append(
                        Node(content=[Plain(summary_node_text)], name=f"AI 深度总结 · {author}", uin=bot_uin)
                    )

                # 后续节点：知乎原版极清长图（1:1 官方排版，公式零浮动，插图 100% 真实呈现）
                # 若走浏览器通道已在同一会话中完成截图，则直接复用，避免二次导航
                if not article_img_paths:
                    article_img_paths = await ZhihuRenderer.render_direct_zhihu_page(
                        url=source_url,
                        cookie_str=cookie_str,
                        max_slice_height=max_slice_h,
                        content_width=content_width,
                        content_padding=content_padding,
                        wait_timeout=wait_timeout,
                    )
                total_parts = len(article_img_paths)
                added_img = 0
                for idx, p in enumerate(article_img_paths, 1):
                    if os.path.exists(p):
                        part_label = f"知乎原版解析 ({idx}/{total_parts})" if total_parts > 1 else "知乎原版解析"
                        forward_nodes.append(
                            Node(content=[Image.fromFileSystem(p)], name=part_label, uin=bot_uin)
                        )
                        added_img += 1

                # 安全网：长图截图未产出任何图片时（如知乎对回答直达页返回错误页），
                # 回退为纯文本分段，确保用户仍能拿到完整正文而不是只剩一段总结
                if added_img == 0 and full_text:
                    logger.warning("[ZhihuArticlePlugin] 长图截图未产出图片，回退为纯文本分段转发")
                    chunks = self._split_text_to_chunks(full_text, max_chars=1800)
                    total_chunks = len(chunks)
                    for idx, chunk in enumerate(chunks, 1):
                        node_label = f"知乎原文 ({idx}/{total_chunks})" if total_chunks > 1 else "知乎原文"
                        forward_nodes.append(
                            Node(content=[Plain(chunk)], name=node_label, uin=bot_uin)
                        )

            # 发送合并转发
            if forward_nodes:
                yield event.chain_result([Nodes(forward_nodes)])
            else:
                yield event.plain_result("❌ 未能生成有效的合并转发节点内容。")

        except Exception as e:
            logger.error(f"[ZhihuArticlePlugin] 处理知乎内容异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 解析知乎内容时发生异常: {str(e)}")
