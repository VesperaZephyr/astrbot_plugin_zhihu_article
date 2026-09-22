# -*- coding: utf-8 -*-
"""
Playwright 知乎原版极清截图与 MathJax 总结卡片渲染引擎

v1.1.3 关键修复：
  知乎**问题页**的公式是「进入视野才排版」的：目标回答不在视野内时，等再久也不会
  产生排版产物（实测 31/31 全部停留在裸 data-tex）；而问题页滚到底又会触发 SPA
  加载更多回答，无头会话随即被反爬踢成「出了一点问题」错误页 —— 且目标回答常位于
  文档末尾，「滚进视野」等价于「滚到底」，滚动这条路根本走不通。
  现改为：问题页一律不做全页滚动，直接令 MathJax 对目标容器执行一次排版
  （MathJax.Hub.Queue(["Typeset", Hub, el])），并带最多 3 次补推。
  实测：同一回答 31 个公式由 0/31 变为 31/31，页面全程存活。

v1.1.2 关键修复：
  知乎对「回答直达页」返回错误页时，兜底会导航到问题页定位该条回答；但导航后
  没有重跑「带滚动的资源就绪等待」。由于干净舞台是**克隆** DOM，未排版的公式克隆
  进去后 MathJax 不再处理，只在舞台里等待永远等不到 —— 最终截到的是原始 LaTeX 源码。
  现由 _ensure_not_error_page 回传「是否发生过导航」，导航后、克隆前重跑一次就绪等待。

v1.1.1 关键修复：
  公式就绪判定不再把 MathJax_Preview 当作「已排版」标志（它是排版前的占位元素），
  改为要求真实排版产物存在且具备实际尺寸，杜绝「公式未渲染就截图」。

核心能力：
1. 【内容就绪检测】唤醒懒加载 -> 强制注入真实图片地址 -> 主动触发公式排版 ->
   轮询等待全部图片解码完成 & 全部公式（MathJax / KaTeX）排版完成 -> 等待 DOM 稳定。
   彻底根治「公式还没渲染完就截图」「长图下半部分图片空白」。
2. 【纯净舞台裁剪】按优先级精确锁定正文容器，克隆进隔离的干净舞台中渲染，
   屏蔽顶栏 / 侧栏 / 广告 / 评论区 / 页脚等一切非正文内容，并压缩无意义留白。
   彻底根治「截图边角太多、非正文区域过多」。
"""

import os
import re
import time
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

# Chromium 单张截图的最大边限制，超出会产出空白图
_MAX_CAPTURE_PX = 16000

# 全覆盖数学公式正则：支持 $$, $, \[, \(
_MATH_TOKEN_RE = re.compile(
    r"(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|"
    r"\\\[[\s\S]*?\\\]|"
    r"\\\([^\n]*?\\\)|"
    r"(?<!\\)\$[^\n]*?(?<!\\)\$"
)

# 正文容器候选：按优先级命中即止（不再使用「最长 innerHTML」的粗暴启发式，
# 该启发式会误选 .Post-content / .RichContent-inner 等带作者卡与操作栏的外层容器）
_CAPTURE_SELECTORS = [
    ".Post-RichText",                      # 专栏文章正文
    ".QuestionAnswer-content .RichText",   # 回答正文（单回答视图）
    ".AnswerCard .RichText",               # 回答正文（卡片视图）
    ".RichContent-inner .RichText",        # 回答正文（通用）
    ".Post-content .RichText",             # 专栏正文（旧版）
    ".RichText.ztext",                     # 知乎通用富文本容器
    ".RichText",
    ".QuestionAnswer-content",
    ".RichContent-inner",
    ".Post-content",
    "article",
    "main",
]


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
                "--allow-file-access-from-files",  # 允许 file:// 页面加载本地 MathJax
            ]
        )
        return _GLOBAL_BROWSER


# ---------------------------------------------------------------------------
# 页面侧 JS 脚本
# ---------------------------------------------------------------------------

# 展开被折叠的长回答 / 长文，并解除折叠高度限制
_JS_EXPAND_CONTENT = r"""() => {
    const btnSelectors = [
        "button.ContentItem-more", ".ContentItem-rightButton",
        ".RichText-expand button", ".RichContent-expand button",
        ".Post-RichText .expandBtn", ".ContentItem-arrow",
        "button[class*=expand]", ".ExpandButton", ".ContentItem-expandButton"
    ];
    let clicked = 0;
    btnSelectors.forEach(sel => {
        document.querySelectorAll(sel).forEach(b => {
            try { b.click(); clicked++; } catch (e) {}
        });
    });
    // 解除折叠产生的高度截断与渐变遮罩
    const blocks = document.querySelectorAll(
        ".RichContent-inner, .RichText, .Post-RichText, .QuestionAnswer-content, .Post-content"
    );
    blocks.forEach(el => {
        el.style.maxHeight = "none";
        el.style.height = "auto";
        el.style.overflow = "visible";
        el.style.webkitMaskImage = "none";
        el.style.maskImage = "none";
    });
    document.querySelectorAll(
        ".ContentItem-collapseMask, .CollapseMask, .RichContent-collapsed"
    ).forEach(el => el.remove());
    return clicked;
}"""

