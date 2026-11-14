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
    import trafilatura
except ImportError:
    trafilatura = None

try:
    import markdown as markdown_lib
    import bleach
except ImportError:
    markdown_lib = None
    bleach = None

try:
    from ddgs import DDGS
except ImportError:  # allows the deterministic rule-only renderer to be tested
    DDGS = None

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "data" / "config.json"
LOGGER = logging.getLogger("global_capital")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    config["_root"] = str(ROOT)
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
    path_parts = [part for part in path.split("/") if part]
    generic_segments = {"search", "topic", "topics", "tag", "tags", "category",
        "categories", "author", "authors", "archive", "archives", "feed", "rss",
        "video", "videos", "podcasts", "newsletter", "latest", "most-read"}
    if any(part in generic_segments for part in path_parts):
        return True
    shallow_sections = {"us", "china", "world", "europe", "asia-pacific",
        "business", "markets", "economy", "technology", "politics", "companies"}
    if len(path_parts) <= 2 and path_parts and path_parts[-1] in shallow_sections:
        return True
    if any(key in parsed.query.lower() for key in ("search=", "q=", "output=1")):
        return True
    if parsed.path.lower().endswith((".xml", ".rss", ".atom", ".json", ".pdf")):
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



def _clean_article_url(value: str, base_url: str) -> str:
    from urllib.parse import urljoin, urlsplit, urlunsplit
    absolute = urljoin(base_url, (value or "").strip())
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


async def _fetch_one_article(client: httpx.AsyncClient, row: dict[str, str],
                             semaphore: asyncio.Semaphore) -> dict[str, str]:
    """Fetch article metadata only; never extract or store the full article body."""
    enriched = dict(row)
    enriched.setdefault("original_url", row.get("url", ""))
    enriched.update({"content_text": "", "content_status": "metadata_only",
                     "published_date": "", "author": "", "resolved_url": row.get("url", ""),
                     "link_status": "not_verified", "meta_description": ""})
    async with semaphore:
        try:
            response = await client.get(row["url"], follow_redirects=True)
            enriched["http_status"] = str(response.status_code)
            response.raise_for_status()
            enriched["link_status"] = "reachable"
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                enriched["content_status"] = "metadata_unavailable"
                return enriched
            final_url, html_text = str(response.url), response.text
            enriched["resolved_url"] = final_url
            # Keep only short page metadata, never article body text.
            for pattern in (
                r'<meta[^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\']'
            ):
                match = re.search(pattern, html_text, re.I)
                if match:
                    enriched["meta_description"] = html.unescape(match.group(1)).strip()[:700]
                    break
            if trafilatura is not None:
                try:
                    metadata = trafilatura.extract_metadata(html_text, default_url=final_url)
                    if metadata:
                        enriched["published_date"] = str(getattr(metadata, "date", None) or "").strip()
                        enriched["author"] = str(getattr(metadata, "author", None) or "").strip()
                        description = str(getattr(metadata, "description", None) or "").strip()
                        if description and not enriched["meta_description"]:
                            enriched["meta_description"] = description[:700]
                        canonical = str(getattr(metadata, "url", None) or "").strip()
                        canonical_url = _clean_article_url(canonical, final_url) if canonical else ""
                        if canonical_url and not _is_low_quality_page({**row, "url": canonical_url}):
                            enriched["canonical_url"] = canonical_url
                except Exception as exc:
                    LOGGER.debug("Metadata extraction failed for %s: %s", final_url, exc)
            enriched["content_status"] = "metadata_only"
        except httpx.HTTPStatusError as exc:
            enriched["http_status"] = str(exc.response.status_code)
            enriched["link_status"] = "http_error"
            enriched["content_status"] = "metadata_unavailable"
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            enriched["link_status"] = "not_verified"
            enriched["content_status"] = "metadata_unavailable"
            LOGGER.info("Metadata fetch failed for %s: %s", row.get("url"), exc)
        except Exception as exc:
            enriched["content_status"] = "metadata_unavailable"
            LOGGER.info("Metadata extraction failed for %s: %s", row.get("url"), exc)
    return enriched


