# astrbot_plugin_zhihu_article 知乎回答与专栏文章全文精读与公式总结插件

<div align="center">

![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue.svg)
![Version](https://img.shields.io/badge/Version-1.0.0-green.svg)
![License](https://img.shields.io/badge/License-MIT-orange.svg)
![Author](https://img.shields.io/badge/Author-VesperaZephyr-purple.svg)

专为 **[AstrBot](https://github.com/Soulter/AstrBot)** 打造的知乎（zhihu.com / zhuanlan.zhihu.com）回答与专栏文章全文精读插件。  
彻底解决传统解析器**数学公式丢失乱码、排版崩塌、长文刷屏、图片丢失占位**等痛点，支持**无公式秒回纯文本**与**知乎原版极清长图**智能自适应。

</div>

---

## 🏗️ 双通道抓取架构

知乎的内容接口对普通 HTTP 请求有严格的反爬限制，本插件采用**双通道自适应**架构保证 100% 可用：

| 内容类型 | 抓取通道 | 说明 |
|---|---|---|
| **回答、问题高赞** | 知乎 API (`api/v4`) | 直连官方接口，0.5 秒极速返回 |
| **专栏文章** (`zhuanlan.zhihu.com/p/...`) | **Playwright 浏览器渲染** | 知乎专栏文章接口需签名（无签名返回 403），插件自动切换至浏览器渲染通道，注入 Cookie 后提取正文 |
| **原版高清长图** | Playwright 浏览器 | 1:1 官方排版截图 |

对于专栏文章，浏览器通道会在**同一次导航会话中**完成「正文提取 + 原版截图」，避免二次加载，速度最优。

---

## ✨ 核心特性

- 📐 **全格式 LaTeX 公式保护与离线 MathJax 排版**：
  - 自动深入知乎 DOM，从 `<span class="ztext-math" data-tex="...">` 精准提取原始 LaTeX 代码；
  - 全面支持包括 `\[ ... \]`、`\( ... \)`、`$$ ... $$`、`$ ... $` 在内的全部 LaTeX 语法；
  - 采用服务器本地离线 **MathJax 2.7.7** 引擎排版，彻底根治公式乱码，呈现标准教材级矢量数学排版！
- 📸 **知乎官方原网页 1:1 极清截图，插图 100% 真实呈现**：
  - 只要回答/文章包含插图或数学公式，直接通过 Playwright Chromium 导航知乎原版网页截图；
  - **插图 100% 真实原版高清呈现，绝无 `[配图: 插图]` 占位符！**
  - **公式零浮动、零位移、零变形！** 官方怎么排版，长图就怎么呈现；
  - 自动净化页面：剔除知乎顶部导航、浮动登录弹窗、侧边栏推荐、底部开会广告与评论区等冗余杂项。
- 🎯 **AI 导读自适应定制，拒绝生搬硬套**：
  - **无数学公式内容**：采用社科/通用问答 Prompt，**严禁生硬出现数学词汇**，语言通俗自然；
  - **包含数学公式内容**：开启专业数理逻辑与公式推导分析。
- ⚡ **智能无图无公式极速纯文本模式**：
  - 若内容纯为无图短文，直接以合并转发形式发送纯文本总结与正文，秒级响应。
- 📑 **优雅的合并转发消息结构**：
  - **节点 1**：`以下是对知乎 <URL> 内容的解析和总结：`；
  - **节点 2**：AI 深度精读总结（有公式为 MathJax 渲染卡片图，无公式为清晰纯文本总结）；
  - **后续节点**：知乎原文（知乎原版高清长图切片，插图真实、排版完美）。
- 🤖 **原生 LLM Agent 工具 (Function Calling)**：
  - 注册 `@filter.llm_tool(name="read_zhihu_article")`；
  - 在群聊或私聊中 **@机器人 帮我看看这篇知乎回答讲了什么 <链接>** 时，大模型可自主感知并调用该工具。

---

## 🚀 安装方法

### 方式一：克隆到插件目录

在 AstrBot 的插件目录中执行：

```bash
cd data/plugins/
git clone https://github.com/VesperaZephyr/astrbot_plugin_zhihu_article.git
```

### 方式二：安装依赖

本插件依赖 `beautifulsoup4`、`playwright`、`markdown`、`pillow` 与 `aiohttp`：

```bash
pip install beautifulsoup4 playwright markdown pillow aiohttp -i https://mirrors.aliyun.com/pypi/simple/

# 安装 Playwright 的 Chromium 内核
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ python -m playwright install chromium
python -m playwright install-deps chromium
```

安装完成后，在 AstrBot 管理面板重启即可自动加载。

---

## 📖 使用方法

### 1. 自动识别模式（最常用）
在任意群聊或私聊中，直接发送知乎链接：
- 回答：`https://www.zhihu.com/question/123456/answer/789012`
- 文章：`https://zhuanlan.zhihu.com/p/123456`
- 问题：`https://www.zhihu.com/question/123456`（自动获取高赞回答）
机器人将自动捕获链接，并发送合并转发精读报告。

### 2. 自然语言与 @机器人
直接在群内 @机器人：
```text
@机器人 帮我解析一下这篇知乎回答讲了什么 https://www.zhihu.com/question/123456/answer/789012
```
大模型将通过内置的 `read_zhihu_article` 工具自主处理并回复。

### 3. 指令触发
```text
/zh https://www.zhihu.com/question/123456/answer/789012
```
（别名支持：`/知乎`、`/zhihu`、`/读知乎`、`/知乎总结`）

---

## ⚙️ 配置说明

在 AstrBot Web 控制台的「插件配置」中可以直接调节各项参数：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `zhihu_cookie` | `""` | 知乎 Cookie（包含 z_c0 等）。若未配置，插件会自动尝试从服务器已有知乎插件配置中继承 |
| `enable_auto_detect` | `true` | 是否开启群聊/私聊链接自动识别 |
| `fast_text_when_no_formula` | `true` | 是否在文章无图无公式时启用极速纯文本模式 |
| `summary_style` | `academic_math` | AI 总结风格：`academic_math`(学术与数理精读) / `concise`(极简速读) / `detailed`(详细全面) |
| `max_slice_height` | `12000` | 单张长图最大高度阈值（像素），超过自动进行智能无缝分段切片 |
| `access_mode` | `blacklist` | 群聊权限控制模式：`blacklist`(黑名单) / `whitelist`(白名单) |
| `group_list` | `""` | 黑名单或白名单的群号列表（逗号分隔） |

---

## 📄 开源许可

本项目遵循 [MIT License](LICENSE) 许可开源。欢迎提交 Issue 或 Pull Request！
