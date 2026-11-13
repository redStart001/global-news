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
cd global-capital-intelligence
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

## 数据质量与边界

- 搜索结果是公开网页线索，不代表覆盖所有新闻、机构公告或交易数据库。
- 规则引擎的状态判断只是标题/摘要提示，不能代替正文核实。
- 不推测未披露金额、投资主体、政策资格或因果关系。
- 机构交易和国家政策应尽量回到公司公告、政府官网、监管文件及可靠媒体原文核实。
- 本系统不提供投资建议。Gemini 免费额度、可用模型和限额可能变化。
