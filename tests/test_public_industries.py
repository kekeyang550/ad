from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ths_stock_picker.cli import main
from ths_stock_picker.models import QuoteRealtime
from ths_stock_picker.public_industries import IndustryClassification, fetch_eastmoney_industry_one
from ths_stock_picker.storage import Repository
from ths_stock_picker.web_panel import render_data_health_page, render_industries_page, render_symbol_detail


class PublicIndustriesTests(unittest.TestCase):
    def test_fetches_current_eastmoney_industry_label(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"jbzl": [{"EM2016": "金融-银行-股份制与城商行", "INDUSTRYCSRC1": "金融业-货币金融服务"}]}).encode("utf-8")

        with patch("ths_stock_picker.public_industries.urllib.request.urlopen", return_value=FakeResponse()) as mocked_urlopen:
            row = fetch_eastmoney_industry_one("000001")

        self.assertIsNotNone(row)
        self.assertEqual((row.symbol, row.industry), ("000001", "金融-银行-股份制与城商行"))
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("CompanySurvey", request.full_url)
        self.assertIn("code=SZ000001", request.full_url)

    def test_falls_back_to_regulatory_industry_when_eastmoney_classification_is_absent(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"jbzl": [{"EM2016": "", "INDUSTRYCSRC1": "金融业-货币金融服务"}]}).encode("utf-8")

        with patch("ths_stock_picker.public_industries.urllib.request.urlopen", return_value=FakeResponse()):
            row = fetch_eastmoney_industry_one("000001")

        self.assertIsNotNone(row)
        self.assertEqual(row.industry, "金融业-货币金融服务")

    def test_industry_refresh_prioritizes_missing_then_oldest_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Repository(Path(temp_dir) / "picker.db")
            try:
                repo.init_schema()
                repo.upsert_stock_industries(
                    [
                        IndustryClassification("000001", "银行"),
                        IndustryClassification("000002", "银行"),
                    ]
                )
                with repo.conn:
                    repo.conn.execute(
                        "UPDATE stock_industries SET updated_at = ? WHERE symbol = ?",
                        ("2026-01-01 00:00:00", "000001"),
                    )
                    repo.conn.execute(
                        "UPDATE stock_industries SET updated_at = ? WHERE symbol = ?",
                        ("2026-06-01 00:00:00", "000002"),
                    )
                selected = repo.industry_refresh_symbols(["000002", "000003", "000001"], limit=3)
            finally:
                repo.close()

        self.assertEqual(selected, ["000003", "000001", "000002"])

    def test_industry_health_reports_current_score_coverage_in_cli_and_web(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.upsert_stock_industries(
                    [
                        IndustryClassification("000001", "银行"),
                        IndustryClassification("600000", "银行"),
                    ]
                )
                repo.insert_quotes(
                    [
                        QuoteRealtime("000001", "平安银行", "sz", 12.0, 1.2, 2_000_000, 150_000_000),
                        QuoteRealtime("600000", "浦发银行", "sh", 10.0, -1.0, 1_000_000, 80_000_000),
                    ]
                )
                repo.score_latest_quotes()
                health = repo.industry_health()
                html = render_data_health_page(repo)
            finally:
                repo.close()

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "data-health"]), 0)

        self.assertEqual(health["label_records"], 2)
        self.assertEqual(health["industry_count"], 1)
        self.assertEqual(health["scored_symbols"], 2)
        self.assertIn("Industry label health", output.getvalue())
        self.assertIn("labels=2 industries=1 scored_symbols=2", output.getvalue())
        self.assertIn("行业归属覆盖", html)
        self.assertIn("当前评分已覆盖", html)

    def test_cli_imports_industries_and_web_renders_context_and_heat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            records = [
                IndustryClassification("000001", "银行"),
                IndustryClassification("600000", "银行"),
            ]
            output = io.StringIO()
            with patch("ths_stock_picker.cli.fetch_eastmoney_industry_one", side_effect=records), redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "import-public-industries", "000001", "600000"]), 0)
            repo = Repository(db)
            try:
                repo.init_schema()
                repo.insert_quotes(
                    [
                        QuoteRealtime("000001", "平安银行", "sz", 12.0, 1.2, 2_000_000, 150_000_000),
                        QuoteRealtime("600000", "浦发银行", "sh", 10.0, -1.0, 1_000_000, 80_000_000),
                    ]
                )
                repo.score_latest_quotes()
                industry = repo.industry_for_symbol("000001")
                heat = repo.industry_heat(limit=10, min_scored=1)
                heat_html = render_industries_page(repo, limit=10, min_scored=1)
                detail_html = render_symbol_detail(repo, "000001")
            finally:
                repo.close()

        self.assertIn("Imported public industry labels: 2", output.getvalue())
        self.assertIsNotNone(industry)
        self.assertEqual(industry["industry"], "银行")
        self.assertEqual(heat["items"][0]["scored_count"], 2)
        self.assertIn("行业评分汇总", heat_html)
        self.assertIn("行业归属", detail_html)
        self.assertIn("银行", detail_html)
