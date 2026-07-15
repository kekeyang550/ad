from __future__ import annotations

import tempfile
import unittest
import json
import io
import struct
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ths_stock_picker.ai_decision import AIDecision, analyze_symbol, rank_candidates, summarize_news_signal
from ths_stock_picker.cli import DAILY_STRATEGY_SNAPSHOT_OPTIONS, main
from ths_stock_picker.fundamentals import FundamentalRecord
from ths_stock_picker.history_import import DailyBar
from ths_stock_picker.news_import import classify_news, fetch_eastmoney_announcements, load_ths_news_xml
from ths_stock_picker.public_industries import IndustryClassification
from ths_stock_picker.quote_observer import QuoteObservation
from ths_stock_picker.storage import SQLITE_BUSY_TIMEOUT_SECONDS, Repository, assess_strategy_walk_forward, summarize_ai_decision_outcomes
from ths_stock_picker.ths_monitor import inspect_ths_source
from ths_stock_picker.tdx_blocks import BLOCK_HEADER_SIZE, BLOCK_RECORD_SIZE, ThemeMembership
from ths_stock_picker.models import NewsItem, QuoteRealtime, Security, WatchlistEntry
from ths_stock_picker.web_panel import (
    DashboardFilters,
    NotesFilters,
    render_candidates_csv,
    render_dashboard,
    render_data_health_page,
    render_daily_runs_page,
    render_diagnose_page,
    render_strategy_validation_page,
    render_strategy_backtest_runs_page,
    render_strategy_backtest_run_detail_page,
    render_ai_page,
    render_ai_history_page,
    render_ai_changes_page,
    render_ai_outcomes_page,
    render_backtest_page,
    render_factor_detail_page,
    render_factors_page,
    render_notes_csv,
    render_notes_page,
    render_news_page,
    render_symbol_detail,
    render_themes_page,
    render_ths_monitor_page,
)
from tests.test_ths_local import make_fake_ths


