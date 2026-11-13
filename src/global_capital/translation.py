"""Multi-provider English-to-Chinese translation with fallback and SQLite cache.

Providers are attempted in configured order. API credentials are optional; missing
credentials simply skip that provider. MyMemory is a low-volume fallback, not a
high-throughput guarantee. LibreTranslate should preferably be self-hosted.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger("global_capital.translation")


def _cache_path(config: dict[str, Any]) -> Path:
    root = Path(config.get("_root", Path(__file__).resolve().parents[2]))
    configured = config.get("translation", {}).get("cache_path", "data/translation-cache.sqlite3")
    path = root / configured
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _cache_get(path: Path, text: str) -> tuple[str, str] | None:
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS translations (cache_key TEXT PRIMARY KEY, source_text TEXT NOT NULL, translated_text TEXT NOT NULL, provider TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        row = db.execute("SELECT translated_text, provider FROM translations WHERE cache_key=?", (key,)).fetchone()
    return (row[0], row[1]) if row else None


def _cache_put(path: Path, text: str, translated: str, provider: str) -> None:
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS translations (cache_key TEXT PRIMARY KEY, source_text TEXT NOT NULL, translated_text TEXT NOT NULL, provider TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT OR REPLACE INTO translations(cache_key,source_text,translated_text,provider) VALUES(?,?,?,?)", (key, text, translated, provider))


def split_text(text: str, max_chars: int = 2800) -> list[str]:
    """Split on paragraph/sentence boundaries while respecting provider limits."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    paragraphs = text.splitlines()
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        pieces = [paragraph[i:i + max_chars] for i in range(0, len(paragraph), max_chars)] if len(paragraph) > max_chars else [paragraph]
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


async def _gemini(client: httpx.AsyncClient, text: str, cfg: dict[str, Any]) -> str:
    key = os.getenv(cfg.get("gemini_key_env", "GEMINI_API_KEY"), "").strip()
    if not key:
        raise RuntimeError("Gemini key not configured")
    model = cfg.get("gemini_model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = ("Translate the following English business and investment news into accurate, natural Simplified Chinese. "
              "Preserve all names, numbers, dates, currencies, qualifications and uncertainty. Do not summarize, "
              "add facts, or omit paragraphs. Return only the Chinese translation.\n\n" + text)
    response = await client.post(url, params={"key": key}, json={
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max(1200, min(7000, len(text) * 2))}
    })
    response.raise_for_status()
    data = response.json()
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    result = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    if len(result) < max(20, int(len(text) * 0.12)):
        raise RuntimeError("Gemini translation response was empty or too short")
    return result


async def _google(client: httpx.AsyncClient, text: str, cfg: dict[str, Any]) -> str:
    key = os.getenv(cfg.get("google_key_env", "GOOGLE_TRANSLATE_API_KEY"), "").strip()
    if not key:
        raise RuntimeError("Google Cloud Translation key not configured")
    response = await client.post("https://translation.googleapis.com/language/translate/v2", params={"key": key}, json={"q": text, "source": "en", "target": "zh-CN", "format": "text"})
    response.raise_for_status()
    return response.json()["data"]["translations"][0]["translatedText"]


async def _microsoft(client: httpx.AsyncClient, text: str, cfg: dict[str, Any]) -> str:
    key = os.getenv(cfg.get("microsoft_key_env", "MICROSOFT_TRANSLATOR_KEY"), "").strip()
    region = os.getenv(cfg.get("microsoft_region_env", "MICROSOFT_TRANSLATOR_REGION"), "").strip()
    if not key or not region:
        raise RuntimeError("Microsoft Translator key/region not configured")
    response = await client.post("https://api.cognitive.microsofttranslator.com/translate", params={"api-version": "3.0", "from": "en", "to": "zh-Hans"}, headers={"Ocp-Apim-Subscription-Key": key, "Ocp-Apim-Subscription-Region": region, "Content-Type": "application/json"}, json=[{"text": text}])
    response.raise_for_status()
    return response.json()[0]["translations"][0]["text"]


