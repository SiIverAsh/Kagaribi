"""Four-level source health probe (L1 reachable, L2 search, L3 chapter, L4 playable)."""

from __future__ import annotations

import asyncio
import random
import re
import ssl
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Literal
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import lxml.etree as ET
import lxml.html as LH
from jsonpath_ng import parse as jsonpath_parse
from pydantic import BaseModel, Field

from kagaribi.schema import ApiRequestConfig, EpisodePageConfig, KazumiRule

ProbeStatus = Literal["passed", "failed", "skipped"]
HealthBadge = Literal["🟢", "🟡", "🔴"]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_KEYWORDS: tuple[str, ...] = ("海贼王", "EVA", "火影忍者")

PARKED_DOMAIN_RE = re.compile(
    r"本域名|出售|parked|购买此域名|godaddy",
    re.IGNORECASE,
)
PARKED_MAX_BYTES = 2048

MEDIA_STREAM_RE = re.compile(r"\.(?:m3u8|mp4)(?:\?|#|$)", re.IGNORECASE)


class ProbeResult(BaseModel):
    """Structured probe result for a single probe level (L1-L4)."""

    status: ProbeStatus
    latency_ms: float = 0.0
    reason: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class SourceProbeReport(BaseModel):
    """Aggregated L1-L4 probe report for one source rule."""

    name: str
    api: str
    version: str
    base_url: str
    search_mode: Literal["xpath", "api"]
    chapter_mode: Literal["xpath", "api"]
    deprecated: bool = False
    listed_in_index: bool = False
    anti_crawler: bool = False
    attempts: int = 1
    l1: ProbeResult
    l2: ProbeResult
    l3: ProbeResult
    l4: ProbeResult

    @property
    def total_latency_ms(self) -> float:
        return round(
            self.l1.latency_ms + self.l2.latency_ms + self.l3.latency_ms + self.l4.latency_ms,
            1,
        )

    @property
    def badge(self) -> HealthBadge:
        """Compute health indicator: 🟢 healthy, 🟡 partial/captcha-skipped, 🔴 unreachable."""
        if (
            self.l1.status == "passed"
            and self.l2.status == "passed"
            and self.l3.status == "passed"
            and self.l4.status in ("passed", "skipped")
            and self.l4.reason != "no_media_stream"
        ):
            if self.l4.status == "skipped" and self.l4.reason not in (
                "deep_disabled",
                "playwright_not_installed",
            ):
                return "🟡"
            return "🟢"
        if self.l1.status == "passed" or (
            self.l1.status == "skipped" and self.l1.reason == "anti_crawler"
        ):
            return "🟡"
        return "🔴"

    @property
    def passed_depth(self) -> int:
        depth = 0
        for level in (self.l1, self.l2, self.l3, self.l4):
            if level.status == "passed":
                depth += 1
            else:
                break
        return depth


@dataclass(slots=True)
class SearchHit:
    """Internal representation of a validated L2 search hit passed to L3."""

    keyword: str
    name: str
    raw_source: str
    detail_url: str


@dataclass(slots=True)
class RequestLimiter:
    """Per-host concurrency semaphore + optional polite jitter between requests."""

    per_host_limit: int = 2
    jitter_range: tuple[float, float] = (0.05, 0.20)
    _semaphores: dict[str, asyncio.Semaphore] | None = None

    def _get_semaphore(self, url: str) -> asyncio.Semaphore:
        if self._semaphores is None:
            self._semaphores = {}
        host = urlsplit(url).netloc.lower() or "_default"
        sem = self._semaphores.get(host)
        if sem is None:
            sem = asyncio.Semaphore(max(1, self.per_host_limit))
            self._semaphores[host] = sem
        return sem

    async def run(self, url: str, coro_factory: Callable[[], Any]) -> Any:
        low, high = self.jitter_range
        if high > 0:
            delay = random.uniform(max(0.0, low), max(low, high))
            if delay > 0:
                await asyncio.sleep(delay)
        sem = self._get_semaphore(url)
        async with sem:
            return await coro_factory()


