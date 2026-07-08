from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Security:
    ths_code: str
    symbol: str
    name: str
    market_id: str
    source_file: Path
    security_type: str


@dataclass(frozen=True)
class MarketConfig:
    markets: dict[str, str]
    raw: dict[str, str]


@dataclass(frozen=True)
class SnapshotDiagnostics:
    source_file: Path
    market: str
    read_at: datetime
    file_mtime: datetime | None
    file_size: int
    status: str
    format_version: str | None
    header_hex: str
    message: str


@dataclass(frozen=True)
class QuoteRealtime:
    symbol: str
    name: str | None
    market: str
    latest_price: float | None
    pct_change: float | None
    volume: float | None
    amount: float | None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    market_cap: float | None = None
    float_market_cap: float | None = None
    turnover_rate: float | None = None
    board: str | None = None
    observed_at: str | None = None
    quote_source: str | None = None
    quote_status: str | None = None
    source_snapshot_id: int | None = None


@dataclass(frozen=True)
class WatchlistEntry:
    name: str
    symbol: str | None
    raw_value: str
    source_file: Path
    status: str


@dataclass(frozen=True)
class NewsItem:
    news_id: str
    title: str
    summary: str
    source: str
    event_time: str | None
    importance: int | None
    tags: str
    source_file: Path


@dataclass(frozen=True)
class StocknowRecordInspection:
    symbol: str
    market: str
    record_index: int
    record_offset: int
    record_length: int
    name_from_record: str | None
    name_from_master: str | None
    first_bytes_hex: str
    ascii_runs: list[tuple[int, str]]
    numeric_candidates: list[dict[str, object]]
