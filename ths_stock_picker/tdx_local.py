from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .history_import import DailyBar


DEFAULT_TDX_ROOT = Path(r"D:\new_tdx")
DAY_RECORD_SIZE = 32
DAY_RECORD_FORMAT = "IIIIIfII"


@dataclass(frozen=True)
class TDXDailyFile:
    market: str
    symbol: str
    path: Path
    records: int


@dataclass(frozen=True)
class TDXDailyStatus:
    root: Path
    stock_file_count: int
    index_file_count: int
    stock_latest_trade_date: str | None
    index_latest_trade_date: str | None
    latest_trade_date: str | None


def discover_tdx_daily_files(
    tdx_root: Path = DEFAULT_TDX_ROOT,
    symbols: list[str] | None = None,
    include_indices: bool = False,
    limit_symbols: int | None = None,
) -> list[TDXDailyFile]:
    root = Path(tdx_root)
    wanted = {_clean_symbol(symbol) for symbol in symbols or []}
    wanted_markets = {_clean_symbol(symbol): _preferred_market(symbol) for symbol in symbols or []}
    files: list[TDXDailyFile] = []
    for market in ("sh", "sz"):
        lday_dir = root / "vipdoc" / market / "lday"
        if not lday_dir.exists():
            continue
        for path in sorted(lday_dir.glob(f"{market}*.day")):
            symbol = path.stem[2:]
            if wanted and symbol not in wanted:
                continue
            preferred_market = wanted_markets.get(symbol)
            if preferred_market and preferred_market != market:
                continue
            is_stock = _is_a_share_symbol(market, symbol)
            is_index = _is_tdx_index(market, symbol)
            if not is_stock and not (include_indices and is_index):
                continue
            storage_symbol = symbol if is_stock else f"{market}{symbol}"
            records = path.stat().st_size // DAY_RECORD_SIZE
            files.append(TDXDailyFile(market=market, symbol=storage_symbol, path=path, records=records))
            if limit_symbols is not None and len(files) >= limit_symbols:
                return files
    return files


def inspect_tdx_daily_status(tdx_root: Path = DEFAULT_TDX_ROOT) -> TDXDailyStatus:
    stock_files = discover_tdx_daily_files(tdx_root=tdx_root)
    all_files = discover_tdx_daily_files(tdx_root=tdx_root, include_indices=True)
    stock_paths = {item.path for item in stock_files}
    index_files = [item for item in all_files if item.path not in stock_paths]
    stock_latest_trade_date = _latest_tdx_trade_date(stock_files)
    index_latest_trade_date = _latest_tdx_trade_date(index_files)
    latest_trade_date = max(
        (item for item in (stock_latest_trade_date, index_latest_trade_date) if item),
        default=None,
    )
    return TDXDailyStatus(
        root=Path(tdx_root),
        stock_file_count=len(stock_files),
        index_file_count=len(index_files),
        stock_latest_trade_date=stock_latest_trade_date,
        index_latest_trade_date=index_latest_trade_date,
        latest_trade_date=latest_trade_date,
    )


def load_tdx_daily_file(path: Path, symbol: str | None = None, start_date: str = "", end_date: str = "") -> list[DailyBar]:
    path = Path(path)
    parsed_symbol = _clean_storage_symbol(symbol or path.stem)
    bars: list[DailyBar] = []
    data = path.read_bytes()
    usable_length = len(data) - (len(data) % DAY_RECORD_SIZE)
    source = Path(f"tdx://lday/{path.parent.parent.name}/{path.name}")
    for offset in range(0, usable_length, DAY_RECORD_SIZE):
        record = data[offset : offset + DAY_RECORD_SIZE]
        raw_date, raw_open, raw_high, raw_low, raw_close, raw_amount, raw_volume, _ = struct.unpack(DAY_RECORD_FORMAT, record)
        trade_date = _format_tdx_date(raw_date)
        if not trade_date:
            continue
        if start_date and trade_date < start_date:
            continue
        if end_date and trade_date > end_date:
            continue
        bars.append(
            DailyBar(
                symbol=parsed_symbol,
                trade_date=trade_date,
                open=raw_open / 100.0,
                high=raw_high / 100.0,
                low=raw_low / 100.0,
                close=raw_close / 100.0,
                volume=float(raw_volume),
                amount=float(round(raw_amount)),
                source_file=source,
            )
        )
    return bars


def load_tdx_daily_bars(
    tdx_root: Path = DEFAULT_TDX_ROOT,
    symbols: list[str] | None = None,
    include_indices: bool = False,
    limit_symbols: int | None = None,
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[DailyBar], list[TDXDailyFile]]:
    files = discover_tdx_daily_files(
        tdx_root=tdx_root,
        symbols=symbols,
        include_indices=include_indices,
        limit_symbols=limit_symbols,
    )
    bars: list[DailyBar] = []
    for item in files:
        bars.extend(load_tdx_daily_file(item.path, symbol=item.symbol, start_date=start_date, end_date=end_date))
    return bars, files


def _format_tdx_date(raw_date: int) -> str:
    text = str(raw_date)
    if len(text) != 8 or not text.isdigit():
        return ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _latest_tdx_trade_date(files: list[TDXDailyFile]) -> str | None:
    latest = ""
    for item in files:
        if item.records < 1:
            continue
        with item.path.open("rb") as handle:
            handle.seek((item.records - 1) * DAY_RECORD_SIZE)
            raw_date = struct.unpack("I", handle.read(4))[0]
        trade_date = _format_tdx_date(raw_date)
        if trade_date > latest:
            latest = trade_date
    return latest or None


def _clean_symbol(value: str) -> str:
    text = str(value).lower().strip()
    if text.startswith(("sh", "sz")):
        text = text[2:]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _clean_storage_symbol(value: str) -> str:
    text = str(value).lower().strip()
    if text.startswith(("sh", "sz")):
        market = text[:2]
        symbol = _clean_symbol(text[2:])
        if _is_a_share_symbol(market, symbol):
            return symbol
        return f"{market}{symbol}"
    return _clean_symbol(text)


def _preferred_market(value: str) -> str | None:
    text = str(value).lower().strip()
    if text.startswith("sh"):
        return "sh"
    if text.startswith("sz"):
        return "sz"
    symbol = _clean_symbol(text)
    if symbol.startswith(("6", "5", "9")):
        return "sh"
    if symbol.startswith(("0", "1", "2", "3")):
        return "sz"
    return None


def _is_a_share_symbol(market: str, symbol: str) -> bool:
    if market == "sh":
        return symbol.startswith(("600", "601", "603", "605", "688"))
    if market == "sz":
        return symbol.startswith(("000", "001", "002", "003", "300", "301"))
    return False


def _is_tdx_index(market: str, symbol: str) -> bool:
    if market == "sh":
        return symbol.startswith(("000", "880", "899"))
    if market == "sz":
        return symbol.startswith("399")
    return False