# 强制唤醒懒加载图片：把 data-* 中的真实地址写回 src，并关闭 lazy 策略
_JS_WAKE_IMAGES = r"""() => {
    const realAttrs = [
        "data-original", "data-actualsrc", "data-src",
        "data-lazy-src", "data-original-src", "data-thumbnail"
    ];
    let woken = 0;
    document.querySelectorAll("img").forEach(im => {
        const cur = im.getAttribute("src") || "";
        if (!cur || cur.indexOf("data:") === 0) {
            for (const a of realAttrs) {
                const v = im.getAttribute(a);
                if (v && v.indexOf("data:") !== 0) {
                    im.setAttribute("src", v);
                    woken++;
                    break;
                }
            }
        }
        im.removeAttribute("loading");
        im.setAttribute("decoding", "sync");
    });
    return woken;
}"""

# 渐进式滚动整页，触发懒加载与 MathJax 的分批排版，最后回到顶部
_JS_SCROLL_THROUGH = r"""async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
    let prevH = -1;
    for (let i = 0; i < 100; i++) {
        const h = Math.max(
            document.body ? document.body.scrollHeight : 0,
            document.documentElement ? document.documentElement.scrollHeight : 0
        );
        const y = i * step;
        if (y > h + 200) break;
        window.scrollTo(0, y);
        await sleep(110);
        prevH = h;
    }
    const finalH = Math.max(
        document.body ? document.body.scrollHeight : 0,
        document.documentElement ? document.documentElement.scrollHeight : 0
    );
    window.scrollTo(0, finalH);
    await sleep(400);
    window.scrollTo(0, 0);
    await sleep(150);
    return finalH;
}"""

# 主动请求页面上的 MathJax 对目标正文容器执行一次排版。
#
# 为什么需要它：知乎问题页的公式是「进入视野才排版」的。经实测，目标回答不在
# 视野内时，无论等多久都不会产生排版产物（31/31 公式全部停留在裸 data-tex）。
# 而把问题页滚到底又会触发 SPA 加载更多回答，无头会话随即被反爬踢成
# 「出了一点问题」错误页，整页内容丢失 —— 且目标回答常位于文档末尾，任何
# 「滚进视野」的操作都等价于滚到底。因此这里完全不滚动，直接令 MathJax 排版。
_JS_FORCE_TYPESET = r"""async () => {
    const el = document.getElementById("zh-capture-stage")
        || document.querySelector("[data-zh-capture]")
        || document.body;
    if (!el) return { ok: false, reason: "no-target" };
    const M = window.MathJax;
    if (!M) return { ok: false, reason: "no-mathjax" };
    try {
        if (M.Hub && typeof M.Hub.Queue === "function") {
            M.Hub.Queue(["Typeset", M.Hub, el]);
            return { ok: true, engine: "mathjax2-hub" };
        }
        if (typeof M.typesetPromise === "function") {
            await M.typesetPromise([el]);
            return { ok: true, engine: "mathjax3-typesetPromise" };
        }
        if (typeof M.typeset === "function") {
            const p = M.typeset(el);
            if (p && typeof p.then === "function") { try { await p; } catch (e) {} }
            return { ok: true, engine: "mathjax3-typeset" };
        }
    } catch (e) {
        return { ok: false, reason: String(e) };
    }
    return { ok: false, reason: "no-typeset-entry" };
}"""

# 判定当前页面类型：问题页本身（/question/<id>）还是回答直达页（.../answer/<aid>）
# 问题页禁止全页滚动（滚到底触发反爬），回答页与专栏页则依赖滚动唤醒懒加载。
_JS_PAGE_KIND = r"""() => {
    const p = location.pathname || "";
    const m = p.match(/^\/question\/(\d+)\/?$/);
    return { path: p,
             isQuestionPage: !!m,
             qid: m ? m[1] : null,
             isAnswerPage: /\/answer\/\d+/.test(p) };
}"""

# 统计待完成资源：未解码图片 / 未排版公式 / MathJax 队列积压 / DOM 长度指纹
_JS_ASSET_STAT = r"""() => {
    const root = document.getElementById("zh-capture-stage")
        || document.querySelector("[data-zh-capture]")
        || document.body;
    if (!root) return null;

    let imgTotal = 0, pendingImg = 0;
    root.querySelectorAll("img").forEach(im => {
        const src = im.getAttribute("src") || "";
        if (!src || src.indexOf("data:") === 0) return;
        imgTotal++;
        if (!(im.complete && im.naturalWidth > 0)) pendingImg++;
    });

    let mathTotal = 0, pendingMath = 0;
    root.querySelectorAll(".ztext-math, [data-tex]").forEach(m => {
        // 图片型公式（zhihu equation 服务出图）由上面的图片等待逻辑负责
        if (m.tagName === "IMG") return;
        mathTotal++;
        // 关键：绝不能把 .MathJax_Preview 当作「已排版」标志。
        // 它是 MathJax 排版【之前】插入的占位元素，排版完成后仍会残留在 DOM 中（通常为空），
        // 一旦把它算作渲染完成，等待循环就会在公式尚未真正排版时提前放行，最终截到空白公式。
        // 这里改为要求「真实排版产物存在且具备实际尺寸」才算完成。
        const prod = m.querySelector(
            ".MathJax_SVG, .MathJax_SVG_Display, .MathJax_CHTML, .MathJax_MathML, "
            + "mjx-container, .katex"
        ) || m.querySelector("svg");
        let done = false;
        if (prod) {
            const r = prod.getBoundingClientRect();
            done = r.width > 1 && r.height > 1;
        }
        if (!done) pendingMath++;
    });

    let mjPending = 0;
    try {
        const q = window.MathJax && window.MathJax.Hub && window.MathJax.Hub.queue;
        if (q && q.pending > 0) mjPending = q.pending;
    } catch (e) {}

    return {
        imgTotal: imgTotal,
        pendingImg: pendingImg,
        mathTotal: mathTotal,
        pendingMath: pendingMath,
        mjPending: mjPending,
        htmlLen: (root.innerHTML || "").length,
    };
}"""

