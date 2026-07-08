from __future__ import annotations

import csv
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    name: str
    latest_price: float
    pct_change: float
    volume: float
    amount: float
    open: float
    high: float
    low: float
    previous_close: float
    observed_at: str
    source: str
    market_cap: float | None = None
    float_market_cap: float | None = None
    turnover_rate: float | None = None
    board: str | None = None


def fetch_sina_observations(symbols: list[str], timeout: float = 10.0, batch_size: int = 80) -> list[QuoteObservation]:
    if not symbols:
        return []
    observations: list[QuoteObservation] = []
    for start in range(0, len(symbols), batch_size):
        observations.extend(_fetch_sina_batch(symbols[start : start + batch_size], timeout))
    return observations


def fetch_tencent_observations(symbols: list[str], timeout: float = 10.0, batch_size: int = 80) -> list[QuoteObservation]:
    if not symbols:
        return []
    observations: list[QuoteObservation] = []
    for start in range(0, len(symbols), batch_size):
        observations.extend(_fetch_tencent_batch(symbols[start : start + batch_size], timeout))
    return observations


def _fetch_tencent_batch(symbols: list[str], timeout: float) -> list[QuoteObservation]:
    query = ",".join(_sina_code(symbol) for symbol in symbols)
    url = f"https://qt.gtimg.cn/q={query}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read().decode("gb18030", errors="replace")
    return _parse_tencent_response(content)


def _fetch_sina_batch(symbols: list[str], timeout: float) -> list[QuoteObservation]:
    query = ",".join(_sina_code(symbol) for symbol in symbols)
    url = f"https://hq.sinajs.cn/list={query}"
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read().decode("gb18030", errors="replace")
    return _parse_sina_response(content)


def write_observations_csv(path: Path, observations: list[QuoteObservation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "symbol",
        "name",
        "latest_price",
        "pct_change",
        "volume",
        "amount",
        "open",
        "high",
        "low",
        "previous_close",
        "market_cap",
        "float_market_cap",
        "turnover_rate",
        "board",
        "observed_at",
        "source",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in observations:
            writer.writerow(
                {
                    "symbol": item.symbol,
                    "name": item.name,
                    "latest_price": item.latest_price,
                    "pct_change": item.pct_change,
                    "volume": item.volume,
                    "amount": item.amount,
                    "open": item.open,
                    "high": item.high,
                    "low": item.low,
                    "previous_close": item.previous_close,
                    "market_cap": item.market_cap,
                    "float_market_cap": item.float_market_cap,
                    "turnover_rate": item.turnover_rate,
                    "board": item.board,
                    "observed_at": item.observed_at,
                    "source": item.source,
                }
            )


def _sina_code(symbol: str) -> str:
    symbol = symbol.strip()
    if symbol.startswith(("sh", "sz")):
        return symbol
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _parse_sina_response(content: str) -> list[QuoteObservation]:
    observations: list[QuoteObservation] = []
    for match in re.finditer(r'var hq_str_(sh|sz)(\d{6})="([^"]*)";', content):
        market, symbol, payload = match.groups()
        fields = payload.split(",")
        if len(fields) < 32 or not fields[0]:
            continue
        previous_close = _to_float(fields[2])
        latest_price = _to_float(fields[3])
        pct_change = ((latest_price - previous_close) / previous_close * 100) if previous_close else 0.0
        observations.append(
            QuoteObservation(
                symbol=symbol,
                name=fields[0],
                open=_to_float(fields[1]),
                previous_close=previous_close,
                latest_price=latest_price,
                high=_to_float(fields[4]),
                low=_to_float(fields[5]),
                volume=_to_float(fields[8]),
                amount=_to_float(fields[9]),
                pct_change=pct_change,
                observed_at=f"{fields[30]} {fields[31]}",
                source=f"sina:{market}{symbol}",
                board=_board_for_symbol(symbol),
            )
        )
    return observations


def _parse_tencent_response(content: str) -> list[QuoteObservation]:
    observations: list[QuoteObservation] = []
    for match in re.finditer(r'v_(sh|sz)(\d{6})="([^"]*)";', content):
        market, symbol, payload = match.groups()
        fields = payload.split("~")
        if len(fields) < 58 or not fields[1]:
            continue
        observations.append(
            QuoteObservation(
                symbol=symbol,
                name=fields[1],
                latest_price=_to_float(fields[3]),
                previous_close=_to_float(fields[4]),
                open=_to_float(fields[5]),
                volume=_to_float(fields[36]) * 100,
                amount=_to_float(fields[57]) * 10000,
                pct_change=_to_float(fields[32]),
                high=_to_float(fields[33]),
                low=_to_float(fields[34]),
                observed_at=_format_tencent_time(fields[30]),
                source=f"tencent:{market}{symbol}",
                market_cap=_to_float(fields[45]) * 100_000_000,
                float_market_cap=_to_float(fields[44]) * 100_000_000,
                turnover_rate=_to_float(fields[46]),
                board=_board_for_symbol(symbol),
            )
        )
    return observations


def _to_float(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


def _format_tencent_time(value: str) -> str:
    if len(value) >= 14 and value[:14].isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}:{value[12:14]}"
    return value


def _board_for_symbol(symbol: str) -> str:
    if symbol.startswith(("300", "301")):
        return "创业板"
    if symbol.startswith("688"):
        return "科创板"
    if symbol.startswith(("600", "601", "603", "605")):
        return "沪主板"
    if symbol.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "其他"
