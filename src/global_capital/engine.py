from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

try:
    from ddgs import DDGS
except ImportError:  # allows the deterministic rule-only renderer to be tested
    DDGS = None

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "data" / "config.json"
LOGGER = logging.getLogger("global_capital")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("timezone", "recipient", "smtp", "search", "ai", "sections", "editorial_rules"):
        if key not in config:
            raise ValueError(f"Missing config key: {key}")
    if not config["sections"]:
        raise ValueError("At least one intelligence section is required")
    return config


def _search_sync(query: str, limit: int, region: str, timelimit: str) -> list[dict[str, str]]:
    if DDGS is None:
        raise RuntimeError("DDGS dependency is missing. Install ddgs to enable web collection.")
    output = []
    with DDGS() as client:
        for item in client.text(query, max_results=limit, region=region, timelimit=timelimit):
            url = str(item.get("href") or item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("body") or item.get("snippet") or "").strip()
            if title and url.startswith(("https://", "http://")):
                output.append({"title": title, "url": url, "snippet": snippet, "query": query})
    return output



def _is_obviously_off_topic(row: dict[str, str]) -> bool:
    """Conservative exclusions for obvious non-capital/business results."""
    text = f"{row.get('title', '')} {row.get('snippet', '')}".lower()
    exclusions = (
        "airport currency exchange", "currency exchange rates at airport",
        "immigration news", "asylum seeker", "visa application",
        "weather forecast", "sports results", "football scores",
        "horoscope", "celebrity gossip", "job vacancies",
        "etf price today", "stock price today", "share price today",
        "market quote", "fund directory", "economic calendar",
    )
    return any(term in text for term in exclusions)


def _is_low_quality_page(row: dict[str, str]) -> bool:
    """Reject obvious homepages, section indexes, calendars and encyclopedia pages."""
    parsed = urlparse(row.get("url", ""))
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = (parsed.path or "").strip("/").lower()
    title = (row.get("title") or "").strip().lower()
    snippet = (row.get("snippet") or "").strip().lower()

    if not host or host.endswith("wikipedia.org"):
        return True
    if any(token in host for token in ("calendar.fxstreet",)) or "economic calendar" in title:
        return True

    generic_title_phrases = (
        "today's breaking stories", "latest news", "breaking news",
        "news |", "news -", "home page", "homepage", "economic calendar",
    )
    generic_snippet_phrases = (
        "your online source for the latest", "latest news stories and current events",
        "read the latest news", "breaking news and headlines",
    )
    if any(x in title for x in generic_title_phrases):
        return True
    if any(x in snippet for x in generic_snippet_phrases):
        return True

    # Known section/index URL patterns that are not individual articles.
    section_paths = {
        "world/china", "world/us", "world", "business", "markets", "technology",
        "news", "politics", "economy", "china", "us", "world/asia-pacific",
    }
    if path in section_paths or not path:
        return True
    return False


async def collect(config: dict[str, Any]) -> list[dict[str, str]]:
    search = config["search"]
    max_queries = int(search.get("max_queries", 18))
    limit = int(search.get("max_results_per_query", 5))
    all_rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for section in config["sections"]:
        for query in section["queries"]:
            try:
                rows = await asyncio.to_thread(
                    _search_sync, query, limit, search.get("region", "wt-wt"), search.get("timelimit", "d")
                )
                for row in rows:
                    if _is_low_quality_page(row) or _is_obviously_off_topic(row):
                        LOGGER.info("Filtered low-quality/off-topic result: %s", row.get("url", ""))
                        continue
                    normalized = row["url"].split("#", 1)[0].rstrip("/")
                    if normalized not in seen:
                        seen.add(normalized)
                        row["section_id"] = section["id"]
                        row["section_title"] = section["title"]
                        all_rows.append(row)
            except Exception as exc:
                LOGGER.warning("Search failed (%s): %s", query, exc)
            await asyncio.sleep(float(search.get("delay_seconds", 1.0)))
            max_queries -= 1
            if max_queries <= 0:
                return all_rows
    return all_rows


def _status_hint(text: str) -> str:
    lowered = text.lower()
    if any(x in lowered for x in ("completed", "closed", "has acquired", "acquired", "opened", "began operations", "已完成")):
        return "Completed / 已完成（需核对正文）"
    if any(x in lowered for x in ("plans to", "will invest", "announced", "agreement", "to acquire", "计划", "宣布")):
        return "Announced or planned / 已宣布或计划（尚不等于完成）"
    if any(x in lowered for x in ("considering", "in talks", "negotiation", "reportedly", "may invest", "谈判", "考虑")):
        return "Reported or under discussion / 媒体报道或洽谈中"
    return "Unverified status / 状态待核实"