_JS_DOM_FINGERPRINT = r"""() => {
    const root = document.getElementById("zh-capture-stage")
        || document.querySelector("[data-zh-capture]")
        || document.body;
    return root ? (root.innerHTML || "").length : -1;
}"""

_JS_FONTS_READY = r"""() => {
    if (document.fonts && document.fonts.ready) {
        return document.fonts.ready.then(() => true);
    }
    return Promise.resolve(true);
}"""

# 检测知乎错误页（回答直达页常被知乎对无头浏览器返回「出了一点问题」）
_JS_IS_ERROR_PAGE = r"""() => {
    const t = (document.body ? document.body.innerText : "") || "";
    const markers = [
        "出了一点问题", "我们正在解决", "去往首页", "页面不存在",
        "该内容已被删除", "内容已删除", "你访问的页面出错了", "页面走丢了"
    ];
    let hit = "";
    for (const m of markers) { if (t.indexOf(m) >= 0) { hit = m; break; } }
    const errEl = document.querySelector(".ErrorPage, .ZhihuErrorPage, .error-page");
    return { isError: !!hit || !!errEl, marker: hit, bodyLen: t.trim().length };
}"""

# 在问题页中定位指定回答，并锁定为截图目标
_JS_FOCUS_ANSWER = r"""(aid) => {
    const anchors = document.querySelectorAll('a[href*="/answer/' + aid + '"]');
    let card = null;
    for (const a of anchors) {
        const c = a.closest(".ContentItem") || a.closest(".AnswerItem")
            || a.closest(".Card") || a.closest(".List-item") || a.parentElement;
        if (c && (c.innerText || "").trim().length > 80) { card = c; break; }
    }
    if (!card) return { ok: false, reason: "answer-not-found" };

    const body = card.querySelector(".RichText, .RichContent-inner, .Post-RichText");
    const target = body || card;
    document.querySelectorAll("[data-zh-capture], [data-zh-capture-locked]").forEach(el => {
        el.removeAttribute("data-zh-capture");
        el.removeAttribute("data-zh-capture-locked");
    });
    target.setAttribute("data-zh-capture", "1");
    target.setAttribute("data-zh-capture-locked", "1");
    return { ok: true, textLen: (target.innerText || "").trim().length };
}"""

# 按优先级标记正文容器（打 data-zh-capture 属性，避免 JSHandle 在 SPA 跳转后失效）
_JS_MARK_TARGET = r"""(selectors) => {
    // 已被回答页兜底逻辑锁定的目标优先保留
    const locked = document.querySelector("[data-zh-capture-locked]");
    if (locked) {
        locked.setAttribute("data-zh-capture", "1");
        return { ok: true, selector: "locked-answer", textLen: (locked.innerText || "").trim().length };
    }
    document.querySelectorAll("[data-zh-capture]").forEach(el => el.removeAttribute("data-zh-capture"));
    const MIN_LEN = 120;
    let best = null, bestSel = "";
    for (const s of selectors) {
        const el = document.querySelector(s);
        if (el && (el.innerText || "").trim().length >= MIN_LEN) {
            best = el;
            bestSel = s;
            break;
        }
    }
    if (!best) {
        // 兜底：退化为"内容最长的富文本容器"，再不行才用 body
        let maxLen = 0;
        document.querySelectorAll(".RichText, .Post-RichText, .RichContent-inner").forEach(el => {
            const len = (el.innerText || "").trim().length;
            if (len > maxLen) { maxLen = len; best = el; bestSel = "fallback-longest"; }
        });
        if (!best && document.body) { best = document.body; bestSel = "body"; }
    }
    if (!best) return { ok: false, selector: "", textLen: 0 };
    best.setAttribute("data-zh-capture", "1");
    return { ok: true, selector: bestSel, textLen: (best.innerText || "").trim().length };
}"""

