"""Mocked HTTP tests for L1-L3 probes and async runner using respx (zero real network)."""

from __future__ import annotations

import asyncio

import httpx
import respx

from kagaribi.probe import RequestLimiter, probe_source
from kagaribi.runner import run_probes
from kagaribi.schema import KazumiRule


def _make_api5_rule(
    name: str = "MockXPath",
    base_url: str = "https://xpath-anime.test/",
    *,
    user_agent: str = "MockBot/1.0",
    anti_crawler: bool = False,
) -> KazumiRule:
    return KazumiRule.model_validate(
        {
            "api": "5",
            "type": "anime",
            "name": name,
            "version": "1.6",
            "baseURL": base_url,
            "userAgent": user_agent,
            "searchURL": f"{base_url.rstrip('/')}/search?query=@keyword",
            "searchList": "//div[@class='results']/div[@class='card']",
            "searchName": "//div/h5/a",
            "searchResult": "//div/h5/a",
            "chapterRoads": "//div[@class='roads']/div[@class='road']",
            "chapterResult": "//ul/li/a",
            "antiCrawlerConfig": {"enabled": anti_crawler},
        }
    )


def _make_api9_rule(
    name: str = "MockJsonApi",
    base_url: str = "https://json-anime.test/",
) -> KazumiRule:
    return KazumiRule.model_validate(
        {
            "api": "9",
            "name": name,
            "version": "2.4",
            "baseURL": base_url,
            "searchMode": "api",
            "chapterMode": "xpath",
            "searchApiConfig": {
                "request": {
                    "method": "GET",
                    "url": f"{base_url.rstrip('/')}/index.php/ajax/suggest",
                    "query": {"mid": "1", "wd": "@keyword"},
                },
                "listPath": "$.list[*]",
                "namePath": "$.name",
                "sourcePath": "$.id",
                "sourceTemplate": f"{base_url.rstrip('/')}/vod/detail/id/@source.html",
            },
            "chapterRoads": "//div[@class*='module-player-list']",
            "chapterResult": ".//a[contains(@href,'/play/')]",
        }
    )


async def test_api5_xpath_probe_full_pass() -> None:
    rule = _make_api5_rule()
    search_html = """
    <html><body>
      <div class="results">
        <div class="card"><div><h5><a href="/detail/101">海贼王 TV</a></h5></div></div>
        <div class="card"><div><h5><a href="/detail/102">海贼王 剧场版</a></h5></div></div>
      </div>
    </body></html>
    """
    detail_html = """
    <html><body>
      <div class="roads">
        <div class="road"><ul><li><a href="/play/101-1">第01集</a></li><li><a href="/play/101-2">第02集</a></li></ul></div>
        <div class="road"><ul><li><a href="/play/101-b1">备用01集</a></li></ul></div>
      </div>
    </body></html>
    """

    with respx.mock(assert_all_mocked=True) as router:
        home_route = router.get("https://xpath-anime.test/").mock(
            return_value=httpx.Response(200, text="<html><body>Welcome Anime Portal</body></html>")
        )
        router.get("https://xpath-anime.test/search").mock(
            return_value=httpx.Response(200, text=search_html)
        )
        router.get("https://xpath-anime.test/detail/101").mock(
            return_value=httpx.Response(200, text=detail_html)
        )

        async with httpx.AsyncClient() as client:
            report = await probe_source(
                rule,
                client,
                limiter=RequestLimiter(per_host_limit=2, jitter_range=(0.0, 0.0)),
            )

        assert home_route.called
        sent_ua = home_route.calls[0].request.headers["user-agent"]
        assert sent_ua == "MockBot/1.0"

    assert report.badge == "🟢"
    assert report.l1.status == "passed"
    assert report.l2.status == "passed"
    assert report.l2.evidence["result_count"] == 2
    assert report.l2.evidence["first_name"] == "海贼王 TV"
    assert report.l2.evidence["detail_url"] == "https://xpath-anime.test/detail/101"
    assert report.l3.status == "passed"
    assert report.l3.evidence["road_count"] == 2
    assert report.l3.evidence["episode_count"] == 3
    assert report.l4.status == "skipped"
    assert report.l4.reason == "deep_disabled"