async def _mymemory(client: httpx.AsyncClient, text: str, cfg: dict[str, Any]) -> str:
    # MyMemory has a small anonymous daily quota and short request limits.
    # Split long inputs into conservative sub-requests and join in original order.
    params_base = {"langpair": "en|zh-CN"}
    email = os.getenv(cfg.get("mymemory_email_env", "MYMEMORY_EMAIL"), "").strip()
    if email:
        params_base["de"] = email
    translated_parts = []
    for part in split_text(text, 430):
        params = {**params_base, "q": part}
        response = await client.get("https://api.mymemory.translated.net/get", params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("responseStatus") != 200:
            raise RuntimeError(f"MyMemory status: {data.get('responseDetails', 'unknown')}")
        result = data.get("responseData", {}).get("translatedText", "").strip()
        if not result:
            raise RuntimeError("MyMemory returned empty translation")
        translated_parts.append(result)
    return "\n\n".join(translated_parts)


async def _libre(client: httpx.AsyncClient, text: str, cfg: dict[str, Any]) -> str:
    endpoint = os.getenv(cfg.get("libre_url_env", "LIBRETRANSLATE_URL"), cfg.get("libre_url", "")).strip()
    if not endpoint:
        raise RuntimeError("LibreTranslate endpoint not configured")
    payload = {"q": text, "source": "en", "target": "zh", "format": "text"}
    api_key = os.getenv(cfg.get("libre_key_env", "LIBRETRANSLATE_API_KEY"), "").strip()
    if api_key:
        payload["api_key"] = api_key
    response = await client.post(endpoint.rstrip("/") + "/translate", json=payload)
    response.raise_for_status()
    result = response.json().get("translatedText", "").strip()
    if not result:
        raise RuntimeError("LibreTranslate returned empty translation")
    return result


PROVIDERS = {
    "gemini": _gemini,
    "google": _google,
    "microsoft": _microsoft,
    "mymemory": _mymemory,
    "libretranslate": _libre,
}


async def translate_text(text: str, config: dict[str, Any], client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    settings = config.get("translation", {})
    if not settings.get("enabled", True):
        raise RuntimeError("Translation is disabled")
    cache_path = _cache_path(config)
    cached = _cache_get(cache_path, text)
    if cached:
        return cached
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=float(settings.get("timeout_seconds", 35)))
    assert client is not None
    try:
        errors = []
        for name in settings.get("provider_order", ["gemini", "google", "microsoft", "mymemory", "libretranslate"]):
            provider = PROVIDERS.get(name)
            if provider is None:
                continue
            try:
                translated = await provider(client, text, settings)
                _cache_put(cache_path, text, translated, name)
                LOGGER.info("Translation succeeded with %s", name)
                return translated, name
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                LOGGER.warning("Translation provider %s failed: %s", name, exc)
        raise RuntimeError("All translation providers failed: " + " | ".join(errors))
    finally:
        if owns_client:
            await client.aclose()


async def translate_records(config: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, str]]:
    settings = config.get("translation", {})
    if not settings.get("enabled", True):
        return []
    max_articles = max(0, int(settings.get("max_articles", 12)))
    min_body = max(100, int(settings.get("min_body_chars", 250)))
    candidates = [r for r in records if r.get("content_status") == "extracted" and len(r.get("content_text", "")) >= min_body]
    candidates = candidates[:max_articles]
    output: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=float(settings.get("timeout_seconds", 35))) as client:
        for row in candidates:
            original = row.get("content_text", "").strip()
            chunks = split_text(original, int(settings.get("chunk_chars", 2800)))
            translated_chunks: list[str] = []
            providers: list[str] = []
            try:
                for chunk in chunks:
                    translated, provider = await translate_text(chunk, config, client)
                    translated_chunks.append(translated)
                    providers.append(provider)
                title = row.get("title", "Untitled")
                translated_title, title_provider = await translate_text(title, config, client)
                providers.append(title_provider)
                output.append({
                    "title": title, "translated_title": translated_title,
                    "url": row.get("url", ""), "section_title": row.get("section_title", ""),
                    "original": original, "translated": "\n\n".join(translated_chunks),
                    "provider": ", ".join(dict.fromkeys(providers)),
                    "translation_status": "translated",
                })
            except Exception as exc:
                LOGGER.warning("Translation failed for %s: %s", row.get("url", ""), exc)
                output.append({
                    "title": row.get("title", "Untitled"), "translated_title": "",
                    "url": row.get("url", ""), "section_title": row.get("section_title", ""),
                    "original": original, "translated": "", "provider": "",
                    "translation_status": f"failed: {type(exc).__name__}",
                })
    return output


