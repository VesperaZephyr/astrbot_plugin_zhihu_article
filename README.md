# astrbot_plugin_zhihu_article 知乎回答与专栏文章全文精读与公式总结插件

<div align="center">

![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue.svg)
![Version](https://img.shields.io/badge/Version-1.1.3-green.svg)
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
- 📸 **知乎官方原网页极清截图，插图 100% 真实呈现**：
  - 只要回答/文章包含插图或数学公式，直接通过 Playwright Chromium 导航知乎原版网页截图；
  - **插图 100% 真实原版高清呈现，绝无 `[配图: 插图]` 占位符！**
  - **公式零浮动、零位移、零变形！** 官方怎么排版，长图就怎么呈现；
  - 🆕 **内容就绪检测**：唤醒懒加载图片（专栏页/回答页渐进滚动；问题页改用主动触发 MathJax 排版，规避反爬）→ 强制注入真实图片地址 → 主动令 MathJax 排版目标容器 → 轮询等待**全部图片解码完成**与**全部公式（MathJax / KaTeX）排版完成**（含 MathJax 队列排空）→ DOM 收敛后才截图，**彻底根治「公式还没渲染完就截图」**；
  - 🆕 **纯净舞台裁剪**：按优先级精确锁定正文容器，克隆进隔离的白底舞台中渲染，顶栏、侧栏、推荐流、广告、评论区、页脚**全部不进画面**；舞台边距紧凑可调，**根治「截图边角太多、非正文占比过高」**；
  - 🆕 **错误页兜底**：知乎偶发对「回答直达页」返回错误页时，自动重试并改从问题页定位该条回答；彻底失败时降级为纯文本分段，绝不产出空白长图。
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
| `content_wait_timeout` | `25` | 正文资源等待上限（秒）。等待图片解码与公式排版完成的最长时间，长文或公式极多时可调大 |
| `content_width` | `760` | 长图正文宽度（像素），即最终长图的视觉宽度 |
| `content_padding` | `26` | 长图正文四周留白（像素），越小则正文占比越高 |
| `access_mode` | `blacklist` | 群聊权限控制模式：`blacklist`(黑名单) / `whitelist`(白名单) |
| `group_list` | `""` | 黑名单或白名单的群号列表（逗号分隔） |

---

## 📝 更新日志

### v1.1.3

**修复**

1. **回答页长图里公式整片空白（根因修复，问题页通道）** —— 知乎**问题页**的公式是「进入视野才排版」的：
   目标回答不在视野内时，无论等多久都不会产生排版产物（实测 31 个公式全部停留在裸 `data-tex`）。
   而把问题页滚到底又会触发 SPA 加载更多回答，无头会话随即被反爬踢成「出了一点问题」错误页，
   整页内容丢失 —— 且目标回答常位于文档末尾，「滚进视野」等价于「滚到底」，**滚动这条路根本走不通**。
   现改为：问题页一律不做全页滚动，直接令 MathJax 对目标容器执行一次排版
   （`MathJax.Hub.Queue(["Typeset", MathJax.Hub, el])`，兼容 MathJax v3 的 `typeset` / `typesetPromise`），
   并在等待期间最多补推 3 次。

   实测（同一条回答、同一页面状态，唯一变量是等待逻辑）：

   | 分支 | 页面存活 | 公式排版 |
   |---|---|---|
   | 旧行为（直接克隆进舞台） | 存活 | **0 / 31** |
   | 新行为（先主动触发排版再克隆） | **存活** | **31 / 31** |

2. **问题页滚动触发反爬** —— `_wait_assets_ready` 原先在问题页也执行整页滚动唤醒，导致页面被踢成错误页。
   现按页面类型分流：仅「问题页本身」（`/question/<id>`）跳过滚动并改用主动排版；专栏页与回答直达页
   仍保留滚动唤醒懒加载（这两类页面滚动安全，且专栏页的长图正文依赖滚动加载插图）。

---

### v1.1.2

**修复**

1. **错误页兜底后公式仍为裸 LaTeX** —— 回答直达页被拦截时会兜底导航到问题页定位该条回答，但导航后没有重跑资源就绪等待。
   由于干净舞台是**克隆** DOM，未排版的公式克隆进去后 MathJax 不再处理它，只在舞台里等待永远等不到。
   现由 `_ensure_not_error_page` 回传「是否发生过导航」，导航后、克隆前重跑一次就绪等待。

---

### v1.1.1

**修复**

1. **长图里公式整片空白（根因修复）** —— v1.1.0 新增的资源就绪判定，误把 `MathJax_Preview` 当作「公式已排版完成」的标志。
   但 `MathJax_Preview` 恰恰是 MathJax 在排版**之前**插入的占位元素，排版完成后仍会残留在 DOM 中（通常为空）。
   其后果是：只要 MathJax 刚开始处理、插入了占位符，等待循环就会在约 1.2 秒后（DOM 长度连续两次不变）判定「资源就绪」并提前放行，
   而此时异步排版尚未产出真正的排版产物 —— 于是长图截到了尚未渲染的公式。
   现改为要求**真实排版产物存在且具备实际尺寸**（`.MathJax_SVG` / `.MathJax_CHTML` / `mjx-container` / `.katex` 等，且 bounding box 宽高 > 1px）才算完成。
   实测验证：人为移除某个公式的排版产物、只保留 `MathJax_Preview` 时，旧判定报告 `pending=0`（误判完成），新判定正确报告 `pending=1`。
2. **等待作用域过大，在问题页易超时** —— `_wait_assets_ready` 原先在「定位正文容器」之前执行，此时页面还没有任何目标标记，
   就绪统计的 root 会退化成 `document.body`。在问题页上这意味着要等待**数十条回答**的全部图片与公式渲染完成，
   极易超时后带着未渲染的公式去截图。现改为先对正文容器做一次「预定位」，使等待只作用于目标正文。

---

### v1.1.0

**修复**

1. **公式/插图未加载完就截图** —— 原实现仅等待固定的 2.5 秒，知乎的 MathJax/KaTeX 异步排版与图片懒加载经常尚未完成。
   现改为「就绪检测」：渐进式滚动唤醒懒加载 → 把 `data-original` / `data-actualsrc` 等真实地址写回 `src` → 轮询等待所有图片
   `complete && naturalWidth > 0`、所有 `.ztext-math` 出现 MathJax/KaTeX 排版产物且 `MathJax.Hub.queue.pending` 归零
   → 正文 DOM 指纹连续两次不变，才执行截图（全程带超时降级，不会卡死）。
2. **截图混入大量非正文（页面边角过多）** —— 原实现用「innerHTML 最长」的启发式挑选容器，容易命中带作者卡、赞同按钮、
   版权声明的外层容器；兜底 `document.body` 更是把顶栏、侧栏、推荐流、广告、评论区、页脚一并截入。现改为按优先级选择器
   精确定位正文（`.Post-RichText` > `.QuestionAnswer-content .RichText` > …），克隆进隔离的白底舞台 `#zh-capture-stage`，
   隐藏页面其余全部内容，并去除克隆体内残留的非正文模块。
3. **回答直达页产出空白长图** —— 知乎对 `/question/{id}/answer/{id}` 有时会向无头浏览器返回「出了一点问题」错误页。
   现加入错误页检测，先重试原链接，仍失败则改从问题页定位该条回答；全部失败时不产出空白图，并在群内降级为纯文本分段。
4. **折叠长文被截断** —— 新增点击「阅读全文」并解除 `max-height` 截断与渐变遮罩。

**其他**

- 新增 `content_wait_timeout` / `content_width` / `content_padding` 三项配置。
- 视口宽度保持桌面尺寸不变，仅调整高度，避免窄视口触发知乎移动端媒体查询导致排版变形。
- 公式子树跳过 `max-width` 归一化，防止长公式被挤压换行错乱。
- 隐藏滚动条，避免占位宽度把截图舞台挤出可视区。
- AI 总结卡片渲染同样等待 MathJax 队列排空后再截图。

---

## 📄 开源许可

本项目遵循 [MIT License](LICENSE) 许可开源。欢迎提交 Issue 或 Pull Request！
