"""JSON report serializer and Markdown health leaderboard generator."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kagaribi.probe import ProbeResult, SourceProbeReport

BADGE_ORDER: dict[str, int] = {"🟢": 0, "🟡": 1, "🔴": 2}
BADGE_TO_HEALTH: dict[str, str] = {
    "🟢": "healthy",
    "🟡": "degraded",
    "🔴": "dead",
}


def sort_reports(reports: list[SourceProbeReport]) -> list[SourceProbeReport]:
    """Sort source probe reports by health tier (🟢 -> 🟡 -> 🔴), passed depth, active status, and latency."""
    return sorted(
        reports,
        key=lambda r: (
            BADGE_ORDER.get(r.badge, 9),
            -r.passed_depth,
            0 if r.anti_crawler else 1,
            0 if not r.deprecated else 1,
            r.total_latency_ms,
            r.name.lower(),
        ),
    )


def build_summary(reports: list[SourceProbeReport]) -> dict[str, int]:
    """Compute aggregate counts across all probed sources and active (non-deprecated) sources."""
    active = [r for r in reports if not r.deprecated]
    return {
        "total": len(reports),
        "active_total": len(active),
        "healthy": sum(1 for r in reports if r.badge == "🟢"),
        "degraded": sum(1 for r in reports if r.badge == "🟡"),
        "dead": sum(1 for r in reports if r.badge == "🔴"),
        "active_healthy": sum(1 for r in active if r.badge == "🟢"),
        "active_degraded": sum(1 for r in active if r.badge == "🟡"),
        "active_dead": sum(1 for r in active if r.badge == "🔴"),
        "parked_domains": sum(1 for r in reports if r.l1.reason == "parked_domain"),
        "anti_crawler_skipped": sum(
            1
            for r in reports
            if r.l1.reason == "anti_crawler" or r.l2.reason == "anti_crawler"
        ),
        "l1_passed": sum(1 for r in reports if r.l1.status == "passed"),
        "l2_passed": sum(1 for r in reports if r.l2.status == "passed"),
        "l3_passed": sum(1 for r in reports if r.l3.status == "passed"),
        "l4_passed": sum(1 for r in reports if r.l4.status == "passed"),
    }


def serialize_source_report(report: SourceProbeReport) -> dict[str, Any]:
    """Convert a SourceProbeReport into its JSON-serializable dictionary representation."""
    base = report.model_dump(mode="json")
    return {
        "badge": report.badge,
        "health": BADGE_TO_HEALTH[report.badge],
        "total_latency_ms": report.total_latency_ms,
        **base,
    }


def build_report_payload(
    reports: list[SourceProbeReport],
    *,
    rules_source: str = "",
    deep: bool = False,
    elapsed_s: float = 0.0,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the complete report.json payload."""
    sorted_list = sort_reports(reports)
    ts = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generated_at": ts,
        "rules_source": rules_source,
        "deep": deep,
        "elapsed_s": round(elapsed_s, 2),
        "summary": build_summary(sorted_list),
        "sources": [serialize_source_report(r) for r in sorted_list],
    }


def _escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _format_level_cell(res: ProbeResult) -> str:
    if res.status == "passed":
        return f"✅ {res.latency_ms:.0f}ms"
    if res.status == "skipped":
        if res.reason == "anti_crawler":
            return "⏭️ captcha"
        return "⏭️ -"
    return f"❌ {_escape_md_cell(res.reason or 'failed')}"


def _format_diagnosis(r: SourceProbeReport) -> str:
    if r.badge == "🟢":
        kw = r.l2.evidence.get("keyword", "")
        roads = r.l3.evidence.get("road_count", 0)
        eps = r.l3.evidence.get("episode_count", 0)
        extra = ""
        if r.l4.status == "passed":
            extra = f" · 直链:{r.l4.evidence.get('stream_type', '')}"
        return _escape_md_cell(f"命中「{kw}」({roads}线路/{eps}集){extra}")

    if r.l1.reason == "anti_crawler" or r.l2.reason == "anti_crawler":
        return "验证码保护 (antiCrawler)，跳过检索"

    if r.l1.status == "passed":
        if r.l2.status == "failed":
            return _escape_md_cell(f"站点存活，L2 搜索失败 ({r.l2.reason})")
        if r.l3.status == "failed":
            return _escape_md_cell(f"搜索正常，L3 章节解析失败 ({r.l3.reason})")
        if r.l4.status == "failed":
            return _escape_md_cell(f"L1-L3 正常，L4 播放嗅探失败 ({r.l4.reason})")

    if r.l1.reason == "parked_domain":
        matched = r.l1.evidence.get("matched", "")
        return _escape_md_cell(f"域名停放/出售 ({matched})")

    return _escape_md_cell(f"L1 不可达 ({r.l1.reason})")


