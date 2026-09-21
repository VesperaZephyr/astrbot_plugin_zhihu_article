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

# 知乎长图可达上亿像素，解除 Pillow 的像素上限保护
PILImage.MAX_IMAGE_PIXELS = None

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

    @staticmethod
    async def _build_context(browser: Browser, cookie_str: str = ""):
        """构建带 Cookie 与反检测的浏览器上下文"""
        context = await browser.new_context(
            viewport={"width": 860, "height": 1200},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
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
        return context

    @staticmethod
    async def _clean_page(page) -> None:
        """净化知乎页面：关闭登录弹窗、移除广告与无关模块（带重试以应对客户端跳转）"""
        for attempt in range(3):
            try:
                await page.evaluate("""() => {
                    document.querySelectorAll(".Modal-wrapper, .sign_modal, div.Modal").forEach(el => el.remove());
                    const junkSelectors = [
                        "header.AppHeader", ".Sticky", ".Question-sideColumn",
                        ".Reward", ".ContentItem-actions", ".Comments-container",
                        ".CornerButtons", ".Ad-placeholder", ".OpenInAppButton",
                        ".MobileAppHeader", ".ViewAll-Question", ".Recommendations-Main",
                        ".QuestionHeader-footer", ".Post-SideActions", ".ColumnPageHeader"
                    ];
                    junkSelectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });
                    document.querySelectorAll("button.ContentItem-more").forEach(btn => btn.click());
                    if (document.body) {
                        document.body.style.backgroundColor = "#f6f8fa";
                    }
                }""")
                return
            except Exception as e:
                # 页面可能正在客户端跳转（如 zhi.hu 短链重定向），等待后重试
                if attempt < 2:
                    await asyncio.sleep(1.5)
                else:
                    logger.warning(f"[ZhihuRenderer] 页面净化失败(已重试): {e}")

    @staticmethod
    async def _wait_settled(page, timeout_ms: int = 8000) -> None:
        """等待页面网络与 DOM 稳定，避免客户端跳转导致执行上下文被销毁"""
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    @staticmethod
    async def _safe_evaluate(page, script: str, default: Any = None, retries: int = 3) -> Any:
        """
        健壮地执行页面 JS。
        知乎为 SPA，客户端路由跳转常在 domcontentloaded 之后发生，
        会销毁执行上下文（Execution context was destroyed），此处自动等待并重试。
        """
        last_err = None
        for attempt in range(retries):
            try:
                return await page.evaluate(script)
            except Exception as e:
                last_err = e
                msg = str(e)
                if "Execution context was destroyed" in msg or "navigating" in msg:
                    await asyncio.sleep(1.5)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    continue
                # 其他错误直接抛出
                raise
        logger.warning(f"[ZhihuRenderer] 页面 JS 执行失败(已重试 {retries} 次): {last_err}")
        return default

    @staticmethod
    async def _mark_capture_target(page) -> None:
        """
        在页面中标记"内容最丰富的正文容器"（打上 data-zh-capture 属性）。
        采用标记属性而非 JSHandle，可安全地配合重试逻辑，避免上下文销毁导致的句柄失效。
        """
        await ZhihuRenderer._safe_evaluate(page, """() => {
            document.querySelectorAll("[data-zh-capture]").forEach(el => el.removeAttribute("data-zh-capture"));
            const candidates = document.querySelectorAll(
                ".RichText, .RichContent-inner, .Post-RichText, .QuestionAnswer-content, .Post-content"
            );
            let best = null;
            let bestLen = 0;
            candidates.forEach((el) => {
                const len = el.innerHTML.length;
                if (len > bestLen) { bestLen = len; best = el; }
            });
            (best || document.body).setAttribute("data-zh-capture", "1");
            return true;
        }""", default=False)

    @staticmethod
    async def _get_capture_element(page):
        """获取被标记的截图目标元素，未标记时回退到 body"""
        return (await page.query_selector("[data-zh-capture]")) or (await page.query_selector("body"))

    @classmethod
    async def extract_and_screenshot_via_browser(
        cls,
        url: str,
        cookie_str: str = "",
        max_slice_height: int = 12000,
        need_screenshot: bool = True,
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """
        通过 Playwright 浏览器渲染提取知乎正文内容，并在同一次导航会话中完成原版极清截图。
        （这是知乎专栏文章的唯一可靠通道：其 API 无签名会 403）

        返回 (content_html 提取结果, 截图路径列表)。
        need_screenshot 为 True 时会截图；若提取后发现无插图且无公式，则自动跳过截图以加速。
        """
        from .fetcher import ZhihuFetcher  # 延迟导入避免循环依赖

        browser = await get_browser()
        context = await cls._build_context(browser, cookie_str)
        page = await context.new_page()

        try:
            logger.info(f"[ZhihuRenderer] 浏览器渲染提取知乎内容: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await cls._wait_settled(page)
            await cls._clean_page(page)
            await page.wait_for_timeout(2500)

            info = await cls._safe_evaluate(page, """() => {
                const pick = (sels) => {
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.innerText.trim()) return el;
                    }
                    return null;
                };
                const titleEl = pick([".QuestionHeader-title", ".Post-Title", "h1"]);
                const authorEl = pick([".AuthorInfo-name", ".Post-Author .AuthorInfo-name", ".AuthorInfo"]);

                // 赞同数：多层次兜底查找（回答页 / 专栏文章页 DOM 结构不同，且为客户端渲染）
                let voteText = "";
                const voteCandidates = document.querySelectorAll(
                    ".VoteButton, .VoteButton--up, button[class*=VoteButton], .Post-Actions .Button"
                );
                for (const el of voteCandidates) {
                    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
                    if (t && /赞同|推荐/.test(t)) { voteText = t; break; }
                }
                if (!voteText) {
                    const allEls = document.querySelectorAll("button, .Button, span, div");
                    for (const el of allEls) {
                        const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
                        if (/^赞同\\s*[\\d.]+\\s*万?$/.test(t)) {
                            voteText = t;
                            break;
                        }
                    }
                }

                // 选取内容最丰富的正文容器
                const candidates = document.querySelectorAll(
                    ".RichText, .RichContent-inner, .Post-RichText, .QuestionAnswer-content, .Post-content"
                );
                let best = null;
                let bestLen = 0;
                candidates.forEach((el) => {
                    const len = el.innerHTML.length;
                    if (len > bestLen) { bestLen = len; best = el; }
                });

                return {
                    title: titleEl ? titleEl.innerText.trim() : "",
                    author: authorEl ? authorEl.innerText.trim() : "知乎用户",
                    voteText: voteText,
                    contentHtml: best ? best.innerHTML : "",
                };
            }""", default={})

            content_html = (info or {}).get("contentHtml", "")
            if not content_html:
                return None, []

            content_data = ZhihuFetcher.build_from_browser_html(
                content_html=content_html,
                title=info.get("title", ""),
                author=info.get("author", ""),
                vote_text=info.get("voteText", ""),
                source_url=url,
            )

            # 无插图无公式且启用极速模式时，跳过截图（页面已经在手，无需二次导航）
            has_media = (content_data["formula_count"] > 0 or content_data["image_count"] > 0)
            if not need_screenshot or not has_media:
                logger.info(
                    f"[ZhihuRenderer] 跳过截图 (need_screenshot={need_screenshot}, "
                    f"公式={content_data['formula_count']}, 插图={content_data['image_count']})"
                )
                return content_data, []

            # 在同一会话中直接截图
            await cls._mark_capture_target(page)
            element = await cls._get_capture_element(page)

            box = await element.bounding_box()
            target_h = int(box["height"]) if box else 2000
            await page.set_viewport_size({"width": 860, "height": target_h + 100})

            uid = uuid.uuid4().hex[:8]
            full_img_path = os.path.join(OUTPUT_DIR, f"zh_direct_{uid}_full.png")
            await element.screenshot(path=full_img_path, type="png")

            return content_data, cls._slice_image(full_img_path, uid, max_slice_height)

        except Exception as e:
            logger.error(f"[ZhihuRenderer] 浏览器提取知乎内容失败: {e}", exc_info=True)
            return None, []
        finally:
            await page.close()
            await context.close()

    @staticmethod
    def _slice_image(full_img_path: str, uid: str, max_slice_height: int) -> List[str]:
        """使用 Pillow 对超长图片进行均衡无缝切片（避免出现极小的尾图）"""
        with PILImage.open(full_img_path) as im:
            img_w, img_h = im.size
            if img_h <= max_slice_height:
                return [full_img_path]

            # 均衡切片：按总高度均分为若干等份，每份不超过阈值
            part_count = (img_h + max_slice_height - 1) // max_slice_height
            part_h = (img_h + part_count - 1) // part_count

            logger.info(f"[ZhihuRenderer] 原文截图高 {img_h}px，均衡切分为 {part_count} 段 (每段约 {part_h}px)...")
            file_paths = []
            for idx in range(part_count):
                top = idx * part_h
                bottom = min(top + part_h, img_h)
                if top >= bottom:
                    break
                part_img = im.crop((0, top, img_w, bottom))
                part_path = os.path.join(OUTPUT_DIR, f"zh_direct_{uid}_part{idx + 1}.png")
                part_img.save(part_path, "PNG")
                file_paths.append(part_path)

            try:
                os.remove(full_img_path)
            except Exception:
                pass

            return file_paths

    @classmethod
    async def render_direct_zhihu_page(cls, url: str, cookie_str: str = "", max_slice_height: int = 12000) -> List[str]:
        """
        直接通过 Playwright 访问知乎原网页进行 1:1 官方原版极清截图。
        自动注入 Cookie 绕过登录拦截，公式与插图完美原汁原味呈现。
        """
        cls._ensure_output_dir()
        browser = await get_browser()
        context = await cls._build_context(browser, cookie_str)
        page = await context.new_page()

        try:
            logger.info(f"[ZhihuRenderer] 导航知乎原网页截图: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await cls._wait_settled(page)
            await cls._clean_page(page)
            await page.wait_for_timeout(2500)

            # 定位正文主体（优先选取内容最丰富的容器）
            await cls._mark_capture_target(page)
            element = await cls._get_capture_element(page)

            box = await element.bounding_box()
            target_h = int(box["height"]) if box else 2000

            await page.set_viewport_size({"width": 860, "height": target_h + 100})

            uid = uuid.uuid4().hex[:8]
            full_img_path = os.path.join(OUTPUT_DIR, f"zh_direct_{uid}_full.png")
            await element.screenshot(path=full_img_path, type="png")

            return cls._slice_image(full_img_path, uid, max_slice_height)

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

        badge_info = f'<span class="badge">包含 {formula_count} 个数学公式</span>' if formula_count > 0 else '<span class="badge">深度精读</span>'
        # 赞同数可能因页面异步渲染而缺失，仅在获取到时展示，避免显示误导性的 0
        vote_info = f" · {voteup} 赞同" if voteup and voteup > 0 else "" 

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
      <div class="article-author">答主/作者：{author}{vote_info}</div>
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