async def enrich_articles(config: dict[str, Any], records: list[dict[str, str]]) -> list[dict[str, str]]:
    if not records:
        return records
    settings = config.get("article_extraction", {})
    max_articles = max(1, int(settings.get("max_articles", 36)))
    concurrency = max(1, int(settings.get("concurrency", 5)))
    timeout = float(settings.get("timeout_seconds", 18))
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in records:
        grouped.setdefault(row.get("section_id", "other"), []).append(row)
    candidates = []
    while len(candidates) < max_articles and any(grouped.values()):
        for section_id in grouped:
            if grouped[section_id] and len(candidates) < max_articles:
                candidates.append(grouped[section_id].pop(0))
    selected_ids = {id(row) for row in candidates}
    remaining = [row for row in records if id(row) not in selected_ids]
    semaphore = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HorizonCapitalIntelligence/1.0)",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), headers=headers, limits=limits) as client:
        enriched = await asyncio.gather(*[
            _fetch_one_article(client, row, semaphore) for row in candidates
        ])
    for row in remaining:
        item = dict(row)
        item.setdefault("original_url", row.get("url", ""))
        item.update({"content_text": "", "content_status": "not_fetched_limit",
                     "published_date": "", "author": ""})
        enriched.append(item)
    return enriched

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
    """Compact bilingual fallback. No article bodies are collected or rendered."""
    date_label = now.strftime("%Y-%m-%d %H:%M %Z")
    by_section = {section["id"]: [] for section in config["sections"]}
    for record in records:
        by_section.setdefault(record.get("section_id", "other"), []).append(record)
    lines = ["# Horizon Global Investment Intelligence", f"> {date_label}", ""]
    for section in config["sections"]:
        lines.extend([f"## {section['title']}", ""])
        for index, row in enumerate(by_section.get(section["id"], []), 1):
            title = row.get("title") or "Untitled"
            url = row.get("canonical_url") or row.get("resolved_url") or row.get("url", "")
            brief = row.get("meta_description") or row.get("snippet") or "A concise verified summary is unavailable; please check the original source."
            lines.extend([
                f"**{index}. [{title}]({url})**",
                f"EN: {brief[:500]}",
                "中文：中文简报暂不可用，需由 AI 摘要服务生成；请以原文为准。",
                f"Copy link: {url}",
                ""
            ])
    return "\n".join(lines).rstrip() + "\n"

