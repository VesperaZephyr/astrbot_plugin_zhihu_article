# -*- coding: utf-8 -*-
"""
Playwright 知乎原版极清截图与 MathJax 总结卡片渲染引擎
"""

import os
import re
import asyncio
import logging
import uuid
from typing import List, Dict, Any, Optional, Tuple
import mistune
from PIL import Image as PILImage
from playwright.async_api import async_playwright, Browser, Playwright

logger = logging.getLogger(__name__)

_BROWSER_LOCK = asyncio.Lock()
_GLOBAL_PLAYWRIGHT: Optional[Playwright] = None
_GLOBAL_BROWSER: Optional[Browser] = None

OUTPUT_DIR = "/AstrBot/data/temp/zhihu_article"
LOCAL_MATHJAX_PATH = "/AstrBot/data/plugins/astrbot_plugin_mathsolve/vendor/md2img/mathjax-2.7.7/MathJax.js"

# 全覆盖数学公式正则：支持 $$, $, \[, \(
_MATH_TOKEN_RE = re.compile(
    r"(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|"
    r"\\\[[\s\S]*?\\\]|"
    r"\\\([^\n]*?\\\)|"
    r"(?<!\\)\$[^\n]*?(?<!\\)\$"
)

def _protect_math_for_markdown(text: str) -> Tuple[str, List[str]]:
    pieces = []
    def repl(m):
        pieces.append(m.group(0))
        return f"<!--MATH_TOKEN_{len(pieces)-1}-->"
    text = _MATH_TOKEN_RE.sub(repl, text)
    return text, pieces

def _restore_math_tokens(html: str, pieces: List[str]) -> str:
    for i, piece in enumerate(pieces):
        normalized = piece
        if normalized.startswith(r"\[") and normalized.endswith(r"\]"):
            normalized = "$$" + normalized[2:-2] + "$$"
        elif normalized.startswith(r"\(") and normalized.endswith(r"\)"):
            normalized = "$" + normalized[2:-2] + "$"
        html = html.replace(f"<!--MATH_TOKEN_{i}-->", normalized)
    return html

async def get_browser() -> Browser:
    global _GLOBAL_PLAYWRIGHT, _GLOBAL_BROWSER
    async with _BROWSER_LOCK:
        if _GLOBAL_BROWSER is not None and _GLOBAL_BROWSER.is_connected():
            return _GLOBAL_BROWSER

        if _GLOBAL_PLAYWRIGHT is None:
            _GLOBAL_PLAYWRIGHT = await async_playwright().start()

        _GLOBAL_BROWSER = await _GLOBAL_PLAYWRIGHT.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        return _GLOBAL_BROWSER