class StorageCliTests(unittest.TestCase):
    def test_repository_configures_a_busy_timeout_for_concurrent_web_and_cli_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Repository(Path(temp_dir) / "picker.db")
            try:
                self.assertEqual(
                    repo.conn.execute("PRAGMA busy_timeout").fetchone()[0],
                    SQLITE_BUSY_TIMEOUT_SECONDS * 1000,
                )
            finally:
                repo.close()

    def test_theme_memberships_replace_source_and_render_latest_score_heat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            concept_source = Path(temp_dir) / "block_gn.dat"
            style_source = Path(temp_dir) / "block_fg.dat"
            repo = Repository(db)
            try:
                repo.init_schema()
                imported = repo.replace_stock_themes(
                    [
                        ThemeMembership("000001", "概念", "人工智能", concept_source),
                        ThemeMembership("600000", "概念", "人工智能", concept_source),
                        ThemeMembership("000001", "风格", "专精特新", style_source),
                    ],
                    [concept_source, style_source],
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol=symbol,
                            trade_date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                            open=base + increment * index,
                            high=base + increment * index,
                            low=base + increment * index,
                            close=base + increment * index,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path(f"tdx://lday/{symbol}.day"),
                        )
                        for symbol, base, increment in (("000001", 100.0, 1.0), ("600000", 200.0, 2.0))
                        for index in range(21)
                    ]
                )
                repo.insert_quotes(
                    [
                        QuoteRealtime("000001", "平安银行", "sz", 12.0, 1.2, 2_000_000, 150_000_000),
                        QuoteRealtime("600000", "浦发银行", "sh", 10.0, -1.0, 1_000_000, 80_000_000),
                    ]
                )
                repo.score_latest_quotes()
                heat = repo.theme_heat(limit=10, min_scored=1)
                symbol_themes = repo.themes_for_symbol("000001")
                html = render_themes_page(repo, min_scored=1)
                detail_html = render_symbol_detail(repo, "000001")
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-01-21",
                            open=130.0,
                            high=130.0,
                            low=130.0,
                            close=130.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/000001.day"),
                        )
                    ]
                )
                updated_heat = repo.theme_heat(limit=10, min_scored=1)
                replaced = repo.replace_stock_themes(
                    [ThemeMembership("000001", "概念", "人工智能", concept_source)],
                    [concept_source],
                )
                counts = repo.table_counts()
            finally:
                repo.close()

        self.assertEqual(imported, 3)
        self.assertEqual(replaced, 1)
        self.assertEqual(counts["stock_themes"], 2)
        self.assertEqual(counts["theme_price_cache"], 1)
        self.assertEqual({(row["category"], row["theme"]) for row in symbol_themes}, {("概念", "人工智能"), ("风格", "专精特新")})
        ai_theme = next(row for row in heat["items"] if row["theme"] == "人工智能")
        self.assertEqual(ai_theme["scored_count"], 2)
        self.assertIsNotNone(ai_theme["average_score"])
        self.assertEqual(heat["price_as_of_date"], "2026-01-21")
        self.assertEqual(ai_theme["priced_count"], 2)
        self.assertAlmostEqual(float(ai_theme["price_coverage_rate"]), 100.0)
        self.assertAlmostEqual(float(ai_theme["return_20d"]), 20.0)
        self.assertEqual(ai_theme["return_20d_count"], 2)
        updated_ai_theme = next(row for row in updated_heat["items"] if row["theme"] == "人工智能")
        self.assertAlmostEqual(float(updated_ai_theme["return_20d"]), 25.0)
        self.assertIn("主题评分与价格表现", html)
        self.assertIn("20 日等权", html)
        self.assertIn("人工智能", html)
        self.assertIn("概念与风格", detail_html)

    def test_daily_bars_prefer_tdx_source_and_report_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                tdx_source = Path("tdx://lday/sz/sz000001.day")
                tencent_source = Path("tencent://sz000001/qfqday")
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-06-{day:02d}",
                            open=10 + day,
                            high=10 + day + 0.2,
                            low=10 + day - 0.2,
                            close=10 + day,
                            volume=1_000_000 + day,
                            amount=None,
                            source_file=tdx_source,
                        )
                        for day in range(1, 6)
                    ]
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-06-{day:02d}",
                            open=20 + day,
                            high=20 + day + 0.2,
                            low=20 + day - 0.2,
                            close=20 + day,
                            volume=2_000_000 + day,
                            amount=None,
                            source_file=tencent_source,
                        )
                        for day in range(1, 6)
                    ]
                )
                canonical = repo.daily_bars_for_symbol("000001")
                recent = repo.recent_daily_bars("000001", limit=2)
                health = repo.daily_bar_health()
            finally:
                repo.close()

            self.assertEqual([row["close"] for row in canonical], [11, 12, 13, 14, 15])
            self.assertEqual([row["close"] for row in recent], [15, 14])
            self.assertEqual(health["duplicate_symbol_days"], 5)
            self.assertEqual(health["status"], "attention")
            self.assertEqual(health["sources"][0]["source_kind"], "tdx_unadjusted")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "data-health"]), 0)
            self.assertIn("Canonical source precedence", output.getvalue())

    def test_data_health_web_page_renders_source_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-07-08",
                            open=10.0,
                            high=10.2,
                            low=9.8,
                            close=10.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sz/sz000001.day"),
                        ),
                        DailyBar(
                            symbol="sh118069",
                            trade_date="2026-06-09",
                            open=100.0,
                            high=101.0,
                            low=99.0,
                            close=100.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sh/sh118069.day"),
                        ),
                    ]
                )
                html = render_data_health_page(repo)
            finally:
                repo.close()

            self.assertIn("日线数据健康", html)
            self.assertIn("tdx_unadjusted", html)
            self.assertIn("规范来源", html)
            self.assertIn("最近股票日线", html)

    def test_daily_bar_health_reports_latest_date_and_weekday_lag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-06-05",
                            open=10.0,
                            high=10.2,
                            low=9.8,
                            close=10.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sz/sz000001.day"),
                        ),
                        DailyBar(
                            symbol="sh118069",
                            trade_date="2026-06-09",
                            open=100.0,
                            high=101.0,
                            low=99.0,
                            close=100.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sh/sh118069.day"),
                        ),
                    ]
                )
                health = repo.daily_bar_health(as_of=date(2026, 6, 9))
                freshness = repo.daily_bar_freshness(as_of=date(2026, 6, 9))
            finally:
                repo.close()

        self.assertEqual(health["status"], "clean")
        self.assertEqual(health["latest_trade_date"], "2026-06-05")
        self.assertEqual(health["latest_any_trade_date"], "2026-06-09")
        self.assertEqual(health["weekday_lag_days"], 2)
        self.assertEqual(health["freshness_status"], "lagging")
        self.assertEqual(freshness["latest_trade_date"], "2026-06-05")
        self.assertEqual(freshness["weekday_lag_days"], 2)
        self.assertEqual(freshness["freshness_status"], "lagging")

    def test_daily_bar_health_cache_is_reused_and_invalidated_by_new_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-06-05",
                            open=10.0,
                            high=10.2,
                            low=9.8,
                            close=10.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sz/sz000001.day"),
                        )
                    ]
                )
                first = repo.daily_bar_health(as_of=date(2026, 6, 9))
                cached = repo.daily_bar_health(as_of=date(2026, 6, 9))
                cache_rows_before = repo.conn.execute("SELECT COUNT(*) FROM daily_bar_health_cache").fetchone()[0]
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-06-09",
                            open=10.0,
                            high=10.2,
                            low=9.8,
                            close=10.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sz/sz000001.day"),
                        )
                    ]
                )
                refreshed = repo.daily_bar_health(as_of=date(2026, 6, 9))
                cache_rows_after = repo.conn.execute("SELECT COUNT(*) FROM daily_bar_health_cache").fetchone()[0]
            finally:
                repo.close()

        self.assertEqual(first["latest_trade_date"], "2026-06-05")
        self.assertEqual(cached, first)
        self.assertEqual(cache_rows_before, 1)
        self.assertEqual(refreshed["latest_trade_date"], "2026-06-09")
        self.assertEqual(cache_rows_after, 1)

    def test_quote_health_reports_empty_current_and_lagging_prices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                empty = repo.quote_health(as_of=date(2026, 7, 9))
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="600000",
                            name="浦发银行",
                            latest_price=12.0,
                            pct_change=1.0,
                            volume=20_000_000,
                            amount=200_000_000,
                            open=11.8,
                            high=12.1,
                            low=11.7,
                            previous_close=11.88,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=100_000_000_000,
                            turnover_rate=1.0,
                            board="沪主板",
                        )
                    ]
                )
                current = repo.quote_health(as_of=date(2026, 7, 9))
                lagging = repo.quote_health(as_of=date(2026, 7, 13))
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.0,
                            pct_change=1.0,
                            volume=20_000_000,
                            amount=200_000_000,
                            open=9.8,
                            high=10.1,
                            low=9.7,
                            previous_close=9.88,
                            observed_at="2026-07-13 10:52:47",
                            source="test",
                            market_cap=100_000_000_000,
                            turnover_rate=1.0,
                            board="深主板",
                        )
                    ]
                )
                partial = repo.quote_health(as_of=date(2026, 7, 13))
                ai_html = render_ai_page(repo)
            finally:
                repo.close()

        self.assertEqual(empty["freshness_status"], "empty")
        self.assertEqual(current["freshness_status"], "current")
        self.assertEqual(current["priced_symbols"], 1)
        self.assertEqual(lagging["freshness_status"], "lagging")
        self.assertEqual(lagging["weekday_lag_days"], 3)
        self.assertEqual(partial["freshness_status"], "partial")
        self.assertEqual(partial["current_priced_symbols"], 1)
        self.assertEqual(partial["stale_priced_symbols"], 1)
        self.assertIn("行情时效提醒", ai_html)

    def test_fundamental_health_counts_only_reports_disclosed_before_the_as_of_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_fundamentals(
                    [
                        FundamentalRecord(
                            symbol="600000",
                            report_date="2026-03-31",
                            notice_date="2026-04-25",
                            revenue=100.0,
                            net_profit=10.0,
                            roe=8.5,
                            operating_cash_flow=None,
                            pe_ttm=None,
                            pb=None,
                            source_file=Path("public"),
                        ),
                        FundamentalRecord(
                            symbol="000001",
                            report_date="2026-06-30",
                            notice_date="2026-07-15",
                            revenue=100.0,
                            net_profit=10.0,
                            roe=8.5,
                            operating_cash_flow=None,
                            pe_ttm=None,
                            pb=None,
                            source_file=Path("public"),
                        ),
                        FundamentalRecord(
                            symbol="000001",
                            report_date="2025-12-31",
                            notice_date=None,
                            revenue=100.0,
                            net_profit=10.0,
                            roe=8.5,
                            operating_cash_flow=20.0,
                            pe_ttm=8.0,
                            pb=1.0,
                            source_file=Path("manual"),
                        ),
                    ]
                )
                health = repo.fundamental_health(as_of=date(2026, 7, 14))
                html = render_data_health_page(repo)
            finally:
                repo.close()

        self.assertEqual(health["total_records"], 3)
        self.assertEqual(health["total_symbols"], 2)
        self.assertEqual(health["disclosed_records"], 1)
        self.assertEqual(health["disclosed_symbols"], 1)
        self.assertEqual(health["latest_imported_report_date"], "2026-06-30")
        self.assertEqual(health["latest_disclosed_report_date"], "2026-03-31")
        self.assertEqual(health["latest_disclosed_notice_date"], "2026-04-25")
        self.assertIn("财务披露覆盖", html)
        self.assertIn("已披露股票", html)

    def test_lagging_daily_bars_render_warning_on_research_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2020-01-02",
                            open=10.0,
                            high=10.2,
                            low=9.8,
                            close=10.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sz/sz000001.day"),
                        )
                    ]
                )
                dashboard_html = render_dashboard(repo)
                pages = [
                    dashboard_html,
                    render_ai_page(repo),
                    render_factors_page(repo, limit=1),
                    render_backtest_page(repo, limit_symbols=1, max_bars=10),
                ]
            finally:
                repo.close()

        for page in pages:
            self.assertIn("日线时效提醒", page)
        self.assertIn("行情待补齐", dashboard_html)

    def test_strategy_backtest_next_open_execution_uses_matching_benchmark_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                start = datetime(2026, 1, 1)
                stock_bars = []
                benchmark_bars = []
                for day in range(45):
                    trade_date = (start + timedelta(days=day)).strftime("%Y-%m-%d")
                    close = 10.0 + day * 0.2
                    stock_bars.append(
                        DailyBar(
                            symbol="000001",
                            trade_date=trade_date,
                            open=close + 0.5,
                            high=close + 0.8,
                            low=close - 1.0,
                            close=close,
                            volume=10_000_000,
                            amount=None,
                            source_file=Path("test"),
                        )
                    )
                    benchmark_close = 100.0 + day * 0.4
                    benchmark_bars.append(
                        DailyBar(
                            symbol="sh000300",
                            trade_date=trade_date,
                            open=benchmark_close + 1.0,
                            high=benchmark_close + 1.2,
                            low=benchmark_close - 0.5,
                            close=benchmark_close,
                            volume=100_000_000,
                            amount=None,
                            source_file=Path("benchmark"),
                        )
                    )
                repo.upsert_daily_bars(stock_bars)
                repo.upsert_daily_bars(benchmark_bars)
                result = repo.strategy_backtest(
                    horizon_days=2,
                    top_n=1,
                    min_signal_score=-100,
                    limit_symbols=1,
                    benchmark_symbol="sh000300",
                    max_bars=45,
                    execution_mode="next_open",
                )
                benchmark_indices = repo.available_benchmark_indices()
            finally:
                repo.close()

            self.assertEqual(result["execution_mode"], "next_open")
            self.assertEqual(result["position_mode"], "non_overlapping")
            self.assertTrue(result["trades"])
            period_stats = result["period_stats"]
            self.assertTrue(period_stats["monthly"])
            self.assertTrue(any(row["period"] == "2026-01" for row in period_stats["monthly"]))
            self.assertEqual(sum(int(row["batches"]) for row in period_stats["monthly"]), result["day_count"])
            self.assertEqual(sum(int(row["batches"]) for row in period_stats["yearly"]), result["day_count"])
            signal_dates = sorted({str(row["signal_date"]) for row in result["trades"]})
            self.assertGreater(len(signal_dates), 1)
            for previous, current in zip(signal_dates, signal_dates[1:]):
                self.assertGreaterEqual(
                    datetime.strptime(current, "%Y-%m-%d") - datetime.strptime(previous, "%Y-%m-%d"),
                    timedelta(days=2),
                )
            first_trade = result["trades"][-1]
            signal_at = datetime.strptime(str(first_trade["signal_date"]), "%Y-%m-%d")
            self.assertEqual(first_trade["trade_date"], (signal_at + timedelta(days=1)).strftime("%Y-%m-%d"))
            self.assertEqual(first_trade["exit_date"], (signal_at + timedelta(days=2)).strftime("%Y-%m-%d"))
            expected_entry = 10.0 + (signal_at - start).days * 0.2 + 0.2 + 0.5
            expected_exit = 10.0 + (signal_at - start).days * 0.2 + 0.4
            self.assertAlmostEqual(float(first_trade["entry_price"]), expected_entry)
            self.assertAlmostEqual(float(first_trade["gross_return_pct"]), (expected_exit / expected_entry - 1) * 100)
            benchmark = result["benchmark"]
            self.assertIsNotNone(benchmark)
            self.assertEqual(benchmark["daily_returns"][0]["trade_date"], first_trade["trade_date"])
            self.assertEqual(benchmark_indices, [{"symbol": "sh000300", "name": "沪深300", "latest_trade_date": "2026-02-14"}])

    def test_strategy_backtest_skips_locked_limit_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                start = datetime(2026, 1, 1)
                bars = []
                for day in range(45):
                    trade_date = (start + timedelta(days=day)).strftime("%Y-%m-%d")
                    close = 10.0 + day * 0.2
                    if day == 20:
                        close = (10.0 + 19 * 0.2) * 1.1
                        open_price = close
                        high = close
                        low = close
                    else:
                        open_price = close + 0.5
                        high = close + 0.8
                        low = close - 1.0
                    bars.append(
                        DailyBar(
                            symbol="000001",
                            trade_date=trade_date,
                            open=open_price,
                            high=high,
                            low=low,
                            close=close,
                            volume=10_000_000,
                            amount=None,
                            source_file=Path("test"),
                        )
                    )
                repo.upsert_daily_bars(bars)
                result = repo.strategy_backtest(
                    horizon_days=2,
                    top_n=1,
                    min_signal_score=-100,
                    limit_symbols=1,
                    max_bars=45,
                )
            finally:
                repo.close()

            locked_entry_date = (start + timedelta(days=20)).strftime("%Y-%m-%d")
            self.assertGreaterEqual(result["skipped_locked_entries"], 1)
            self.assertFalse(any(row["entry_date"] == locked_entry_date for row in result["trades"]))

    def test_strategy_walk_forward_keeps_training_and_test_windows_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                start = datetime(2025, 1, 1)
                bars = []
                for day in range(80):
                    close = 10.0 + day * 0.08
                    bars.append(
                        DailyBar(
                            symbol="000001",
                            trade_date=(start + timedelta(days=day)).strftime("%Y-%m-%d"),
                            open=close + 0.1,
                            high=close + 0.2,
                            low=close - 1.0,
                            close=close,
                            volume=10_000_000,
                            amount=None,
                            source_file=Path("test"),
                        )
                    )
                repo.upsert_daily_bars(bars)
                result = repo.strategy_walk_forward(
                    train_days=30,
                    test_days=15,
                    max_folds=2,
                    horizon_days=2,
                    top_n=1,
                    min_signal_score=-100,
                    limit_symbols=1,
                )
            finally:
                repo.close()

            self.assertEqual(result["validation_mode"], "walk_forward")
            self.assertEqual(len(result["folds"]), 2)
            first, second = result["folds"]
            self.assertLess(first["train_end_date"], first["test_start_date"])
            self.assertLess(second["train_end_date"], second["test_start_date"])
            self.assertGreater(second["train_end_date"], first["train_end_date"])
            self.assertGreaterEqual(result["total_test_days"], 1)

    def test_walk_forward_assessment_rejects_negative_out_of_sample_result(self) -> None:
        assessment = assess_strategy_walk_forward(
            {
                "validation_mode": "walk_forward",
                "folds": [
                    {
                        "fold": 1,
                        "trade_count": 35,
                        "test_days": 8,
                        "portfolio_avg_return": -0.40,
                        "max_drawdown": -7.0,
                        "benchmark": {"sample_count": 8, "avg_return": 0.10},
                    },
                    {
                        "fold": 2,
                        "trade_count": 35,
                        "test_days": 8,
                        "portfolio_avg_return": -0.10,
                        "max_drawdown": -6.0,
                        "benchmark": {"sample_count": 8, "avg_return": 0.05},
                    },
                ],
                "total_test_days": 16,
                "total_trades": 70,
            },
            min_folds=2,
            min_trades=60,
        )

        self.assertEqual(assessment["verdict"], "未通过")
        self.assertEqual(assessment["positive_fold_count"], 0)
        self.assertLess(float(assessment["portfolio_avg_return"]), 0.0)
        self.assertLess(float(assessment["benchmark_excess_return"]), 0.0)

    def test_strategy_validation_run_is_saved_with_data_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                run_id = repo.save_strategy_validation_run(
                    parameters={"train_days": 252, "test_days": 63, "top_n": 10},
                    result={"folds": [], "total_trades": 0},
                    assessment={"verdict": "样本不足", "summary": "测试样本不足。"},
                )
                rows = repo.strategy_validation_runs(limit=5)
            finally:
                repo.close()

        self.assertGreater(run_id, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "样本不足")
        self.assertIn("daily_bars_v=", rows[0]["data_fingerprint"])
        self.assertEqual(json.loads(rows[0]["parameters_json"])["top_n"], 10)

    def test_strategy_validation_runs_command_lists_saved_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.save_strategy_validation_run(
                    parameters={"train_days": 252},
                    result={"folds": [], "total_trades": 0},
                    assessment={"verdict": "样本不足", "summary": "测试样本不足。"},
                )
            finally:
                repo.close()

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "strategy-validation-runs", "--limit", "5"]), 0)

        self.assertIn("verdict=样本不足", output.getvalue())

    def test_strategy_validation_web_page_renders_saved_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.save_strategy_validation_run(
                    parameters={"train_days": 252, "benchmark_symbol": "sh000300"},
                    result={"folds": [], "total_trades": 0},
                    assessment={
                        "verdict": "未通过",
                        "summary": "样本外收益未达标。",
                        "fold_count": 3,
                        "total_trades": 190,
                        "portfolio_avg_return": -0.26,
                        "benchmark_excess_return": -0.44,
                        "max_drawdown": -14.11,
                    },
                )
                html = render_strategy_validation_page(repo)
            finally:
                repo.close()

        self.assertIn("策略样本外验证", html)
        self.assertIn("未通过", html)
        self.assertIn("-0.44%", html)
        self.assertIn("日线版本", html)

    def test_strategy_backtest_run_is_saved_and_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                run_id = repo.save_strategy_backtest_run(
                    parameters={"horizon_days": 5, "top_n": 10, "execution_mode": "next_open", "position_mode": "non_overlapping"},
                    result={
                        "trade_count": 24,
                        "day_count": 3,
                        "win_rate": 58.3,
                        "avg_return": 0.8,
                        "gross_avg_return": 0.9,
                        "portfolio_avg_return": 0.7,
                        "max_drawdown": -3.2,
                        "best_return": 4.2,
                        "worst_return": -2.1,
                        "round_trip_cost_pct": 0.2,
                        "execution_mode": "next_open",
                        "position_mode": "non_overlapping",
                        "skipped_locked_entries": 1,
                        "skipped_locked_exits": 0,
                        "excess_portfolio_avg_return": 0.3,
                        "benchmark": {"symbol": "sh000300", "sample_count": 3, "avg_return": 0.4, "cumulative_return": 1.2, "max_drawdown": -1.0},
                    },
                )
                rows = repo.strategy_backtest_runs(limit=5)
                html = render_strategy_backtest_runs_page(repo, saved_run_id=run_id)
            finally:
                repo.close()

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "strategy-backtest-runs", "--limit", "5"]), 0)

        self.assertGreater(run_id, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["summary_json"])["trade_count"], 24)
        self.assertIn("已保存策略回测", html)
        self.assertIn("已保存策略回测记录", html)
        self.assertIn("0.30%", html)
        self.assertIn("trades=24", output.getvalue())

    def test_daily_run_lifecycle_persists_parameters_summary_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                run_id = repo.start_daily_run({"limit": 200, "universe": "auto"})
                repo.finish_daily_run(
                    run_id,
                    status="succeeded",
                    summary={"history_bars_imported": 120, "artifacts": ["outputs/daily_report.md"]},
                )
                rows = repo.daily_runs(limit=5)
            finally:
                repo.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertEqual(json.loads(rows[0]["parameters_json"])["universe"], "auto")
        self.assertEqual(json.loads(rows[0]["summary_json"])["history_bars_imported"], 120)
        self.assertIsNotNone(rows[0]["finished_at"])

    def test_audit_run_tables_can_be_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = Repository(root / "picker.db")
            try:
                repo.init_schema()
                daily_run_id = repo.start_daily_run({"limit": 200})
                repo.finish_daily_run(daily_run_id, "succeeded", {"history_bars_imported": 0})
                repo.save_strategy_validation_run(
                    parameters={"train_days": 252},
                    result={"folds": [], "total_trades": 0},
                    assessment={"verdict": "样本不足", "summary": "测试样本不足。"},
                )
                daily_export_count = repo.export_table_csv("daily_runs", root / "daily_runs.csv")
                validation_export_count = repo.export_table_csv(
                    "strategy_validation_runs", root / "strategy_validation_runs.csv"
                )
            finally:
                repo.close()

            daily_csv = (root / "daily_runs.csv").read_text(encoding="utf-8-sig")
            validation_csv = (root / "strategy_validation_runs.csv").read_text(encoding="utf-8-sig")

        self.assertEqual(daily_export_count, 1)
        self.assertEqual(validation_export_count, 1)
        self.assertIn("summary_json", daily_csv)
        self.assertIn("assessment_json", validation_csv)

    def test_public_history_supplement_excludes_symbols_with_tdx_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-07-08",
                            open=10.0,
                            high=10.2,
                            low=9.8,
                            close=10.0,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sz/sz000001.day"),
                        )
                    ]
                )
                missing = repo.symbols_without_tdx_daily_bars(["000001", "000002", "000003"])
            finally:
                repo.close()

        self.assertEqual(missing, ["000002", "000003"])

    def test_tdx_status_cli_reports_stock_and_index_latest_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tdx_root = root / "tdx"
            sh = tdx_root / "vipdoc" / "sh" / "lday"
            sh.mkdir(parents=True)
            stock_payload = struct.pack("IIIIIfII", 20260708, 100, 101, 99, 100, 1000.0, 100, 0)
            index_payload = struct.pack("IIIIIfII", 20260709, 100, 101, 99, 100, 1000.0, 100, 0)
            (sh / "sh600000.day").write_bytes(stock_payload)
            (sh / "sh000300.day").write_bytes(index_payload)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(root / "picker.db"), "tdx-status", "--tdx-root", str(tdx_root)]), 0)

        self.assertIn("stock_latest_trade_date=2026-07-08", output.getvalue())
        self.assertIn("index_latest_trade_date=2026-07-09", output.getvalue())

    def test_run_daily_can_incrementally_import_tdx_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            ths_root = make_fake_ths(root)
            tdx_root = root / "tdx"
            lday = tdx_root / "vipdoc" / "sh" / "lday"
            lday.mkdir(parents=True)
            (lday / "sh600000.day").write_bytes(
                struct.pack("IIIIIfII", 20260709, 1000, 1020, 990, 1010, 1_000_000.0, 100_000, 0)
            )
            block_path = tdx_root / "T0002" / "hq_cache" / "block_gn.dat"
            block_path.parent.mkdir(parents=True)
            block_payload = b"\0\0" + "人工智能".encode("gbk") + b"\0"
            block_payload += (1).to_bytes(2, "little") + b"\x02\0" + b"600000\0"
            block_header = b"Registry ver:1.0 (1999-9-28)".ljust(BLOCK_HEADER_SIZE, b"\0")
            block_path.write_bytes(block_header + block_payload.ljust(BLOCK_RECORD_SIZE, b"\0"))
            output = io.StringIO()
            with (
                patch("ths_stock_picker.cli._import_public_quotes", return_value=0),
                patch("ths_stock_picker.cli._select_universe_symbols", return_value=("test", ["600000"])),
                patch("ths_stock_picker.cli._export", return_value=0) as export_mock,
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db),
                            "--ths-root",
                            str(ths_root),
                            "run-daily",
                            "--tdx-root",
                            str(tdx_root),
                            "--tdx-import-themes",
                        ]
                    ),
                    0,
                )
            exported_tables = export_mock.call_args.args[2]

            repo = Repository(db)
            try:
                repo.init_schema()
                bars = repo.daily_bars_for_symbol("600000")
                themes = repo.themes_for_symbol("600000")
                run = repo.daily_runs(limit=1)[0]
                health = repo.daily_bar_health()
                daily_runs_html = render_daily_runs_page(repo)
            finally:
                repo.close()

        summary = json.loads(run["summary_json"])
        parameters = json.loads(run["parameters_json"])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["trade_date"], "2026-07-09")
        self.assertEqual(health["sources"][0]["source_kind"], "tdx_unadjusted")
        self.assertEqual(summary["tdx_daily_bars_imported"], 1)
        self.assertEqual(summary["tdx_daily_files"], 1)
        self.assertEqual(summary["tdx_theme_memberships_imported"], 1)
        self.assertEqual(len(themes), 1)
        self.assertEqual(summary["daily_bar_health"]["latest_trade_date"], "2026-07-09")
        self.assertIn(summary["daily_bar_health"]["freshness_status"], {"current", "lagging", "unknown"})
        self.assertEqual(summary["quote_health"]["freshness_status"], "empty")
        self.assertEqual(summary["ai_snapshot_status"], "skipped_stale_daily_bars")
        self.assertEqual(summary["ai_decisions_saved"], 0)
        self.assertEqual(summary["ai_snapshot_latest_trade_date"], "2026-07-09")
        self.assertEqual(summary["daily_audit_export_tables"], exported_tables)
        self.assertNotIn("daily_bars", exported_tables)
        self.assertIn("scores", exported_tables)
        self.assertIn("stock_themes", exported_tables)
        self.assertEqual(parameters["tdx_root"], str(tdx_root))
        self.assertIn("Step 1/9: importing TongDaXin local daily bars", output.getvalue())
        self.assertIn("Step 2/9: importing TongDaXin local themes", output.getvalue())
        self.assertIn("TDX 同步", daily_runs_html)
        self.assertIn("日线时效", daily_runs_html)
        self.assertIn("行情时效", daily_runs_html)
        self.assertIn("AI 快照", daily_runs_html)
        self.assertIn("2026-07-09", daily_runs_html)
        self.assertIn("日线滞后至 2026-07-09，未保存", daily_runs_html)

    def test_run_daily_optionally_imports_public_fundamentals_and_records_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            ths_root = make_fake_ths(root)
            record = FundamentalRecord(
                symbol="600000",
                report_date="2026-03-31",
                notice_date="2026-04-25",
                revenue=100.0,
                net_profit=10.0,
                roe=8.5,
                operating_cash_flow=None,
                pe_ttm=None,
                pb=None,
                source_file=Path("public"),
                revenue_yoy=5.0,
                net_profit_yoy=3.0,
            )
            output = io.StringIO()
            with (
                patch("ths_stock_picker.cli._import_public_quotes", return_value=0),
                patch("ths_stock_picker.cli._select_universe_symbols", return_value=("test", ["600000"])),
                patch("ths_stock_picker.cli.fetch_tencent_daily_bars", return_value=[]),
                patch("ths_stock_picker.cli.fetch_eastmoney_fundamentals_one", return_value=[record]) as fundamentals_mock,
                patch("ths_stock_picker.cli._export", return_value=0),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db),
                            "--ths-root",
                            str(ths_root),
                            "run-daily",
                            "--limit",
                            "1",
                            "--public-fundamentals",
                            "--public-fundamental-reports",
                            "3",
                            "--public-fundamental-limit",
                            "1",
                            "--out-dir",
                            str(root / "outputs"),
                        ]
                    ),
                    0,
                )

            repo = Repository(db)
            try:
                repo.init_schema()
                summary = json.loads(repo.daily_runs(limit=1)[0]["summary_json"])
                parameters = json.loads(repo.daily_runs(limit=1)[0]["parameters_json"])
                fundamental = repo.latest_fundamental("600000")
                html = render_daily_runs_page(repo)
            finally:
                repo.close()

        self.assertEqual(summary["public_fundamentals_imported"], 1)
        self.assertEqual(summary["public_fundamentals_failures"], 0)
        self.assertTrue(summary["public_fundamentals_enabled"])
        self.assertEqual(summary["fundamental_health"]["disclosed_symbols"], 1)
        self.assertTrue(parameters["public_fundamentals"])
        self.assertEqual(parameters["public_fundamental_reports"], 3)
        self.assertEqual(parameters["public_fundamental_limit"], 1)
        self.assertEqual(summary["public_fundamental_symbols_requested"], 1)
        self.assertEqual(fundamental["revenue_yoy"], 5.0)
        fundamentals_mock.assert_called_once_with("600000", reports=3)
        self.assertIn("Step 5/8: fetching public financial reports", output.getvalue())
        self.assertIn("Using 1 of 1 selected symbols for public financial reports", output.getvalue())
        self.assertIn("公开财报", html)
        self.assertIn("1 条", html)
        self.assertIn("1 只已披露", html)

    def test_run_daily_optionally_imports_public_industries_and_records_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            ths_root = make_fake_ths(root)
            output = io.StringIO()
            with (
                patch("ths_stock_picker.cli._import_public_quotes", return_value=0),
                patch("ths_stock_picker.cli._select_universe_symbols", return_value=("test", ["600000"])),
                patch("ths_stock_picker.cli.fetch_tencent_daily_bars", return_value=[]),
                patch(
                    "ths_stock_picker.cli.fetch_eastmoney_industry_one",
                    return_value=IndustryClassification("600000", "金融-银行-股份制与城商行"),
                ) as industries_mock,
                patch("ths_stock_picker.cli._export", return_value=0) as export_mock,
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db),
                            "--ths-root",
                            str(ths_root),
                            "run-daily",
                            "--limit",
                            "1",
                            "--public-industries",
                            "--public-industry-limit",
                            "1",
                            "--out-dir",
                            str(root / "outputs"),
                        ]
                    ),
                    0,
                )

            repo = Repository(db)
            try:
                repo.init_schema()
                run = repo.daily_runs(limit=1)[0]
                summary = json.loads(run["summary_json"])
                parameters = json.loads(run["parameters_json"])
                industry = repo.industry_for_symbol("600000")
                html = render_daily_runs_page(repo)
            finally:
                repo.close()

        self.assertTrue(summary["public_industries_enabled"])
        self.assertEqual(summary["public_industry_symbols_requested"], 1)
        self.assertEqual(summary["public_industries_imported"], 1)
        self.assertEqual(summary["public_industries_failures"], 0)
        self.assertTrue(parameters["public_industries"])
        self.assertEqual(parameters["public_industry_limit"], 1)
        self.assertEqual(industry["industry"], "金融-银行-股份制与城商行")
        self.assertIn("stock_industries", export_mock.call_args.args[2])
        industries_mock.assert_called_once_with("600000")
        self.assertIn("Step 5/8: fetching public industry labels", output.getvalue())
        self.assertIn("Using 1 of 1 selected symbols for public industry labels", output.getvalue())
        self.assertIn("公开行业", html)
        self.assertIn("1 条", html)

    def test_run_daily_saves_ai_snapshot_and_tolerates_ai_errors(self) -> None:
        decision = AIDecision(
            symbol="600000",
            name="浦发银行",
            board="沪主板",
            decision="观察",
            confidence=72.0,
            summary="测试观点",
            strengths=["测试正向证据"],
            risks=["测试风险"],
            trigger_conditions=["测试触发条件"],
            invalidation_conditions=["测试失效条件"],
            next_actions=["测试动作"],
            evidence={},
        )
        healthy_daily_bars = {
            "status": "clean",
            "latest_trade_date": "2026-07-15",
            "freshness_status": "current",
            "weekday_lag_days": 0,
            "freshness_checked_on": "2026-07-15",
        }
        stale_daily_bars = {**healthy_daily_bars, "latest_trade_date": "2026-07-08", "freshness_status": "lagging", "weekday_lag_days": 5}
        healthy_quotes = {
            "priced_symbols": 1,
            "current_priced_symbols": 1,
            "stale_priced_symbols": 0,
            "latest_price_date": "2026-07-15",
            "freshness_status": "current",
            "weekday_lag_days": 0,
            "freshness_checked_on": "2026-07-15",
        }
        partial_quotes = {
            "priced_symbols": 2,
            "current_priced_symbols": 1,
            "stale_priced_symbols": 1,
            "latest_price_date": "2026-07-15",
            "freshness_status": "partial",
            "weekday_lag_days": 0,
            "freshness_checked_on": "2026-07-15",
        }
        scenarios = [
            ("saved", healthy_daily_bars, healthy_quotes, {"return_value": [decision]}, 1),
            ("failed", healthy_daily_bars, healthy_quotes, {"side_effect": RuntimeError("AI unavailable")}, 0),
            ("skipped_stale_daily_bars", stale_daily_bars, healthy_quotes, {"return_value": [decision]}, 0),
            ("skipped_stale_quotes", healthy_daily_bars, partial_quotes, {"return_value": [decision]}, 0),
        ]
        for expected_status, daily_bar_health, quote_health, rank_patch, expected_rows in scenarios:
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                db = root / "picker.db"
                ths_root = make_fake_ths(root)
                output = io.StringIO()
                with (
                    patch("ths_stock_picker.cli._import_public_quotes", return_value=0),
                    patch("ths_stock_picker.cli._select_universe_symbols", return_value=("test", [])),
                    patch("ths_stock_picker.cli._export", return_value=0),
                    patch.object(Repository, "daily_bar_health", return_value=daily_bar_health),
                    patch.object(Repository, "quote_health", return_value=quote_health),
                    patch("ths_stock_picker.cli.rank_candidates", **rank_patch) as rank_mock,
                    redirect_stdout(output),
                ):
                    self.assertEqual(
                        main(
                            [
                                "--db",
                                str(db),
                                "--ths-root",
                                str(ths_root),
                                "run-daily",
                                "--limit",
                                "1",
                                "--out-dir",
                                str(root / "outputs"),
                            ]
                        ),
                        0,
                    )

                repo = Repository(db)
                try:
                    repo.init_schema()
                    summary = json.loads(repo.daily_runs(limit=1)[0]["summary_json"])
                    rows = repo.latest_ai_decisions(limit=5)
                    html = render_daily_runs_page(repo)
                finally:
                    repo.close()

                self.assertEqual(summary["ai_snapshot_status"], expected_status)
                self.assertEqual(summary["ai_decisions_saved"], expected_rows)
                self.assertEqual(len(rows), expected_rows)
                if expected_status == "failed":
                    self.assertIn("RuntimeError: AI unavailable", summary["ai_snapshot_error"])
                    self.assertIn("AI snapshot skipped", output.getvalue())
                elif expected_status == "skipped_stale_daily_bars":
                    rank_mock.assert_not_called()
                    self.assertEqual(summary["ai_snapshot_latest_trade_date"], "2026-07-08")
                    self.assertIn("日线滞后至 2026-07-08，未保存", html)
                    self.assertIn("AI snapshot skipped: daily bars freshness=lagging", output.getvalue())
                elif expected_status == "skipped_stale_quotes":
                    rank_mock.assert_not_called()
                    self.assertEqual(summary["ai_snapshot_quote_freshness"], "partial")
                    self.assertIn("行情部分过期（近 1 日 1/2 只），未保存", html)
                    self.assertIn("AI snapshot skipped: quotes freshness=partial", output.getvalue())

    def test_run_daily_optionally_saves_a_non_blocking_strategy_snapshot(self) -> None:
        snapshot_result = {
            "trade_count": 12,
            "portfolio_avg_return": 1.25,
            "max_drawdown": -4.5,
        }
        healthy_daily_bars = {
            "status": "clean",
            "latest_trade_date": "2026-07-15",
            "freshness_status": "current",
            "weekday_lag_days": 0,
            "freshness_checked_on": "2026-07-15",
        }
        stale_daily_bars = {**healthy_daily_bars, "latest_trade_date": "2026-07-08", "freshness_status": "lagging", "weekday_lag_days": 5}
        scenarios = [
            ("saved", healthy_daily_bars, {"return_value": snapshot_result}),
            ("failed", healthy_daily_bars, {"side_effect": RuntimeError("backtest unavailable")}),
            ("skipped_stale_daily_bars", stale_daily_bars, {"return_value": snapshot_result}),
        ]
        for expected_status, daily_bar_health, backtest_patch in scenarios:
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                db = root / "picker.db"
                ths_root = make_fake_ths(root)
                output = io.StringIO()
                with (
                    patch("ths_stock_picker.cli._import_public_quotes", return_value=0),
                    patch("ths_stock_picker.cli._select_universe_symbols", return_value=("test", [])),
                    patch("ths_stock_picker.cli._export", return_value=0),
                    patch.object(Repository, "daily_bar_health", return_value=daily_bar_health),
                    patch.object(Repository, "strategy_backtest", **backtest_patch) as backtest_mock,
                    patch.object(Repository, "save_strategy_backtest_run", return_value=42) as save_mock,
                    redirect_stdout(output),
                ):
                    self.assertEqual(
                        main(
                            [
                                "--db",
                                str(db),
                                "--ths-root",
                                str(ths_root),
                                "run-daily",
                                "--limit",
                                "1",
                                "--strategy-snapshot",
                                "--out-dir",
                                str(root / "outputs"),
                            ]
                        ),
                        0,
                    )

                repo = Repository(db)
                try:
                    repo.init_schema()
                    run = repo.daily_runs(limit=1)[0]
                    summary = json.loads(run["summary_json"])
                    parameters = json.loads(run["parameters_json"])
                    html = render_daily_runs_page(repo)
                finally:
                    repo.close()

                self.assertEqual(run["status"], "succeeded")
                self.assertTrue(parameters["strategy_snapshot"])
                self.assertEqual(summary["strategy_snapshot_status"], expected_status)
                self.assertIn("策略快照", html)
                if expected_status == "saved":
                    self.assertEqual(summary["strategy_snapshot_run_id"], 42)
                    self.assertEqual(summary["strategy_snapshot_trade_count"], 12)
                    self.assertAlmostEqual(summary["strategy_snapshot_portfolio_avg_return"], 1.25)
                    backtest_mock.assert_called_once_with(**DAILY_STRATEGY_SNAPSHOT_OPTIONS)
                    saved_parameters = save_mock.call_args.args[0]
                    self.assertEqual(saved_parameters["source"], "daily_run_strategy_snapshot")
                    self.assertIn("已保存 #42 · 12 笔 · 组合 +1.25%", html)
                    self.assertIn("Saved strategy snapshot", output.getvalue())
                elif expected_status == "failed":
                    save_mock.assert_not_called()
                    self.assertIn("RuntimeError: backtest unavailable", summary["strategy_snapshot_error"])
                    self.assertIn("失败，不影响数据更新", html)
                    self.assertIn("Strategy snapshot skipped", output.getvalue())
                else:
                    backtest_mock.assert_not_called()
                    save_mock.assert_not_called()
                    self.assertEqual(summary["strategy_snapshot_latest_trade_date"], "2026-07-08")
                    self.assertIn("日线滞后至 2026-07-08，未保存", html)
                    self.assertIn("Strategy snapshot skipped: daily bars freshness=lagging", output.getvalue())

    def test_run_daily_public_announcements_failure_is_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            ths_root = make_fake_ths(root)
            output = io.StringIO()
            with (
                patch("ths_stock_picker.cli._import_public_quotes", return_value=0),
                patch("ths_stock_picker.cli._select_universe_symbols", return_value=("test", ["000538"])),
                patch("ths_stock_picker.cli.fetch_eastmoney_announcements", side_effect=RuntimeError("network down")),
                patch("ths_stock_picker.cli.fetch_tencent_daily_bars", return_value=[]),
                patch("ths_stock_picker.cli._export", return_value=0),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db),
                            "--ths-root",
                            str(ths_root),
                            "run-daily",
                            "--limit",
                            "1",
                            "--public-announcements",
                        ]
                    ),
                    0,
                )

            repo = Repository(db)
            try:
                repo.init_schema()
                run = repo.daily_runs(limit=1)[0]
                html = render_daily_runs_page(repo)
            finally:
                repo.close()
            listed = io.StringIO()
            with redirect_stdout(listed):
                self.assertEqual(main(["--db", str(db), "daily-runs", "--limit", "1"]), 0)

        summary = json.loads(run["summary_json"])
        self.assertEqual(summary["public_announcement_status"], "failed")
        self.assertIn("RuntimeError: network down", summary["public_announcement_error"])
        self.assertIn("Public announcements skipped", output.getvalue())
        self.assertIn("公告", html)
        self.assertIn("失败", html)
        self.assertIn("announcements=failed:0", listed.getvalue())

    def test_run_daily_failure_is_recorded_and_listed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "missing-ths"
            db = Path(temp_dir) / "picker.db"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "--ths-root", str(root), "run-daily"]), 2)

            repo = Repository(db)
            try:
                repo.init_schema()
                rows = repo.daily_runs(limit=5)
            finally:
                repo.close()

            listed = io.StringIO()
            with redirect_stdout(listed):
                self.assertEqual(main(["--db", str(db), "daily-runs", "--limit", "5"]), 0)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("import_local_cache", json.loads(rows[0]["summary_json"])["failed_step"])
        self.assertIn("status=failed", listed.getvalue())

    def test_daily_runs_web_page_renders_saved_pipeline_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                run_id = repo.start_daily_run({"limit": 200, "universe": "auto"})
                repo.finish_daily_run(
                    run_id,
                    status="succeeded",
                    summary={
                        "history_bars_imported": 120,
                        "history_symbols": 2,
                        "tdx_covered_symbols": 2,
                        "quote_health": {"freshness_status": "empty", "priced_symbols": 0},
                    },
                )
                html = render_daily_runs_page(repo)
            finally:
                repo.close()

        self.assertIn("每日运行记录", html)
        self.assertIn("TDX 已覆盖", html)
        self.assertIn("行情时效", html)
        self.assertIn("公告", html)
        self.assertIn("succeeded", html)
        self.assertIn("120", html)

    def test_factor_scan_and_backtest_use_daily_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                bars = []
                for day in range(1, 46):
                    close = 10.0 + day * 0.05
                    if day == 45:
                        close = 12.7
                    low = close - 2.0 if day == 45 else close - 0.25
                    bars.append(
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-06-{day:02d}",
                            open=close - 0.08,
                            high=close + 0.12,
                            low=low,
                            close=close,
                            volume=10_000_000 + day * 1000,
                            amount=None,
                            source_file=Path("test"),
                        )
                    )
                repo.upsert_daily_bars(bars)
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000300",
                            trade_date=f"2026-06-{day:02d}",
                            open=100 + day * 0.1,
                            high=100 + day * 0.1 + 0.2,
                            low=100 + day * 0.1 - 0.2,
                            close=100 + day * 0.1,
                            volume=100_000_000 + day,
                            amount=None,
                            source_file=Path("benchmark"),
                        )
                        for day in range(1, 46)
                    ]
                )
                signals = repo.factor_scan(symbols=["000001"])
                backtest = repo.factor_backtest(horizon_days=3)
                matrix = repo.factor_backtest_matrix(horizons=[3, 5])
                strategy = repo.strategy_backtest(
                    horizon_days=3,
                    top_n=3,
                    min_signal_score=1,
                    cost_bps=10,
                    slippage_bps=5,
                    benchmark_symbol="000300",
                )
            finally:
                repo.close()

            self.assertTrue(any(row["factor_id"] == "ma_multi_breakout" for row in signals))
            self.assertTrue(backtest)
            self.assertIn("samples", backtest[0])
            self.assertTrue(matrix)
            self.assertIn("effectiveness_score", matrix[0])
            self.assertGreater(strategy["trade_count"], 0)
            self.assertIn("win_rate", strategy)
            self.assertIn("max_drawdown", strategy)
            self.assertIn("equity_curve", strategy)
            self.assertIn("gross_avg_return", strategy)
            self.assertAlmostEqual(strategy["round_trip_cost_pct"], 0.3)
            self.assertLess(strategy["avg_return"], strategy["gross_avg_return"])
            self.assertIsNotNone(strategy["benchmark"])
            self.assertGreater(strategy["benchmark"]["sample_count"], 0)
            self.assertIsNotNone(strategy["excess_portfolio_avg_return"])

    def test_rps_factor_scan_uses_cross_sectional_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                bars = []
                shapes = {
                    "000001": 1.8,
                    "000002": 1.1,
                    "000003": 0.8,
                }
                for symbol, multiplier in shapes.items():
                    for day in range(1, 132):
                        trade_date = (datetime(2026, 1, 1) + timedelta(days=day - 1)).strftime("%Y-%m-%d")
                        close = 10.0 * (1 + (multiplier - 1) * day / 131)
                        bars.append(
                            DailyBar(
                                symbol=symbol,
                                trade_date=trade_date,
                                open=close,
                                high=close + 0.1,
                                low=close - 0.1,
                                close=close,
                                volume=1_000_000 + day,
                                amount=None,
                                source_file=Path("test"),
                            )
                        )
                repo.upsert_daily_bars(bars)
                rows = repo.factor_scan(limit=20, symbols=list(shapes))
            finally:
                repo.close()

            strong = [row for row in rows if row["symbol"] == "000001" and row["factor_id"] == "rps_60_strength"]
            weak = [row for row in rows if row["symbol"] == "000003" and row["factor_id"] == "rps_60_weakness"]
            self.assertTrue(strong)
            self.assertTrue(weak)

    def test_factor_cli_and_web_page_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-07-{day:02d}",
                            open=10 + day * 0.02,
                            high=10 + day * 0.02 + 0.1,
                            low=10 + day * 0.02 - 0.1,
                            close=10 + day * 0.02,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 46)
                    ]
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000300",
                            trade_date=f"2026-07-{day:02d}",
                            open=100 + day * 0.1,
                            high=100 + day * 0.1 + 0.2,
                            low=100 + day * 0.1 - 0.2,
                            close=100 + day * 0.1,
                            volume=100_000_000 + day,
                            amount=None,
                            source_file=Path("benchmark"),
                        )
                        for day in range(1, 46)
                    ]
                )
                html = render_factors_page(repo)
                detail_html = render_factor_detail_page(repo, "ma_multi_breakout")
                backtest_html = render_backtest_page(
                    repo,
                    horizon=3,
                    top_n=3,
                    min_signal_score=1,
                    cost_bps=10,
                    slippage_bps=5,
                    benchmark_symbol="000300",
                )
            finally:
                repo.close()

            self.assertIn("公式因子", html)
            self.assertIn("因子定义", html)
            self.assertIn("因子多周期回测矩阵", html)
            self.assertIn("/factors/ma_multi_breakout", html)
            self.assertIn("因子详情", detail_html)
            self.assertIn("均线共振突破", detail_html)
            self.assertIn("未来函数风险", detail_html)
            self.assertIn("当前命中", detail_html)
            self.assertIn("多周期历史表现", detail_html)
            self.assertIn("策略回测", backtest_html)
            self.assertIn("策略交易样本", backtest_html)
            self.assertIn("组合权益曲线", backtest_html)
            self.assertIn("最大回撤", backtest_html)
            self.assertIn("成本 bps", backtest_html)
            self.assertIn("净收益", backtest_html)
            self.assertIn("基准 000300", backtest_html)
            self.assertIn("超额日均", backtest_html)
            self.assertIn("年度批次表现", backtest_html)
            self.assertIn("月度批次表现", backtest_html)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "factors"]), 0)
            self.assertIn("均线共振突破", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "factor-matrix", "--horizons", "3,5"]), 0)
            self.assertIn("Factor matrix", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db),
                            "strategy-backtest",
                            "--horizon",
                            "3",
                            "--top-n",
                            "3",
                            "--min-signal-score",
                            "1",
                            "--cost-bps",
                            "10",
                            "--slippage-bps",
                            "5",
                            "--benchmark-symbol",
                            "000300",
                            "--save",
                        ]
                    ),
                    0,
                )
            self.assertIn("Strategy backtest", output.getvalue())
            self.assertIn("net_avg", output.getvalue())
            self.assertIn("benchmark=000300", output.getvalue())
            self.assertIn("saved_backtest_run=", output.getvalue())

            repo = Repository(db)
            try:
                saved_runs = repo.strategy_backtest_runs(limit=5)
                saved_detail_html = render_strategy_backtest_run_detail_page(repo, int(saved_runs[0]["id"]))
            finally:
                repo.close()
            self.assertEqual(len(saved_runs), 1)
            self.assertIn("策略回测记录", saved_detail_html)
            self.assertIn("策略交易样本", saved_detail_html)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "strategy-backtest-runs", "--limit", "5"]), 0)
            self.assertIn("trades=", output.getvalue())

    def test_factor_matrix_cache_invalidates_when_daily_bars_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-06-{day:02d}",
                            open=10 + day * 0.04,
                            high=10 + day * 0.04 + 0.1,
                            low=10 + day * 0.04 - 0.1,
                            close=10 + day * 0.04,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 46)
                    ]
                )
                initial = repo.factor_backtest_matrix(horizons=[3], max_bars=45, use_cache=True)
                self.assertTrue(initial)
                self.assertEqual(repo.table_counts()["factor_backtest_cache"], 1)

                cache_key = repo._factor_backtest_cache_key([3], None, 45)
                stale_rows = [{"factor_id": "stale", "horizons": {"3": {"samples": 1}}}]
                with repo.conn:
                    repo.conn.execute(
                        "UPDATE factor_backtest_cache SET rows_json = ? WHERE cache_key = ?",
                        (json.dumps(stale_rows), cache_key),
                    )
                cached = repo.factor_backtest_matrix(horizons=[3], max_bars=45, use_cache=True)
                self.assertEqual(cached[0]["factor_id"], "stale")
                self.assertIn(3, cached[0]["horizons"])

                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-07-16",
                            open=12.0,
                            high=12.3,
                            low=11.8,
                            close=12.2,
                            volume=11_000_000,
                            amount=None,
                            source_file=Path("test"),
                        )
                    ]
                )
                fresh = repo.factor_backtest_matrix(horizons=[3], max_bars=45, use_cache=True)
            finally:
                repo.close()

            self.assertTrue(fresh)
            self.assertNotEqual(fresh[0]["factor_id"], "stale")

    def test_factor_scan_cache_invalidates_when_daily_bars_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-06-{day:02d}",
                            open=10 + day * 0.04,
                            high=10 + day * 0.04 + 0.1,
                            low=10 + day * 0.04 - 0.1,
                            close=10 + day * 0.04,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 46)
                    ]
                )
                initial = repo.factor_scan(limit=20, symbols=["000001"], use_cache=True)
                self.assertTrue(initial)
                self.assertEqual(repo.table_counts()["factor_scan_cache"], 1)

                cache_key = repo._factor_scan_cache_key(20, ["000001"])
                stale_rows = [{"symbol": "stale", "factor_id": "stale"}]
                with repo.conn:
                    repo.conn.execute(
                        "UPDATE factor_scan_cache SET rows_json = ? WHERE cache_key = ?",
                        (json.dumps(stale_rows), cache_key),
                    )
                cached = repo.factor_scan(limit=20, symbols=["000001"], use_cache=True)
                self.assertEqual(cached[0]["factor_id"], "stale")

                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-07-16",
                            open=12.0,
                            high=12.3,
                            low=11.8,
                            close=12.2,
                            volume=11_000_000,
                            amount=None,
                            source_file=Path("test"),
                        )
                    ]
                )
                fresh = repo.factor_scan(limit=20, symbols=["000001"], use_cache=True)
            finally:
                repo.close()

            self.assertTrue(fresh)
            self.assertNotEqual(fresh[0]["factor_id"], "stale")

    def test_disclosed_fundamental_factor_uses_notice_date_and_invalidates_factor_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-06-{day:02d}",
                            open=10 + day * 0.04,
                            high=10 + day * 0.04 + 0.1,
                            low=10 + day * 0.04 - 0.1,
                            close=10 + day * 0.04,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 46)
                    ]
                )
                record = FundamentalRecord(
                    symbol="000001",
                    report_date="2026-03-31",
                    notice_date="2026-06-20",
                    revenue=100.0,
                    net_profit=10.0,
                    roe=12.0,
                    operating_cash_flow=None,
                    pe_ttm=None,
                    pb=None,
                    source_file=Path("public"),
                    revenue_yoy=4.0,
                    net_profit_yoy=3.0,
                )
                repo.upsert_fundamentals([record])

                self.assertIsNone(repo.disclosed_fundamental_as_of("000001", "2026-06-20"))
                self.assertEqual(repo.disclosed_fundamental_as_of("000001", "2026-06-21")["report_date"], "2026-03-31")
                initial = repo.factor_scan(limit=20, symbols=["000001"], use_cache=True)
                backtest = repo.factor_backtest(horizon_days=3)
                strategy = repo.strategy_backtest(horizon_days=3, min_signal_score=1)
                self.assertTrue(any(row["factor_id"] == "disclosed_profitability_quality" for row in initial))
                financial_row = next(row for row in backtest if row["factor_id"] == "disclosed_profitability_quality")
                self.assertEqual(financial_row["samples"], 22)
                growth_row = next(row for row in backtest if row["factor_id"] == "disclosed_growth_quality")
                self.assertEqual(growth_row["samples"], 22)
                self.assertGreater(strategy["trade_count"], 0)
                self.assertTrue(any("已披露盈利质量" in str(row["factors"]) for row in strategy["trades"]))

                cache_key = repo._factor_scan_cache_key(20, ["000001"])
                with repo.conn:
                    repo.conn.execute(
                        "UPDATE factor_scan_cache SET rows_json = ? WHERE cache_key = ?",
                        (json.dumps([{"symbol": "stale", "factor_id": "stale"}]), cache_key),
                    )
                self.assertEqual(repo.factor_scan(limit=20, symbols=["000001"], use_cache=True)[0]["factor_id"], "stale")

                repo.upsert_fundamentals([record])
                refreshed = repo.factor_scan(limit=20, symbols=["000001"], use_cache=True)
            finally:
                repo.close()

        self.assertTrue(any(row["factor_id"] == "disclosed_profitability_quality" for row in refreshed))
        self.assertNotEqual(refreshed[0]["factor_id"], "stale")

    def test_factor_scan_auto_cache_skips_universe_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-06-{day:02d}",
                            open=10 + day * 0.04,
                            high=10 + day * 0.04 + 0.1,
                            low=10 + day * 0.04 - 0.1,
                            close=10 + day * 0.04,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 46)
                    ]
                )
                initial = repo.factor_scan(limit=20, use_cache=True)

                def unexpected_universe_lookup(*_args: object, **_kwargs: object) -> list[str]:
                    raise AssertionError("cached factor scan should not rebuild the automatic universe")

                repo.symbols_with_daily_bars = unexpected_universe_lookup  # type: ignore[method-assign]
                cached = repo.factor_scan(limit=20, use_cache=True)
            finally:
                repo.close()

            self.assertTrue(initial)
            self.assertEqual(cached, initial)

    def test_ths_monitor_reports_realtime_cache_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shase = root / "realtime" / "shase"
            sznse = root / "realtime" / "sznse"
            shase.mkdir(parents=True)
            sznse.mkdir(parents=True)
            (shase / "stocknow.dat").write_bytes(b"hd1.0" + b"\0" * 32)
            (sznse / "stocknow.dat").write_bytes(b"hd1.0" + b"\0" * 32)

            now = datetime.now()
            snapshot = inspect_ths_source(root, now=now)
            html = render_ths_monitor_page(root)

            self.assertEqual([item.market for item in snapshot.files], ["shase", "sznse"])
            self.assertIn(snapshot.files[0].status, {"active", "stale"})
            self.assertIn("同花顺数据源", html)
            self.assertIn("A 股实时缓存", html)

            old_time = (now - timedelta(hours=2)).timestamp()
            import os

            os.utime(shase / "stocknow.dat", (old_time, old_time))
            old_snapshot = inspect_ths_source(root, now=now)
            self.assertEqual(old_snapshot.files[0].status, "old")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--ths-root", str(root), "ths-monitor"]), 0)
            self.assertIn("Realtime stocknow.dat", output.getvalue())

    def test_ths_news_import_feeds_ai_decision_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            news_path = root / "实时解盘.xml"
            news_path.write_bytes(
                (
                    '<?xml version="1.0" encoding="gbk"?>\n'
                    "<infodata><dataset><data>"
                    "<title><![CDATA[中芯国际：AI芯片需求带动订单增长]]></title>"
                    "<time>1783420041</time><id>n1</id>"
                    "<properties><![CDATA[ctime=1783420041\n"
                    "summ=中芯国际受益于AI算力和半导体国产化需求，订单持续增长。\n"
                    "imp=3\nsource=同花顺7x24快讯\n]]></properties>"
                    "</data></dataset></infodata>"
                ).encode("gbk")
            )

            items = load_ths_news_xml(news_path)
            self.assertEqual(items[0].title, "中芯国际：AI芯片需求带动订单增长")
            self.assertIn("AI算力", items[0].tags)

            db = root / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_news_items(items)
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="688981",
                            name="中芯国际",
                            latest_price=154.0,
                            pct_change=2.5,
                            volume=100_000_000,
                            amount=9_000_000_000,
                            open=150.0,
                            high=156.0,
                            low=149.0,
                            previous_close=150.25,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=1_300_000_000_000,
                            turnover_rate=6.9,
                            board="科创板",
                        )
                    ]
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="688981",
                            trade_date=f"2026-06-{day:02d}",
                            open=120 + day,
                            high=121 + day,
                            low=119 + day,
                            close=120 + day,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 26)
                    ]
                )
                repo.score_latest_quotes()
                decision = analyze_symbol(repo, "688981")
                news_rows = repo.latest_news(query="中芯")
                html = render_news_page(repo, query="中芯")
                detail_html = render_symbol_detail(repo, "688981")
            finally:
                repo.close()

            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual(decision.evidence["news"][0]["title"], "中芯国际：AI芯片需求带动订单增长")
            self.assertIn("相关新闻", decision.summary)
            self.assertEqual(news_rows[0]["news_id"], "n1")
            self.assertIn("资讯列表", html)
            self.assertIn("相关新闻", detail_html)

    def test_eastmoney_public_announcements_parse_jsonp_and_feed_related_news(self) -> None:
        payload = (
            'jQuery1123({"data":{"list":[{'
            '"art_code":"AN202607101826875477",'
            '"codes":[{"stock_code":"000538","short_name":"云南白药"}],'
            '"columns":[{"column_name":"调研活动"}],'
            '"display_time":"2026-07-10 17:13:03:756",'
            '"title":"云南白药:2026年7月10日调研活动附件之投资者调研会议记录",'
            '"title_ch":"云南白药:2026年7月10日调研活动附件之投资者调研会议记录"'
            '}]}})'
        )

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return payload.encode("utf-8")

        def fake_open(_request: object, timeout: float = 10.0) -> FakeResponse:
            self.assertEqual(timeout, 5.0)
            return FakeResponse()

        items = fetch_eastmoney_announcements(["000538"], per_symbol=2, timeout=5.0, opener=fake_open)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].news_id, "eastmoney:AN202607101826875477")
        self.assertEqual(items[0].source, "东方财富公告")
        self.assertEqual(items[0].event_time, "2026-07-10 17:13:03")
        self.assertIn("000538 云南白药", items[0].summary)
        self.assertIn("公告", items[0].tags)
        self.assertNotIn("政策监管", items[0].tags)
        self.assertNotIn("并购投资", items[0].tags)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_news_items(items)
                rows = repo.related_news_for_symbol("000538", name="云南白药")
            finally:
                repo.close()

        self.assertEqual(rows[0]["title"], "云南白药:2026年7月10日调研活动附件之投资者调研会议记录")

    def test_reclassify_news_separates_performance_risk_from_positive_catalysts(self) -> None:
        self.assertIn("业绩风险", classify_news("公司发布业绩预亏，净利润同比下降", ""))
        self.assertNotIn("业绩利好", classify_news("公司发布业绩预亏，净利润同比下降", ""))
        self.assertIn("业绩利好", classify_news("公司发布业绩预增，预计净利润增长", ""))
        recovery_tags = classify_news(
            "渤海租赁：预计上半年净利润30亿元-36亿元",
            "预计2026年上半年净利润30亿元到36亿元，上年同期亏损20.19亿元。",
        )
        self.assertIn("业绩利好", recovery_tags)
        self.assertNotIn("业绩风险", recovery_tags)
        self.assertIn("业绩风险", classify_news("公司预计净亏损4亿元", "上年同期亏损1亿元。"))
        self.assertIn("减持质押", classify_news("控股股东拟减持公司股份", ""))
        self.assertNotIn("政策监管", classify_news("公司受益于促消费政策", ""))

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_news_items(
                    [
                        NewsItem("risk", "平安银行：业绩预亏，净利润同比下降", "000001 平安银行", "测试", None, None, "业绩预告", Path("test")),
                        NewsItem("positive", "平安银行：业绩预增，预计净利润增长", "000001 平安银行", "测试", None, None, "业绩预告", Path("test")),
                        NewsItem("neutral", "平安银行：投资者关系活动记录表", "000001 平安银行", "测试", None, None, "业绩预告", Path("test")),
                    ]
                )
            finally:
                repo.close()

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "reclassify-news"]), 0)

            repo = Repository(db)
            try:
                rows = {str(row["news_id"]): row for row in repo.latest_news(limit=10)}
            finally:
                repo.close()

        self.assertIn("业绩风险", rows["risk"]["tags"])
        self.assertIn("业绩利好", rows["positive"]["tags"])
        self.assertEqual(rows["neutral"]["tags"], "资讯")
        self.assertEqual(summarize_news_signal([rows["risk"], rows["positive"], rows["neutral"]]), {"positive": 1.0, "risk": 1.0})
        self.assertIn("Reclassified news tags: 3", output.getvalue())

    def test_import_public_announcements_cli_populates_news_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            item = NewsItem(
                news_id="eastmoney:AN1",
                title="云南白药:调研活动记录",
                summary="000538 云南白药；公告栏目：调研活动",
                source="东方财富公告",
                event_time="2026-07-10 17:13:03",
                importance=None,
                tags="公告",
                source_file=Path("public/eastmoney_announcements/000538"),
            )

            with patch("ths_stock_picker.cli.fetch_eastmoney_announcements", return_value=[item]) as mocked_fetch:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(
                        [
                            "--db",
                            str(db),
                            "import-public-announcements",
                            "000538",
                            "--per-symbol",
                            "1",
                        ]
                    )

            self.assertEqual(code, 0)
            mocked_fetch.assert_called_once()
            self.assertIn("Imported public announcements: 1", output.getvalue())

            repo = Repository(db)
            try:
                rows = repo.latest_news(query="云南白药")
            finally:
                repo.close()

            self.assertEqual(rows[0]["source"], "东方财富公告")

    def test_import_cli_populates_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ths = make_fake_ths(root)
            db = root / "picker.db"

            code = main(["--ths-root", str(ths), "--db", str(db), "import"])

            self.assertEqual(code, 0)
            repo = Repository(db)
            try:
                counts = repo.table_counts()
            finally:
                repo.close()
            self.assertEqual(counts["securities"], 3)
            self.assertEqual(counts["market_snapshots"], 2)
            self.assertEqual(counts["quotes_realtime"], 2)
            self.assertGreaterEqual(counts["watchlists"], 2)

    def test_score_is_empty_when_no_realtime_fields_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ths = make_fake_ths(root)
            db = root / "picker.db"

            self.assertEqual(main(["--ths-root", str(ths), "--db", str(db), "import"]), 0)
            self.assertEqual(main(["--ths-root", str(ths), "--db", str(db), "score"]), 0)

            repo = Repository(db)
            try:
                counts = repo.table_counts()
            finally:
                repo.close()
            self.assertEqual(counts["scores"], 0)

    def test_capture_symbols_cli_writes_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ths = make_fake_ths(root)
            output = root / "capture.json"

            self.assertEqual(main(["--ths-root", str(ths), "capture-symbols", "600000", "--out", str(output)]), 0)

            self.assertTrue(output.exists())
            self.assertIn("600000", output.read_text(encoding="utf-8"))

    def test_import_history_cli_populates_daily_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            csv_path = root / "daily.csv"
            csv_path.write_text(
                "代码,日期,开盘,最高,最低,收盘,成交量,成交额\n"
                "600000,20260708,12.1,12.5,12.0,12.3,10000,123000\n",
                encoding="utf-8-sig",
            )

            self.assertEqual(main(["--db", str(db), "import-history", str(csv_path)]), 0)

            repo = Repository(db)
            try:
                counts = repo.table_counts()
            finally:
                repo.close()
            self.assertEqual(counts["daily_bars"], 1)

    def test_import_tdx_history_cli_populates_daily_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tdx_root = root / "tdx"
            lday_dir = tdx_root / "vipdoc" / "sh" / "lday"
            lday_dir.mkdir(parents=True)
            (lday_dir / "sh600000.day").write_bytes(
                struct.pack("IIIIIfII", 20260708, 897, 901, 887, 900, 131376013.0, 14700900, 0)
            )
            db = root / "picker.db"
            output = io.StringIO()

            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db),
                            "import-tdx-history",
                            "600000",
                            "--tdx-root",
                            str(tdx_root),
                            "--replace-existing",
                        ]
                    ),
                    0,
                )

            self.assertIn("TDX daily bars: 1", output.getvalue())
            repo = Repository(db)
            try:
                rows = repo.recent_daily_bars("600000", limit=5)
            finally:
                repo.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trade_date"], "2026-07-08")
            self.assertEqual(rows[0]["close"], 9.0)

    def test_public_quotes_enable_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="600000",
                            name="浦发银行",
                            latest_price=8.96,
                            pct_change=0.79,
                            volume=25809663,
                            amount=230464262,
                            open=8.85,
                            high=9.0,
                            low=8.79,
                            previous_close=8.89,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                        )
                    ]
                )
                scored = repo.score_latest_quotes()
                counts = repo.table_counts()
            finally:
                repo.close()

            self.assertEqual(scored, 1)
            self.assertEqual(counts["scores"], 1)

    def test_scoring_components_include_liquidity_and_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                        )
                    ]
                )
                repo.score_latest_quotes()
                row = repo.latest_scores(1)[0]
            finally:
                repo.close()

            components = json.loads(row["components_json"])
            self.assertIn("liquidity", components)
            self.assertIn("intraday_position", components)
            self.assertGreater(row["total_score"], 0)

    def test_score_cli_applies_custom_profile_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "name": "liquidity-heavy",
                        "component_weights": {"liquidity": 2.0},
                        "disabled_components": ["board_quality"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            board="深主板",
                        )
                    ]
                )
            finally:
                repo.close()

            self.assertEqual(main(["--db", str(db), "score", "--profile", str(profile_path)]), 0)
            repo = Repository(db)
            try:
                row = repo.latest_scores(1)[0]
                runs = repo.latest_score_runs(1)
            finally:
                repo.close()

            components = json.loads(row["components_json"])
            self.assertEqual(components["liquidity"], 60.0)
            self.assertNotIn("board_quality", components)
            self.assertEqual(row["profile_name"], "liquidity-heavy")
            self.assertEqual(runs[0]["profile_name"], "liquidity-heavy")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "score-runs", "--limit", "1"]), 0)
            self.assertIn("liquidity-heavy", output.getvalue())

    def test_compare_runs_reports_latest_score_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps({"name": "momentum-off", "disabled_components": ["intraday_momentum"]}),
                encoding="utf-8",
            )
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            board="深主板",
                        )
                    ]
                )
            finally:
                repo.close()

            self.assertEqual(main(["--db", str(db), "score"]), 0)
            self.assertEqual(main(["--db", str(db), "score", "--profile", str(profile_path)]), 0)
            repo = Repository(db)
            try:
                changes = repo.compare_score_runs(limit=5)
                html = render_dashboard(repo)
            finally:
                repo.close()

            self.assertEqual(changes[0]["symbol"], "000001")
            self.assertEqual(changes[0]["status"], "down")
            self.assertLess(changes[0]["delta"], 0)
            self.assertIn("批次变化", html)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "compare-runs", "--limit", "5"]), 0)
            self.assertIn("000001", output.getvalue())
            self.assertIn("down", output.getvalue())

    def test_write_default_profile_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            out = root / "scoring.json"
            db = root / "unused.db"

            self.assertEqual(main(["--db", str(db), "write-default-profile", "--out", str(out)]), 0)

            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("component_weights", payload)
            self.assertIn("liquidity", payload["component_weights"])

    def test_stock_notes_cli_and_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"

            self.assertEqual(
                main(["--db", str(db), "note", "688981", "--status", "watch", "--tags", "半导体,放量", "--text", "等回踩"]),
                0,
            )

            repo = Repository(db)
            try:
                note = repo.stock_note("688981")
                notes = repo.list_stock_notes(status="watch")
            finally:
                repo.close()

            self.assertIsNotNone(note)
            self.assertEqual(note["status"], "watch")
            self.assertIn("半导体", note["tags"])
            self.assertEqual(notes[0]["symbol"], "688981")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "notes", "--limit", "5", "--status", "watch", "--q", "放量"]), 0)
            self.assertIn("688981", output.getvalue())

            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--db", str(db), "delete-note", "688981"]), 0)
            repo = Repository(db)
            try:
                self.assertIsNone(repo.stock_note("688981"))
            finally:
                repo.close()

    def test_web_notes_page_lists_and_filters_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="688981",
                            name="中芯国际",
                            latest_price=154.0,
                            pct_change=5.5,
                            volume=100_000_000,
                            amount=9_000_000_000,
                            open=145.0,
                            high=156.0,
                            low=142.0,
                            previous_close=145.0,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=1_300_000_000_000,
                            turnover_rate=6.9,
                            board="科创板",
                        )
                    ]
                )
                repo.score_latest_quotes()
                repo.upsert_stock_note("688981", status="review", tags="半导体", note="复盘放量")
                html_all = render_notes_page(repo)
                html_review = render_notes_page(repo, status="review")
                html_watch = render_notes_page(repo, status="watch")
                html_query = render_notes_page(repo, filters=NotesFilters(query="放量", sort="score"))
                csv_text = render_notes_csv(repo, filters=NotesFilters(status="review"))
            finally:
                repo.close()

            self.assertIn("本地观察池", html_all)
            self.assertIn("中芯国际", html_all)
            self.assertIn("复盘放量", html_review)
            self.assertNotIn("中芯国际", html_watch)
            self.assertIn("/export/notes.csv", html_query)
            self.assertIn("/notes/delete", html_query)
            self.assertIn("pct_change", csv_text)
            self.assertIn("688981", csv_text)

    def test_scoring_uses_daily_bar_trend_factors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="600000",
                            name="浦发银行",
                            latest_price=12.0,
                            pct_change=1.0,
                            volume=20_000_000,
                            amount=200_000_000,
                            open=11.8,
                            high=12.1,
                            low=11.7,
                            previous_close=11.88,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=100_000_000_000,
                            turnover_rate=1.0,
                            board="沪主板",
                        )
                    ]
                )
                bars = [
                    DailyBar(
                        symbol="600000",
                        trade_date=f"2026-06-{day:02d}",
                        open=10 + day * 0.05,
                        high=10 + day * 0.05 + 0.1,
                        low=10 + day * 0.05 - 0.1,
                        close=10 + day * 0.05,
                        volume=10_000_000,
                        amount=None,
                        source_file=Path("test"),
                    )
                    for day in range(1, 26)
                ]
                repo.upsert_daily_bars(bars)
                repo.score_latest_quotes()
                row = repo.latest_scores(1)[0]
            finally:
                repo.close()

            components = json.loads(row["components_json"])
            self.assertIn("trend_ma5", components)
            self.assertIn("trend_ma20", components)
            self.assertIn("momentum_20d", components)

    def test_scores_cli_handles_saved_scores_without_current_quote_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                with repo.conn:
                    score_run = repo.conn.execute(
                        "INSERT INTO score_runs (score_date, profile_name, profile_json) VALUES (?, ?, ?)",
                        ("2026-07-08", "default", "{}"),
                    )
                    repo.conn.execute(
                        """
                        INSERT INTO scores
                            (score_run_id, score_date, symbol, profile_name, total_score, components_json, triggered_rules_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (score_run.lastrowid, "2026-07-08", "600000", "default", 66.0, "{}", "[]"),
                    )
                coverage = repo.latest_score_quote_coverage()
                ai_html = render_ai_page(repo)
                dashboard_html = render_dashboard(repo)
            finally:
                repo.close()

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "scores", "--positive-only"]), 0)

        self.assertIn("600000", output.getvalue())
        self.assertIn("pct=-", output.getvalue())
        self.assertEqual(coverage["score_count"], 1)
        self.assertEqual(coverage["priced_score_count"], 0)
        self.assertIn("AI 候选暂不可用", ai_html)
        self.assertIn("带价格行情 0 / 1", ai_html)
        self.assertIn("AI 候选暂不可用", dashboard_html)

    def test_local_quote_import_preserves_public_prices_for_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="600000",
                            name="浦发银行",
                            latest_price=12.0,
                            pct_change=1.0,
                            volume=20_000_000,
                            amount=200_000_000,
                            open=11.8,
                            high=12.1,
                            low=11.7,
                            previous_close=11.88,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=100_000_000_000,
                            turnover_rate=1.0,
                            board="沪主板",
                        )
                    ]
                )
                repo.insert_quotes(
                    [
                        QuoteRealtime(
                            symbol="600000",
                            name="浦发银行",
                            market="shase",
                            latest_price=None,
                            pct_change=None,
                            volume=None,
                            amount=None,
                            observed_at="2026-07-09 09:31:00",
                            quote_status="code_only",
                        )
                    ]
                )
                score_count = repo.score_latest_quotes()
                quote = repo.latest_quote_for_symbol("600000")
                candidates = repo.latest_candidates(limit=5, min_score=1)
                coverage = repo.latest_score_quote_coverage()
            finally:
                repo.close()

        self.assertIsNotNone(quote)
        self.assertEqual(quote["latest_price"], 12.0)
        self.assertEqual(score_count, 1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["latest_price"], 12.0)
        self.assertEqual(coverage["score_count"], 1)
        self.assertEqual(coverage["priced_score_count"], 1)

    def test_public_quote_refresh_keeps_previous_symbols_when_request_is_partial(self) -> None:
        def observation(symbol: str, name: str, price: float, observed_at: str) -> QuoteObservation:
            return QuoteObservation(
                symbol=symbol,
                name=name,
                latest_price=price,
                pct_change=1.0,
                volume=20_000_000,
                amount=200_000_000,
                open=price - 0.2,
                high=price + 0.1,
                low=price - 0.3,
                previous_close=price - 0.12,
                observed_at=observed_at,
                source="test",
                market_cap=100_000_000_000,
                turnover_rate=1.0,
                board="沪主板",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        observation("600000", "浦发银行", 12.0, "2026-07-08 10:52:47"),
                        observation("000001", "平安银行", 10.0, "2026-07-08 10:52:47"),
                    ]
                )
                refreshed = repo.upsert_public_quotes(
                    [observation("600000", "浦发银行", 12.5, "2026-07-09 10:52:47")]
                )
                empty_refresh = repo.upsert_public_quotes([])
                first_quote = repo.latest_quote_for_symbol("600000")
                second_quote = repo.latest_quote_for_symbol("000001")
                public_count = repo.conn.execute(
                    "SELECT COUNT(*) AS count FROM quotes_realtime WHERE quote_status = 'public_quote'"
                ).fetchone()["count"]
                score_count = repo.score_latest_quotes()
            finally:
                repo.close()

        self.assertEqual(refreshed, 1)
        self.assertEqual(empty_refresh, 0)
        self.assertEqual(public_count, 2)
        self.assertEqual(first_quote["latest_price"], 12.5)
        self.assertEqual(second_quote["latest_price"], 10.0)
        self.assertEqual(score_count, 2)

    def test_ai_decision_generates_symbol_thesis_and_can_be_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="688981",
                            name="中芯国际",
                            latest_price=154.0,
                            pct_change=2.5,
                            volume=100_000_000,
                            amount=9_000_000_000,
                            open=150.0,
                            high=156.0,
                            low=149.0,
                            previous_close=150.25,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=1_300_000_000_000,
                            turnover_rate=6.9,
                            board="科创板",
                        )
                    ]
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="688981",
                            trade_date=f"2026-06-{day:02d}",
                            open=120 + day,
                            high=121 + day,
                            low=119 + day,
                            close=120 + day,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 26)
                    ]
                )
                repo.upsert_stock_note("688981", status="watch", tags="半导体", note="观察回踩")
                repo.score_latest_quotes()
                decision = analyze_symbol(repo, "688981")
                ranked = rank_candidates(repo, limit=5)
                html = render_ai_page(repo)
                detail_html = render_symbol_detail(repo, "688981")
                diagnose_html = render_diagnose_page(repo, "688981")
            finally:
                repo.close()

            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertIn(decision.decision, {"重点观察", "观察", "等待回踩", "谨慎复盘", "回避"})
            self.assertIn("中芯国际", decision.summary)
            self.assertTrue(decision.trigger_conditions)
            self.assertTrue(decision.invalidation_conditions)
            self.assertEqual(ranked[0].symbol, "688981")
            self.assertIn("AI 选股", html)
            self.assertIn("AI 候选观点", html)
            self.assertIn("公式因子", html)
            self.assertIn("/notes/quick-add", html)
            self.assertIn("加入观察", html)
            self.assertIn("触发条件", detail_html)
            self.assertIn("失效条件", detail_html)
            self.assertIn("数据覆盖状态", detail_html)
            self.assertIn("近60日线", detail_html)
            self.assertIn("一键诊股", diagnose_html)
            self.assertIn("中芯国际", diagnose_html)
            self.assertIn("触发条件", diagnose_html)
            self.assertIn("数据覆盖状态", diagnose_html)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "ai-explain", "688981", "--save"]), 0)
            self.assertIn("中芯国际", output.getvalue())
            self.assertIn("Trigger conditions:", output.getvalue())
            self.assertIn("Invalidation conditions:", output.getvalue())
            diagnose_output = io.StringIO()
            with redirect_stdout(diagnose_output):
                self.assertEqual(main(["--db", str(db), "diagnose", "688981"]), 0)
            self.assertIn("Diagnosis: 688981", diagnose_output.getvalue())
            self.assertIn("Conclusion:", diagnose_output.getvalue())
            repo = Repository(db)
            try:
                rows = repo.latest_ai_decisions(5)
                filtered = repo.latest_ai_decisions(5, symbol="688981")
                history_html = render_ai_history_page(repo)
                repo.insert_ai_decisions(
                    [
                        {
                            "symbol": "688981",
                            "name": "中芯国际",
                            "decision": "回避",
                            "confidence": 40.0,
                            "summary": "旧观点",
                            "thesis_json": "{}",
                        }
                    ]
                )
                repo.insert_ai_decisions(
                    [
                        {
                            "symbol": "688981",
                            "name": "中芯国际",
                            "decision": "重点观察",
                            "confidence": 90.0,
                            "summary": "新观点",
                            "thesis_json": "{}",
                        }
                    ]
                )
                changes = repo.ai_decision_changes(limit=5)
                changes_html = render_ai_changes_page(repo)
            finally:
                repo.close()
            self.assertEqual(rows[0]["symbol"], "688981")
            self.assertEqual(filtered[0]["symbol"], "688981")
            saved_thesis = json.loads(rows[0]["thesis_json"])
            self.assertIn("trigger_conditions", saved_thesis)
            self.assertIn("invalidation_conditions", saved_thesis)
            self.assertIn("AI 历史", history_html)
            self.assertIn("中芯国际", history_html)
            self.assertEqual(changes[0]["status"], "changed")
            self.assertIn("AI 变化", changes_html)
            self.assertIn("结论变化", changes_html)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "ai-history", "--symbol", "688981"]), 0)
            self.assertIn("688981", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "ai-changes"]), 0)
            self.assertIn("changed", output.getvalue())

    def test_ai_decision_outcomes_use_next_open_and_keep_incomplete_observations_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="600000",
                            trade_date=trade_date,
                            open=open_price,
                            high=close_price + 0.2,
                            low=open_price - 0.2,
                            close=close_price,
                            volume=1_000_000,
                            amount=None,
                            source_file=Path("tdx://lday/sh/sh600000.day"),
                        )
                        for trade_date, open_price, close_price in [
                            ("2026-01-01", 9.5, 10.0),
                            ("2026-01-02", 10.0, 10.5),
                            ("2026-01-03", 10.5, 11.0),
                            ("2026-01-04", 11.0, 12.0),
                        ]
                    ]
                )
                repo.insert_ai_decisions(
                    [
                        {
                            "symbol": "600000",
                            "name": "浦发银行",
                            "decision": "观察",
                            "confidence": 70.0,
                            "summary": "已到期观点",
                            "thesis_json": json.dumps({"evidence": {"score_date": "2026-01-01"}}),
                        },
                        {
                            "symbol": "000001",
                            "name": "平安银行",
                            "decision": "观察",
                            "confidence": 65.0,
                            "summary": "尚未到期观点",
                            "thesis_json": json.dumps({"evidence": {"score_date": "2026-01-05"}}),
                        },
                    ]
                )
                repo.insert_ai_decisions(
                    [
                        {
                            "symbol": "600000",
                            "name": "浦发银行",
                            "decision": "重点观察",
                            "confidence": 88.0,
                            "summary": "同日较新观点",
                            "thesis_json": json.dumps({"evidence": {"score_date": "2026-01-01"}}),
                        }
                    ]
                )
                outcomes = repo.ai_decision_outcomes(limit=10, horizon_days=3)
                outcomes_by_symbol = {str(row["symbol"]): row for row in outcomes}
                outcome_summary = summarize_ai_decision_outcomes(outcomes)
                outcomes_html = render_ai_outcomes_page(repo, limit=10, horizon=3)
            finally:
                repo.close()

            evaluated = outcomes_by_symbol["600000"]
            pending = outcomes_by_symbol["000001"]
            self.assertEqual(len(outcomes), 2)
            self.assertEqual(evaluated["entry_date"], "2026-01-02")
            self.assertEqual(evaluated["decision"], "重点观察")
            self.assertEqual(evaluated["entry_price"], 10.0)
            self.assertEqual(evaluated["exit_date"], "2026-01-04")
            self.assertEqual(evaluated["exit_price"], 12.0)
            self.assertAlmostEqual(float(evaluated["return_pct"]), 20.0)
            self.assertEqual(evaluated["status"], "evaluated")
            self.assertEqual(pending["status"], "pending")
            self.assertEqual(pending["status_label"], "待下一交易日")
            self.assertEqual(outcome_summary["evaluated"], 1)
            self.assertEqual(outcome_summary["pending"], 1)
            self.assertEqual(outcome_summary["positive"], 1)
            self.assertAlmostEqual(float(outcome_summary["hit_rate"]), 100.0)
            self.assertAlmostEqual(float(outcome_summary["average_return"]), 20.0)
            self.assertIn("AI 观点复盘", outcomes_html)
            self.assertIn("按保存结论汇总", outcomes_html)
            self.assertIn("已完成", outcomes_html)
            self.assertIn("待下一交易日", outcomes_html)

            repo = Repository(db)
            try:
                repo.init_schema()
                repo.insert_ai_decisions(
                    [
                        {
                            "symbol": "600000",
                            "name": "浦发银行",
                            "decision": "观察",
                            "confidence": 72.0,
                            "summary": "同日替换观点",
                            "thesis_json": json.dumps({"evidence": {"score_date": "2026-01-01"}}),
                        }
                    ],
                    replace_same_signal=True,
                )
                same_day_rows = [
                    row
                    for row in repo.latest_ai_decisions(limit=10, symbol="600000")
                    if json.loads(row["thesis_json"])["evidence"]["score_date"] == "2026-01-01"
                ]
            finally:
                repo.close()
            self.assertEqual(len(same_day_rows), 1)
            self.assertEqual(same_day_rows[0]["summary"], "同日替换观点")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "ai-outcomes", "--limit", "10", "--horizon", "3"]), 0)
            self.assertIn("600000", output.getvalue())
            self.assertIn("outcome=+20.00%", output.getvalue())

    def test_explain_cli_prints_score_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="600000",
                            name="浦发银行",
                            latest_price=12.0,
                            pct_change=1.0,
                            volume=20_000_000,
                            amount=200_000_000,
                            open=11.8,
                            high=12.1,
                            low=11.7,
                            previous_close=11.88,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=100_000_000_000,
                            turnover_rate=1.0,
                            board="沪主板",
                        )
                    ]
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="600000",
                            trade_date=f"2026-06-{day:02d}",
                            open=10 + day * 0.05,
                            high=10 + day * 0.05 + 0.1,
                            low=10 + day * 0.05 - 0.1,
                            close=10 + day * 0.05,
                            volume=10_000_000,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 8)
                    ]
                )
                repo.score_latest_quotes()
            finally:
                repo.close()

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--db", str(db), "explain", "600000", "--bars", "2"])

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("浦发银行", text)
            self.assertIn("Components:", text)
            self.assertIn("Recent daily bars:", text)

    def test_candidates_filter_risky_names_and_write_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            candidates_path = root / "candidates.csv"
            report_path = root / "daily_report.md"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_stock_industries(
                    [
                        IndustryClassification("000001", "金融-银行-股份制与城商行"),
                        IndustryClassification("000016", "金融-银行-股份制与城商行"),
                    ]
                )
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                        ),
                        QuoteObservation(
                            symbol="000016",
                            name="*ST康佳A",
                            latest_price=2.5,
                            pct_change=4.17,
                            volume=10_000_000,
                            amount=39_000_000,
                            open=2.4,
                            high=2.5,
                            low=2.38,
                            previous_close=2.4,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                        ),
                    ]
                )
                repo.upsert_news_items(
                    [
                        NewsItem(
                            news_id="eastmoney:PA1",
                            title="平安银行:关于投资者关系活动记录表",
                            summary="000001 平安银行；公告栏目：调研活动",
                            source="东方财富公告",
                            event_time="2026-07-08 18:00:00",
                            importance=None,
                            tags="公告",
                            source_file=Path("public/eastmoney_announcements/000001"),
                        )
                    ]
                )
                repo.score_latest_quotes()
                count = repo.write_candidates_csv(candidates_path)
                report_count = repo.write_daily_report(report_path)
            finally:
                repo.close()

            self.assertEqual(count, 1)
            self.assertEqual(report_count, 1)
            candidates_text = candidates_path.read_text(encoding="utf-8-sig")
            self.assertIn("平安银行", candidates_text)
            self.assertIn("news_count", candidates_text)
            self.assertIn("latest_news_title", candidates_text)
            self.assertIn("平安银行:关于投资者关系活动记录表", candidates_text)
            self.assertNotIn("*ST康佳A", candidates_text)
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("# A 股选股日报", report_text)
            self.assertIn("消息面", report_text)
            self.assertIn("1条：平安银行:关于投资者关系活动记录表", report_text)
            self.assertIn("## 当前行业热度", report_text)
            self.assertIn("金融-银行-股份制与城商行", report_text)

    def test_web_dashboard_renders_candidates_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=230_000_000_000,
                            turnover_rate=1.2,
                            board="深主板",
                        )
                    ]
                )
                repo.upsert_news_items(
                    [
                        NewsItem(
                            news_id="eastmoney:PA-dashboard",
                            title="平安银行:关于投资者关系活动记录表",
                            summary="000001 平安银行；公告栏目：调研活动",
                            source="东方财富公告",
                            event_time="2026-07-08 18:00:00",
                            importance=None,
                            tags="公告",
                            source_file=Path("public/eastmoney_announcements/000001"),
                        )
                    ]
                )
                repo.score_latest_quotes()
                html = render_dashboard(repo)
            finally:
                repo.close()

            self.assertIn("A 股选股面板", html)
            self.assertIn("平安银行", html)
            self.assertIn("/symbol/000001", html)
            self.assertIn("当前评分 default", html)
            self.assertIn("候选池", html)
            self.assertIn("消息面", html)
            self.assertIn("1条：平安银行:关于投资者关系活动记录表", html)
            self.assertIn("评分榜", html)

    def test_web_dashboard_filters_by_query_and_board(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=230_000_000_000,
                            turnover_rate=1.2,
                            board="深主板",
                        ),
                        QuoteObservation(
                            symbol="688981",
                            name="中芯国际",
                            latest_price=154.0,
                            pct_change=5.5,
                            volume=100_000_000,
                            amount=9_000_000_000,
                            open=145.0,
                            high=156.0,
                            low=142.0,
                            previous_close=145.0,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=1_300_000_000_000,
                            turnover_rate=6.9,
                            board="科创板",
                        ),
                    ]
                )
                repo.score_latest_quotes()
                html = render_dashboard(repo, filters=DashboardFilters(query="中芯", board="科创板"))
            finally:
                repo.close()

            self.assertIn("中芯国际", html)
            self.assertNotIn("平安银行", html)
            self.assertIn('name="q" value="中芯"', html)
            self.assertIn("/export/candidates.csv", html)

    def test_web_dashboard_candidate_export_uses_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=230_000_000_000,
                            turnover_rate=1.2,
                            board="深主板",
                        ),
                        QuoteObservation(
                            symbol="688981",
                            name="中芯国际",
                            latest_price=154.0,
                            pct_change=5.5,
                            volume=100_000_000,
                            amount=9_000_000_000,
                            open=145.0,
                            high=156.0,
                            low=142.0,
                            previous_close=145.0,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=1_300_000_000_000,
                            turnover_rate=6.9,
                            board="科创板",
                        ),
                    ]
                )
                repo.upsert_news_items(
                    [
                        NewsItem(
                            news_id="eastmoney:SMIC1",
                            title="中芯国际:港股公告证券变动月报表",
                            summary="688981 中芯国际；公告栏目：港股公告",
                            source="东方财富公告",
                            event_time="2026-07-03 19:10:30",
                            importance=None,
                            tags="公告",
                            source_file=Path("public/eastmoney_announcements/688981"),
                        )
                    ]
                )
                repo.score_latest_quotes()
                csv_text = render_candidates_csv(repo, filters=DashboardFilters(query="中芯", board="科创板"))
            finally:
                repo.close()

            self.assertIn("688981", csv_text)
            self.assertIn("中芯国际", csv_text)
            self.assertIn("news_count", csv_text)
            self.assertIn("latest_news_title", csv_text)
            self.assertIn("中芯国际:港股公告证券变动月报表", csv_text)
            self.assertNotIn("000001", csv_text)

    def test_web_dashboard_symbol_detail_renders_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=230_000_000_000,
                            turnover_rate=1.2,
                            board="深主板",
                        )
                    ]
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date="2026-07-08",
                            open=10.4,
                            high=10.6,
                            low=10.3,
                            close=10.55,
                            volume=46_000_000,
                            amount=None,
                            source_file=Path("test"),
                        )
                    ]
                )
                repo.upsert_stock_note("000001", status="review", tags="银行,低波", note="观察缩量回踩")
                repo.score_latest_quotes()
                html = render_symbol_detail(repo, "000001")
            finally:
                repo.close()

            self.assertIn("000001 平安银行", html)
            self.assertIn("分项分", html)
            self.assertIn("触发规则", html)
            self.assertIn("最近日线", html)
            self.assertIn("本地观察记录", html)
            self.assertIn("观察缩量回踩", html)
            self.assertIn("公式型因子", html)

    def test_web_dashboard_symbol_detail_renders_svg_chart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_public_quotes(
                    [
                        QuoteObservation(
                            symbol="000001",
                            name="平安银行",
                            latest_price=10.55,
                            pct_change=0.76,
                            volume=46_000_000,
                            amount=490_000_000,
                            open=10.44,
                            high=10.59,
                            low=10.34,
                            previous_close=10.47,
                            observed_at="2026-07-08 10:52:47",
                            source="test",
                            market_cap=230_000_000_000,
                            turnover_rate=1.2,
                            board="深主板",
                        )
                    ]
                )
                repo.upsert_daily_bars(
                    [
                        DailyBar(
                            symbol="000001",
                            trade_date=f"2026-07-{day:02d}",
                            open=10 + day * 0.02,
                            high=10 + day * 0.02 + 0.1,
                            low=10 + day * 0.02 - 0.1,
                            close=10 + day * 0.02,
                            volume=10_000_000 + day,
                            amount=None,
                            source_file=Path("test"),
                        )
                        for day in range(1, 6)
                    ]
                )
                repo.score_latest_quotes()
                html = render_symbol_detail(repo, "000001")
            finally:
                repo.close()

            self.assertIn("日线走势", html)
            self.assertIn("<svg", html)
            self.assertIn("price-line", html)

    def test_auto_universe_prefers_watchlist_when_enough_valid_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.replace_watchlists(
                    [
                        WatchlistEntry("自选", "600000", "600000", Path("watch.ini"), "parsed"),
                        WatchlistEntry("自选", "600001", "600001", Path("watch.ini"), "parsed"),
                        WatchlistEntry("自选", "600002", "600002", Path("watch.ini"), "parsed"),
                        WatchlistEntry("自选", "000001", "000001", Path("watch.ini"), "parsed"),
                        WatchlistEntry("自选", "300033", "300033", Path("watch.ini"), "parsed"),
                        WatchlistEntry("自选", "000000", "000000", Path("watch.ini"), "parsed"),
                    ]
                )
                source, symbols = repo.list_auto_universe_symbols(limit=10)
            finally:
                repo.close()

            self.assertEqual(source, "watchlist")
            self.assertEqual(symbols, ["000001", "300033", "600000", "600001", "600002"])

    def test_auto_universe_falls_back_when_watchlist_is_too_small(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.replace_watchlists([WatchlistEntry("自选", "300033", "300033", Path("watch.ini"), "parsed")])
                repo.replace_securities(
                    [Security("600000", "600000", "浦发银行", "16", Path("stockname.txt"), "stock")]
                )
                source, symbols = repo.list_auto_universe_symbols(limit=10)
            finally:
                repo.close()

            self.assertEqual(source, "securities")
            self.assertEqual(symbols, ["600000"])

    def test_security_universe_filters_risky_and_non_a_share_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.replace_securities(
                    [
                        Security("600000", "600000", "浦发银行", "16", Path("stockname.txt"), "stock"),
                        Security("000001", "000001", "平安银行", "32", Path("stockname.txt"), "stock"),
                        Security("000016", "000016", "*ST康佳A", "32", Path("stockname.txt"), "stock"),
                        Security("399006", "399006", "创业板指", "32", Path("stockname.txt"), "index"),
                    ]
                )
                symbols = repo.list_security_universe_symbols(limit=10)
            finally:
                repo.close()

            self.assertEqual(set(symbols), {"000001", "600000"})
