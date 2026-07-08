from __future__ import annotations

import struct
import unittest
from pathlib import Path

from ths_stock_picker.ths_local import THSLocalAdapter


def make_fake_ths(root: Path) -> Path:
    ths = root / "ths"
    (ths / "stockname").mkdir(parents=True)
    (ths / "realtime" / "shase").mkdir(parents=True)
    (ths / "realtime" / "sznse").mkdir(parents=True)
    (ths / "user_a").mkdir(parents=True)
    (ths / "hexin.exe").write_bytes(b"")
    (ths / "stockname" / "stockname_16_0.txt").write_bytes(
        "\n".join(
            [
                "[name_16_16]",
                "ConfigVer=20260708_2552009064",
                "1A0001=上证指数|000001@s",
                "600000=浦发银行|浦发银行@f",
            ]
        ).encode("gb18030")
    )
    (ths / "stockname" / "stockname_32_0.txt").write_bytes(
        "\n".join(
            [
                "[name_32_32]",
                "000001=平安银行|平安银行@f",
            ]
        ).encode("gb18030")
    )
    (ths / "realtime" / "market.txt").write_bytes(
        (
            "[Market]\r\nTotal=2\r\nMarket0=16\r\nMarket1=32\r\n"
            "[Market_16]\r\nPathName=shase\\\r\n"
            "[Market_32]\r\nPathName=sznse\\\r\n"
        ).encode("ascii")
    )
    header = bytearray(528)
    header[:6] = b"hd1.0\x00"
    struct.pack_into("<I", header, 6, 1)
    record = bytearray(546)
    record[1:7] = b"600000"
    record[10:18] = "浦发银行".encode("gb18030")
    (ths / "realtime" / "shase" / "stocknow.dat").write_bytes(bytes(header + record))
    sz_header = bytearray(528)
    sz_header[:6] = b"hd1.0\x00"
    struct.pack_into("<I", sz_header, 6, 1)
    sz_record = bytearray(546)
    sz_record[0:7] = b"!000001"
    sz_record[10:18] = "平安银行".encode("gb18030")
    (ths / "realtime" / "sznse" / "stocknow.dat").write_bytes(bytes(sz_header + sz_record))
    (ths / "user_a" / "watch.json").write_text('{"watch": ["600000", "000001"]}', encoding="utf-8")
    (ths / "user_a" / "stockblock.ini").write_text(
        "[encrypt_section]\nabc=1\n", encoding="utf-8"
    )
    return ths


class THSLocalAdapterTests(unittest.TestCase):
    def test_validate_and_parse_stocknames(self) -> None:
        with self.subTest("stockname parsing"):
            import tempfile

            with tempfile.TemporaryDirectory() as temp_dir:
                adapter = THSLocalAdapter(make_fake_ths(Path(temp_dir)))

                self.assertEqual(adapter.validate(), [])
                securities = adapter.read_stocknames()

                names = {item.symbol: item.name for item in securities}
                self.assertEqual(names["000001"], "平安银行")
                self.assertEqual(names["600000"], "浦发银行")

    def test_market_config_and_unknown_realtime_snapshots(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = THSLocalAdapter(make_fake_ths(Path(temp_dir)))

            config = adapter.read_market_config()
            snapshots = adapter.read_realtime_snapshots()

            self.assertEqual(config.markets["16"], "shase")
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(any(item.format_version == "hd1.0" for item in snapshots))
            self.assertTrue(all(item.status == "code_only" for item in snapshots))

    def test_extracts_code_only_quotes_without_prices(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = THSLocalAdapter(make_fake_ths(Path(temp_dir)))
            snapshots = adapter.read_realtime_snapshots()
            snapshot_ids = {snapshot.source_file: index + 1 for index, snapshot in enumerate(snapshots)}

            quotes = adapter.read_stocknow_quotes(snapshot_ids)

            self.assertEqual(len(quotes), 2)
            self.assertEqual(quotes[0].symbol, "600000")
            self.assertEqual(quotes[0].name, "浦发银行")
            self.assertIsNone(quotes[0].latest_price)

    def test_inspect_symbol_reads_record_name_and_offset(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = THSLocalAdapter(make_fake_ths(Path(temp_dir)))

            inspections = adapter.inspect_symbol("600000")

            self.assertEqual(len(inspections), 1)
            self.assertEqual(inspections[0].name_from_record, "浦发银行")
            self.assertEqual(inspections[0].name_from_master, "浦发银行")
            self.assertEqual(inspections[0].record_offset, 528)
            self.assertGreater(len(inspections[0].first_bytes_hex), 0)

    def test_inspect_symbol_uses_market_specific_master_name(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = THSLocalAdapter(make_fake_ths(Path(temp_dir)))

            inspections = adapter.inspect_symbol("000001")

            self.assertEqual(len(inspections), 1)
            self.assertEqual(inspections[0].market, "sznse")
            self.assertEqual(inspections[0].name_from_record, "平安银行")
            self.assertEqual(inspections[0].name_from_master, "平安银行")

    def test_watchlists_skip_encrypted_but_parse_plain_files(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = THSLocalAdapter(make_fake_ths(Path(temp_dir)))

            entries = adapter.read_watchlists()

            self.assertTrue(any(item.symbol == "600000" and item.status == "parsed" for item in entries))
            self.assertTrue(
                any(item.raw_value == "encrypted_or_obfuscated" and item.status == "skipped" for item in entries)
            )
