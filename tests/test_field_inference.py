from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from ths_stock_picker.field_inference import (
    compare_capture_payloads,
    decode_record_values,
    load_observations,
    match_observations,
    summarize_matches,
)


class FieldInferenceTests(unittest.TestCase):
    def test_decode_record_values_includes_scaled_u32(self) -> None:
        record = bytearray(64)
        struct.pack_into("<I", record, 20, 1234)

        values = decode_record_values(bytes(record))

        self.assertTrue(
            any(item.offset == 20 and item.kind == "u32" and item.scale == 100 and item.value == 12.34 for item in values)
        )

    def test_compare_capture_payloads_reports_changed_offsets(self) -> None:
        before = {"records": [{"symbol": "600000", "market": "shase", "record_hex": "000102"}]}
        after = {"records": [{"symbol": "600000", "market": "shase", "record_hex": "000902"}]}

        rows = compare_capture_payloads(before, after)

        self.assertEqual(rows[0]["changed_byte_count"], 1)
        self.assertEqual(rows[0]["changed_offsets"], [1])

    def test_match_observations_summarizes_candidate_offsets(self) -> None:
        record = bytearray(64)
        struct.pack_into("<I", record, 20, 1234)
        captures = [{"symbol": "600000", "market": "shase", "record_hex": bytes(record).hex()}]
        observations = [{"symbol": "600000", "latest_price": "12.34"}]

        matches = match_observations(captures, observations, ["latest_price"], 0.0001)
        summary = summarize_matches(matches)

        self.assertTrue(any(item["offset"] == 20 and item["scale"] == 100 for item in summary["latest_price"]))

    def test_load_observations_reads_utf8_sig_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.csv"
            path.write_text("symbol,latest_price\n600000,12.34\n", encoding="utf-8-sig")

            rows = load_observations(path)

            self.assertEqual(rows[0]["symbol"], "600000")
