from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ths_stock_picker.history_import import fetch_tencent_daily_bars_one, load_daily_bars_csv


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