GLOSSARY = [
    ("private equity", "私募股权", "/ˌpraɪvət ˈekwəti/"),
    ("cross-border", "跨境的", "/ˌkrɔːs ˈbɔːrdər/"),
    ("acquisition", "收购", "/ˌækwɪˈzɪʃən/"),
    ("merger", "合并", "/ˈmɜːrdʒər/"),
    ("investment", "投资", "/ɪnˈvestmənt/"),
    ("manufacturing", "制造业；制造", "/ˌmænjuˈfæktʃərɪŋ/"),
    ("capital allocation", "资本配置", "/ˈkæpɪtəl ˌæləˈkeɪʃən/"),
    ("sovereign wealth fund", "主权财富基金", "/ˈsɑːvrən welθ fʌnd/"),
    ("foreign direct investment", "外国直接投资", "/ˈfɔːrən dəˈrekt ɪnˈvestmənt/"),
    ("subsidy", "补贴", "/ˈsʌbsədi/"),
    ("tariff", "关税", "/ˈtærɪf/"),
    ("export controls", "出口管制", "/ˈekspɔːrt kənˈtroʊlz/"),
    ("buyout", "控股收购", "/ˈbaɪaʊt/"),
    ("divestment", "撤资；剥离资产", "/daɪˈvestmənt/"),
    ("greenfield investment", "绿地投资", "/ˈɡriːnfiːld ɪnˈvestmənt/"),
]


def append_bilingual_section(report: str, translations: list[dict[str, str]]) -> str:
    if not translations:
        return report.rstrip() + "\n\n## 英中对照翻译\n\n本次没有可翻译的完整正文，或所有翻译服务均不可用；英文原文及原文链接保留。\n"
    lines = [report.rstrip(), "", "## 英中对照翻译", "", "> 翻译按免费/可用服务顺序自动切换；同一文本使用本地 SQLite 缓存。翻译不改变原始事实，关键数字和交易状态仍应以原文核验。", ""]
    for i, item in enumerate(translations, 1):
        lines.extend([f"### {i}. [{item['title']}]({item['url']})", f"**中文标题：** {item.get('translated_title') or '翻译未成功，保留英文标题'}", f"**翻译服务：** {item.get('provider') or item.get('translation_status')}", "", "**English original**", item["original"], ""])
        if item.get("translated"):
            lines.extend(["**中文翻译**", item["translated"], ""])
        else:
            lines.extend(["**中文翻译**", "本条翻译失败，已保留英文正文；不会用不完整内容冒充译文。", ""])
        lines.append(f"**原文直达：** [阅读原文]({item['url']})")
        lines.append("")
    glossary_lines = ["## 重点商业词汇与美式音标", "", "| English | 中文 | 美式 IPA |", "|---|---|---|"]
    for en, zh, ipa in GLOSSARY:
        glossary_lines.append(f"| {en} | {zh} | {ipa} |")
    lines.extend(glossary_lines)
    return "\n".join(lines) + "\n"
