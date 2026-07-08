from __future__ import annotations

import configparser
import json
import math
import re
import struct
from datetime import datetime
from pathlib import Path

from .models import (
    MarketConfig,
    QuoteRealtime,
    Security,
    SnapshotDiagnostics,
    StocknowRecordInspection,
    WatchlistEntry,
)
from .shared_read import read_bytes_shared

DEFAULT_THS_ROOT = Path(r"D:\同花顺软件\同花顺")
GB18030 = "gb18030"


class THSLocalAdapter:
    def __init__(self, root: Path = DEFAULT_THS_ROOT):
        self.root = Path(root)

    @property
    def hexin_exe(self) -> Path:
        return self.root / "hexin.exe"

    @property
    def stockname_dir(self) -> Path:
        return self.root / "stockname"

    @property
    def realtime_dir(self) -> Path:
        return self.root / "realtime"

    def validate(self) -> list[str]:
        missing: list[str] = []
        checks = [
            (self.root, "root"),
            (self.hexin_exe, "hexin.exe"),
            (self.stockname_dir, "stockname directory"),
            (self.realtime_dir, "realtime directory"),
        ]
        for path, label in checks:
            if not path.exists():
                missing.append(f"{label} missing: {path}")
        return missing

    def read_stocknames(self) -> list[Security]:
        securities: dict[tuple[str, str], Security] = {}
        for file_path in sorted(self.stockname_dir.glob("stockname_*.txt")):
            market_id = _market_id_from_stockname_file(file_path)
            text = read_bytes_shared(file_path).decode(GB18030, errors="replace")
            for line in text.splitlines():
                parsed = _parse_stockname_line(line, market_id, file_path)
                if parsed is None:
                    continue
                securities.setdefault((parsed.market_id, parsed.ths_code), parsed)
        return sorted(securities.values(), key=lambda item: (item.market_id, item.ths_code))

    def read_market_config(self) -> MarketConfig:
        path = self.realtime_dir / "market.txt"
        parser = configparser.ConfigParser()
        text = read_bytes_shared(path).decode(GB18030, errors="replace")
        parser.read_string(text)
        raw = dict(parser["Market"]) if parser.has_section("Market") else {}
        markets = {
            value: _market_dir_for_id(value)
            for key, value in raw.items()
            if key.lower().startswith("market")
        }
        return MarketConfig(markets=markets, raw=raw)

    def read_realtime_snapshots(self) -> list[SnapshotDiagnostics]:
        snapshots: list[SnapshotDiagnostics] = []
        for stock_file in sorted(self.realtime_dir.glob("*/stocknow.dat")):
            market = stock_file.parent.name
            snapshots.append(self.inspect_stocknow(stock_file, market))
        return snapshots

    def inspect_stocknow(self, path: Path, market: str) -> SnapshotDiagnostics:
        read_at = datetime.now()
        try:
            stat = path.stat()
            header = read_bytes_shared(path, 256)
        except OSError as exc:
            return SnapshotDiagnostics(
                source_file=path,
                market=market,
                read_at=read_at,
                file_mtime=None,
                file_size=0,
                status="read_error",
                format_version=None,
                header_hex="",
                message=str(exc),
            )

        version = _detect_stocknow_version(header)
        record_count = _stocknow_record_count(header)
        status = "unknown_format"
        message = "stocknow.dat header recognized; code records can be extracted, price fields are not implemented"
        if version is None:
            message = "stocknow.dat header is not recognized"
        elif record_count is not None:
            status = "code_only"
            message = f"stocknow.dat hd1.0 record_count={record_count}, record_len=546; price fields are not implemented"
        return SnapshotDiagnostics(
            source_file=path,
            market=market,
            read_at=read_at,
            file_mtime=datetime.fromtimestamp(stat.st_mtime),
            file_size=stat.st_size,
            status=status,
            format_version=version,
            header_hex=header[:64].hex(" "),
            message=message,
        )

    def read_stocknow_quotes(self, snapshot_ids_by_file: dict[Path, int]) -> list[QuoteRealtime]:
        securities = _index_securities_by_symbol_and_market(self.read_stocknames())
        market_ids_by_dir = self._market_ids_by_realtime_dir()
        quotes: list[QuoteRealtime] = []
        for stock_file in sorted(self.realtime_dir.glob("*/stocknow.dat")):
            market = stock_file.parent.name
            market_id = market_ids_by_dir.get(market)
            snapshot_id = snapshot_ids_by_file.get(stock_file)
            for _, _, record in _iter_stocknow_records(stock_file):
                symbol = _symbol_from_record(record)
                if symbol is None:
                    continue
                security = _find_security_for_symbol(securities, symbol, market_id)
                record_name = _name_from_stocknow_record(record)
                quotes.append(
                    QuoteRealtime(
                        symbol=symbol,
                        name=record_name or (security.name if security else None),
                        market=market,
                        latest_price=None,
                        pct_change=None,
                        volume=None,
                        amount=None,
                        source_snapshot_id=snapshot_id,
                    )
                )
        return quotes

    def inspect_symbol(self, symbol: str) -> list[StocknowRecordInspection]:
        securities = _index_securities_by_symbol_and_market(self.read_stocknames())
        market_ids_by_dir = self._market_ids_by_realtime_dir()
        inspections: list[StocknowRecordInspection] = []
        for stock_file in sorted(self.realtime_dir.glob("*/stocknow.dat")):
            market = stock_file.parent.name
            market_id = market_ids_by_dir.get(market)
            for record_index, record_offset, record in _iter_stocknow_records(stock_file):
                record_symbol = _symbol_from_record(record)
                if record_symbol != symbol:
                    continue
                security = _find_security_for_symbol(securities, symbol, market_id)
                inspections.append(
                    StocknowRecordInspection(
                        symbol=symbol,
                        market=market,
                        record_index=record_index,
                        record_offset=record_offset,
                        record_length=len(record),
                        name_from_record=_name_from_stocknow_record(record),
                        name_from_master=security.name if security else None,
                        first_bytes_hex=record[:160].hex(" "),
                        ascii_runs=_ascii_runs(record[:240]),
                        numeric_candidates=_numeric_candidates(record),
                    )
                )
        return inspections

    def capture_symbols(self, symbols: list[str]) -> list[dict[str, object]]:
        wanted = set(symbols)
        securities = _index_securities_by_symbol_and_market(self.read_stocknames())
        market_ids_by_dir = self._market_ids_by_realtime_dir()
        captures: list[dict[str, object]] = []
        for stock_file in sorted(self.realtime_dir.glob("*/stocknow.dat")):
            market = stock_file.parent.name
            market_id = market_ids_by_dir.get(market)
            for record_index, record_offset, record in _iter_stocknow_records(stock_file):
                symbol = _symbol_from_record(record)
                if symbol not in wanted:
                    continue
                security = _find_security_for_symbol(securities, symbol, market_id)
                captures.append(
                    {
                        "symbol": symbol,
                        "market": market,
                        "market_id": market_id,
                        "record_index": record_index,
                        "record_offset": record_offset,
                        "record_length": len(record),
                        "name_from_record": _name_from_stocknow_record(record),
                        "name_from_master": security.name if security else None,
                        "record_hex": record.hex(),
                    }
                )
        return captures

    def _market_ids_by_realtime_dir(self) -> dict[str, str]:
        path = self.realtime_dir / "market.txt"
        if not path.exists():
            return {"shase": "16", "sznse": "32"}
        text = read_bytes_shared(path).decode(GB18030, errors="replace")
        mapping: dict[str, str] = {}
        current_market_id: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            match = re.match(r"\[Market_(.+)\]", line)
            if match and "_" not in match.group(1):
                current_market_id = match.group(1)
                continue
            if current_market_id and line.lower().startswith("pathname="):
                dirname = line.split("=", 1)[1].strip().strip("\\/").lower()
                if dirname:
                    mapping[dirname] = current_market_id
        mapping.setdefault("shase", "16")
        mapping.setdefault("sznse", "32")
        return mapping

    def read_watchlists(self) -> list[WatchlistEntry]:
        entries: list[WatchlistEntry] = []
        user_dirs = [
            path
            for path in self.root.iterdir()
            if path.is_dir()
            and _looks_like_user_data_dir(path)
        ]
        for user_dir in user_dirs:
            for file_path in sorted(user_dir.rglob("*")):
                if not file_path.is_file() or file_path.suffix.lower() not in {".json", ".ini"}:
                    continue
                if _looks_sensitive(file_path):
                    continue
                entries.extend(_read_watchlist_file(file_path))
        return entries