async def test_api9_json_search_multi_keyword_union() -> None:
    rule = _make_api9_rule()
    detail_html = """
    <html><body>
      <div class="box module-player-list">
        <a href="/play/42-1-1.html">第01话</a>
        <a href="/play/42-1-2.html">第02话</a>
      </div>
    </body></html>
    """

    def _suggest_handler(request: httpx.Request) -> httpx.Response:
        wd = request.url.params.get("wd")
        if wd == "EVA":
            return httpx.Response(
                200,
                json={"list": [{"id": 42, "name": "新世纪福音战士 EVA"}]},
            )
        return httpx.Response(200, json={"list": []})

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://json-anime.test/").mock(
            return_value=httpx.Response(200, text="<html><body>JSON Portal</body></html>")
        )
        router.get("https://json-anime.test/index.php/ajax/suggest").mock(
            side_effect=_suggest_handler
        )
        router.get("https://json-anime.test/vod/detail/id/42.html").mock(
            return_value=httpx.Response(200, text=detail_html)
        )

        async with httpx.AsyncClient() as client:
            report = await probe_source(
                rule,
                client,
                keywords=("海贼王", "EVA"),
                limiter=RequestLimiter(per_host_limit=2, jitter_range=(0.0, 0.0)),
            )

    assert report.badge == "🟢"
    assert report.l2.status == "passed"
    assert report.l2.evidence["keyword"] == "EVA"
    assert report.l2.evidence["tried_keywords"] == ["海贼王", "EVA"]
    assert report.l3.status == "passed"
    assert report.l3.evidence["episode_count"] == 2


async def test_api_delimited_chapter_mode() -> None:
    rule = KazumiRule.model_validate(
        {
            "api": "9",
            "name": "MockDelimited",
            "version": "1.0",
            "baseURL": "https://delimited-anime.test/",
            "searchMode": "api",
            "chapterMode": "api",
            "searchApiConfig": {
                "request": {
                    "method": "GET",
                    "url": "https://delimited-anime.test/api/list",
                    "query": {"wd": "@keyword"},
                },
                "listPath": "$.rows[*]",
                "namePath": "$.vod_name",
                "sourcePath": "$.vod_id",
            },
            "chapterApiConfig": {
                "request": {
                    "method": "GET",
                    "url": "https://delimited-anime.test/api/detail",
                    "query": {"vod_id": "@source"},
                },
                "format": "delimited",
                "roadNamesPath": "$.info.play_from",
                "roadEpisodesPath": "$.info.play_url",
                "roadSeparator": "$$$",
                "episodeSeparator": "#",
                "fieldSeparator": "$",
                "episodePage": {
                    "url": "https://delimited-anime.test/player",
                    "query": {"url": "@episodeUrl"},
                },
            },
        }
    )

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://delimited-anime.test/").mock(
            return_value=httpx.Response(200, text="<html>OK</html>")
        )
        router.get("https://delimited-anime.test/api/list").mock(
            return_value=httpx.Response(
                200,
                json={"rows": [{"vod_id": "900", "vod_name": "海贼王"}]},
            )
        )
        router.get("https://delimited-anime.test/api/detail").mock(
            return_value=httpx.Response(
                200,
                json={
                    "info": {
                        "play_from": "lineA$$$lineB",
                        "play_url": "第01集$ep1.m3u8#第02集$ep2.m3u8$$$第01集$ep1_b.m3u8",
                    }
                },
            )
        )

        async with httpx.AsyncClient() as client:
            report = await probe_source(rule, client, keywords=("海贼王",))

    assert report.badge == "🟢"
    assert report.l3.status == "passed"
    assert report.l3.evidence["road_count"] == 2
    assert report.l3.evidence["episode_count"] == 3
    assert "url=ep1.m3u8" in report.l3.evidence["first_episode_url"]


