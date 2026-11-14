"""Unit tests for report serialization, leaderboard sorting, and CLI execution."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from kagaribi.cli import main
from kagaribi.probe import ProbeResult, SourceProbeReport
from kagaribi.report import sort_reports, write_reports


def _sample_report(
    name: str,
    *,
    l1: ProbeResult,
    l2: ProbeResult,
    l3: ProbeResult,
    deprecated: bool = False,
    anti_crawler: bool = False,
) -> SourceProbeReport:
    return SourceProbeReport(
        name=name,
        api="9",
        version="1.0",
        base_url=f"https://{name.lower()}.test/",
        search_mode="api",
        chapter_mode="xpath",
        deprecated=deprecated,
        listed_in_index=not deprecated,
        anti_crawler=anti_crawler,
        l1=l1,
        l2=l2,
        l3=l3,
        l4=ProbeResult(status="skipped", latency_ms=0.0, reason="deep_disabled"),
    )


def test_leaderboard_sorting_and_report_files(tmp_path: Path) -> None:
    dead = _sample_report(
        "DeadSite",
        l1=ProbeResult(status="failed", latency_ms=120.0, reason="parked_domain", evidence={"matched": "parked"}),
        l2=ProbeResult(status="skipped", reason="upstream_failed"),
        l3=ProbeResult(status="skipped", reason="upstream_failed"),
    )
    degraded = _sample_report(
        "CaptchaSite",
        anti_crawler=True,
        l1=ProbeResult(status="passed", latency_ms=80.0, reason="ok"),
        l2=ProbeResult(status="skipped", reason="anti_crawler"),
        l3=ProbeResult(status="skipped", reason="upstream_failed"),
    )
    slow_healthy = _sample_report(
        "SlowHealthy",
        l1=ProbeResult(status="passed", latency_ms=300.0, reason="ok"),
        l2=ProbeResult(status="passed", latency_ms=400.0, reason="ok", evidence={"keyword": "EVA"}),
        l3=ProbeResult(status="passed", latency_ms=300.0, reason="ok", evidence={"road_count": 1, "episode_count": 12}),
    )
    fast_healthy = _sample_report(
        "FastHealthy",
        l1=ProbeResult(status="passed", latency_ms=50.0, reason="ok"),
        l2=ProbeResult(status="passed", latency_ms=60.0, reason="ok", evidence={"keyword": "海贼王"}),
        l3=ProbeResult(status="passed", latency_ms=70.0, reason="ok", evidence={"road_count": 2, "episode_count": 24}),
    )

    ordered = sort_reports([dead, slow_healthy, degraded, fast_healthy])
    assert [r.name for r in ordered] == [
        "FastHealthy",
        "SlowHealthy",
        "CaptchaSite",
        "DeadSite",
    ]

    out_json = tmp_path / "report.json"
    written_json, written_md = write_reports(
        ordered,
        json_path=out_json,
        rules_source="mock-rules",
        elapsed_s=1.23,
    )
    assert written_json.is_file()
    assert written_md.is_file()

    payload = json.loads(written_json.read_text(encoding="utf-8"))
    assert payload["summary"]["healthy"] == 2
    assert payload["summary"]["degraded"] == 1
    assert payload["summary"]["dead"] == 1
    assert payload["summary"]["parked_domains"] == 1

    md_text = written_md.read_text(encoding="utf-8")
    assert "🟢 | `FastHealthy`" in md_text
    assert "🟡 | `CaptchaSite`" in md_text
    assert "🔴 | `DeadSite`" in md_text


def test_cli_probe_end_to_end_mocked(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "Alpha.json").write_text(
        json.dumps(
            {
                "api": "5",
                "name": "Alpha",
                "version": "1.0",
                "baseURL": "https://alpha-cli.test/",
                "searchURL": "https://alpha-cli.test/s?q=@keyword",
                "searchList": "//li",
                "searchName": "//a",
                "searchResult": "//a",
                "chapterRoads": "//div[@class='road']",
                "chapterResult": "//a",
            }
        ),
        encoding="utf-8",
    )

    out_json = tmp_path / "out" / "report.json"

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://alpha-cli.test/").mock(
            return_value=httpx.Response(200, text="<html>Home</html>")
        )
        router.get("https://alpha-cli.test/s").mock(
            return_value=httpx.Response(
                200,
                text="<ul><li><a href='/d/1'>海贼王</a></li></ul>",
            )
        )
        router.get("https://alpha-cli.test/d/1").mock(
            return_value=httpx.Response(
                200,
                text="<div class='road'><a href='/p/1'>EP1</a></div>",
            )
        )

        exit_code = main(
            [
                "probe",
                "--rules",
                str(rules_dir),
                "--only",
                "alpha",
                "--out",
                str(out_json),
            ]
        )

    assert exit_code == 0
    assert out_json.is_file()
    assert out_json.with_suffix(".md").is_file()
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["summary"]["healthy"] == 1
