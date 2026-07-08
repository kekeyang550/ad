from __future__ import annotations

import csv
import json
import math
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SCALES = (1, 10, 100, 1000, 10000, 100000)


@dataclass(frozen=True)
class DecodedValue:
    offset: int
    kind: str
    scale: int | None
    value: float


def decode_record_values(record: bytes) -> list[DecodedValue]:
    values: list[DecodedValue] = []
    for offset in range(0, max(0, len(record) - 4), 1):
        u32 = struct.unpack_from("<I", record, offset)[0]
        i32 = struct.unpack_from("<i", record, offset)[0]
        f32 = struct.unpack_from("<f", record, offset)[0]
        for scale in SCALES:
            scaled_u32 = u32 / scale
            if 0 <= scaled_u32 <= 10_000_000:
                values.append(DecodedValue(offset, "u32", scale, scaled_u32))
            scaled_i32 = i32 / scale
            if -1_000_000 <= scaled_i32 <= 10_000_000:
                values.append(DecodedValue(offset, "i32", scale, scaled_i32))
        if math.isfinite(f32) and -1_000_000 <= f32 <= 10_000_000:
            values.append(DecodedValue(offset, "f32", None, f32))
    return values


def load_observations(path: Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def match_observations(
    captures: list[dict[str, Any]],
    observations: list[dict[str, str]],
    fields: list[str],
    tolerance: float,
) -> dict[str, list[dict[str, Any]]]:
    by_symbol = {item["symbol"]: item for item in captures}
    matches: dict[str, list[dict[str, Any]]] = {field: [] for field in fields}
    for observation in observations:
        symbol = observation.get("symbol", "").strip()
        capture = by_symbol.get(symbol)
        if not capture:
            continue
        record = bytes.fromhex(capture["record_hex"])
        decoded = decode_record_values(record)
        for field in fields:
            raw_expected = observation.get(field)
            if raw_expected in (None, ""):
                continue
            try:
                expected = float(raw_expected)
            except ValueError:
                continue
            for candidate in decoded:
                if abs(candidate.value - expected) <= tolerance:
                    matches[field].append(
                        {
                            "symbol": symbol,
                            "offset": candidate.offset,
                            "kind": candidate.kind,
                            "scale": candidate.scale,
                            "value": candidate.value,
                            "expected": expected,
                            "delta": candidate.value - expected,
                        }
                    )
    return matches


def summarize_matches(matches: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    summary: dict[str, list[dict[str, Any]]] = {}
    for field, rows in matches.items():
        grouped: dict[tuple[int, str, int | None], dict[str, Any]] = {}
        for row in rows:
            key = (int(row["offset"]), str(row["kind"]), row["scale"])
            grouped.setdefault(
                key,
                {
                    "offset": key[0],
                    "kind": key[1],
                    "scale": key[2],
                    "match_count": 0,
                    "symbols": [],
                    "max_abs_delta": 0.0,
                },
            )
            item = grouped[key]
            item["match_count"] += 1
            item["symbols"].append(row["symbol"])
            item["max_abs_delta"] = max(item["max_abs_delta"], abs(float(row["delta"])))
        summary[field] = sorted(
            grouped.values(),
            key=lambda item: (-item["match_count"], item["max_abs_delta"], item["offset"]),
        )
    return summary


def write_capture(path: Path, captures: list[dict[str, Any]]) -> None:
    payload = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "records": captures,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_capture(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_capture_payloads(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before_by_key = {
        (item["symbol"], item["market"]): bytes.fromhex(item["record_hex"])
        for item in before.get("records", [])
    }
    after_by_key = {
        (item["symbol"], item["market"]): bytes.fromhex(item["record_hex"])
        for item in after.get("records", [])
    }
    rows: list[dict[str, Any]] = []
    for key, before_record in before_by_key.items():
        after_record = after_by_key.get(key)
        if after_record is None:
            continue
        changed_offsets = [
            index
            for index, (left, right) in enumerate(zip(before_record, after_record, strict=False))
            if left != right
        ]
        rows.append(
            {
                "symbol": key[0],
                "market": key[1],
                "changed_byte_count": len(changed_offsets),
                "changed_offsets": changed_offsets[:200],
            }
        )
    return rows