# 把正文克隆进一个隔离、紧凑、白底的"舞台"容器，隐藏页面其余全部元素
_JS_BUILD_STAGE = r"""(cfg) => {
    const src = document.querySelector("[data-zh-capture]");
    if (!src) return { ok: false, reason: "no-capture-target" };
    document.querySelectorAll("#zh-capture-stage").forEach(el => el.remove());

    const clone = src.cloneNode(true);

    // 1) 剔除克隆体内残留的非正文模块
    const innerJunk = [
        ".AuthorInfo", ".AuthorInfo-content", ".ContentItem-actions", ".ContentItem-time",
        ".ContentItem-meta", ".Post-SideActions", ".Post-Header", ".Post-Footer",
        ".RichContent-actions", ".Reward", ".Reward-container", ".Comments-container",
        ".CommentsV2", ".CommentBox", ".CornerButtons", ".VoteButton", ".OpenInAppButton",
        ".Ad-placeholder", ".AdblockBanner", ".ColumnPageHeader", ".ArticleItem-label",
        ".KfeCollection", ".MCNLinkCard", ".MembershipCard", ".PcWordCard",
        ".RichText-expand", ".ContentItem-more", ".ContentItem-rightButton",
        ".Banner", ".Topstory", ".GlobalSideBar", ".BackToTop", ".Footer", ".AppFooter",
        "button", "script", "style", "noscript", "iframe", "video", "audio"
    ];
    innerJunk.forEach(sel => {
        try { clone.querySelectorAll(sel).forEach(el => el.remove()); } catch (e) {}
    });

    // 2) 克隆体内的图片同样强制真实地址，并避免超宽撑破舞台
    const realAttrs = [
        "data-original", "data-actualsrc", "data-src",
        "data-lazy-src", "data-original-src", "data-thumbnail"
    ];
    clone.querySelectorAll("img").forEach(im => {
        const cur = im.getAttribute("src") || "";
        if (!cur || cur.indexOf("data:") === 0) {
            for (const a of realAttrs) {
                const v = im.getAttribute(a);
                if (v && v.indexOf("data:") !== 0) { im.setAttribute("src", v); break; }
            }
        }
        im.removeAttribute("loading");
        im.setAttribute("decoding", "sync");
        im.style.maxWidth = "100%";
        im.style.height = "auto";
        // 行内公式图片必须保持 inline，否则公式会掉行
        if (!im.classList.contains("ztext-math")) {
            im.style.display = "block";
            im.style.margin = "12px auto";
        }
    });

    // 3) 解除内部元素的截断与横向溢出
    //    注意：跳过 MathJax / KaTeX 产物，强行改其 max-width 会挤压长公式导致换行错乱
    clone.querySelectorAll("*").forEach(el => {
        const cls = (el.className && el.className.toString) ? el.className.toString() : "";
        if (cls.indexOf("MathJax") >= 0 || cls.indexOf("katex") >= 0 || cls.indexOf("mjx") >= 0) return;
        el.style.maxWidth = "100%";
        el.style.maxHeight = "none";
        el.style.overflow = "visible";
    });

    // 4) 隐藏滚动条，避免占位宽度导致舞台被挤出可视区
    let st = document.getElementById("zh-capture-style");
    if (!st) {
        st = document.createElement("style");
        st.id = "zh-capture-style";
        (document.head || document.documentElement).appendChild(st);
    }
    st.textContent = "::-webkit-scrollbar{width:0;height:0;display:none;}"
        + " html,body{scrollbar-width:none;}";

    const W = cfg.width;
    const PAD = cfg.padding;

    const stage = document.createElement("div");
    stage.id = "zh-capture-stage";
    stage.style.cssText = [
        "display:block", "box-sizing:border-box", "width:100%",
        "margin:0", "padding:" + PAD + "px",
        "background:#ffffff", "color:#1a1a1a",
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC',"
            + "'Hiragino Sans GB','Microsoft YaHei','WenQuanYi Zen Hei',sans-serif",
        "text-align:left", "position:relative", "z-index:2147483647", "overflow:visible"
    ].join(";");

    const inner = document.createElement("div");
    inner.id = "zh-capture-inner";
    inner.style.cssText = "display:block;box-sizing:border-box;width:100%;max-width:100%;"
        + "overflow:visible;background:#ffffff;";
    inner.appendChild(clone);
    stage.appendChild(inner);

    clone.style.margin = "0";
    clone.style.padding = "0";
    clone.style.width = "100%";
    clone.style.maxWidth = "100%";
    clone.style.background = "transparent";

    // 5) 隐藏原页面的一切内容，只留下舞台
    document.querySelectorAll("body > *").forEach(el => {
        if (el.id === "zh-capture-stage") return;
        el.style.setProperty("display", "none", "important");
    });
    document.documentElement.style.cssText =
        "margin:0;padding:0;min-width:0;background:#ffffff;";
    document.body.style.cssText = "margin:0;padding:0;min-width:0;max-width:" + W
        + "px;width:" + W + "px;background:#ffffff;overflow:visible;";

    document.body.appendChild(stage);
    return { ok: true, width: W, padding: PAD };
}"""

_JS_EXTRACT_INFO = r"""() => {
    const pick = (sels) => {
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el && el.innerText && el.innerText.trim()) return el;
        }
        return null;
    };
    const titleEl = pick([".QuestionHeader-title", ".Post-Title", ".ArticleItem-title", "h1"]);
    const authorEl = pick([".AuthorInfo-name", ".Post-Author .AuthorInfo-name", ".AuthorInfo", ".Post-Author"]);

    // 赞同数：多层次兜底查找（回答页 / 专栏文章页 DOM 结构不同，且为客户端渲染）
    let voteText = "";
    const voteCandidates = document.querySelectorAll(
        ".VoteButton, .VoteButton--up, button[class*=VoteButton], .Post-Actions .Button"
    );
    for (const el of voteCandidates) {
        const t = (el.innerText || "").replace(/\s+/g, " ").trim();
        if (t && /赞同|推荐/.test(t)) { voteText = t; break; }
    }
    if (!voteText) {
        const allEls = document.querySelectorAll("button, .Button, span, div");
        for (const el of allEls) {
            const t = (el.innerText || "").replace(/\s+/g, " ").trim();
            if (/^赞同\s*[\d.]+\s*万?$/.test(t)) { voteText = t; break; }
        }
    }

    const target = document.querySelector("[data-zh-capture]");
    return {
        title: titleEl ? titleEl.innerText.trim() : "",
        author: authorEl ? authorEl.innerText.trim() : "",
        voteText: voteText,
        contentHtml: target ? target.innerHTML : "",
    };
}"""


