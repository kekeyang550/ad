from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ths_stock_picker.quote_observer import _parse_sina_response, _parse_tencent_response, write_observations_csv


class QuoteObserverTests(unittest.TestCase):
    def test_parse_sina_response(self) -> None:
        content = (
            'var hq_str_sh600000="浦发银行,8.850,8.890,8.960,9.000,8.790,'
            '8.960,8.970,25809663,230464262.000,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
            '2026-07-08,10:52:47,00,";'
        )

        observations = _parse_sina_response(content)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].symbol, "600000")
        self.assertEqual(observations[0].latest_price, 8.96)
        self.assertAlmostEqual(observations[0].pct_change, (8.96 - 8.89) / 8.89 * 100)

    def test_write_observations_csv(self) -> None:
        observations = _parse_sina_response(
            'var hq_str_sz000001="平安银行,10.440,10.470,10.560,10.590,10.340,'
            '10.550,10.560,46816822,491741731.820,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
            '2026-07-08,10:52:48,00";'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "obs.csv"

            write_observations_csv(path, observations)

            self.assertIn("平安银行", path.read_text(encoding="utf-8-sig"))

    def test_parse_tencent_response_with_market_cap_and_turnover(self) -> None:
        content = (
            'v_sh600000="1~浦发银行~600000~8.98~8.89~8.85~291458~170293~120819~8.96~156~'
            '8.95~597~8.94~471~8.93~1764~8.92~1463~8.98~1045~8.99~8852~9.00~11998~'
            '9.01~3892~9.02~3309~~20260708111215~0.09~1.01~9.00~8.79~'
            '8.98/291458/260354164~291458~26035~0.09~5.95~~9.00~8.79~2.36~'
            '2990.86~2990.86~0.40~9.78~8.00~1.04~-24645~8.93~4.19~5.98~~~'
            '0.16~26035.4164~0.0000~0~   A~GP-A";'
        )

        observations = _parse_tencent_response(content)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].symbol, "600000")
        self.assertEqual(observations[0].board, "沪主板")
        self.assertEqual(observations[0].turnover_rate, 0.40)
        self.assertEqual(observations[0].market_cap, 2990.86 * 100_000_000)
        self.assertEqual(observations[0].amount, 26035.4164 * 10000)
