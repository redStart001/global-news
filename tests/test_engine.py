import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from global_capital.engine import _is_low_quality_page, _select_ai_records, build_rule_report, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_config_has_six_sections():
    config = load_config(ROOT / "data" / "config.json")
    assert len(config["sections"]) == 6
    assert config["timezone"] == "Asia/Shanghai"


def test_rule_report_is_bilingual_and_cites_sources():
    config = load_config(ROOT / "data" / "config.json")
    records = [{
        "title": "Example manufacturer investment announced",
        "url": "https://example.com/investment",
        "snippet": "The company announced a planned factory investment.",
        "section_id": "major_investments",
        "section_title": "Major Global Investments and M&A",
    }]
    report = build_rule_report(config, records, datetime(2026, 11, 13, 7, 11, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert "Executive overview / 核心摘要" in report
    assert "Announced or planned" in report
    assert "https://example.com/investment" in report
    assert "American IPA" in report


def test_no_results_does_not_claim_no_activity():
    config = load_config(ROOT / "data" / "config.json")
    report = build_rule_report(config, [], datetime.now(ZoneInfo("Asia/Shanghai")))
    assert "does not establish that no activity occurred" in report


def test_section_scope_has_immediate_chinese_translation():
    config = load_config(ROOT / "data" / "config.json")
    report = build_rule_report(config, [], datetime(2026, 11, 13, 7, 11, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert "**Scope / 监测范围：**\nMaterial capital allocation decisions" in report
    assert "重点关注大型银行、投资银行、资产管理公司" in report


def test_homepage_and_wikipedia_results_are_filtered():
    assert _is_low_quality_page({"url": "https://www.reuters.com/world/china/", "title": "China News | Today's Breaking Stories", "snippet": "Your online source for the latest China news stories"})
    assert _is_low_quality_page({"url": "https://en.wikipedia.org/wiki/Investment", "title": "Investment", "snippet": ""})
    assert not _is_low_quality_page({"url": "https://example.com/news/company-announces-factory-investment", "title": "Company announces factory investment", "snippet": "The company announced a new factory."})


def test_ai_evidence_is_capped_per_section():
    config = load_config(ROOT / "data" / "config.json")
    records = [{"section_id": "one", "title": str(i)} for i in range(8)] + [{"section_id": "two", "title": str(i)} for i in range(8)]
    selected = _select_ai_records(config, records)
    assert len(selected) == 8
    assert sum(row["section_id"] == "one" for row in selected) == 4
    assert sum(row["section_id"] == "two" for row in selected) == 4
