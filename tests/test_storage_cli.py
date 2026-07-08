from __future__ import annotations

import tempfile
import unittest
import json
import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from ths_stock_picker.ai_decision import analyze_symbol, rank_candidates
from ths_stock_picker.cli import main
from ths_stock_picker.history_import import DailyBar
from ths_stock_picker.news_import import load_ths_news_xml
from ths_stock_picker.quote_observer import QuoteObservation
from ths_stock_picker.storage import Repository
from ths_stock_picker.ths_monitor import inspect_ths_source
from ths_stock_picker.models import Security, WatchlistEntry
from ths_stock_picker.web_panel import (
    DashboardFilters,
    NotesFilters,
    render_candidates_csv,
    render_dashboard,
    render_ai_page,
    render_ai_history_page,
    render_ai_changes_page,
    render_factors_page,
    render_notes_csv,
    render_notes_page,
    render_news_page,
    render_symbol_detail,
    render_ths_monitor_page,
)
from tests.test_ths_local import make_fake_ths


class StorageCliTests(unittest.TestCase):
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
                signals = repo.factor_scan(symbols=["000001"])
                backtest = repo.factor_backtest(horizon_days=3)
                matrix = repo.factor_backtest_matrix(horizons=[3, 5])
            finally:
                repo.close()

            self.assertTrue(any(row["factor_id"] == "ma_multi_breakout" for row in signals))
            self.assertTrue(backtest)
            self.assertIn("samples", backtest[0])
            self.assertTrue(matrix)
            self.assertIn("effectiveness_score", matrix[0])

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
                html = render_factors_page(repo)
            finally:
                repo.close()

            self.assertIn("公式因子", html)
            self.assertIn("因子定义", html)
            self.assertIn("因子多周期回测矩阵", html)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "factors"]), 0)
            self.assertIn("均线共振突破", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "factor-matrix", "--horizons", "3,5"]), 0)
            self.assertIn("Factor matrix", output.getvalue())

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
            finally:
                repo.close()

            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertIn(decision.decision, {"重点观察", "观察", "等待回踩", "谨慎复盘", "回避"})
            self.assertIn("中芯国际", decision.summary)
            self.assertEqual(ranked[0].symbol, "688981")
            self.assertIn("AI 选股", html)
            self.assertIn("AI 候选观点", html)
            self.assertIn("公式因子", html)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "ai-explain", "688981", "--save"]), 0)
            self.assertIn("中芯国际", output.getvalue())
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
                repo.score_latest_quotes()
                count = repo.write_candidates_csv(candidates_path)
                report_count = repo.write_daily_report(report_path)
            finally:
                repo.close()

            self.assertEqual(count, 1)
            self.assertEqual(report_count, 1)
            self.assertIn("平安银行", candidates_path.read_text(encoding="utf-8-sig"))
            self.assertNotIn("*ST康佳A", candidates_path.read_text(encoding="utf-8-sig"))
            self.assertIn("# A 股选股日报", report_path.read_text(encoding="utf-8"))

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
                repo.score_latest_quotes()
                html = render_dashboard(repo)
            finally:
                repo.close()

            self.assertIn("A 股选股面板", html)
            self.assertIn("平安银行", html)
            self.assertIn("/symbol/000001", html)
            self.assertIn("当前评分 default", html)
            self.assertIn("候选池", html)
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
                repo.score_latest_quotes()
                csv_text = render_candidates_csv(repo, filters=DashboardFilters(query="中芯", board="科创板"))
            finally:
                repo.close()

            self.assertIn("688981", csv_text)
            self.assertIn("中芯国际", csv_text)
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
