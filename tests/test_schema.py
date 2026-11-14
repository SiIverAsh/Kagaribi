"""Unit tests for KazumiRule and index.json Pydantic schemas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kagaribi.schema import (
    KazumiRule,
    load_rules_from_dir,
    resolve_rules_path,
)


def test_parse_api5_xpath_rule() -> None:
    raw = {
        "api": "5",
        "type": "anime",
        "name": "DemoXPath",
        "version": "1.6",
        "muliSources": True,
        "adBlocker": True,
        "useWebview": True,
        "useNativePlayer": True,
        "userAgent": "",
        "baseURL": "https://xpath-demo.test/",
        "searchURL": "https://xpath-demo.test/search?query=@keyword",
        "searchList": "//div[@class='list']/div",
        "searchName": "//h5/a",
        "searchResult": "//h5/a",
        "chapterRoads": "//div[@class='roads']/div",
        "chapterResult": "//ul/li/a",
    }
    rule = KazumiRule.model_validate(raw)
    assert rule.api == "5"
    assert rule.effective_search_mode == "xpath"
    assert rule.effective_chapter_mode == "xpath"
    assert rule.anti_crawler_enabled is False


def test_parse_api9_json_search_rule() -> None:
    raw = {
        "api": 9,
        "name": "DemoApi",
        "version": 2.4,
        "baseURL": "https://api-demo.test/",
        "searchMode": "api",
        "chapterMode": "xpath",
        "usePost": False,
        "useLegacyParser": False,
        "searchApiConfig": {
            "request": {
                "method": "get",
                "url": "https://api-demo.test/index.php/ajax/suggest",
                "query": {"mid": "1", "wd": "@keyword"},
            },
            "listPath": "$.list[*]",
            "namePath": "$.name",
            "sourcePath": "$.id",
            "sourceTemplate": "https://api-demo.test/vod/detail/id/@source.html",
        },
        "chapterRoads": "//div[contains(@class,'module-player-list')]",
        "chapterResult": ".//a[contains(@href,'/play/')]",
        "referer": "",
    }
    rule = KazumiRule.model_validate(raw)
    assert rule.api == "9"
    assert rule.version == "2.4"
    assert rule.effective_search_mode == "api"
    assert rule.effective_chapter_mode == "xpath"
    assert rule.searchApiConfig is not None
    assert rule.searchApiConfig.request.method == "GET"


def test_reject_invalid_api_mode_without_config() -> None:
    with pytest.raises(ValidationError):
        KazumiRule.model_validate(
            {
                "api": "9",
                "name": "BrokenApi",
                "baseURL": "https://broken.test/",
                "searchMode": "api",
            }
        )


def test_load_rules_dir_propagates_index_anti_crawler(tmp_path: Path) -> None:
    (tmp_path / "index.json").write_text(
        json.dumps(
            [
                {
                    "name": "CaptchaSite",
                    "version": "1.0",
                    "useNativePlayer": True,
                    "antiCrawlerEnabled": True,
                    "author": "tester",
                    "lastUpdate": 1700000000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "CaptchaSite.json").write_text(
        json.dumps(
            {
                "api": "5",
                "name": "CaptchaSite",
                "version": "1.0",
                "baseURL": "https://captcha-site.test/",
                "searchURL": "https://captcha-site.test/s?q=@keyword",
                "searchList": "//li",
                "searchName": "//a",
                "searchResult": "//a",
                "chapterRoads": "//ul",
                "chapterResult": "//li/a",
            }
        ),
        encoding="utf-8",
    )

    rules, idx = load_rules_from_dir(tmp_path)
    assert len(rules) == 1
    assert "captchasite" in idx
    assert rules[0].anti_crawler_enabled is True


def test_parse_all_real_kazumi_rules_if_present() -> None:
    sample_dir = resolve_rules_path("/tmp/kazumi-rules")
    if not sample_dir.is_dir():
        pytest.skip("/tmp/kazumi-rules sample clone not present")

    json_files = [
        p for p in sample_dir.glob("*.json") if p.name.lower() != "index.json"
    ]
    rules, idx = load_rules_from_dir(sample_dir)
    assert len(rules) == len(json_files)
    assert len(rules) > 0
    assert len(idx) > 0