def render_markdown_report(
    reports: list[SourceProbeReport],
    *,
    rules_source: str = "",
    deep: bool = False,
    elapsed_s: float = 0.0,
    generated_at: datetime | None = None,
) -> str:
    """Render a compact Markdown leaderboard sorted by health tier (🟢 -> 🟡 -> 🔴)."""
    sorted_list = sort_reports(reports)
    summary = build_summary(sorted_list)
    ts = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = [
        "# 🔥 Kagaribi 番剧采集源健康度榜单",
        "",
        f"- **更新时间**：`{ts}` | **探测耗时**：`{elapsed_s:.2f}s` | **深度模式 (L4)**：`{'开启' if deep else '关闭'}`"
        + (f" | **规则源**：`{_escape_md_cell(rules_source)}`" if rules_source else ""),
        "",
        "## 概览统计",
        "",
        "| 口径 | 总数 | 🟢 可用 | 🟡 受限/待修 | 🔴 失联 | 域名停放 | 验证码跳过 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| 现役源 (`index.json` / 未弃用) | {summary['active_total']} | "
            f"{summary['active_healthy']} | {summary['active_degraded']} | {summary['active_dead']} | "
            f"{sum(1 for r in sorted_list if not r.deprecated and r.l1.reason == 'parked_domain')} | "
            f"{sum(1 for r in sorted_list if not r.deprecated and (r.l1.reason == 'anti_crawler' or r.l2.reason == 'anti_crawler'))} |"
        ),
        (
            f"| 全部规则文件 | {summary['total']} | "
            f"{summary['healthy']} | {summary['degraded']} | {summary['dead']} | "
            f"{summary['parked_domains']} | {summary['anti_crawler_skipped']} |"
        ),
        "",
        "## 采集源榜单",
        "",
        "| 状态 | 源名称 | API/模式 | 现役 | L1 连通 | L2 搜索 | L3 章节 | L4 播放 | 总耗时 | 诊断摘要 |",
        "| :---: | :--- | :--- | :---: | :--- | :--- | :--- | :--- | ---: | :--- |",
    ]

    for r in sorted_list:
        mode_tag = f"v{r.api} ({r.search_mode}/{r.chapter_mode})"
        active_tag = "是" if not r.deprecated else "弃用"
        latency_str = f"{r.total_latency_ms:.0f}ms" if r.total_latency_ms > 0 else "-"
        lines.append(
            f"| {r.badge} | `{_escape_md_cell(r.name)}` | `{mode_tag}` | {active_tag} | "
            f"{_format_level_cell(r.l1)} | {_format_level_cell(r.l2)} | "
            f"{_format_level_cell(r.l3)} | {_format_level_cell(r.l4)} | "
            f"{latency_str} | {_format_diagnosis(r)} |"
        )

    lines.append("")
    return "\n".join(lines)


def write_reports(
    reports: list[SourceProbeReport],
    *,
    json_path: str | Path,
    md_path: str | Path | None = None,
    rules_source: str = "",
    deep: bool = False,
    elapsed_s: float = 0.0,
    generated_at: datetime | None = None,
) -> tuple[Path, Path]:
    """Write report.json and companion Markdown leaderboard to disk."""
    now = generated_at or datetime.now(timezone.utc)
    out_json = Path(json_path)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    if md_path is not None:
        out_md = Path(md_path)
    elif out_json.suffix.lower() == ".md":
        out_md = out_json
        out_json = out_json.with_suffix(".json")
    else:
        out_md = out_json.with_suffix(".md")
    out_md.parent.mkdir(parents=True, exist_ok=True)

    payload = build_report_payload(
        reports,
        rules_source=rules_source,
        deep=deep,
        elapsed_s=elapsed_s,
        generated_at=now,
    )
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    markdown = render_markdown_report(
        reports,
        rules_source=rules_source,
        deep=deep,
        elapsed_s=elapsed_s,
        generated_at=now,
    )
    out_md.write_text(markdown, encoding="utf-8")
    return out_json, out_md