async def test_parked_domain_detection_and_size_threshold() -> None:
    parked_rule = _make_api5_rule(name="ParkedSite", base_url="https://parked-site.test/")
    large_normal_rule = _make_api5_rule(name="NormalLargeSite", base_url="https://large-site.test/")

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://parked-site.test/").mock(
            return_value=httpx.Response(
                200,
                text="<html><body><h1>本域名正在出售 - Parked by GoDaddy</h1></body></html>",
            )
        )
        large_body = "<html><body><div>周边出售公告</div>" + ("<p>番剧列表正常内容</p>" * 120) + "</body></html>"
        router.get("https://large-site.test/").mock(
            return_value=httpx.Response(200, text=large_body)
        )
        router.get("https://large-site.test/search").mock(
            return_value=httpx.Response(200, text="<html><body><div class='results'></div></body></html>")
        )

        async with httpx.AsyncClient() as client:
            reports, _ = await run_probes(
                [parked_rule, large_normal_rule],
                client=client,
                jitter_range=(0.0, 0.0),
                keywords=("EVA",),
            )

    by_name = {r.name: r for r in reports}
    parked_res = by_name["ParkedSite"]
    assert parked_res.l1.status == "failed"
    assert parked_res.l1.reason == "parked_domain"
    assert parked_res.l2.status == "skipped"
    assert parked_res.l3.status == "skipped"
    assert parked_res.badge == "🔴"
    assert parked_res.attempts == 1

    normal_res = by_name["NormalLargeSite"]
    assert normal_res.l1.status == "passed"
    assert normal_res.l2.status == "failed"
    assert normal_res.l2.reason == "empty_search_results"


async def test_empty_search_results_cascades_l3_skip() -> None:
    rule = _make_api9_rule(name="EmptySearchSite", base_url="https://empty-search.test/")

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://empty-search.test/").mock(
            return_value=httpx.Response(200, text="<html><body>Alive Home</body></html>")
        )
        suggest_route = router.get("https://empty-search.test/index.php/ajax/suggest").mock(
            return_value=httpx.Response(200, json={"list": []})
        )

        async with httpx.AsyncClient() as client:
            report = await probe_source(
                rule,
                client,
                keywords=("海贼王", "EVA"),
                limiter=RequestLimiter(jitter_range=(0.0, 0.0)),
            )

        assert suggest_route.call_count == 2

    assert report.l1.status == "passed"
    assert report.l2.status == "failed"
    assert report.l2.reason == "empty_search_results"
    assert report.l3.status == "skipped"
    assert report.l3.reason == "upstream_failed"
    assert report.badge == "🟡"


async def test_timeout_and_retry_once_behavior() -> None:
    flaky_rule = _make_api9_rule(name="FlakyTimeout", base_url="https://flaky-timeout.test/")
    dead_rule = _make_api9_rule(name="AlwaysTimeout", base_url="https://always-timeout.test/")

    flaky_calls = 0

    def _flaky_home(_req: httpx.Request) -> httpx.Response:
        nonlocal flaky_calls
        flaky_calls += 1
        if flaky_calls == 1:
            raise httpx.ReadTimeout("first attempt timed out")
        return httpx.Response(200, text="<html>Recovered</html>")

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://flaky-timeout.test/").mock(side_effect=_flaky_home)
        router.get("https://flaky-timeout.test/index.php/ajax/suggest").mock(
            return_value=httpx.Response(200, json={"list": [{"id": "7", "name": "EVA"}]})
        )
        router.get("https://flaky-timeout.test/vod/detail/id/7.html").mock(
            return_value=httpx.Response(
                200,
                text="<div class='module-player-list'><a href='/play/7-1.html'>01</a></div>",
            )
        )
        dead_route = router.get("https://always-timeout.test/").mock(
            side_effect=httpx.ConnectTimeout("connection timed out")
        )

        async with httpx.AsyncClient() as client:
            reports, _ = await run_probes(
                [flaky_rule, dead_rule],
                client=client,
                retries=1,
                jitter_range=(0.0, 0.0),
                keywords=("EVA",),
            )

        assert dead_route.call_count == 2

    by_name = {r.name: r for r in reports}
    assert by_name["FlakyTimeout"].attempts == 2
    assert by_name["FlakyTimeout"].badge == "🟢"

    assert by_name["AlwaysTimeout"].attempts == 2
    assert by_name["AlwaysTimeout"].l1.status == "failed"
    assert by_name["AlwaysTimeout"].l1.reason == "timeout"
    assert by_name["AlwaysTimeout"].l2.status == "skipped"
    assert by_name["AlwaysTimeout"].badge == "🔴"


