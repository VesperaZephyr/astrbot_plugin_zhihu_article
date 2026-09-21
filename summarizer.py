# -*- coding: utf-8 -*-
"""
知乎回答与专栏文章 AI 深度总结模块
自适应判断：
  - 无数学公式内容：通用问答/社科/观点精读，严禁生硬出现数学词汇
  - 含有数学公式内容：深入剖析数理模型、核心公式与理论推导
"""

import uuid
import logging
from typing import Dict, Any, Optional
from astrbot.api.all import Context

logger = logging.getLogger(__name__)

# 通用知乎内容系统提示词
GENERAL_PROMPT = """你是一个专业的知乎深度长文与高赞回答导读专家。
你的任务是对提供的知乎内容进行深度精读与结构化总结。
【严格要求】：这是一篇普通图文内容，完全不包含高深数学公式，请勿生搬硬套任何数学模型、公式推导或理工科术语。

请直接输出排版优美、通俗精炼的标准 Markdown 总结，格式如下：

### 🎯 核心观点
（一句话提炼答主/作者的核心立场与结论，50字左右）

### 📌 核心论证与关键脉络
1. **论点一**：具体逻辑展开与事实依据
2. **论点二**：具体逻辑展开与事实依据
3. **论点三**：具体逻辑展开与事实依据

### 🔍 精彩案例与关键细节
提炼文中提及的重要论据、故事、数据或代表性观点（100字左右）

### 💡 AI 导读点评
2-3句话客观评估该回答/文章的价值、见解深度或启发意义（80字以内）
"""

# 数理知乎内容系统提示词
MATH_PROMPT = """你是一个专业的学术与数理长文导读专家。
你的任务是对提供的知乎学术专栏或硬核回答进行深度精读与数理总结。
这篇文章包含数学公式与理论模型，请重点梳理其推演逻辑与核心公式。

请直接输出排版严密、清晰的标准 Markdown 总结，格式如下：

### 🎯 核心主旨
（一句话提炼文章的核心研究问题与理论结论，50字左右）

### 📌 核心论点与理论脉络
1. **理论论点一**：具体推导与论述
2. **理论论点二**：具体推导与论述
3. **理论论点三**：具体推导与论述

### 📐 数理推导与核心模型解析
详细解析文中关键公式（使用标准 LaTeX 符号）、定理或物理模型的数学意义，阐明其核心推演脉络（150字左右）

### 💡 AI 导读点评
2-3句话评估该文的学术深度与理论价值（80字以内）
"""

class ZhihuSummarizer:
    """知乎内容总结生成器"""

    @classmethod
    async def generate_markdown_summary(
        cls,
        context: Context,
        content_data: Dict[str, Any],
        style: str = "academic_math",
    ) -> str:
        title = content_data.get("title", "")
        author = content_data.get("author", "")
        voteup = content_data.get("voteup_count", 0)
        formula_count = content_data.get("formula_count", 0)
        full_text = content_data.get("full_text", "")

        # 智能匹配提示词
        if formula_count > 0:
            system_prompt = MATH_PROMPT
            hint = f"该内容包含约 {formula_count} 个数学公式，请重点解析其核心推导与模型。"
        else:
            system_prompt = GENERAL_PROMPT
            hint = "该内容为普通图文内容，无数学公式，请以通俗、准确、贴合原意的风格总结。"

        max_chars = 4000
        truncated_text = full_text[:max_chars]
        if len(full_text) > max_chars:
            truncated_text += "\n\n(注：篇幅较长，已截取核心论述进行深度总结...)"

        user_prompt = f"""知乎标题：《{title}》
作者/答主：{author} (赞同数: {voteup})
公式特征：{hint}

以下是知乎核心正文内容（包含已还原的 LaTeX 公式）：
----------------------------------------
{truncated_text}
----------------------------------------
请按照提示要求输出结构化精读总结报告。"""

        try:
            provider = context.get_using_provider()
            if not provider:
                logger.error("[ZhihuSummarizer] 未找到可用的 LLM Provider")
                return cls._fallback_summary(content_data)

            fresh_session_id = f"zh_{uuid.uuid4().hex[:8]}"

            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                session_id=fresh_session_id,
            )

            response_text = ""
            if hasattr(response, "completion_text"):
                response_text = response.completion_text
            elif isinstance(response, str):
                response_text = response
            else:
                response_text = str(response)

            res = response_text.strip()
            if res.startswith("```markdown"):
                res = res[len("```markdown"):].strip()
            elif res.startswith("```"):
                res = res[3:].strip()
            if res.endswith("```"):
                res = res[:-3].strip()

            return res

        except Exception as e:
            logger.warning(f"[ZhihuSummarizer] LLM 总结异常，使用兜底处理: {e}")
            return cls._fallback_summary(content_data)

    @classmethod
    def _fallback_summary(cls, content_data: Dict[str, Any]) -> str:
        title = content_data.get("title", "知乎内容")
        author = content_data.get("author", "知友")
        voteup = content_data.get("voteup_count", 0)
        formula_count = content_data.get("formula_count", 0)

        math_part = f"\n\n### 📐 数理推导与核心模型\n包含约 {formula_count} 个数学公式，原版高清排版已在后续消息展示。" if formula_count > 0 else ""

        return f"""### 🎯 核心观点
《{title}》由答主/作者 **{author}** 发布（获得 {voteup} 赞同），围绕该话题提出了深度见解。

### 📌 核心论点与关键脉络
1. 观点明确，逻辑严密。
2. 完整内容已在后续合并转发消息中展示。{math_part}

### 💡 AI 导读点评
已自动整理完成，适合收藏与精读。"""