class ZhihuRenderer:
    """知乎内容渲染器"""

    @classmethod
    def _ensure_output_dir(cls):
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # 浏览器上下文
    # ------------------------------------------------------------------

    @staticmethod
    async def _build_context(browser: Browser, cookie_str: str = ""):
        """构建带 Cookie 与反检测的浏览器上下文"""
        context = await browser.new_context(
            viewport={"width": 1000, "height": 1200},
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

    # ------------------------------------------------------------------
    # 健壮的页面 JS 执行
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe_evaluate(page, script: str, default: Any = None, retries: int = 3, arg: Any = None) -> Any:
        """
        健壮地执行页面 JS。
        知乎为 SPA，客户端路由跳转常在 domcontentloaded 之后发生，
        会销毁执行上下文（Execution context was destroyed），此处自动等待并重试。
        """
        last_err = None
        for attempt in range(retries):
            try:
                return await (page.evaluate(script, arg) if arg is not None else page.evaluate(script))
            except Exception as e:
                last_err = e
                msg = str(e)
                if ("Execution context was destroyed" in msg
                        or "navigating" in msg
                        or "Target crashed" in msg):
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
    async def _wait_settled(page, timeout_ms: int = 8000) -> None:
        """等待页面网络与 DOM 稳定，避免客户端跳转导致执行上下文被销毁"""
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 页面净化
    # ------------------------------------------------------------------

    @staticmethod
    async def _clean_page(page) -> None:
        """净化知乎页面：关闭登录弹窗、移除广告与无关模块（带重试以应对客户端跳转）"""
        for attempt in range(3):
            try:
                await page.evaluate(r"""() => {
                    document.querySelectorAll(".Modal-wrapper, .sign_modal, div.Modal").forEach(el => el.remove());
                    const junkSelectors = [
                        "header.AppHeader", ".AppHeader", ".Sticky", ".Question-sideColumn",
                        ".GlobalSideBar", ".Reward", ".ContentItem-actions", ".Comments-container",
                        ".CommentsV2", ".CornerButtons", ".Ad-placeholder", ".OpenInAppButton",
                        ".MobileAppHeader", ".ViewAll-Question", ".Recommendations-Main",
                        ".QuestionHeader-footer", ".Post-SideActions", ".ColumnPageHeader",
                        ".Topstory", ".BackToTop", ".Footer", ".AppFooter", ".PcWordCard",
                        ".Banner", ".AdblockBanner", ".KfeCollection"
                    ];
                    junkSelectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });
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

    # ------------------------------------------------------------------
    # 内容就绪检测（核心新增）
    # ------------------------------------------------------------------

    @staticmethod
    def _question_url_from_answer(url: str) -> Tuple[str, str]:
        """从回答直达页 URL 中解析出问题页 URL 与回答 ID，失败返回 ('', '')"""
        m = re.search(r"zhihu\.com/question/(\d+)/answer/(\d+)", url or "")
        if not m:
            return "", ""
        return f"https://www.zhihu.com/question/{m.group(1)}", m.group(2)

    @classmethod
    async def _is_error_page(cls, page) -> bool:
        """判断当前是否为知乎错误页"""
        info = await cls._safe_evaluate(page, _JS_IS_ERROR_PAGE, default=None)
        if not info:
            return False
        return bool(info.get("isError"))

    @classmethod
    async def _fallback_to_question_page(cls, page, url: str) -> bool:
        """
        知乎偶发对「回答直达页」返回错误页，此时改从问题页定位该条回答。
        成功锁定返回 True。
        """
        q_url, aid = cls._question_url_from_answer(url)
        if not q_url:
            logger.warning(f"[ZhihuRenderer] 命中知乎错误页，但该 URL 无法推导问题页: {url}")
            return False

        logger.warning(f"[ZhihuRenderer] 回答直达页被知乎拦截，改从问题页定位该回答 (answer={aid})")
        try:
            await page.goto(q_url, wait_until="domcontentloaded", timeout=30000)
            await cls._wait_settled(page, timeout_ms=8000)
            await cls._clean_page(page)
            focused = await cls._safe_evaluate(page, _JS_FOCUS_ANSWER, arg=aid,
                                               default={"ok": False})
            logger.info(f"[ZhihuRenderer] 问题页定位回答结果: {focused}")
            return bool((focused or {}).get("ok"))
        except Exception as e:
            logger.warning(f"[ZhihuRenderer] 问题页兜底失败: {e}")
            return False

    @classmethod
    async def _ensure_not_error_page(cls, page, url: str) -> Tuple[bool, bool]:
        """
        确保当前页面不是知乎错误页：先重试一次原链接，仍失败则走问题页兜底。

        返回 (是否可用, 是否发生过导航)。

        「是否发生过导航」必须回传给调用方：一旦发生导航，整页内容被重置，
        此前唤醒过的懒加载图片与公式全部作废；而后续的干净舞台是**克隆** DOM，
        克隆进去的未排版公式 MathJax 不会再处理，只靠在舞台里等待永远等不到。
        因此调用方必须在导航后、克隆**之前**重跑一次带滚动的资源就绪等待。
        """
        if not await cls._is_error_page(page):
            return True, False
        logger.warning(f"[ZhihuRenderer] 检测到知乎错误页，重试一次原链接: {url}")
        navigated = False
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await cls._wait_settled(page, timeout_ms=8000)
            await cls._clean_page(page)
            navigated = True
        except Exception as e:
            logger.warning(f"[ZhihuRenderer] 重试导航失败: {e}")
        if not await cls._is_error_page(page):
            return True, navigated
        return await cls._fallback_to_question_page(page, url), True

    @classmethod
    async def _prepare_page(cls, page, url: str) -> None:
        """
        导航并完成基础准备：净化页面、展开折叠正文、唤醒懒加载图片。
        若命中知乎错误页（回答直达页常见），则改从问题页定位该条回答。
        """
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("load", timeout=8000)
        except Exception:
            pass
        await cls._wait_settled(page, timeout_ms=6000)
        await cls._clean_page(page)
        await cls._ensure_not_error_page(page, url)

        expanded = await cls._safe_evaluate(page, _JS_EXPAND_CONTENT, default=0)
        woken = await cls._safe_evaluate(page, _JS_WAKE_IMAGES, default=0)
        logger.info(f"[ZhihuRenderer] 页面准备完成: 展开按钮={expanded}, 唤醒图片={woken}")

    @classmethod
    async def _wait_dom_stable(cls, page, timeout_s: float = 6.0, interval: float = 0.5,
                               need_stable: int = 2) -> bool:
        """等待目标容器 DOM 长度连续多次不变，判定渲染已收敛"""
        deadline = time.monotonic() + timeout_s
        last = -1
        stable = 0
        while time.monotonic() < deadline:
            cur = await cls._safe_evaluate(page, _JS_DOM_FINGERPRINT, default=-1)
            if cur == last and cur >= 0:
                stable += 1
                if stable >= need_stable:
                    return True
            else:
                stable = 0
                last = cur
            await asyncio.sleep(interval)
        logger.warning(f"[ZhihuRenderer] DOM 稳定等待超时({timeout_s}s)，按当前状态继续")
        return False

    @classmethod
    async def _wait_assets_ready(cls, page, timeout_s: float = 25.0, do_scroll: bool = True) -> Dict[str, Any]:
        """
        等待正文资源真正就绪：
        1. 唤醒懒加载内容（问题页改为主动触发 MathJax 排版，见下）
        2. 等待图片解码完成 (complete && naturalWidth > 0)
        3. 等待公式排版完成 (MathJax / KaTeX 产物出现，且 MathJax 队列排空)
        4. 等待 DOM 收敛
        """
        kind = await cls._safe_evaluate(page, _JS_PAGE_KIND, default=None) or {}

        if do_scroll:
            if kind.get("isQuestionPage"):
                # 问题页禁止全页滚动：滚到底会让知乎 SPA 去加载更多回答，
                # 无头会话随即被反爬踢成「出了一点问题」错误页，整页内容丢失。
                logger.info(
                    "[ZhihuRenderer] 当前为问题页，跳过全页滚动（会触发反爬），改用主动排版公式"
                )
            else:
                await cls._safe_evaluate(page, _JS_SCROLL_THROUGH, default=None, retries=2)

        try:
            await cls._safe_evaluate(page, _JS_FONTS_READY, default=True, retries=1)
        except Exception:
            pass

        # 主动触发一次目标容器的公式排版：知乎问题页的公式「进入视野才排版」，
        # 目标回答不在视野内时纯等待永远不会完成；直接令 MathJax 排版既可绕开
        # 「滚动触发反爬」的死结，也不再依赖懒加载时机。
        pushes = 0
        push = await cls._safe_evaluate(page, _JS_FORCE_TYPESET, default=None, retries=1)
        if (push or {}).get("ok"):
            pushes += 1
            logger.info(f"[ZhihuRenderer] 已主动请求公式排版: {push.get('engine')}")
        elif push:
            logger.info(f"[ZhihuRenderer] 主动排版未执行: {push.get('reason')}")

        deadline = time.monotonic() + timeout_s
        stable = 0
        last_len = -1
        next_push = time.monotonic() + 5.0
        stat: Dict[str, Any] = {}

        while time.monotonic() < deadline:
            stat = await cls._safe_evaluate(page, _JS_ASSET_STAT, default=None) or {}
            if not stat:
                await asyncio.sleep(0.5)
                continue

            pending = (int(stat.get("pendingImg", 0))
                       + int(stat.get("pendingMath", 0))
                       + int(stat.get("mjPending", 0)))

            if pending == 0:
                if stat.get("htmlLen") == last_len:
                    stable += 1
                    if stable >= 2:
                        logger.info(
                            f"[ZhihuRenderer] 资源就绪: 图片 {stat.get('imgTotal', 0)} 张全部解码, "
                            f"公式 {stat.get('mathTotal', 0)} 个全部排版完成"
                        )
                        return stat
                else:
                    stable = 0
                    last_len = stat.get("htmlLen")
            else:
                stable = 0
                last_len = stat.get("htmlLen")

                # 公式迟迟不排版时补推（最多 3 次，间隔 5s），
                # 覆盖 MathJax 尚未加载完 / 引擎需要二次驱动的情况。
                if pushes < 3 and time.monotonic() >= next_push:
                    again = await cls._safe_evaluate(page, _JS_FORCE_TYPESET, default=None, retries=1)
                    next_push = time.monotonic() + 5.0
                    if (again or {}).get("ok"):
                        pushes += 1
                        logger.info(
                            f"[ZhihuRenderer] 再次请求公式排版(第 {pushes} 次)，"
                            f"仍待处理公式 {stat.get('pendingMath', '?')}/{stat.get('mathTotal', '?')}"
                        )

            await asyncio.sleep(0.6)

        logger.warning(
            f"[ZhihuRenderer] 资源等待超时({timeout_s}s)，仍待处理: "
            f"图片 {stat.get('pendingImg', '?')}/{stat.get('imgTotal', '?')}, "
            f"公式 {stat.get('pendingMath', '?')}/{stat.get('mathTotal', '?')}, "
            f"MathJax 队列 {stat.get('mjPending', '?')}"
        )
        return stat or {}

    @classmethod
    async def _is_question_page(cls, page) -> bool:
        """当前是否停留在问题页本身（问题页滚到底会被知乎反爬踢成错误页）"""
        info = await cls._safe_evaluate(page, _JS_PAGE_KIND, default=None)
        return bool((info or {}).get("isQuestionPage"))

    # ------------------------------------------------------------------
    # 纯净舞台截图
    # ------------------------------------------------------------------

    @classmethod
    async def _capture_clean_stage(
        cls,
        page,
        url: str = "",
        width: int = 760,
        padding: int = 26,
        wait_timeout: float = 25.0,
        max_slice_height: int = 12000,
    ) -> List[str]:
        """
        在页面上构建隔离的干净舞台并截图。
        先充分等待资源就绪，再裁剪，确保公式与插图均已完整渲染。
        """
        # 1) 先行「预定位」正文容器（此处失败不中止流程）：
        #    让紧随其后的资源就绪等待把作用域收敛到目标容器。否则 root 会退化成
        #    document.body —— 在问题页上等于要等数十条回答的全部图片与公式，
        #    极易在超时后带着尚未渲染的公式去截图。
        pre_mark = await cls._safe_evaluate(page, _JS_MARK_TARGET, arg=_CAPTURE_SELECTORS,
                                            default={"ok": False})
        logger.info(f"[ZhihuRenderer] 正文容器预定位: {pre_mark}")

        # 2) 滚动唤醒 + 等待图片解码与公式排版完成
        await cls._wait_assets_ready(page, timeout_s=wait_timeout, do_scroll=True)

        # 2.5) 错误页可能在滚动/懒加载过程中才出现，此处再确认一次
        if url:
            ok, navigated = await cls._ensure_not_error_page(page, url)
            if not ok:
                logger.error("[ZhihuRenderer] 页面确认为知乎错误页且兜底失败，放弃截图")
                return []
            if navigated:
                # 兜底/重试导航把整页重置了：懒加载的插图与公式尚未被唤醒，
                # 而舞台是克隆 DOM —— 未排版的公式克隆进去后 MathJax 不再处理，
                # 只能截到原始 LaTeX 源码。必须在这里重跑一次资源就绪等待
                # （问题页不会滚动，改为主动请求 MathJax 排版目标容器）。
                logger.info("[ZhihuRenderer] 兜底导航后重跑资源就绪等待")
                await cls._wait_assets_ready(page, timeout_s=wait_timeout, do_scroll=True)

        # 3) 精确定位正文容器（滚动后 DOM 可能已变化，重新确认一次）
        mark = await cls._safe_evaluate(page, _JS_MARK_TARGET, arg=_CAPTURE_SELECTORS,
                                        default={"ok": False})
        logger.info(f"[ZhihuRenderer] 正文容器命中: {mark}")
        if not (mark or {}).get("ok"):
            logger.error("[ZhihuRenderer] 未找到任何正文容器，放弃截图")
            return []
        if int((mark or {}).get("textLen", 0)) < 30:
            logger.error(
                f"[ZhihuRenderer] 正文容器内容过少(textLen={mark.get('textLen')})，"
                f"疑似页面被拦截或内容未渲染，放弃截图以避免产出空白长图"
            )
            return []

        # 3) 克隆进干净舞台，屏蔽一切非正文
        #    舞台宽度不得超过视口宽度，否则会被挤出可视区；
        #    视口宽度保持桌面尺寸不变，避免触发知乎的窄屏移动端样式导致排版变形
        vp = page.viewport_size or {"width": 1000, "height": 1200}
        vp_w = int(vp.get("width", 1000))
        stage_width = max(600, min(int(width), vp_w))

        built = await cls._safe_evaluate(
            page, _JS_BUILD_STAGE,
            arg={"width": stage_width, "padding": padding},
            default={"ok": False},
        )
        if not (built or {}).get("ok"):
            logger.warning(f"[ZhihuRenderer] 舞台构建失败: {built}")

        stage = (await page.query_selector("#zh-capture-stage")
                 or await page.query_selector("[data-zh-capture]")
                 or await page.query_selector("body"))

        # 4) 抬高视口：此前处于视口外的懒加载图片此刻才开始请求，需补等一轮
        box = await stage.bounding_box()
        target_h = int(box["height"]) if box else 0
        if target_h < 120:
            logger.error(f"[ZhihuRenderer] 舞台高度仅 {target_h}px，内容为空白，放弃截图")
            return []
        await page.set_viewport_size({"width": vp_w, "height": cls._clamp_height(target_h + 80)})
        await page.wait_for_timeout(400)
        await cls._wait_assets_ready(page, timeout_s=min(wait_timeout, 12.0), do_scroll=False)
        await cls._wait_dom_stable(page, timeout_s=5.0)

        # 5) 二次测高（等待期间布局可能增高）后正式截图
        box = await stage.bounding_box()
        target_h = int(box["height"]) if box else target_h
        if target_h + 80 > _MAX_CAPTURE_PX:
            logger.warning(
                f"[ZhihuRenderer] 正文高度 {target_h}px 接近 Chromium 截图上限，"
                f"将按 {_MAX_CAPTURE_PX}px 截断（可增大 max_slice_height 切片阈值缓解）"
            )
        await page.set_viewport_size({"width": vp_w, "height": cls._clamp_height(target_h + 80)})
        await page.wait_for_timeout(250)

        uid = uuid.uuid4().hex[:8]
        full_img_path = os.path.join(OUTPUT_DIR, f"zh_direct_{uid}_full.png")
        await stage.screenshot(path=full_img_path, type="png")
        logger.info(f"[ZhihuRenderer] 干净舞台截图完成: {full_img_path} ({target_h}px)")

        return cls._slice_image(full_img_path, uid, max_slice_height)

    @staticmethod
    def _clamp_height(h: int) -> int:
        return max(800, min(int(h), _MAX_CAPTURE_PX))

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

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    @classmethod
    async def extract_and_screenshot_via_browser(
        cls,
        url: str,
        cookie_str: str = "",
        max_slice_height: int = 12000,
        need_screenshot: bool = True,
        content_width: int = 760,
        content_padding: int = 26,
        wait_timeout: float = 25.0,
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """
        通过 Playwright 浏览器渲染提取知乎正文内容，并在同一次导航会话中完成纯净舞台截图。
        （这是知乎专栏文章的唯一可靠通道：其 API 无签名会 403）

        返回 (content_data 提取结果, 截图路径列表)。
        need_screenshot 为 True 时会截图；若提取后发现无插图且无公式，则自动跳过截图以加速。
        """
        from .fetcher import ZhihuFetcher  # 延迟导入避免循环依赖

        cls._ensure_output_dir()
        browser = await get_browser()
        context = await cls._build_context(browser, cookie_str)
        page = await context.new_page()

        try:
            logger.info(f"[ZhihuRenderer] 浏览器渲染提取知乎内容: {url}")
            await cls._prepare_page(page, url)
            # 文本提取只需 DOM 收敛，无需等待图片与公式，保持响应速度
            await cls._wait_dom_stable(page, timeout_s=6.0)

            await cls._safe_evaluate(page, _JS_MARK_TARGET, arg=_CAPTURE_SELECTORS,
                                     default={"ok": False})
            info = await cls._safe_evaluate(page, _JS_EXTRACT_INFO, default={}) or {}

            content_html = info.get("contentHtml", "")
            if not content_html:
                logger.warning("[ZhihuRenderer] 未提取到正文 HTML")
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

            images = await cls._capture_clean_stage(
                page=page,
                url=url,
                width=content_width,
                padding=content_padding,
                wait_timeout=wait_timeout,
                max_slice_height=max_slice_height,
            )
            return content_data, images

        except Exception as e:
            logger.error(f"[ZhihuRenderer] 浏览器提取知乎内容失败: {e}", exc_info=True)
            return None, []
        finally:
            await page.close()
            await context.close()

    @classmethod
    async def render_direct_zhihu_page(
        cls,
        url: str,
        cookie_str: str = "",
        max_slice_height: int = 12000,
        content_width: int = 760,
        content_padding: int = 26,
        wait_timeout: float = 25.0,
    ) -> List[str]:
        """
        直接通过 Playwright 访问知乎原网页进行纯净舞台极清截图。
        自动注入 Cookie 绕过登录拦截，等待公式与插图完整渲染后再截图。
        """
        cls._ensure_output_dir()
        browser = await get_browser()
        context = await cls._build_context(browser, cookie_str)
        page = await context.new_page()

        try:
            logger.info(f"[ZhihuRenderer] 导航知乎原网页截图: {url}")
            await cls._prepare_page(page, url)
            return await cls._capture_clean_stage(
                page=page,
                url=url,
                width=content_width,
                padding=content_padding,
                wait_timeout=wait_timeout,
                max_slice_height=max_slice_height,
            )
        except Exception as e:
            logger.error(f"[ZhihuRenderer] 知乎原网页截图失败: {e}", exc_info=True)
            return []
        finally:
            await page.close()
            await context.close()

    # ------------------------------------------------------------------
    # AI 总结卡片
    # ------------------------------------------------------------------

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
                    # 确认排版队列彻底排空后再截图，避免公式半渲染
                    await page.wait_for_function(
                        "() => !(window.MathJax && window.MathJax.Hub && window.MathJax.Hub.queue "
                        "&& window.MathJax.Hub.queue.pending > 0)",
                        timeout=6000,
                    )
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
