"""Async concurrent probe scheduler with per-source timeout, per-host cap, jitter, and retry-once."""

from __future__ import annotations

import asyncio
import ssl
import time
from typing import Iterable

import httpx

from kagaribi.probe import (
    DEFAULT_KEYWORDS,
    ProbeResult,
    RequestLimiter,
    SourceProbeReport,
    probe_source,
)
from kagaribi.schema import KazumiRule, RuleIndexEntry

NON_RETRYABLE_L1_REASONS = frozenset({"parked_domain", "dns_error", "http_404", "http_410"})


def _should_retry(report: SourceProbeReport) -> bool:
    """Determine whether a failed source report warrants a single retry."""
    if report.badge == "🟢":
        return False
    if report.anti_crawler and report.l2.status == "skipped":
        return False
    if report.l1.status == "failed" and report.l1.reason in NON_RETRYABLE_L1_REASONS:
        return False
    for level in (report.l1, report.l2, report.l3):
        if level.status == "failed":
            if level.reason in ("timeout", "connect_error", "http_error", "tls_error") or (
                level.reason.startswith("http_5") or level.reason == "http_429"
            ):
                return True
    return False


def _timeout_report(
    rule: KazumiRule,
    *,
    timeout_s: float,
    deep: bool,
    listed_in_index: bool,
) -> SourceProbeReport:
    skipped = ProbeResult(
        status="skipped",
        latency_ms=0.0,
        reason="upstream_failed",
        evidence={"depends_on": "L1"},
    )
    l4 = (
        ProbeResult(status="skipped", latency_ms=0.0, reason="deep_disabled")
        if not deep
        else ProbeResult(
            status="skipped",
            latency_ms=0.0,
            reason="upstream_failed",
            evidence={"depends_on": "L3"},
        )
    )
    return SourceProbeReport(
        name=rule.name,
        api=rule.api,
        version=rule.version,
        base_url=rule.baseURL,
        search_mode=rule.effective_search_mode,
        chapter_mode=rule.effective_chapter_mode,
        deprecated=rule.deprecated,
        listed_in_index=listed_in_index,
        anti_crawler=rule.anti_crawler_enabled,
        l1=ProbeResult(
            status="failed",
            latency_ms=round(timeout_s * 1000, 1),
            reason="timeout",
            evidence={"timeout_s": timeout_s},
        ),
        l2=skipped,
        l3=ProbeResult(
            status="skipped",
            latency_ms=0.0,
            reason="upstream_failed",
            evidence={"depends_on": "L2"},
        ),
        l4=l4,
    )


async def _probe_with_retry(
    rule: KazumiRule,
    client: httpx.AsyncClient,
    *,
    worker_sem: asyncio.Semaphore,
    limiter: RequestLimiter,
    timeout_s: float,
    retries: int,
    keywords: tuple[str, ...],
    deep: bool,
    listed_in_index: bool,
) -> SourceProbeReport:
    async with worker_sem:
        max_attempts = max(1, retries + 1)
        last_report: SourceProbeReport | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                report = await asyncio.wait_for(
                    probe_source(
                        rule,
                        client,
                        keywords=keywords,
                        deep=deep,
                        listed_in_index=listed_in_index,
                        limiter=limiter,
                    ),
                    timeout=timeout_s,
                )
            except TimeoutError:
                report = _timeout_report(
                    rule,
                    timeout_s=timeout_s,
                    deep=deep,
                    listed_in_index=listed_in_index,
                )

            report.attempts = attempt
            last_report = report

            if attempt < max_attempts and _should_retry(report):
                low, high = limiter.jitter_range
                if high > 0:
                    await asyncio.sleep(max(0.1, high))
                continue
            break

        assert last_report is not None
        return last_report


def create_default_http_client(timeout_s: float = 20.0) -> httpx.AsyncClient:
    """Create an async HTTP/2-capable httpx client using OS trust store."""
    connect_timeout = min(6.0, timeout_s)
    io_timeout = min(10.0, timeout_s)
    timeout = httpx.Timeout(
        timeout=timeout_s,
        connect=connect_timeout,
        read=io_timeout,
        write=io_timeout,
        pool=min(5.0, timeout_s),
    )
    ssl_ctx = ssl.create_default_context()
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        timeout=timeout,
        verify=ssl_ctx,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )


async def run_probes(
    rules: Iterable[KazumiRule],
    *,
    index_map: dict[str, RuleIndexEntry] | None = None,
    concurrency: int = 8,
    per_host_concurrency: int = 2,
    timeout_s: float = 20.0,
    retries: int = 1,
    jitter_range: tuple[float, float] = (0.05, 0.20),
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    deep: bool = False,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[SourceProbeReport], float]:
    """Run probes concurrently across rules and return (reports, elapsed_seconds)."""
    rule_list = list(rules)
    if not rule_list:
        return [], 0.0

    idx_lookup = index_map or {}
    effective_timeout_s = max(timeout_s, 45.0) if deep else timeout_s
    worker_sem = asyncio.Semaphore(max(1, concurrency))
    limiter = RequestLimiter(
        per_host_limit=max(1, per_host_concurrency),
        jitter_range=jitter_range,
    )

    owns_client = client is None
    active_client = client if client is not None else create_default_http_client(timeout_s=timeout_s)

    t0 = time.perf_counter()
    try:
        tasks = [
            asyncio.create_task(
                _probe_with_retry(
                    rule,
                    active_client,
                    worker_sem=worker_sem,
                    limiter=limiter,
                    timeout_s=effective_timeout_s,
                    retries=retries,
                    keywords=keywords,
                    deep=deep,
                    listed_in_index=(rule.name.lower() in idx_lookup),
                )
            )
            for rule in rule_list
        ]
        reports = list(await asyncio.gather(*tasks))
    finally:
        if owns_client:
            await active_client.aclose()

    elapsed_s = round(time.perf_counter() - t0, 2)
    return reports, elapsed_s
