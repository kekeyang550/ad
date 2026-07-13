from __future__ import annotations

import tempfile
import unittest
import struct
from pathlib import Path

from ths_stock_picker.history_import import fetch_tencent_daily_bars_one, load_daily_bars_csv
from ths_stock_picker.tdx_local import discover_tdx_daily_files, inspect_tdx_daily_status, load_tdx_daily_file


class HistoryImportTests(unittest.TestCase):
    def test_load_daily_bars_csv_with_chinese_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "daily.csv"
            path.write_text(
                "代码,日期,开盘,最高,最低,收盘,成交量,成交额\n"
                "600000,20260708,12.1,12.5,12.0,12.3,10000,123000\n",
                encoding="utf-8-sig",
            )

            bars = load_daily_bars_csv(path)

            self.assertEqual(len(bars), 1)
            self.assertEqual(bars[0].symbol, "600000")
            self.assertEqual(bars[0].trade_date, "2026-07-08")
            self.assertEqual(bars[0].close, 12.3)

    def test_load_daily_bars_csv_uses_default_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "daily.csv"
            path.write_text(
                "日期,开盘,最高,最低,收盘\n"
                "2026-07-08,12.1,12.5,12.0,12.3\n",
                encoding="utf-8-sig",
            )

            bars = load_daily_bars_csv(path, default_symbol="600000")

            self.assertEqual(bars[0].symbol, "600000")

    def test_tencent_symbol_helper_shape_via_url_fetch_function_exists(self) -> None:
        self.assertTrue(callable(fetch_tencent_daily_bars_one))

    def test_load_tdx_daily_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sh600000.day"
            path.write_bytes(
                struct.pack("IIIIIfII", 20260708, 897, 901, 887, 900, 131376013.0, 14700900, 0)
                + struct.pack("IIIIIfII", 20260709, 900, 905, 890, 902, 151000000.0, 16000000, 0)
            )

            bars = load_tdx_daily_file(path)

            self.assertEqual(len(bars), 2)
            self.assertEqual(bars[0].symbol, "600000")
            self.assertEqual(bars[0].trade_date, "2026-07-08")
            self.assertEqual(bars[0].open, 8.97)
            self.assertEqual(bars[0].close, 9.0)
            self.assertEqual(bars[0].volume, 14700900.0)

    def test_discover_tdx_daily_files_filters_stock_and_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sh = root / "vipdoc" / "sh" / "lday"
            sz = root / "vipdoc" / "sz" / "lday"
            sh.mkdir(parents=True)
            sz.mkdir(parents=True)
            payload = struct.pack("IIIIIfII", 20260708, 100, 101, 99, 100, 1000.0, 100, 0)
            (sh / "sh600000.day").write_bytes(payload)
            (sh / "sh000300.day").write_bytes(payload)
            (sh / "sh118069.day").write_bytes(payload)
            (sz / "sz300750.day").write_bytes(payload)

            stock_files = discover_tdx_daily_files(root)
            all_files = discover_tdx_daily_files(root, include_indices=True)

            self.assertEqual([item.symbol for item in stock_files], ["600000", "300750"])
            self.assertEqual([item.symbol for item in all_files], ["sh000300", "600000", "300750"])

    def test_inspect_tdx_daily_status_separates_stock_and_index_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sh = root / "vipdoc" / "sh" / "lday"
            sz = root / "vipdoc" / "sz" / "lday"
            sh.mkdir(parents=True)
            sz.mkdir(parents=True)
            stock_payload = struct.pack("IIIIIfII", 20260708, 100, 101, 99, 100, 1000.0, 100, 0)
            index_payload = struct.pack("IIIIIfII", 20260709, 100, 101, 99, 100, 1000.0, 100, 0)
            (sh / "sh600000.day").write_bytes(stock_payload)
            (sh / "sh000300.day").write_bytes(index_payload)
            (sh / "sh118069.day").write_bytes(index_payload)

            status = inspect_tdx_daily_status(root)

        self.assertEqual(status.stock_file_count, 1)
        self.assertEqual(status.index_file_count, 1)
        self.assertEqual(status.stock_latest_trade_date, "2026-07-08")
        self.assertEqual(status.index_latest_trade_date, "2026-07-09")
        self.assertEqual(status.latest_trade_date, "2026-07-09")