def _market_id_from_stockname_file(path: Path) -> str:
    match = re.match(r"stockname_([^_]+)_", path.name)
    return match.group(1) if match else "unknown"


def _parse_stockname_line(line: str, market_id: str, source_file: Path) -> Security | None:
    if not line or line.startswith("[") or "=" not in line or "|" not in line:
        return None
    ths_code, payload = line.split("=", 1)
    name, symbol_part = payload.split("|", 1)
    symbol_candidate = symbol_part.split("@", 1)[0].strip()
    if re.fullmatch(r"[036]\d{5}", ths_code):
        symbol = ths_code
    elif re.fullmatch(r"[036]\d{5}", symbol_candidate):
        symbol = symbol_candidate
    else:
        return None
    if not ths_code or not symbol:
        return None
    return Security(
        ths_code=ths_code.strip(),
        symbol=symbol.strip(),
        name=name.strip(),
        market_id=market_id,
        source_file=source_file,
        security_type=_guess_security_type(ths_code.strip(), symbol.strip(), name.strip()),
    )


def _guess_security_type(ths_code: str, symbol: str, name: str) -> str:
    if "指数" in name or ths_code.startswith(("1A", "1B", "399")):
        return "index"
    if symbol.startswith(("60", "68", "00", "30", "83", "87", "92")):
        return "stock"
    if symbol.startswith(("51", "15", "16", "18")):
        return "fund"
    return "unknown"


