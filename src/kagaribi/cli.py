"""Command-line interface for Kagaribi: `kagaribi probe --rules <dir|git-url>`."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from kagaribi import __version__
from kagaribi.report import build_summary, sort_reports, write_reports
from kagaribi.runner import run_probes
from kagaribi.schema import load_rules_from_dir, resolve_rules_path


def _is_git_url(target: str) -> bool:
    stripped = target.strip()
    if stripped.startswith(("http://", "https://", "git://", "ssh://", "git@")):
        local_candidate = Path(stripped)
        return not local_candidate.exists()
    return False


def _force_remove_readonly(func: object, path: str, _exc: object) -> None:
    os.chmod(path, stat.S_IWRITE)
    if callable(func):
        func(path)


@contextmanager
def _prepare_rules_dir(rules_arg: str) -> Iterator[Path]:
    if not _is_git_url(rules_arg):
        yield resolve_rules_path(rules_arg)
        return

    tmp_root = Path(tempfile.mkdtemp(prefix="kagaribi-rules-"))
    clone_dir = tmp_root / "repo"
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", rules_arg.strip(), str(clone_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Failed to git clone rules from '{rules_arg}': {err}")
        yield clone_dir
    finally:
        shutil.rmtree(tmp_root, onexc=_force_remove_readonly)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kagaribi",
        description="🔥 Kagaribi（篝火）—— 番剧采集源健康度探针",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    probe_parser = subparsers.add_parser(
        "probe",
        help="探测规则目录或 Git 仓库中的采集源存活性，输出 JSON 与 Markdown 榜单",
    )
    probe_parser.add_argument(
        "--rules",
        required=True,
        help="本地规则目录路径或 Git 仓库 URL（如 /tmp/kazumi-rules 或 https://github.com/Predidit/KazumiRules）",
    )
    probe_parser.add_argument(
        "--only",
        default=None,
        help="仅探测指定名称的单个源（不区分大小写，如 AGE 或 LMM）",
    )
    probe_parser.add_argument(
        "--deep",
        action="store_true",
        help="启用 L4 Playwright 播放页直链（m3u8/mp4）嗅探（需安装 playwright）",
    )
    probe_parser.add_argument(
        "--out",
        default="report.json",
        help="JSON 报告输出路径（默认 report.json，同目录自动生成同名 .md 榜单）",
    )
    probe_parser.add_argument(
        "--md",
        default=None,
        help="Markdown 榜单输出路径（默认与 --out 同名 .md）",
    )
    probe_parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="全局最大并发探测源数（默认 8）",
    )
    probe_parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="单源单次探测超时秒数（默认 20.0）",
    )
    return parser


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _run_probe_command(args: argparse.Namespace) -> int:
    try:
        with _prepare_rules_dir(args.rules) as rules_dir:
            rules, index_map = load_rules_from_dir(rules_dir, only=args.only)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not rules:
        target_msg = f" matching --only '{args.only}'" if args.only else ""
        print(f"Error: no rule files found in '{args.rules}'{target_msg}", file=sys.stderr)
        return 2

    reports, elapsed_s = asyncio.run(
        run_probes(
            rules,
            index_map=index_map,
            concurrency=args.concurrency,
            timeout_s=args.timeout,
            deep=args.deep,
        )
    )

    sorted_reports = sort_reports(reports)
    out_json, out_md = write_reports(
        sorted_reports,
        json_path=args.out,
        md_path=args.md,
        rules_source=args.rules,
        deep=args.deep,
        elapsed_s=elapsed_s,
    )

    summary = build_summary(sorted_reports)
    print(
        f"🔥 Kagaribi 探测完成 | 共 {summary['total']} 源（现役 {summary['active_total']}）| "
        f"🟢 {summary['healthy']}  🟡 {summary['degraded']}  🔴 {summary['dead']} | "
        f"耗时 {elapsed_s:.2f}s"
    )
    print(f"JSON 报告: {out_json}")
    print(f"MD 榜单  : {out_md}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "probe":
        return _run_probe_command(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