def build_rule_headers(rule: KazumiRule, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    """Build HTTP headers respecting rule userAgent and referer."""
    ua = rule.userAgent.strip() if rule.userAgent and rule.userAgent.strip() else DEFAULT_USER_AGENT
    headers: dict[str, str] = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if rule.referer and rule.referer.strip():
        headers["Referer"] = rule.referer.strip()
    if extra_headers:
        headers.update(extra_headers)
    return headers


def normalize_xpath(expr: str) -> str:
    """Normalize Dart xpath_selector shorthand operators into standard XPath 1.0."""
    out = expr.strip()
    out = re.sub(
        r"@([a-zA-Z0-9_:-]+)\s*\*=\s*(['\"][^'\"]*['\"])",
        r"contains(@\1, \2)",
        out,
    )
    out = re.sub(
        r"@([a-zA-Z0-9_:-]+)\s*\^=\s*(['\"][^'\"]*['\"])",
        r"starts-with(@\1, \2)",
        out,
    )
    return out


def make_relative_xpath(expr: str) -> str:
    """Scope a Kazumi sub-node XPath (e.g. //div/a or /self::* | //a) to the current element."""
    normalized = normalize_xpath(expr)
    if not normalized or normalized == ".":
        return "."

    branches: list[str] = []
    buf: list[str] = []
    in_quote: str | None = None
    bracket_depth = 0

    for ch in normalized:
        if in_quote:
            buf.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
            buf.append(ch)
        elif ch == "[":
            bracket_depth += 1
            buf.append(ch)
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
            buf.append(ch)
        elif ch == "|" and bracket_depth == 0:
            branches.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        branches.append("".join(buf).strip())

    scoped: list[str] = []
    for branch in branches:
        if branch.startswith("/"):
            scoped.append(f".{branch}")
        else:
            scoped.append(branch)
    return " | ".join(scoped)


@lru_cache(maxsize=256)
def _compile_jsonpath(expr: str) -> Any:
    return jsonpath_parse(expr)


def _jsonpath_values(data: Any, expr: str) -> list[Any]:
    if not expr or not expr.strip():
        return []
    compiled = _compile_jsonpath(expr.strip())
    return [match.value for match in compiled.find(data)]


def _scalar_to_str(val: Any) -> str:
    if val is None or isinstance(val, (bool, dict, list)):
        return ""
    return str(val).strip()


def resolve_url(base_url: str, raw_target: str, template: str = "") -> str:
    """Resolve a scraped href or API source ID into an absolute HTTP(S) URL."""
    target = raw_target.strip()
    if not target:
        return ""
    lowered = target.lower()
    if lowered.startswith(("javascript:", "mailto:", "data:")) or target == "#":
        return ""

    if template and template.strip():
        rendered = template.strip().replace("@source", quote(target, safe="/:@-._~"))
    else:
        rendered = target

    if rendered.startswith(("http://", "https://")):
        return rendered
    if rendered.startswith("//"):
        scheme = urlsplit(base_url).scheme or "https"
        return f"{scheme}:{rendered}"

    normalized_base = base_url if base_url.endswith("/") else f"{base_url}/"
    resolved = urljoin(normalized_base, rendered)
    if resolved.startswith(("http://", "https://")):
        return resolved
    return ""


def _substitute_placeholders(obj: Any, mapping: dict[str, str], *, url_encode: bool = False) -> Any:
    if isinstance(obj, str):
        out = obj
        for key, val in mapping.items():
            replacement = quote(val, safe="") if url_encode else val
            out = out.replace(key, replacement)
        return out
    if isinstance(obj, dict):
        return {k: _substitute_placeholders(v, mapping, url_encode=url_encode) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_placeholders(v, mapping, url_encode=url_encode) for v in obj]
    return obj


def classify_http_exception(exc: Exception) -> str:
    """Map httpx / SSL / socket errors into concise machine-readable reason codes."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"

    msg = f"{type(exc).__name__}: {exc}".lower()
    cause = exc.__cause__
    if isinstance(cause, ssl.SSLError) or any(
        tok in msg for tok in ("ssl", "tls", "certificate", "handshake")
    ):
        return "tls_error"
    if any(
        tok in msg
        for tok in (
            "getaddrinfo",
            "name or service not known",
            "nodename nor servname",
            "no address associated",
            "11001",
            "nxdomain",
        )
    ):
        return "dns_error"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    return "http_error"


def _parse_html_document(text: str) -> LH.HtmlElement | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return LH.fromstring(stripped)
    except (ET.ParserError, ValueError):
        return None


async def _send_api_request(
    client: httpx.AsyncClient,
    rule: KazumiRule,
    req_cfg: ApiRequestConfig,
    placeholders: dict[str, str],
    limiter: RequestLimiter | None,
) -> httpx.Response:
    url = _substitute_placeholders(req_cfg.url, placeholders, url_encode=True)
    params = _substitute_placeholders(req_cfg.query, placeholders, url_encode=False)
    extra_headers = _substitute_placeholders(req_cfg.headers, placeholders, url_encode=False)
    headers = build_rule_headers(rule, extra_headers)
    body = _substitute_placeholders(req_cfg.body, placeholders, url_encode=False)

    kwargs: dict[str, Any] = {"headers": headers}
    if params:
        kwargs["params"] = params
    if body is not None:
        if (req_cfg.bodyType or "").lower() == "json" or isinstance(body, (dict, list)):
            kwargs["json"] = body
        elif isinstance(body, str):
            kwargs["content"] = body

    async def _do() -> httpx.Response:
        return await client.request(req_cfg.method, url, **kwargs)

    if limiter is not None:
        return await limiter.run(url, _do)
    return await _do()


async def probe_l1_reachable(
    rule: KazumiRule,
    client: httpx.AsyncClient,
    *,
    limiter: RequestLimiter | None = None,
) -> ProbeResult:
    """L1 reachable: DNS + TLS + HTTP < 400 and not a parked domain (<2KB + parked keywords)."""
    headers = build_rule_headers(rule)
    t0 = time.perf_counter()
    try:
        if limiter is not None:
            resp = await limiter.run(rule.baseURL, lambda: client.get(rule.baseURL, headers=headers))
        else:
            resp = await client.get(rule.baseURL, headers=headers)
    except Exception as exc:
        latency = round((time.perf_counter() - t0) * 1000, 1)
        reason = classify_http_exception(exc)
        return ProbeResult(
            status="failed",
            latency_ms=latency,
            reason=reason,
            evidence={"url": rule.baseURL, "error": str(exc)[:200]},
        )

    latency = round((time.perf_counter() - t0) * 1000, 1)
    final_url = str(resp.url)
    content_bytes = len(resp.content)

    if resp.status_code >= 400:
        if rule.anti_crawler_enabled and resp.status_code in (401, 403, 429, 503):
            return ProbeResult(
                status="skipped",
                latency_ms=latency,
                reason="anti_crawler",
                evidence={"status_code": resp.status_code, "url": final_url},
            )
        return ProbeResult(
            status="failed",
            latency_ms=latency,
            reason=f"http_{resp.status_code}",
            evidence={"status_code": resp.status_code, "url": final_url},
        )

    body_text = resp.text or ""
    if content_bytes < PARKED_MAX_BYTES:
        match = PARKED_DOMAIN_RE.search(body_text)
        if match:
            return ProbeResult(
                status="failed",
                latency_ms=latency,
                reason="parked_domain",
                evidence={
                    "url": final_url,
                    "content_bytes": content_bytes,
                    "matched": match.group(0),
                },
            )

    return ProbeResult(
        status="passed",
        latency_ms=latency,
        reason="ok",
        evidence={
            "status_code": resp.status_code,
            "url": final_url,
            "content_bytes": content_bytes,
        },
    )


def _extract_xpath_text(node: LH.HtmlElement, expr: str) -> str:
    if not expr or not expr.strip():
        return ""
    rel = make_relative_xpath(expr)
    matches = node.xpath(rel)
    if not isinstance(matches, list):
        return str(matches).strip()
    parts: list[str] = []
    for item in matches:
        if isinstance(item, LH.HtmlElement):
            txt = item.text_content().strip()
            if txt:
                parts.append(txt)
        elif isinstance(item, str):
            txt = item.strip()
            if txt:
                parts.append(txt)
    return " ".join(parts).strip()


def _extract_xpath_href(node: LH.HtmlElement, expr: str) -> str:
    if not expr or not expr.strip():
        return ""
    rel = make_relative_xpath(expr)
    matches = node.xpath(rel)
    if not isinstance(matches, list):
        return str(matches).strip()
    for item in matches:
        if isinstance(item, LH.HtmlElement):
            href = (item.get("href") or "").strip()
            if href:
                return href
        elif isinstance(item, str):
            href = item.strip()
            if href:
                return href
    return ""


async def _search_xpath_keyword(
    rule: KazumiRule,
    keyword: str,
    client: httpx.AsyncClient,
    limiter: RequestLimiter | None,
) -> tuple[list[SearchHit], str]:
    if not rule.searchURL.strip() or not rule.searchList.strip():
        return [], "missing_search_xpath_config"

    headers = build_rule_headers(rule)
    if rule.usePost:
        parts = urlsplit(rule.searchURL.strip())
        base_search_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        raw_pairs = parse_qsl(parts.query, keep_blank_values=True)
        form_data = {
            k: v.replace("@keyword", keyword)
            for k, v in raw_pairs
        }
        target_url = base_search_url.replace("@keyword", quote(keyword, safe=""))

        async def _do_post() -> httpx.Response:
            return await client.post(target_url, data=form_data, headers=headers)

        resp = await (limiter.run(target_url, _do_post) if limiter else _do_post())
    else:
        target_url = rule.searchURL.strip().replace("@keyword", quote(keyword, safe=""))

        async def _do_get() -> httpx.Response:
            return await client.get(target_url, headers=headers)

        resp = await (limiter.run(target_url, _do_get) if limiter else _do_get())

    if resp.status_code >= 400:
        return [], f"http_{resp.status_code}"

    doc = _parse_html_document(resp.text)
    if doc is None:
        return [], "empty_html"

    try:
        items = doc.xpath(normalize_xpath(rule.searchList))
    except ET.XPathError:
        return [], "invalid_xpath"

    if not isinstance(items, list) or not items:
        return [], "empty_search_results"

    hits: list[SearchHit] = []
    seen_invalid = False
    for item in items:
        if not isinstance(item, LH.HtmlElement):
            continue
        try:
            name = _extract_xpath_text(item, rule.searchName)
            raw_source = _extract_xpath_href(item, rule.searchResult)
        except ET.XPathError:
            return [], "invalid_xpath"

        detail_url = resolve_url(rule.baseURL, raw_source)
        if name and detail_url:
            hits.append(
                SearchHit(
                    keyword=keyword,
                    name=name,
                    raw_source=raw_source,
                    detail_url=detail_url,
                )
            )
        else:
            seen_invalid = True

    if hits:
        return hits, "ok"
    return [], ("invalid_search_item" if seen_invalid else "empty_search_results")


async def _search_api_keyword(
    rule: KazumiRule,
    keyword: str,
    client: httpx.AsyncClient,
    limiter: RequestLimiter | None,
) -> tuple[list[SearchHit], str]:
    cfg = rule.searchApiConfig
    if cfg is None:
        return [], "missing_search_api_config"

    resp = await _send_api_request(
        client,
        rule,
        cfg.request,
        {"@keyword": keyword},
        limiter,
    )
    if resp.status_code >= 400:
        return [], f"http_{resp.status_code}"

    try:
        payload = resp.json()
    except ValueError:
        return [], "invalid_json"

    try:
        entries = _jsonpath_values(payload, cfg.listPath)
    except Exception:
        return [], "invalid_jsonpath"

    if not entries:
        return [], "empty_search_results"

    hits: list[SearchHit] = []
    seen_invalid = False
    for entry in entries:
        names = _jsonpath_values(entry, cfg.namePath)
        sources = _jsonpath_values(entry, cfg.sourcePath)
        name = _scalar_to_str(names[0]) if names else ""
        raw_source = _scalar_to_str(sources[0]) if sources else ""

        if not name or not raw_source:
            seen_invalid = True
            continue

        if rule.effective_chapter_mode == "api" and not cfg.sourceTemplate.strip():
            chapter_cfg = rule.chapterApiConfig
            if chapter_cfg is not None:
                detail_url = _substitute_placeholders(
                    chapter_cfg.request.url,
                    {"@source": raw_source},
                    url_encode=True,
                )
            else:
                detail_url = resolve_url(rule.baseURL, raw_source)
        else:
            detail_url = resolve_url(rule.baseURL, raw_source, cfg.sourceTemplate)

        if detail_url:
            hits.append(
                SearchHit(
                    keyword=keyword,
                    name=name,
                    raw_source=raw_source,
                    detail_url=detail_url,
                )
            )
        else:
            seen_invalid = True

    if hits:
        return hits, "ok"
    return [], ("invalid_search_item" if seen_invalid else "empty_search_results")


async def probe_l2_search(
    rule: KazumiRule,
    l1: ProbeResult,
    client: httpx.AsyncClient,
    *,
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    limiter: RequestLimiter | None = None,
) -> tuple[ProbeResult, SearchHit | None]:
    """L2 search: query 2-3 evergreen titles (union); pass as soon as >=1 valid hit is parsed."""
    if l1.status != "passed":
        return (
            ProbeResult(
                status="skipped",
                latency_ms=0.0,
                reason="upstream_failed",
                evidence={"depends_on": "L1"},
            ),
            None,
        )

    if rule.anti_crawler_enabled:
        return (
            ProbeResult(
                status="skipped",
                latency_ms=0.0,
                reason="anti_crawler",
                evidence={"anti_crawler_enabled": True},
            ),
            None,
        )

    t0 = time.perf_counter()
    tried_keywords: list[str] = []
    reasons: list[str] = []

    for keyword in keywords:
        tried_keywords.append(keyword)
        try:
            if rule.effective_search_mode == "api":
                hits, reason = await _search_api_keyword(rule, keyword, client, limiter)
            else:
                hits, reason = await _search_xpath_keyword(rule, keyword, client, limiter)
        except Exception as exc:
            reason = classify_http_exception(exc)
            hits = []

        if hits:
            latency = round((time.perf_counter() - t0) * 1000, 1)
            first = hits[0]
            return (
                ProbeResult(
                    status="passed",
                    latency_ms=latency,
                    reason="ok",
                    evidence={
                        "mode": rule.effective_search_mode,
                        "keyword": keyword,
                        "tried_keywords": tried_keywords,
                        "result_count": len(hits),
                        "first_name": first.name,
                        "detail_url": first.detail_url,
                    },
                ),
                first,
            )
        reasons.append(reason)

    latency = round((time.perf_counter() - t0) * 1000, 1)
    non_empty_errors = [r for r in reasons if r != "empty_search_results"]
    final_reason = non_empty_errors[0] if non_empty_errors else "empty_search_results"
    return (
        ProbeResult(
            status="failed",
            latency_ms=latency,
            reason=final_reason,
            evidence={
                "mode": rule.effective_search_mode,
                "tried_keywords": tried_keywords,
                "reasons": reasons,
            },
        ),
        None,
    )


def _build_episode_page_url(
    base_url: str,
    ep_page: EpisodePageConfig | None,
    raw_source: str,
    raw_ep_url: str,
) -> str:
    if ep_page is not None and ep_page.url.strip():
        mapping = {"@source": raw_source, "@episodeUrl": raw_ep_url}
        page_url = _substitute_placeholders(ep_page.url.strip(), mapping, url_encode=True)
        query_dict = _substitute_placeholders(ep_page.query, mapping, url_encode=False)
        if query_dict:
            sep = "&" if "?" in page_url else "?"
            page_url = f"{page_url}{sep}{urlencode(query_dict)}"
        return resolve_url(base_url, page_url)
    return resolve_url(base_url, raw_ep_url)


async def _probe_chapter_xpath(
    rule: KazumiRule,
    hit: SearchHit,
    client: httpx.AsyncClient,
    limiter: RequestLimiter | None,
) -> tuple[str, dict[str, Any]]:
    if not rule.chapterResult.strip():
        return "missing_chapter_xpath_config", {"detail_url": hit.detail_url}

    headers = build_rule_headers(rule)

    async def _do_get() -> httpx.Response:
        return await client.get(hit.detail_url, headers=headers)

    resp = await (limiter.run(hit.detail_url, _do_get) if limiter else _do_get())
    if resp.status_code >= 400:
        return f"http_{resp.status_code}", {
            "status_code": resp.status_code,
            "detail_url": hit.detail_url,
        }

    doc = _parse_html_document(resp.text)
    if doc is None:
        return "empty_html", {"detail_url": hit.detail_url}

    roads_expr = rule.chapterRoads.strip()
    try:
        if not roads_expr or roads_expr == "//":
            road_nodes: list[Any] = [doc]
        else:
            matched = doc.xpath(normalize_xpath(roads_expr))
            road_nodes = matched if isinstance(matched, list) else []
    except ET.XPathError:
        return "invalid_xpath", {"detail_url": hit.detail_url}

    element_roads = [node for node in road_nodes if isinstance(node, LH.HtmlElement)]
    if not element_roads:
        return "no_chapter_roads", {"detail_url": hit.detail_url}

    valid_road_count = 0
    total_episodes = 0
    first_episode_url = ""
    rel_ep_expr = make_relative_xpath(rule.chapterResult)

    for road in element_roads:
        try:
            ep_nodes = road.xpath(rel_ep_expr)
        except ET.XPathError:
            return "invalid_xpath", {"detail_url": hit.detail_url}
        if not isinstance(ep_nodes, list):
            continue

        road_ep_urls: list[str] = []
        for ep in ep_nodes:
            raw_href = ""
            if isinstance(ep, LH.HtmlElement):
                raw_href = (ep.get("href") or "").strip()
            elif isinstance(ep, str):
                raw_href = ep.strip()
            ep_url = resolve_url(str(resp.url) or hit.detail_url, raw_href)
            if ep_url:
                road_ep_urls.append(ep_url)

        if road_ep_urls:
            valid_road_count += 1
            total_episodes += len(road_ep_urls)
            if not first_episode_url:
                first_episode_url = road_ep_urls[0]

    if valid_road_count >= 1 and total_episodes >= 1:
        return "ok", {
            "mode": "xpath",
            "detail_url": hit.detail_url,
            "road_count": valid_road_count,
            "episode_count": total_episodes,
            "first_episode_url": first_episode_url,
        }

    return "no_episodes", {
        "mode": "xpath",
        "detail_url": hit.detail_url,
        "matched_roads": len(element_roads),
    }


async def _probe_chapter_api(
    rule: KazumiRule,
    hit: SearchHit,
    client: httpx.AsyncClient,
    limiter: RequestLimiter | None,
) -> tuple[str, dict[str, Any]]:
    cfg = rule.chapterApiConfig
    if cfg is None:
        return "missing_chapter_api_config", {"detail_url": hit.detail_url}

    resp = await _send_api_request(
        client,
        rule,
        cfg.request,
        {"@source": hit.raw_source},
        limiter,
    )
    if resp.status_code >= 400:
        return f"http_{resp.status_code}", {
            "status_code": resp.status_code,
            "detail_url": hit.detail_url,
        }

    try:
        payload = resp.json()
    except ValueError:
        return "invalid_json", {"detail_url": hit.detail_url}

    valid_road_count = 0
    total_episodes = 0
    first_episode_url = ""

    if cfg.format == "delimited":
        raw_blocks = _jsonpath_values(payload, cfg.roadEpisodesPath)
        raw_str = _scalar_to_str(raw_blocks[0]) if raw_blocks else ""
        if not raw_str:
            return "no_chapter_roads", {"mode": "api", "detail_url": hit.detail_url}

        roads = [r for r in raw_str.split(cfg.roadSeparator or "$$$") if r.strip()]
        if not roads:
            return "no_chapter_roads", {"mode": "api", "detail_url": hit.detail_url}

        for road_str in roads:
            ep_chunks = [e for e in road_str.split(cfg.episodeSeparator or "#") if e.strip()]
            road_eps: list[str] = []
            for chunk in ep_chunks:
                sep = cfg.fieldSeparator or "$"
                raw_ep = chunk.split(sep, 1)[1].strip() if sep in chunk else chunk.strip()
                if raw_ep:
                    road_eps.append(raw_ep)
            if road_eps:
                valid_road_count += 1
                total_episodes += len(road_eps)
                if not first_episode_url:
                    first_episode_url = _build_episode_page_url(
                        rule.baseURL,
                        cfg.episodePage,
                        hit.raw_source,
                        road_eps[0],
                    )
    else:
        roads = _jsonpath_values(payload, cfg.roadsPath or "$")
        if not roads:
            return "no_chapter_roads", {"mode": "api", "detail_url": hit.detail_url}

        for road_obj in roads:
            ep_items = (
                _jsonpath_values(road_obj, cfg.episodesPath)
                if cfg.episodesPath.strip()
                else [road_obj]
            )
            road_eps = []
            for ep_obj in ep_items:
                vals = (
                    _jsonpath_values(ep_obj, cfg.episodeUrlPath)
                    if cfg.episodeUrlPath.strip()
                    else [ep_obj]
                )
                raw_ep = _scalar_to_str(vals[0]) if vals else ""
                if raw_ep:
                    road_eps.append(raw_ep)
            if road_eps:
                valid_road_count += 1
                total_episodes += len(road_eps)
                if not first_episode_url:
                    first_episode_url = _build_episode_page_url(
                        rule.baseURL,
                        cfg.episodePage,
                        hit.raw_source,
                        road_eps[0],
                    )

    if valid_road_count >= 1 and total_episodes >= 1:
        return "ok", {
            "mode": "api",
            "detail_url": hit.detail_url,
            "road_count": valid_road_count,
            "episode_count": total_episodes,
            "first_episode_url": first_episode_url,
        }

    return "no_episodes", {"mode": "api", "detail_url": hit.detail_url}


async def probe_l3_chapter(
    rule: KazumiRule,
    l2: ProbeResult,
    hit: SearchHit | None,
    client: httpx.AsyncClient,
    *,
    limiter: RequestLimiter | None = None,
) -> ProbeResult:
    """L3 chapter: open first search hit's detail page/API and verify >=1 road with >=1 episode."""
    if l2.status != "passed" or hit is None:
        return ProbeResult(
            status="skipped",
            latency_ms=0.0,
            reason="upstream_failed",
            evidence={"depends_on": "L2"},
        )

    t0 = time.perf_counter()
    try:
        if rule.effective_chapter_mode == "api":
            reason, evidence = await _probe_chapter_api(rule, hit, client, limiter)
        else:
            reason, evidence = await _probe_chapter_xpath(rule, hit, client, limiter)
    except Exception as exc:
        latency = round((time.perf_counter() - t0) * 1000, 1)
        return ProbeResult(
            status="failed",
            latency_ms=latency,
            reason=classify_http_exception(exc),
            evidence={"detail_url": hit.detail_url, "error": str(exc)[:200]},
        )

    latency = round((time.perf_counter() - t0) * 1000, 1)
    status: ProbeStatus = "passed" if reason == "ok" else "failed"
    return ProbeResult(
        status=status,
        latency_ms=latency,
        reason=reason,
        evidence=evidence,
    )


async def probe_l4_playable(
    rule: KazumiRule,
    l3: ProbeResult,
    *,
    deep: bool = False,
    timeout_ms: int = 12000,
) -> ProbeResult:
    """L4 playable (optional --deep): intercept network requests for m3u8/mp4 without downloading media."""
    if not deep:
        return ProbeResult(status="skipped", latency_ms=0.0, reason="deep_disabled")

    if l3.status != "passed":
        return ProbeResult(
            status="skipped",
            latency_ms=0.0,
            reason="upstream_failed",
            evidence={"depends_on": "L3"},
        )

    episode_url = str(l3.evidence.get("first_episode_url") or "")
    if not episode_url:
        return ProbeResult(status="skipped", latency_ms=0.0, reason="no_episode_url")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ProbeResult(status="skipped", latency_ms=0.0, reason="playwright_not_installed")

    t0 = time.perf_counter()
    captured_streams: list[str] = []
    stream_found = asyncio.Event()
    ua = rule.userAgent.strip() if rule.userAgent and rule.userAgent.strip() else DEFAULT_USER_AGENT

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=ua, ignore_https_errors=True)
                page = await context.new_page()

                async def _handle_route(route: Any) -> None:
                    req_url = route.request.url
                    if MEDIA_STREAM_RE.search(req_url):
                        captured_streams.append(req_url)
                        stream_found.set()
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", _handle_route)
                goto_task = asyncio.create_task(
                    page.goto(episode_url, wait_until="domcontentloaded", timeout=timeout_ms)
                )
                try:
                    await asyncio.wait_for(stream_found.wait(), timeout=timeout_ms / 1000)
                except TimeoutError:
                    pass
                finally:
                    if not goto_task.done():
                        goto_task.cancel()
                    await asyncio.gather(goto_task, return_exceptions=True)
            finally:
                await browser.close()
    except Exception as exc:
        latency = round((time.perf_counter() - t0) * 1000, 1)
        return ProbeResult(
            status="failed",
            latency_ms=latency,
            reason="browser_error",
            evidence={"episode_url": episode_url, "error": str(exc)[:200]},
        )

    latency = round((time.perf_counter() - t0) * 1000, 1)
    if captured_streams:
        first_stream = captured_streams[0]
        stream_ext = "m3u8" if ".m3u8" in first_stream.lower() else "mp4"
        return ProbeResult(
            status="passed",
            latency_ms=latency,
            reason="ok",
            evidence={
                "episode_url": episode_url,
                "stream_type": stream_ext,
                "stream_host": urlsplit(first_stream).netloc,
            },
        )

    return ProbeResult(
        status="failed",
        latency_ms=latency,
        reason="no_media_stream",
        evidence={"episode_url": episode_url},
    )


async def probe_source(
    rule: KazumiRule,
    client: httpx.AsyncClient,
    *,
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    deep: bool = False,
    listed_in_index: bool = False,
    limiter: RequestLimiter | None = None,
) -> SourceProbeReport:
    """Run L1 -> L2 -> L3 (-> optional L4) sequentially for a single Kazumi rule."""
    l1 = await probe_l1_reachable(rule, client, limiter=limiter)
    l2, hit = await probe_l2_search(rule, l1, client, keywords=keywords, limiter=limiter)
    l3 = await probe_l3_chapter(rule, l2, hit, client, limiter=limiter)
    l4 = await probe_l4_playable(rule, l3, deep=deep)

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
        l1=l1,
        l2=l2,
        l3=l3,
        l4=l4,
    )