def build_rule_report(config: dict[str, Any], records: list[dict[str, str]], now: datetime) -> str:
    """Free deterministic fallback: no model call, no fabricated enrichment."""
    date_label = now.strftime("%Y-%m-%d %H:%M %Z")
    by_section = {section["id"]: [] for section in config["sections"]}
    for record in records:
        by_section.setdefault(record.get("section_id", "other"), []).append(record)
    lines = [
        "# Horizon Global Capital Intelligence / 全球经济与资本流向情报",
        f"> Generated: {date_label}",
        "> Mode: Free rule engine / 免费规则引擎（未使用 AI）",
        f"> Public indexed search leads: {len(records)}. Search coverage is not exhaustive.",
        "",
        "## Executive overview / 核心摘要",
        "- This edition is a rule-filtered collection of public search results. Items below are leads for verification, not independently verified transaction records. / 本期为公开搜索结果的规则筛选汇总，以下内容属于待核查线索，并非独立核实的交易记录。",
        "- Routine stock prices, individual share movements and ordinary bond-price commentary are intentionally excluded from the core focus. / 本系统不以普通股价、个股涨跌和日常债券价格为重点。",
        "",
    ]
    for section in config["sections"]:
        focus_zh = section.get("focus_zh", "监测范围说明：请结合英文范围核对本分类关注主题。")
        lines.extend([f"## {section['title']}", "**Scope / 监测范围：**", section['focus'], focus_zh, ""])
        rows = by_section.get(section["id"], [])
        if not rows:
            lines.append("- No matching public search leads were returned. This does not establish that no activity occurred. / 本次没有获得匹配的公开搜索线索，但不能据此认定没有相关活动。")
        for index, row in enumerate(rows, 1):
            combined = f"{row['title']} {row['snippet']}"
            status = _status_hint(combined)
            lines.extend([
                f"### {index}. {row['title']}",
                f"- Status / 状态：{status}",
                f"- Search snippet / 搜索摘要：{row['snippet'] or 'No snippet / 无摘要'}",
                "  中文翻译：规则引擎未调用 AI，暂无法可靠翻译原文摘要；请打开来源核对。",
                f"- Source / 来源：[{urlparse(row['url']).netloc}]({row['url']})",
                "- Investor, destination, sector, value and policy response / 投资方、目的地、行业、金额及政策响应：Not verified from snippet / 搜索摘要不足以核实，请打开原文确认。",
                "",
            ])
    lines.extend([
        "## Investment destination policy watch / 投资目的地政策观察",
        "- Check recipient-country tax credits, grants, subsidies, local-content rules, foreign-investment screening, ownership limits and permitting changes against official government sources. / 对投资目的地的税收抵免、补贴、地方含量要求、外资审查、持股限制和许可政策，应以政府官方来源核实。",
        "- A policy announcement is not automatically evidence that a named investor received the incentive. / 政策公布不代表某个投资者已经获得相关优惠。",
        "",
        "## Important vocabulary / 重点词汇",
        "| English | American IPA | 中文 |\n|---|---|---|",
        "| Capital allocation | /ˈkæpɪtəl ˌæləˈkeɪʃən/ | 资本配置 |",
        "| Sovereign wealth fund | /ˈsɑːvrɪn welθ fʌnd/ | 主权财富基金 |",
        "| Foreign direct investment | /ˈfɔːrən dəˈrekt ɪnˈvestmənt/ | 外国直接投资 |",
        "| Investment screening | /ɪnˈvestmənt ˈskriːnɪŋ/ | 投资审查 |",
        "| Tax incentive | /tæks ɪnˈsentɪv/ | 税收优惠 |",
        "| Industrial policy | /ɪnˈdʌstriəl ˈpɑːləsi/ | 产业政策 |",
        "| Cross-border acquisition | /ˌkrɔːs ˈbɔːrdər ˌækwɪˈzɪʃən/ | 跨境收购 |",
        "| Divestment | /daɪˈvestmənt/ | 撤资、剥离资产 |",
        "",
        "## Methodology and limitations / 方法与限制",
        "- Rule engine does not infer undisclosed deal values, company identities or policy eligibility. / 规则引擎不会推测未公开的交易金额、企业身份或政策适用资格。",
        "- Search snippets can be incomplete, stale or misleading; verify the original article and primary documents. / 搜索摘要可能不完整、过时或存在误导，请核对原文及一手文件。",
        "- This is business intelligence, not investment advice. / 本报告属于商业情报，不构成投资建议。",
    ])
    return "\n".join(lines)


