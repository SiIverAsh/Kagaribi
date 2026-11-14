"""Pydantic models for Kazumi source rules (api 1-9, XPath & JSON API modes)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ScalarValue = str | int | float | bool


class ApiRequestConfig(BaseModel):
    """HTTP request descriptor used by searchApiConfig and chapterApiConfig."""

    model_config = ConfigDict(extra="allow")

    method: str = "GET"
    url: str
    query: dict[str, ScalarValue] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    bodyType: str | None = None
    body: dict[str, Any] | str | None = None

    @field_validator("method", mode="before")
    @classmethod
    def _normalize_method(cls, value: Any) -> str:
        return str(value or "GET").upper()


class SearchApiConfig(BaseModel):
    """JSON API search rule configuration (Morph B / api 8-9)."""

    model_config = ConfigDict(extra="allow")

    request: ApiRequestConfig
    listPath: str
    namePath: str
    sourcePath: str
    sourceTemplate: str = ""


class EpisodePageConfig(BaseModel):
    """Playable episode page URL builder for API-based chapter configs."""

    model_config = ConfigDict(extra="allow")

    url: str
    query: dict[str, ScalarValue] = Field(default_factory=dict)


class ChapterApiConfig(BaseModel):
    """JSON API chapter/road configuration (delimited MacCMS or nested JSON)."""

    model_config = ConfigDict(extra="allow")

    request: ApiRequestConfig
    format: Literal["delimited", "nested"] | str = "nested"
    roadsPath: str = ""
    roadNamePath: str = ""
    episodesPath: str = ""
    episodeNamePath: str = ""
    episodeUrlPath: str = ""
    roadNamesPath: str = ""
    roadEpisodesPath: str = ""
    roadSeparator: str = "$$$"
    episodeSeparator: str = "#"
    fieldSeparator: str = "$"
    episodePage: EpisodePageConfig | None = None


class AntiCrawlerConfig(BaseModel):
    """Anti-crawler / CAPTCHA detection settings declared by a rule."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    captchaType: int = 1
    captchaImage: str = ""
    captchaInput: str = ""
    captchaButton: str = ""
    captchaDetectType: int = 1
    captchaDetectValue: str = ""
    captchaScript: str = ""


class RuleIndexEntry(BaseModel):
    """Aggregated entry inside KazumiRules index.json."""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    useNativePlayer: bool = True
    antiCrawlerEnabled: bool = False
    author: str = ""
    lastUpdate: int = 0

    @field_validator("version", mode="before")
    @classmethod
    def _coerce_version(cls, value: Any) -> str:
        return str(value)


class KazumiRule(BaseModel):
    """Unified Kazumi rule schema compatible with legacy XPath (api 1-8) and JSON API (api 8-9)."""

    model_config = ConfigDict(extra="allow")

    api: str
    type: str = "anime"
    name: str
    version: str = "1.0"
    deprecated: bool = False

    muliSources: bool = True
    adBlocker: bool = False
    useWebview: bool = True
    useNativePlayer: bool = True
    usePost: bool = False
    useLegacyParser: bool = False

    userAgent: str = ""
    referer: str = ""
    baseURL: str
    searchURL: str = ""

    searchMode: Literal["xpath", "api"] | str | None = None
    chapterMode: Literal["xpath", "api"] | str | None = None
    searchApiConfig: SearchApiConfig | None = None
    chapterApiConfig: ChapterApiConfig | None = None

    searchList: str = ""
    searchName: str = ""
    searchResult: str = ""
    chapterRoads: str = ""
    chapterResult: str = ""

    antiCrawlerConfig: AntiCrawlerConfig | None = None

    @field_validator("api", "version", mode="before")
    @classmethod
    def _coerce_str_fields(cls, value: Any) -> str:
        return str(value)

    @model_validator(mode="after")
    def _validate_mode_configs(self) -> KazumiRule:
        if not self.baseURL.strip():
            raise ValueError(f"Rule '{self.name}' has an empty baseURL")
        if self.effective_search_mode == "api" and self.searchApiConfig is None:
            raise ValueError(f"Rule '{self.name}' specifies searchMode='api' without searchApiConfig")
        if self.effective_chapter_mode == "api" and self.chapterApiConfig is None:
            raise ValueError(f"Rule '{self.name}' specifies chapterMode='api' without chapterApiConfig")
        return self

    @property
    def effective_search_mode(self) -> Literal["xpath", "api"]:
        if (self.searchMode or "").lower() == "api" or self.searchApiConfig is not None:
            return "api"
        return "xpath"

    @property
    def effective_chapter_mode(self) -> Literal["xpath", "api"]:
        if (self.chapterMode or "").lower() == "api" or self.chapterApiConfig is not None:
            return "api"
        return "xpath"

    @property
    def anti_crawler_enabled(self) -> bool:
        return bool(self.antiCrawlerConfig and self.antiCrawlerConfig.enabled)


def resolve_rules_path(raw_path: str | Path) -> Path:
    """Resolve local rules directory path, supporting POSIX /tmp/... paths on Windows."""
    path = Path(raw_path)
    if path.exists():
        return path
    raw_str = str(raw_path).replace("\\", "/")
    if os.name == "nt" and raw_str.startswith("/tmp/"):
        rel = raw_str.removeprefix("/tmp/")
        for candidate in (Path("C:/tmp") / rel, Path(tempfile.gettempdir()) / rel):
            if candidate.exists():
                return candidate
    return path


def load_rule(path: str | Path) -> KazumiRule:
    """Parse and validate a single Kazumi <Name>.json rule file."""
    file_path = Path(path)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    return KazumiRule.model_validate(data)


def load_index(path: str | Path) -> list[RuleIndexEntry]:
    """Parse KazumiRules index.json."""
    file_path = Path(path)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON list in {file_path}, got {type(data).__name__}")
    return [RuleIndexEntry.model_validate(item) for item in data]


def load_rules_from_dir(
    rules_dir: str | Path,
    *,
    only: str | None = None,
) -> tuple[list[KazumiRule], dict[str, RuleIndexEntry]]:
    """Load all <Name>.json rules (and optional index.json) from a directory."""
    resolved_dir = resolve_rules_path(rules_dir)
    if not resolved_dir.is_dir():
        raise FileNotFoundError(f"Rules directory not found: {rules_dir}")

    index_map: dict[str, RuleIndexEntry] = {}
    index_file = resolved_dir / "index.json"
    if index_file.is_file():
        for entry in load_index(index_file):
            index_map[entry.name.lower()] = entry

    only_lower = only.strip().lower() if only else None
    rules: list[KazumiRule] = []
    for json_path in sorted(resolved_dir.glob("*.json"), key=lambda p: p.name.lower()):
        if json_path.name.lower() == "index.json":
            continue
        rule = load_rule(json_path)
        if only_lower and rule.name.lower() != only_lower and json_path.stem.lower() != only_lower:
            continue
        idx_entry = index_map.get(rule.name.lower())
        if idx_entry and idx_entry.antiCrawlerEnabled and not rule.anti_crawler_enabled:
            if rule.antiCrawlerConfig is None:
                rule.antiCrawlerConfig = AntiCrawlerConfig(enabled=True)
            else:
                rule.antiCrawlerConfig.enabled = True
        rules.append(rule)

    return rules, index_map
