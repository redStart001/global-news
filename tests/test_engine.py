import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from global_capital.engine import build_rule_report, load_config

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