async def test_anti_crawler_marks_skipped_not_failed() -> None:
    rule = _make_api5_rule(
        name="CaptchaProtected",
        base_url="https://captcha-anime.test/",
        anti_crawler=True,
    )

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://captcha-anime.test/").mock(
            return_value=httpx.Response(200, text="<html><body>Home OK</body></html>")
        )

        async with httpx.AsyncClient() as client:
            report = await probe_source(rule, client)

    assert report.l1.status == "passed"
    assert report.l2.status == "skipped"
    assert report.l2.reason == "anti_crawler"
    assert report.l3.status == "skipped"
    assert report.badge == "🟡"


async def test_per_host_concurrency_cap_is_enforced() -> None:
    rules = [
        _make_api5_rule(name=f"SharedHost{i}", base_url="https://shared-host.test/", anti_crawler=True)
        for i in range(5)
    ]
    in_flight = 0
    max_observed = 0

    async def _slow_home(_req: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_observed
        in_flight += 1
        max_observed = max(max_observed, in_flight)
        await asyncio.sleep(0.03)
        in_flight -= 1
        return httpx.Response(200, text="<html>Shared Host</html>")

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://shared-host.test/").mock(side_effect=_slow_home)

        async with httpx.AsyncClient() as client:
            reports, _ = await run_probes(
                rules,
                client=client,
                concurrency=8,
                per_host_concurrency=2,
                jitter_range=(0.0, 0.0),
            )

    assert len(reports) == 5
    assert max_observed <= 2


async def test_xpath_post_search_and_dot_result_and_root_roads() -> None:
    rule = KazumiRule.model_validate(
        {
            "api": "4",
            "name": "PostAndDotXPath",
            "version": "1.0",
            "usePost": True,
            "baseURL": "https://post-xpath.test/",
            "searchURL": "https://post-xpath.test/search.php?searchword=@keyword",
            "searchList": "//section[@class='cards']/a",
            "searchName": "//span/text()",
            "searchResult": ".",
            "chapterRoads": "//",
            "chapterResult": "//ul[@class='eps']/li/a",
        }
    )

    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://post-xpath.test/").mock(
            return_value=httpx.Response(200, text="<html>Home</html>")
        )
        post_route = router.post("https://post-xpath.test/search.php").mock(
            return_value=httpx.Response(
                200,
                text="<section class='cards'><a href='/anime/99'><span>海贼王 特别篇</span></a></section>",
            )
        )
        router.get("https://post-xpath.test/anime/99").mock(
            return_value=httpx.Response(
                200,
                text="<div><ul class='eps'><li><a href='/watch/99-1'>EP1</a></li></ul></div>",
            )
        )

        async with httpx.AsyncClient() as client:
            report = await probe_source(rule, client, keywords=("海贼王",))

        assert post_route.called
        assert "searchword=" in post_route.calls[0].request.content.decode("utf-8")

    assert report.badge == "🟢"
    assert report.l2.evidence["first_name"] == "海贼王 特别篇"
    assert report.l3.evidence["road_count"] == 1
    assert report.l3.evidence["episode_count"] == 1

