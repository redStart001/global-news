import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from global_capital.engine import (
    _is_low_quality_page, _select_ai_records, _render_email_html, _clean_article_url, _append_missing_source_links,
    build_rule_report, load_config,
)

ROOT = Path(__file__).resolve().parents[1]


def test_config_has_six_sections_and_extraction_settings():
    config = load_config(ROOT / "data" / "config.json")
    assert len(config["sections"]) == 6
    assert config["timezone"] == "Asia/Shanghai"
    assert config["article_extraction"]["max_articles"] >= 1


def test_rule_report_includes_direct_article_links_and_body():
    config = load_config(ROOT / "data" / "config.json")
    url = "https://example.com/news/investment-announcement"
    record = {
        "title": "Example manufacturer investment announced",
        "url": url,
        "snippet": "The company announced a planned factory investment.",
        "section_id": "major_investments",
        "content_text": "The company announced a new manufacturing facility. " * 12,
        "content_status": "extracted",
        "published_date": "2026-11-12",
        "author": "Example News Desk",
    }
    report = build_rule_report(
        config, [record],
        datetime(2026, 11, 13, 7, 11, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    assert f"[Example manufacturer investment announced]({url})" in report
    assert f"[阅读原文]({url})" in report
    assert "新闻正文提取内容" in report
    assert "2026-11-12" in report


def test_no_results_does_not_claim_no_activity():
    config = load_config(ROOT / "data" / "config.json")
    report = build_rule_report(config, [], datetime.now(ZoneInfo("Asia/Shanghai")))
    assert "这不代表相关活动没有发生" in report


def test_homepage_search_and_category_pages_are_filtered():
    assert _is_low_quality_page({
        "url": "https://www.reuters.com/world/china/",
        "title": "China News | Today's Breaking Stories",
        "snippet": "Your online source for the latest China news stories",
    })
    assert _is_low_quality_page({
        "url": "https://www.reuters.com/markets/us/",
        "title": "US Markets",
        "snippet": "Latest US market news",
    })
    assert _is_low_quality_page({
        "url": "https://example.com/search?q=investment",
        "title": "Search results",
        "snippet": "",
    })
    assert not _is_low_quality_page({
        "url": "https://example.com/news/company-announces-factory-investment",
        "title": "Company announces factory investment",
        "snippet": "The company announced a new factory.",
    })


def test_ai_evidence_prefers_extracted_articles_and_is_capped():
    config = load_config(ROOT / "data" / "config.json")
    records = (
        [{"section_id": "one", "title": str(i), "content_status": "not_fetched"} for i in range(4)]
        + [{"section_id": "one", "title": "body", "content_status": "extracted",
            "content_text": "article body"}]
    )
    selected = _select_ai_records(config, records)
    assert selected[0]["title"] == "body"


def test_html_email_renders_clickable_article_link():
    url = "https://example.com/article?id=123"
    rendered = _render_email_html(f"## News\n\n[Read the article]({url})")
    assert f'href="{url}"' in rendered
    assert "Read the article</a>" in rendered


def test_email_renderer_does_not_emit_javascript_links():
    rendered = _render_email_html("[unsafe](javascript:alert(1))")
    assert 'href="javascript:' not in rendered



def test_clean_article_url_keeps_http_article_and_strips_fragment():
    assert _clean_article_url("https://example.com/story?id=5#section", "https://example.com") == "https://example.com/story?id=5"
    assert _clean_article_url("javascript:alert(1)", "https://example.com") == ""



def test_missing_ai_source_link_is_appended_as_markdown_link():
    url = "https://example.com/story"
    report = _append_missing_source_links("## Report", [{"title": "A story", "url": url}])
    assert f"[A story]({url})" in report