def _market_dir_for_id(market_id: str) -> str:
    known = {
        "16": "shase",
        "32": "sznse",
        "176": "hk",
        "64": "quota",
        "120": "newindx",
        "144": "zzindx",
    }
    return known.get(market_id, market_id.lower())


def _detect_stocknow_version(header: bytes) -> str | None:
    if header.startswith(b"hd1.0\x00"):
        return "hd1.0"
    return None


def _stocknow_record_count(header: bytes) -> int | None:
    if len(header) < 10 or _detect_stocknow_version(header) is None:
        return None
    return struct.unpack_from("<I", header, 6)[0]


def _extract_stocknow_symbols(path: Path) -> list[str]:
    return [
        symbol
        for _, _, record in _iter_stocknow_records(path)
        if (symbol := _symbol_from_record(record)) is not None
    ]


def _iter_stocknow_records(path: Path) -> list[tuple[int, int, bytes]]:
    data = read_bytes_shared(path)
    if _detect_stocknow_version(data[:16]) is None:
        return []
    count = _stocknow_record_count(data[:16])
    if count is None:
        return []
    header_len = 528
    record_len = 546
    expected = header_len + count * record_len
    if len(data) < expected:
        count = max(0, (len(data) - header_len) // record_len)

    records: list[tuple[int, int, bytes]] = []
    for index in range(count):
        start = header_len + index * record_len
        records.append((index, start, data[start : start + record_len]))
    return records


def _symbol_from_record(record: bytes) -> str | None:
    match = re.search(rb"([036]\d{5})", record[:24])
    return match.group(1).decode("ascii") if match else None


def _name_from_stocknow_record(record: bytes) -> str | None:
    raw = record[10:38].split(b"\x00", 1)[0].rstrip(b"\xff")
    if not raw:
        return None
    return raw.decode(GB18030, errors="replace").strip() or None


def _ascii_runs(data: bytes) -> list[tuple[int, str]]:
    runs: list[tuple[int, str]] = []
    current = bytearray()
    start = 0
    for index, byte in enumerate(data):
        if 32 <= byte <= 126:
            if not current:
                start = index
            current.append(byte)
        else:
            if len(current) >= 3:
                runs.append((start, current.decode("ascii", errors="replace")))
            current.clear()
    if len(current) >= 3:
        runs.append((start, current.decode("ascii", errors="replace")))
    return runs


def _numeric_candidates(record: bytes) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    limit = min(len(record) - 4, 546)
    for offset in range(0, limit, 2):
        unsigned = struct.unpack_from("<I", record, offset)[0]
        signed = struct.unpack_from("<i", record, offset)[0]
        float_value = struct.unpack_from("<f", record, offset)[0]
        scaled: list[str] = []
        for scale in (100, 1000, 10000):
            value = unsigned / scale
            if 0.01 <= value <= 10000:
                scaled.append(f"u32/{scale}={value:g}")
        if scaled or (math.isfinite(float_value) and 0.01 <= abs(float_value) <= 10000):
            item: dict[str, object] = {"offset": offset}
            if scaled:
                item["u32"] = unsigned
                item["i32"] = signed
                item["scaled"] = scaled[:3]
            if math.isfinite(float_value) and 0.01 <= abs(float_value) <= 10000:
                item["f32"] = float_value
            candidates.append(item)
    return candidates[:80]


def _index_securities_by_symbol_and_market(securities: list[Security]) -> dict[tuple[str, str], list[Security]]:
    indexed: dict[tuple[str, str], list[Security]] = {}
    for security in securities:
        indexed.setdefault((security.symbol, security.market_id), []).append(security)
    return indexed


def _find_security_for_symbol(
    securities: dict[tuple[str, str], list[Security]], symbol: str, market_id: str | None
) -> Security | None:
    candidates = securities.get((symbol, market_id or ""), [])
    if not candidates:
        candidates = [
            security
            for (candidate_symbol, _), market_candidates in securities.items()
            if candidate_symbol == symbol
            for security in market_candidates
        ]
    if not candidates:
        return None
    stocks = [item for item in candidates if item.security_type == "stock"]
    return stocks[0] if stocks else candidates[0]


def _looks_sensitive(path: Path) -> bool:
    lowered = path.name.lower()
    return any(token in lowered for token in ("cookie", "login", "user", "passport", "token"))


def _looks_like_user_data_dir(path: Path) -> bool:
    markers = ("stockblock.ini", "weituonew.ini", "serverinfo.ini", "shortcut.ini", "message")
    return any((path / marker).exists() for marker in markers)


def _read_watchlist_file(path: Path) -> list[WatchlistEntry]:
    try:
        text = read_bytes_shared(path, 256 * 1024).decode(GB18030, errors="replace")
    except OSError:
        return []
    if "[encrypt_section]" in text or "encrypt" in text.lower():
        return [
            WatchlistEntry(
                name=path.stem,
                symbol=None,
                raw_value="encrypted_or_obfuscated",
                source_file=path,
                status="skipped",
            )
        ]
    if path.suffix.lower() == ".json":
        return _parse_json_watchlist(path, text)
    return _parse_ini_watchlist(path, text)


def _parse_json_watchlist(path: Path, text: str) -> list[WatchlistEntry]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    values = _walk_values(data)
    return [
        WatchlistEntry(path.stem, match.group(1), value, path, "parsed")
        for value in values
        if (match := re.search(r"\b([036]\d{5})\b", value))
    ]


def _parse_ini_watchlist(path: Path, text: str) -> list[WatchlistEntry]:
    entries: list[WatchlistEntry] = []
    for line in text.splitlines():
        for match in re.finditer(r"\b([036]\d{5})\b", line):
            entries.append(WatchlistEntry(path.stem, match.group(1), line.strip(), path, "parsed"))
    return entries


def _walk_values(value: object) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, nested in value.items():
            result.append(str(key))
            result.extend(_walk_values(nested))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_walk_values(item))
        return result
    return [str(value)]
