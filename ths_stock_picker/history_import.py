from __future__ import annotations

import csv
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path


HEADER_ALIASES = {
    "symbol": ("symbol", "code", "代码", "证券代码", "股票代码"),
    "trade_date": ("trade_date", "date", "日期", "交易日期", "时间"),
    "open": ("open", "开盘", "开盘价"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价"),
    "volume": ("volume", "vol", "成交量"),
    "amount": ("amount", "成交额", "成交金额"),
}


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    amount: float | None
    source_file: Path


def load_daily_bars_csv(path: Path, default_symbol: str | None = None) -> list[DailyBar]:
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        mapping = _map_headers(reader.fieldnames)
        bars: list[DailyBar] = []
        for row in reader:
            symbol = _clean_symbol(_get(row, mapping, "symbol") or default_symbol or "")
            trade_date = _clean_date(_get(row, mapping, "trade_date") or "")
            if not symbol or not trade_date:
                continue
            bars.append(
                DailyBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=_to_float(_get(row, mapping, "open")),
                    high=_to_float(_get(row, mapping, "high")),
                    low=_to_float(_get(row, mapping, "low")),
                    close=_to_float(_get(row, mapping, "close")),
                    volume=_to_float(_get(row, mapping, "volume")),
                    amount=_to_float(_get(row, mapping, "amount")),
                    source_file=path,
                )
            )
        return bars


def fetch_tencent_daily_bars(symbols: list[str], count: int = 80, timeout: float = 10.0) -> list[DailyBar]:
    bars: list[DailyBar] = []
    for symbol in symbols:
        bars.extend(fetch_tencent_daily_bars_one(symbol, count=count, timeout=timeout))
    return bars


def fetch_tencent_daily_bars_one(symbol: str, count: int = 80, timeout: float = 10.0) -> list[DailyBar]:
    market_symbol = _tencent_symbol(symbol)
    param = f"{market_symbol},day,,,{count},qfq"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode({"param": param})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    data = payload.get("data", {}).get(market_symbol, {})
    rows = data.get("qfqday") or data.get("day") or []
    bars: list[DailyBar] = []
    for row in rows:
        if len(row) < 6:
            continue
        bars.append(
            DailyBar(
                symbol=symbol,
                trade_date=str(row[0]),
                open=_to_float(str(row[1])),
                close=_to_float(str(row[2])),
                high=_to_float(str(row[3])),
                low=_to_float(str(row[4])),
                volume=_to_float(str(row[5])) * 100 if _to_float(str(row[5])) is not None else None,
                amount=None,
                source_file=Path(f"tencent://{market_symbol}/qfqday"),
            )
        )
    return bars


def _map_headers(headers: list[str]) -> dict[str, str]:
    normalized = {_normalize(header): header for header in headers}
    mapping: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            match = normalized.get(_normalize(alias))
            if match:
                mapping[canonical] = match
                break
    return mapping


def _get(row: dict[str, str], mapping: dict[str, str], canonical: str) -> str | None:
    header = mapping.get(canonical)
    if header is None:
        return None
    return row.get(header)


def _normalize(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "")


def _clean_symbol(value: str) -> str:
    value = value.strip()
    if "." in value:
        parts = value.split(".")
        value = parts[0] if parts[0].isdigit() else parts[-1]
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _clean_date(value: str) -> str:
    value = value.strip().replace("/", "-").replace(".", "-")
    if len(value) == 8 and value.isdigit():
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()
    return value


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip().replace(",", "")
    if value in {"", "--", "None", "nan"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _tencent_symbol(symbol: str) -> str:
    symbol = _clean_symbol(symbol)
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"
