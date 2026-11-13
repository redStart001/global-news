from pathlib import Path

from global_capital.translation import append_bilingual_section, split_text, _cache_get, _cache_put


def test_split_text_respects_limit():
    chunks = split_text(("A" * 100) + "\n\n" + ("B" * 100), 120)
    assert chunks
    assert all(len(chunk) <= 120 for chunk in chunks)
    assert "A" * 100 in chunks[0]


def test_translation_cache_roundtrip(tmp_path: Path):
    path = tmp_path / "cache.sqlite3"
    assert _cache_get(path, "hello") is None
    _cache_put(path, "hello", "你好", "unit-test")
    assert _cache_get(path, "hello") == ("你好", "unit-test")


def test_bilingual_section_contains_links_and_translation():
    report = "# Report"
    rows = [{"title": "Deal announced", "translated_title": "宣布交易", "url": "https://example.com/story", "original": "A company announced a deal.", "translated": "一家公司宣布了一项交易。", "provider": "gemini", "translation_status": "translated"}]
    result = append_bilingual_section(report, rows)
    assert "[Deal announced](https://example.com/story)" in result
    assert "一家公司宣布了一项交易" in result
    assert "美式 IPA" in result


def test_translation_failure_keeps_original():
    result = append_bilingual_section("# Report", [])
    assert "没有可翻译的完整正文" in result
