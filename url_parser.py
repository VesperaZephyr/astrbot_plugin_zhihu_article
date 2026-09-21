# -*- coding: utf-8 -*-
"""
知乎 URL 解析工具
检测文本中的知乎链接，提取内容类型 (answer/article/question) 与目标 ID
"""

import re
from typing import Optional, Tuple, List

_ANSWER_PATTERN = re.compile(r"https?://(?:www\.)?zhihu\.com/question/(\d+)/answer/(\d+)")
_ARTICLE_PATTERN = re.compile(r"https?://zhuanlan\.zhihu\.com/p/(\d+)")
_QUESTION_PATTERN = re.compile(r"https?://(?:www\.)?zhihu\.com/question/(\d+)(?:[/?#\s]|$)", re.IGNORECASE)
_SHORT_LINK_PATTERN = re.compile(r"https?://(?:zhi\.hu|link\.zhihu\.com/\?target=)\S+")
_URL_EXTRACT = re.compile(r'https?://[^\s<>"\']+')

class ZhihuUrlParser:
    @staticmethod
    def detect_zhihu_url(text: str) -> Optional[Tuple[str, str, str]]:
        """
        检测文本中的知乎链接，返回 (content_type, target_id, normalized_url)
        content_type 为:
          - 'answer': 单个回答 (id 为 answer_id)
          - 'article': 专栏文章 (id 为 article_id)
          - 'question': 整个问题 (id 为 question_id，自动取高赞回答)
        """
        # 1. 优先匹配具体回答
        m_ans = _ANSWER_PATTERN.search(text)
        if m_ans:
            qid, aid = m_ans.group(1), m_ans.group(2)
            return ("answer", aid, f"https://www.zhihu.com/question/{qid}/answer/{aid}")

        # 2. 匹配专栏文章
        m_art = _ARTICLE_PATTERN.search(text)
        if m_art:
            pid = m_art.group(1)
            return ("article", pid, f"https://zhuanlan.zhihu.com/p/{pid}")

        # 3. 匹配问题
        m_q = _QUESTION_PATTERN.search(text)
        if m_q and not _ANSWER_PATTERN.search(text):
            qid = m_q.group(1)
            return ("question", qid, f"https://www.zhihu.com/question/{qid}")

        # 4. 提取链接后再匹配
        urls = _URL_EXTRACT.findall(text)
        for u in urls:
            if "zhihu.com" in u:
                m_ans = _ANSWER_PATTERN.search(u)
                if m_ans:
                    return ("answer", m_ans.group(2), f"https://www.zhihu.com/question/{m_ans.group(1)}/answer/{m_ans.group(2)}")
                m_art = _ARTICLE_PATTERN.search(u)
                if m_art:
                    return ("article", m_art.group(1), f"https://zhuanlan.zhihu.com/p/{m_art.group(1)}")
                m_q = _QUESTION_PATTERN.search(u)
                if m_q:
                    return ("question", m_q.group(1), f"https://www.zhihu.com/question/{m_q.group(1)}")

        return None
