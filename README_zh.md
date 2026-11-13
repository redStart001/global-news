# Horizon Global Capital Intelligence

独立的全球经济、政策与机构资本流向情报子项目。与仓库根目录的五封商业情报邮件工作流相互独立，不修改原有工作流。

## 三层 AI 架构

1. **免费规则引擎**：DDGS 搜索公开网页；按六个主题分组、去重、状态提示并生成报告。Gemini 不可用时自动降级，保证可生成基础报告。
2. **Gemini**：配置 `GEMINI_API_KEY` 后进行双语摘要、资本流向分析、投资目的地政策关联分析和词汇提取。免费额度及模型可用性由 Google 账号和地区政策决定，并非无限免费。
3. **可选扩展**：未来可增加 ChatGPT API/授权方案或本地模型适配器，不影响规则引擎。

## 六个监测板块

中国对外经济合作；美国对外经济政策；华尔街与全球机构资本；全球重大投资与并购；美联储与货币政策；投资目的地国家的税收优惠、补贴、外资审查和产业政策响应。

不以普通个股价格、常规股市波动或日常债券价格为重点。投资状态区分已完成、正式宣布/计划、媒体报道/洽谈中、状态待核实。

## 本地运行

```bash
python -m pip install -r requirements.txt
# 设置环境变量 GEMINI_API_KEY（可选）和 BREVO_SMTP_KEY（发送邮件时必需）
PYTHONPATH=src python src/run.py --no-email
```

规则引擎测试运行：

```bash
PYTHONPATH=src python src/run.py --rules-only --no-email
```

## GitHub Actions 配置

在仓库 Settings → Secrets and variables → Actions 添加：

- `BREVO_SMTP_KEY`：Brevo SMTP 密码/密钥（必需，用于发送邮件）
- `GEMINI_API_KEY`：Google AI Studio API Key（可选；缺失或调用失败会自动使用规则引擎）

工作流 `Daily Global Capital Intelligence` 每天北京时间 07:11 运行，也可在 Actions 页面手动运行。

## V2 更新

- Gemini 对临时服务错误自动重试，失败后仍可降级到规则引擎。
- 报告按英文段落后紧跟中文翻译的方式输出；规则引擎无法可靠翻译时会明确标注。
- 自动过滤明显的首页、栏目页、Wikipedia 和经济日历等低质量页面。
- 每个分类最多选择 4 条线索交给 Gemini 深度分析。

## 数据质量与边界

- 搜索结果是公开网页线索，不代表覆盖所有新闻、机构公告或交易数据库。
- 规则引擎的状态判断只是标题/摘要提示，不能代替正文核实。
- 不推测未披露金额、投资主体、政策资格或因果关系。
- 机构交易和国家政策应尽量回到公司公告、政府官网、监管文件及可靠媒体原文核实。
- 本系统不提供投资建议。Gemini 免费额度、可用模型和限额可能变化。


## V4 新闻正文与邮件链接完善

- 搜索结果保留原始 URL；对具体文章页面进行 HTTP 抓取。
- 使用 Trafilatura 提取正文、作者和页面日期元数据；正文不足或页面无法访问时明确标记，不用搜索摘要冒充全文。
- 正文抓取按六个板块轮询分配，避免前几个板块占满抓取额度。
- Gemini 优先读取正文内容，必须保留原文直达 URL；本阶段暂停逐段翻译、词汇表和美式音标。
- 规则引擎报告也保留文章标题链接和“阅读原文”链接。
- HTML 邮件将 Markdown 转换为 HTML，标题和“阅读原文”成为可点击链接；同时保留纯文本邮件版本。
- 新增依赖：`trafilatura`、`Markdown`、`bleach`。
- 正文提取受网站访问限制、付费墙、反爬策略及页面结构影响；失败时会记录状态，不会伪造正文。


## V4 新闻正文与邮件链接完善

- 搜索结果保留原始 URL；对具体文章页面进行 HTTP 抓取。
- 使用 Trafilatura 提取正文、作者和页面日期元数据；正文不足或页面无法访问时明确标记，不用搜索摘要冒充全文。
- 正文抓取按六个板块轮询分配额度，避免前几个板块占满抓取额度。
- Gemini 优先读取正文内容，必须保留原文直达 URL；本阶段暂停逐段翻译、词汇表和美式音标。
- 规则引擎报告保留文章标题链接和“阅读原文”链接。
- HTML 邮件将 Markdown 转换为 HTML，标题和“阅读原文”成为可点击链接；同时保留纯文本邮件版本。
- 新增依赖：`trafilatura`、`Markdown`、`bleach`。
- 正文提取可能受付费墙、反爬策略及页面结构影响；失败时会记录状态，不会伪造正文。

- 默认每次最多抓取 48 篇候选文章正文，按六大板块轮询分配；可在 `data/config.json` 的 `article_extraction.max_articles` 调整。


## V5：多翻译服务自动切换

新增 `src/global_capital/translation.py`，在新闻采集、正文提取和报告事实整理之后执行英译中。默认顺序：

1. Gemini（复用现有 `GEMINI_API_KEY`；翻译模型可在 `data/config.json` 修改）
2. Google Cloud Translation（可选 Secret：`GOOGLE_TRANSLATE_API_KEY`）
3. Microsoft Translator（可选 Secrets：`MICROSOFT_TRANSLATOR_KEY`、`MICROSOFT_TRANSLATOR_REGION`）
4. MyMemory（低额度匿名备用；可选 `MYMEMORY_EMAIL`）
5. LibreTranslate（需配置可用服务地址 `LIBRETRANSLATE_URL`，建议自托管；可选 `LIBRETRANSLATE_API_KEY`）

某服务未配置、超时、额度受限或返回错误时自动尝试下一家。所有服务失败时保留英文正文，不阻断邮件。翻译按段落切分，并缓存到 `data/translation-cache.sqlite3`，重复文本不重复请求。默认每日报告最多翻译 8 篇已成功提取正文的文章，可在 `data/config.json` 的 `translation` 项调整。

### GitHub Secrets 配置

现有 `GEMINI_API_KEY` 可继续使用。其他服务密钥均为可选项，需在对应平台申请后添加到 GitHub 仓库 Settings → Secrets and variables → Actions。不要把密钥写入 JSON、代码或提交到 GitHub。Google/Microsoft 免费额度、资格和是否需要绑定账单账户以服务商账户页面为准；MyMemory 匿名额度有限；公共 LibreTranslate 节点可能限流，不能视为稳定 SLA。

### 说明

- 翻译是新闻正文的附加层，不替代来源核验；数字、日期、金额、主体和交易状态应回看原文。
- 报告附带重点商业词汇及美式 IPA。该词汇表是常用商业术语表，不保证每篇文章中的所有术语都已覆盖。
- V5 未改变 SMTP、邮件收件人、每日 07:11（中国时间）工作流及原文链接机制。