def _select_ai_records(config: dict[str, Any], records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep AI input focused and balanced across the six intelligence sections."""
    per_section = max(1, int(config.get("ai", {}).get("max_records_per_section", 4)))
    selected: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for record in records:
        section_id = record.get("section_id", "other")
        if counts.get(section_id, 0) >= per_section:
            continue
        selected.append(record)
        counts[section_id] = counts.get(section_id, 0) + 1
    return selected


def _gemini_prompt(config: dict[str, Any], records: list[dict[str, str]], date: str) -> str:
    return f"""You are the bilingual editor of a rigorous global macroeconomics and institutional-capital intelligence briefing.
Date: {date}

MANDATORY BILINGUAL LAYOUT (highest priority):
- Every English heading must be immediately followed by its Chinese equivalent.
- Every English paragraph or bullet must be followed immediately by its faithful Chinese translation on the next line/paragraph. Never place all English first and all Chinese later.
- For each section, show Scope / 监测范围, then the English scope paragraph, then its complete Chinese translation.
- For every news item, use this order: English headline; Chinese headline translation; English summary; Chinese summary translation; facts/status; Chinese translation of those facts; investment analysis in English; its Chinese translation; source URL.
- Vocabulary must be a table with English, American IPA, and Chinese meaning.
- If a source title or snippet is too vague to translate reliably, say so in Chinese rather than inventing content.
- Do not pad the report with weak or unrelated results merely to fill a section. It is better to report fewer high-quality items or no qualifying item.
- Keep every English news headline, summary paragraph, fact bullet and analysis paragraph immediately followed by its Chinese translation. Do not combine unrelated English paragraphs before translating them.

Required sections: Executive Summary / 核心摘要; China's International Economic Cooperation / 中国对外经济合作; US External Economic Policy / 美国对外经济政策; Wall Street and Global Institutional Capital / 华尔街与全球机构资本; Major Global Investments and M&A / 全球重大投资与并购; Federal Reserve and Monetary Policy / 美联储与货币政策; Investment Destination Policy Responses / 投资目的地国家政策响应; Important Vocabulary / 重点词汇; Sources and Limitations / 来源与限制。

QUALITY FILTERING:
- Include only specific, identifiable news events, official notices, company announcements, regulatory documents or substantive analysis.
- Exclude homepages, section/category pages, news feeds, search pages, generic landing pages, Wikipedia, calendars, and articles with no material economic, investment, trade, capital-allocation or relevant policy connection.
- Ordinary political news is excluded unless a concrete effect on trade, investment, sanctions, tariffs, capital flows, industrial policy or business operations is evidenced.
- Do not treat a page's generic description or a search-index timestamp as an article or publication date.
- Do not treat a draft rule as final law. Track draft, adopted, published and effective dates separately when evidence supports them.

EVIDENCE AND STATUS:
Use only the evidence records supplied below. For each item label status as Completed, Formally announced/planned, Reported/under negotiation, Draft/proposed, or Unverified. Distinguish source facts from your analysis. For each investment, report investor, destination, sector, disclosed amount, status and date only if evidence supports it. For recipient-country responses, identify government, tax incentives/subsidies/grants, industrial policy, foreign-investment screening, local-content rules and effective dates only if evidenced. Explain links between an investment and a policy only when sources support the link. Exclude routine stock-price and ordinary bond-price commentary. Do not invent facts, amounts, entities, dates, legal effects, causal claims or contacts. Preserve source URLs and cite every claim. If evidence is insufficient, say Not publicly disclosed / 未公开 or Not verified / 尚未核实. No personalized investment advice.

Editorial rules: {json.dumps(config['editorial_rules'], ensure_ascii=False)}
Evidence: {json.dumps(records, ensure_ascii=False)}"""



def _contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def validate_gemini_report(report: str, config: dict[str, Any]) -> None:
    """Reject empty, truncated, non-bilingual, or structurally incomplete AI reports."""
    if not report or len(report.strip()) < 1200:
        raise RuntimeError("Gemini report is empty or unusually short")

    if not _contains_chinese(report):
        raise RuntimeError("Gemini report contains no Chinese translation")

    # Require both language labels for every configured intelligence section.
    missing_sections = []
    for section in config.get("sections", []):
        title = str(section.get("title", ""))
        parts = [part.strip() for part in title.split("/", 1)]
        if len(parts) == 2:
            english, chinese = parts
            if english.lower() not in report.lower() or chinese not in report:
                missing_sections.append(title)
    if missing_sections:
        raise RuntimeError(
            "Gemini report is missing required bilingual sections: "
            + "; ".join(missing_sections)
        )

    lower = report.lower()
    if "important vocabulary" not in lower and "重点词汇" not in report:
        raise RuntimeError("Gemini report is missing the vocabulary section")

    # Check the most important adjacency rule: every numbered news headline
    # must be followed by a Chinese headline line.
    lines = report.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*#{2,4}\s+\d+[\.)]\s+\S", line):
            next_line = ""
            for candidate in lines[index + 1:]:
                if candidate.strip():
                    next_line = candidate.strip()
                    break
            if not _contains_chinese(next_line):
                raise RuntimeError(
                    f"English news headline is not immediately followed by Chinese: {line[:120]}"
                )

    # Check English prose/bullets for an adjacent Chinese translation.
    # Exempt headings, tables, links/sources, and short labels.
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "|", ">", "---", "http")):
            continue
        if stripped.lower().startswith(("source:", "- source", "sources:", "- sources")):
            continue
        is_prose = stripped.startswith(("- ", "* ")) or (
            len(re.findall(r"[A-Za-z]{2,}", stripped)) >= 14
        )
        if not is_prose or _contains_chinese(stripped):
            continue
        next_line = ""
        for candidate in lines[index + 1:]:
            if candidate.strip():
                next_line = candidate.strip()
                break
        if not _contains_chinese(next_line):
            raise RuntimeError(
                f"English paragraph/bullet lacks an adjacent Chinese translation: {stripped[:120]}"
            )

    # Catch obvious output truncation at a token boundary.
    if report.rstrip().endswith(("...", "…", "**", "|")):
        raise RuntimeError("Gemini report appears to be truncated")


async def _gemini_request(
    client: httpx.AsyncClient,
    model: str,
    key: str,
    payload: dict[str, Any],
    max_attempts: int,
    retry_delays: list[Any],
) -> dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    retry_statuses = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(url, params={"key": key}, json=payload)
            response.raise_for_status()
            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                feedback = data.get("promptFeedback", {})
                raise RuntimeError(f"Gemini returned no candidates: {feedback}")
            finish_reason = str(candidates[0].get("finishReason", "")).upper()
            if finish_reason == "MAX_TOKENS":
                raise RuntimeError("Gemini output reached max_output_tokens and was truncated")
            if finish_reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"}:
                raise RuntimeError(f"Gemini generation blocked: {finish_reason}")
            return data
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status = exc.response.status_code
            detail = exc.response.text[:500].replace("\n", " ")
            if status not in retry_statuses or attempt >= max_attempts:
                LOGGER.warning("Gemini model %s failed with HTTP %s: %s", model, status, detail)
                raise
            delay = float(retry_delays[min(attempt - 1, len(retry_delays) - 1)]) if retry_delays else 10.0
            LOGGER.warning(
                "Gemini model %s temporary HTTP %s (attempt %s/%s); retrying in %.1f seconds",
                model, status, attempt, max_attempts, delay
            )
            await asyncio.sleep(delay)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt >= max_attempts:
                raise
            delay = float(retry_delays[min(attempt - 1, len(retry_delays) - 1)]) if retry_delays else 10.0
            LOGGER.warning(
                "Gemini model %s connection issue (attempt %s/%s): %s; retrying in %.1f seconds",
                model, attempt, max_attempts, exc, delay
            )
            await asyncio.sleep(delay)
    if last_error:
        raise last_error
    raise RuntimeError(f"Gemini request failed for model {model}")


async def gemini_enrich(config: dict[str, Any], records: list[dict[str, str]], date: str) -> str:
    ai = config["ai"]
    key = os.getenv(ai.get("api_key_env", "GEMINI_API_KEY"), "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    primary_model = ai.get("model", "gemini-3.8-flash")
    fallback_models = ai.get("fallback_models", ["gemini-3.7-flash"])
    models = [primary_model] + [m for m in fallback_models if m and m != primary_model]

    payload = {
        "systemInstruction": {
            "parts": [{
                "text": (
                    "Follow evidence and attribution rules exactly. Never fabricate. "
                    "Every English prose paragraph and news headline must be followed "
                    "immediately by its faithful Chinese translation."
                )
            }]
        },
        "contents": [{"role": "user", "parts": [{"text": _gemini_prompt(config, records, date)}]}],
        # Gemini 3.8 migration guidance: omit legacy temperature/top_p/top_k settings.
        "generationConfig": {
            "maxOutputTokens": int(ai.get("max_output_tokens", 7000))
        },
    }

    primary_attempts = max(1, int(ai.get("max_attempts", 4)))
    primary_delays = ai.get("retry_delays_seconds", [10, 20, 40])
    fallback_attempts = max(1, int(ai.get("fallback_max_attempts", 2)))
    fallback_delays = ai.get("fallback_retry_delays_seconds", [5])

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=float(ai.get("timeout_seconds", 90))) as client:
        for index, model in enumerate(models):
            attempts = primary_attempts if index == 0 else fallback_attempts
            delays = primary_delays if index == 0 else fallback_delays
            if index:
                LOGGER.warning(
                    "Primary Gemini model %s unavailable or invalid; trying fallback model %s",
                    primary_model, model
                )
            try:
                data = await _gemini_request(
                    client, model, key, payload, attempts, list(delays)
                )
                parts = data["candidates"][0]["content"]["parts"]
                report = "\n".join(
                    part["text"] for part in parts if isinstance(part, dict) and "text" in part
                ).strip()
                validate_gemini_report(report, config)
                LOGGER.info("Gemini report validated successfully using model %s", model)
                return report
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}: {exc}")
                LOGGER.warning("Gemini model %s did not produce a valid report: %s", model, exc)

    raise RuntimeError("All Gemini models failed: " + " | ".join(errors))


def send_email(config: dict[str, Any], subject: str, body: str) -> None:
    smtp = config["smtp"]
    password = os.getenv(smtp.get("password_env", "BREVO_SMTP_KEY"), "").strip()
    if not password:
        raise RuntimeError("BREVO_SMTP_KEY is not configured")
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{smtp.get('sender_name', 'Horizon Intelligence')} <{smtp['sender']}>"
    message["To"] = config["recipient"]
    message.attach(MIMEText(body, "plain", "utf-8"))
    html_body = "<html><body><pre style='white-space:pre-wrap;font-family:Arial,sans-serif;line-height:1.55'>" + html.escape(body) + "</pre></body></html>"
    message.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL(smtp["host"], int(smtp["port"]), context=ssl.create_default_context(), timeout=30) as server:
        server.login(smtp["username"], password)
        server.send_message(message)


async def run(config_path: Path = CONFIG_PATH, *, send: bool = True, force_rules: bool = False) -> Path:
    config = load_config(config_path)
    now = datetime.now(ZoneInfo(config["timezone"]))
    day = now.strftime("%Y-%m-%d")
    records = await collect(config)
    mode = "rules"
    report = None
    if not force_rules and config["ai"].get("enabled", True):
        try:
            ai_records = _select_ai_records(config, records)
            LOGGER.info("Sending %s selected evidence records to Gemini from %s collected leads", len(ai_records), len(records))
            report = await gemini_enrich(config, ai_records, day)
            mode = "gemini"
        except Exception as exc:
            LOGGER.warning("Gemini unavailable; falling back to free rules: %s", exc)
    if not report:
        report = build_rule_report(config, records, now)
    report = f"> Generation mode: {mode}\n> Public search leads: {len(records)}\n\n" + report
    out_dir = ROOT / config.get("output", {}).get("directory", "data/reports") / day
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "global-capital-intelligence.md"
    report_path.write_text(report, encoding="utf-8")
    if config.get("output", {}).get("save_raw_results", True):
        (out_dir / "search-results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    if send:
        send_email(config, f"全球经济与资本流向情报 | Global Capital Intelligence | {day}", report)
    LOGGER.info("Report saved: %s (mode=%s, records=%s)", report_path, mode, len(records))
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Horizon Global Capital Intelligence")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--no-email", action="store_true", help="Generate report without sending email")
    parser.add_argument("--rules-only", action="store_true", help="Skip Gemini and use deterministic rules")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = asyncio.run(run(args.config, send=not args.no_email, force_rules=args.rules_only))
    print(f"Report generated: {path}")


if __name__ == "__main__":
    main()