class ZhihuRenderer:
    """知乎内容渲染器"""

    @classmethod
    def _ensure_output_dir(cls):
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    @classmethod
    async def render_direct_zhihu_page(cls, url: str, cookie_str: str = "", max_slice_height: int = 12000) -> List[str]:
        """
        直接通过 Playwright 访问知乎原网页进行 1:1 官方原版极清截图。
        自动注入 Cookie 绕过登录拦截，公式与插图完美原汁原味呈现。
        """
        cls._ensure_output_dir()
        browser = await get_browser()

        context = await browser.new_context(
            viewport={"width": 860, "height": 1200},
            device_scale_factor=2, # 2x Retina 极清
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        # 注入知乎 Cookie
        if cookie_str:
            cookies_to_add = []
            for item in cookie_str.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    cookies_to_add.append({
                        "name": k.strip(),
                        "value": v.strip(),
                        "domain": ".zhihu.com",
                        "path": "/",
                    })
            if cookies_to_add:
                try:
                    await context.add_cookies(cookies_to_add)
                except Exception as e:
                    logger.warning(f"[ZhihuRenderer] 注入 Cookie 失败: {e}")

        page = await context.new_page()

        try:
            logger.info(f"[ZhihuRenderer] 导航知乎原网页截图: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)

            # 执行知乎页面净化
            await page.evaluate("""() => {
                // 1. 关闭/移除所有可能弹出的登录弹窗
                document.querySelectorAll(".Modal-wrapper, .sign_modal, div.Modal").forEach(el => el.remove());

                // 2. 移除知乎顶栏、侧边栏、知乎盐选推荐和评论区
                const junkSelectors = [
                    "header.AppHeader", ".Sticky", ".Question-sideColumn",
                    ".Reward", ".ContentItem-actions", ".Comments-container",
                    ".CornerButtons", ".Ad-placeholder", ".AuthorInfo-badge",
                    ".OpenInAppButton", ".MobileAppHeader", ".ViewAll-Question"
                ];
                junkSelectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });

                // 3. 展开被折叠的内容
                document.querySelectorAll("button.ContentItem-more").forEach(btn => btn.click());

                // 4. 背景美化
                if (document.body) {
                    document.body.style.backgroundColor = "#f6f8fa";
                }
            }""")

            await page.wait_for_timeout(2000)

            # 定位主体卡片
            target_el = (
                await page.query_selector(".QuestionAnswer-content")
                or await page.query_selector(".Post-RichTextContainer")
                or await page.query_selector(".AnswerCard")
                or await page.query_selector(".Post-content")
                or await page.query_selector(".Question-main")
                or await page.query_selector("body")
            )

            box = await target_el.bounding_box()
            target_h = int(box["height"]) if box else 2000

            await page.set_viewport_size({"width": 860, "height": target_h + 100})

            uid = uuid.uuid4().hex[:8]
            full_img_path = os.path.join(OUTPUT_DIR, f"zh_direct_{uid}_full.png")
            await target_el.screenshot(path=full_img_path, type="png")

            # Pillow 切片处理
            with PILImage.open(full_img_path) as im:
                img_w, img_h = im.size
                if img_h <= max_slice_height:
                    return [full_img_path]

                logger.info(f"[ZhihuRenderer] 原文截图高达 {img_h}px (阈值 {max_slice_height}px)，进行智能分段切片...")
                file_paths = []
                part_idx = 1
                for y in range(0, img_h, max_slice_height):
                    bottom = min(y + max_slice_height, img_h)
                    part_img = im.crop((0, y, img_w, bottom))
                    part_path = os.path.join(OUTPUT_DIR, f"zh_direct_{uid}_part{part_idx}.png")
                    part_img.save(part_path, "PNG")
                    file_paths.append(part_path)
                    part_idx += 1

                try:
                    os.remove(full_img_path)
                except Exception:
                    pass

                return file_paths

        finally:
            await page.close()
            await context.close()

    @classmethod
    async def render_markdown_summary_card(
        cls,
        summary_text: str,
        title: str,
        author: str,
        voteup: int = 0,
        formula_count: int = 0
    ) -> str:
        """
        基于 Markdown + 本地离线 MathJax 渲染 AI 深度总结卡片
        彻底杜绝公式乱码与斜体误伤
        """
        cls._ensure_output_dir()
        browser = await get_browser()

        # 1. 保护全格式公式
        protected_md, pieces = _protect_math_for_markdown(summary_text)

        # 2. 解析 Markdown
        parser = mistune.create_markdown(escape=False, plugins=["table", "url", "strikethrough"])
        html_body = parser(protected_md)

        # 3. 恢复公式并标准化
        html_body = _restore_math_tokens(html_body, pieces)

        badge_info = f'<span class="badge">包含 {formula_count} 个数学公式</span>' if formula_count > 0 else f'<span class="badge">知乎赞同 {voteup}</span>'

        if os.path.exists(LOCAL_MATHJAX_PATH):
            mathjax_script_tag = f'<script type="text/javascript" src="file://{LOCAL_MATHJAX_PATH}?config=TeX-MML-AM_CHTML"></script>'
        else:
            mathjax_script_tag = '<script type="text/javascript" src="https://cdn.bootcdn.net/ajax/libs/mathjax/2.7.7/MathJax.js?config=TeX-MML-AM_CHTML"></script>'

        full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<script type="text/x-mathjax-config">
  MathJax.Hub.Config({{
    tex2jax: {{
      inlineMath: [["$","$"], ["\\\\(","\\\\)"]],
      displayMath: [["$$","$$"], ["\\\\[","\\\\]"]],
      processEscapes: true
    }},
    "HTML-CSS": {{
      scale: 96,
      linebreaks: {{ automatic: true, width: "container" }}
    }}
  }});
</script>
{mathjax_script_tag}
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "WenQuanYi Zen Hei", sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    padding: 30px;
    display: flex;
    justify-content: center;
  }}
  .card {{
    width: 720px;
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 20px;
    padding: 38px 42px;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.4);
  }}
  .card-top {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #334155;
    padding-bottom: 16px;
    margin-bottom: 22px;
  }}
  .topic-tag {{
    font-size: 13px;
    font-weight: 700;
    color: #0084ff;
    background: rgba(0, 132, 255, 0.12);
    padding: 4px 12px;
    border-radius: 8px;
  }}
  .badge {{
    font-size: 12px;
    color: #a78bfa;
    background: rgba(167, 139, 250, 0.12);
    padding: 3px 10px;
    border-radius: 6px;
    font-weight: 600;
  }}
  .article-title {{
    font-size: 22px;
    font-weight: 700;
    color: #f8fafc;
    line-height: 1.45;
    margin-bottom: 8px;
  }}
  .article-author {{
    font-size: 14px;
    color: #94a3b8;
    margin-bottom: 24px;
  }}
  .md-body {{
    font-size: 15px;
    line-height: 1.8;
    color: #cbd5e1;
  }}
  .md-body h1, .md-body h2, .md-body h3 {{
    color: #38bdf8;
    font-weight: 700;
    margin: 20px 0 10px;
    font-size: 16.5px;
    border-left: 3.5px solid #0084ff;
    padding-left: 10px;
  }}
  .md-body h3:first-child {{ margin-top: 0; }}
  .md-body p {{ margin: 8px 0; word-break: break-word; }}
  .md-body ul, .md-body ol {{ padding-left: 20px; margin: 8px 0; }}
  .md-body li {{ margin: 6px 0; }}
  .md-body strong {{ color: #f1f5f9; }}
  .card-footer {{
    margin-top: 30px;
    padding-top: 16px;
    border-top: 1px solid #334155;
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: #64748b;
  }}
</style>
</head>
<body>
  <div class="card" id="summary-card">
    <div class="card-top">
      <span class="topic-tag">✦ 知乎 AI 深度精读导读</span>
      {badge_info}
    </div>
    <div class="article-meta">
      <h1 class="article-title">{title}</h1>
      <div class="article-author">答主/作者：{author} · {voteup} 赞同</div>
    </div>
    <div class="md-body">
      {html_body}
    </div>
    <div class="card-footer">
      <span>AstrBot Zhihu DeepSummary</span>
      <span>正文高清原文长图已在后续消息合并发送 ⇣</span>
    </div>
  </div>
</body>
</html>"""

        uid = uuid.uuid4().hex[:8]
        tmp_html_path = os.path.join(OUTPUT_DIR, f"zh_summary_page_{uid}.html")
        with open(tmp_html_path, "w", encoding="utf-8") as f:
            f.write(full_html)

        context = await browser.new_context(
            viewport={"width": 800, "height": 1000},
            device_scale_factor=2,
        )
        page = await context.new_page()

        try:
            await page.goto(f"file://{tmp_html_path}", wait_until="load")

            if pieces:
                try:
                    await page.wait_for_function("() => !!(window.MathJax && window.MathJax.Hub)", timeout=4000)
                    await page.evaluate("""() => new Promise((resolve) => {
                        MathJax.Hub.Queue(["Typeset", MathJax.Hub], () => resolve(true));
                    })""")
                except Exception as e:
                    logger.warning(f"[ZhihuRenderer] MathJax Typeset wait timed out: {e}")

            card_el = await page.query_selector("#summary-card") or await page.query_selector("body")
            box = await card_el.bounding_box()
            target_h = int(box["height"]) if box else 1000
            await page.set_viewport_size({"width": 800, "height": target_h + 80})

            img_path = os.path.join(OUTPUT_DIR, f"zh_summary_{uid}.png")
            await card_el.screenshot(path=img_path, type="png")

            try:
                os.remove(tmp_html_path)
            except Exception:
                pass

            return img_path

        finally:
            await page.close()
            await context.close()
