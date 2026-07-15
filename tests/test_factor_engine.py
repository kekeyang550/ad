from __future__ import annotations

import unittest

from ths_stock_picker.factor_engine import evaluate_disclosed_fundamental, evaluate_factors


def _bars(closes: list[float], volumes: list[float] | None = None) -> list[dict[str, float | str]]:
    selected_volumes = volumes or [1_000_000.0] * len(closes)
    return [
        {
            "trade_date": f"2026-01-{index + 1:02d}",
            "open": close - 0.05,
            "high": close + 0.12,
            "low": close - 0.12,
            "close": close,
            "volume": selected_volumes[index],
        }
        for index, close in enumerate(closes)
    ]


class FactorEngineTests(unittest.TestCase):
    def test_disclosed_profitability_factor_waits_until_the_day_after_notice(self) -> None:
        fundamental = {
            "report_date": "2025-12-31",
            "notice_date": "2026-04-25",
            "revenue": 100.0,
            "net_profit": 10.0,
            "roe": 12.0,
        }

        on_notice_day = evaluate_disclosed_fundamental(fundamental, "2026-04-25")
        available_next_day = evaluate_disclosed_fundamental(fundamental, "2026-04-28")

        self.assertEqual(on_notice_day, [])
        self.assertEqual([item.factor_id for item in available_next_day], ["disclosed_profitability_quality"])
        self.assertIn("2026-04-25 已披露", available_next_day[0].reason)

    def test_disclosed_growth_factor_uses_source_yoy_fields_without_deriving_them(self) -> None:
        fundamental = {
            "report_date": "2025-12-31",
            "notice_date": "2026-04-25",
            "revenue": 100.0,
            "net_profit": 10.0,
            "roe": 5.0,
            "revenue_yoy": 4.65,
            "net_profit_yoy": 3.0,
        }

        signals = evaluate_disclosed_fundamental(fundamental, "2026-04-28")

        self.assertEqual([item.factor_id for item in signals], ["disclosed_growth_quality"])
        self.assertIn("营收同比 4.65%", signals[0].reason)

    def test_disclosed_cashflow_factor_requires_positive_cash_conversion_after_notice(self) -> None:
        fundamental = {
            "report_date": "2025-12-31",
            "notice_date": "2026-04-25",
            "net_profit": 10.0,
            "operating_cash_flow": 9.0,
        }

        before_disclosure = evaluate_disclosed_fundamental(fundamental, "2026-04-25")
        available_next_day = evaluate_disclosed_fundamental(fundamental, "2026-04-28")

        self.assertEqual(before_disclosure, [])
        self.assertEqual([item.factor_id for item in available_next_day], ["disclosed_cashflow_quality"])
        self.assertIn("0.90 倍", available_next_day[0].reason)

    def test_platform_breakout_requires_compact_range_and_confirming_volume(self) -> None:
        closes = [10.0 + (index % 3) * 0.04 for index in range(25)] + [10.65]
        volumes = [1_000_000.0] * 25 + [1_500_000.0]

        signals = evaluate_factors(_bars(closes, volumes))

        self.assertIn("platform_breakout", {item.factor_id for item in signals})

    def test_macd_zero_axis_cross_is_detected_without_future_bars(self) -> None:
        closes = [10.0 + index * 0.04 for index in range(29)] + [11.04, 10.96, 10.88, 10.80, 10.72, 11.72]

        signals = evaluate_factors(_bars(closes))

        self.assertIn("macd_zero_axis_cross", {item.factor_id for item in signals})

    def test_rsi_recovery_requires_a_cross_above_the_weakness_threshold(self) -> None:
        closes = [10.0 + index * 0.05 for index in range(25)] + [11.0, 10.8, 10.6, 10.45, 10.85]

        signals = evaluate_factors(_bars(closes))

        self.assertIn("rsi14_recovery", {item.factor_id for item in signals})

    def test_kdj_low_cross_requires_a_cross_while_price_remains_near_ma20(self) -> None:
        closes = [10.0 + index * 0.03 for index in range(25)] + [10.6, 10.4, 10.2, 10.0, 9.9, 9.8, 10.6]

        signals = evaluate_factors(_bars(closes))

        self.assertIn("kdj_low_cross", {item.factor_id for item in signals})

    def test_limit_up_pullback_requires_a_recent_strong_up_day_and_lower_volume(self) -> None:
        closes = [10.0 + index * 0.05 for index in range(25)] + [12.32, 12.1, 11.95, 11.7]
        volumes = [1_000_000.0] * 25 + [2_000_000.0, 1_000_000.0, 900_000.0, 700_000.0]

        signals = evaluate_factors(_bars(closes, volumes))

        self.assertIn("limit_up_pullback", {item.factor_id for item in signals})