def _select_ai_records(config: dict[str, Any], records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep AI input focused and balanced across the six intelligence sections."""
    per_section = max(1, int(config.get("ai", {}).get("max_records_per_section", 4)))
    selected: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    ordered_records = sorted(records, key=lambda row: 0 if row.get("content_status") == "extracted" else 1)
    for record in ordered_records:
        section_id = record.get("section_id", "other")
        if counts.get(section_id, 0) >= per_section:
            continue
        selected_record = dict(record)
        selected_record["content_text"] = str(selected_record.get("content_text", ""))[:7000]
        selected.append(selected_record)
        counts[section_id] = counts.get(section_id, 0) + 1
    return selected


def _gemini_prompt(config: dict[str, Any], records: list[dict[str, str]], date: str) -> str:
    return f"""You are the editor of Horizon Global Investment Intelligence.
Date: {date}

Create a very concise bilingual investment-intelligence email. Collect and present topics and short briefs only. Do NOT reproduce, request, or generate full article text.

OUTPUT FORMAT:
# Horizon Global Investment Intelligence
## Section 1: Foreign Investment in China / 外资对中国投资
1. [Complete English headline](DIRECT_ARTICLE_URL)
EN: One concise sentence (ideally 25-45 words) describing the material event, investor, China sector/region, and policy response if supported.
中文：紧接上一行的准确中文翻译。
Copy link: https://specific-original-article-url

## Section 2: Global Major Funds and Conglomerates / 全球重点财团投资
Use the same numbered, one-item-per-block format. Number sequentially within each section.

EDITORIAL RULES:
- Only use the supplied source records. Never invent facts, amounts, dates, entities or causality.
- Use title, search snippet and short meta_description only. These are discovery evidence, not proof; qualify claims and mark unverified where needed.
- Prefer official company announcements, government/regulator sources and reliable media.
- Include investor, destination country, sector, action and amount/currency only when supported. Use “Not disclosed” / “未披露” or “Unavailable” / “无法获取” where appropriate.
- Distinguish completed, formally announced/planned, reported/under negotiation and unverified.
- Keep publication date and event date distinct when available.
- Exclude homepages, category/index/search pages, sitemaps, rankings, directories, generic explainers, routine stock/bond prices and unrelated stories.
- Every headline must link directly to a specific article or official release URL. Repeat the full URL on a separate Copy link line.
- Each English brief must be followed immediately by its Chinese translation. No separate translation section.
- No full article text, no glossary, no IPA, no methodology paragraphs, no lengthy caveats or decorative prompts.
- If there are no credible items in a section, write one short line saying no qualifying item was verified today.

CONFIGURED SECTIONS: {json.dumps([s.get("title", "") for s in config["sections"]], ensure_ascii=False)}
SOURCE RECORDS (metadata and snippets only): {json.dumps(records, ensure_ascii=False)}"""

def validate_gemini_report(report: str, records: list[dict[str, str]]) -> None:
    if not report or len(report.strip()) < 120:
        raise RuntimeError("Gemini report is empty or unusually short")
    if report.rstrip().endswith(("...", "…", "**", "|")):
        raise RuntimeError("Gemini report appears to be truncated")
    if "## Section 1" not in report or "## Section 2" not in report:
        raise RuntimeError("Gemini report omitted one of the two required sections")
    if "中文：" not in report:
        raise RuntimeError("Gemini report omitted the Chinese line paired with English briefs")
    if not re.search(r"\[[^\]]+\]\(https?://[^)]+\)", report):
        # A no-news report may legitimately contain no article links.
        if not ("No qualifying item" in report or "今日未发现" in report):
            raise RuntimeError("Gemini report contains no direct article link")


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
                    "Preserve every supplied original article URL exactly. "
                    "Do not create translations, vocabulary lists or IPA sections."
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
                validate_gemini_report(report, records)
                LOGGER.info("Gemini report validated successfully using model %s", model)
                return report
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}: {exc}")
                LOGGER.warning("Gemini model %s did not produce a valid report: %s", model, exc)

    raise RuntimeError("All Gemini models failed: " + " | ".join(errors))



def _append_missing_source_links(report: str, records: list[dict[str, str]]) -> str:
    """Guarantee every AI evidence URL appears as a Markdown hyperlink."""
    missing = []
    for row in records:
        url = (row.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            continue
        if f"]({url})" not in report and f"](<{url}>)" not in report:
            title = (row.get("title") or "原文报道").replace("[", r"\[").replace("]", r"\]")
            missing.append(f"- [{title}]({url})")
    if missing:
        report = report.rstrip() + "\n\n## 原文直达链接补充\n\n" + "\n".join(missing) + "\n"
    return report

def _render_email_html(markdown_text: str) -> str:
    """Render Markdown into HTML so article titles are clickable in email clients."""
    if markdown_lib is not None:
        rendered = markdown_lib.markdown(
            markdown_text, extensions=["tables", "fenced_code", "sane_lists"],
            output_format="html5"
        )
    else:
        escaped = html.escape(markdown_text)
        escaped = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                         r'<a href="\2">\1</a>', escaped)
        rendered = "<pre>" + escaped + "</pre>"
    if bleach is not None:
        rendered = bleach.clean(
            rendered,
            tags={"a", "p", "h1", "h2", "h3", "h4", "ul", "ol", "li",
                  "blockquote", "strong", "em", "code", "pre", "table",
                  "thead", "tbody", "tr", "th", "td", "hr", "br"},
            attributes={"a": ["href", "title"], "th": ["align"], "td": ["align"]},
            protocols={"http", "https", "mailto"}, strip=True
        )
    style = """<style>
    body{font-family:Arial,'Microsoft YaHei',sans-serif;color:#202124;line-height:1.65}
    .report{max-width:760px;margin:0 auto;padding:12px}
    h1{font-size:24px;color:#17365d;border-bottom:2px solid #d9e2f3;padding-bottom:10px}
    h2{font-size:19px;color:#1f4e79;margin-top:28px}h3{font-size:16px;color:#333;margin-top:22px}
    a{color:#0563c1;text-decoration:underline;word-break:break-word}
    li,p{margin:8px 0}pre{white-space:pre-wrap;word-break:break-word}
    table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:7px}
    </style>"""
    return f"<!doctype html><html><head><meta charset='utf-8'>{style}</head><body><main class='report'>{rendered}</main></body></html>"


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
    message.attach(MIMEText(_render_email_html(body), "html", "utf-8"))
    with smtplib.SMTP_SSL(smtp["host"], int(smtp["port"]),
                          context=ssl.create_default_context(), timeout=30) as server:
        server.login(smtp["username"], password)
        server.send_message(message)


async def run(config_path: Path = CONFIG_PATH, *, send: bool = True, force_rules: bool = False) -> Path:
    config = load_config(config_path)
    now = datetime.now(ZoneInfo(config["timezone"]))
    day = now.strftime("%Y-%m-%d")
    records = await collect(config)
    records = await enrich_articles(config, records)
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
    out_dir = ROOT / config.get("output", {}).get("directory", "data/reports") / day
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "global-capital-intelligence.md"
    report_path.write_text(report, encoding="utf-8")
    if config.get("output", {}).get("save_raw_results", True):
        (out_dir / "search-results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    if send:
        send_email(config, f"Horizon Global Investment Intelligence | {day}", report)
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
