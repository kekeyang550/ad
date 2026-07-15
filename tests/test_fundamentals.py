from __future__ import annotations

import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ths_stock_picker.cli import main
from ths_stock_picker.fundamentals import FundamentalRecord, fetch_eastmoney_fundamentals_one, load_fundamentals_csv
from ths_stock_picker.models import QuoteRealtime
from ths_stock_picker.storage import Repository
from ths_stock_picker.web_panel import render_symbol_detail


class FundamentalsImportTests(unittest.TestCase):
    def test_loads_chinese_financial_headers_and_normalizes_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fundamentals.csv"
            path.write_text(
                "股票代码,报告期,营业收入,营业收入同比,归母净利润,归母净利润同比,净资产收益率,经营现金流,市盈率TTM,市净率\n"
                "000001,20260331,123,8.5%,45,6.2%,12.5%,67,8.2,1.1\n",
                encoding="utf-8-sig",
            )

            rows = load_fundamentals_csv(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "000001")
        self.assertEqual(rows[0].report_date, "2026-03-31")
        self.assertEqual(rows[0].revenue, 123.0)
        self.assertEqual(rows[0].revenue_yoy, 8.5)
        self.assertEqual(rows[0].net_profit, 45.0)
        self.assertEqual(rows[0].net_profit_yoy, 6.2)
        self.assertEqual(rows[0].roe, 12.5)
        self.assertEqual(rows[0].pe_ttm, 8.2)

    def test_uses_default_symbol_and_skips_invalid_report_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fundamentals.csv"
            path.write_text("报告期,ROE\n2026/03/31,9.8\n2026/13/01,10\n", encoding="utf-8-sig")

            rows = load_fundamentals_csv(path, default_symbol="600000")

        self.assertEqual([(row.symbol, row.report_date, row.roe) for row in rows], [("600000", "2026-03-31", 9.8)])

    def test_cli_imports_financial_records_and_symbol_detail_renders_latest_period(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "picker.db"
            path = root / "fundamentals.csv"
            path.write_text(
                "代码,报告期,营业收入,净利润,净资产收益率,经营活动产生的现金流量净额,市盈率TTM,市净率\n"
                "000001,2025-12-31,1000,120,11.2,200,8.5,0.9\n"
                "000001,2026-03-31,300,36,12.0,60,8.0,1.0\n",
                encoding="utf-8-sig",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "import-fundamentals", str(path)]), 0)

            repo = Repository(db)
            try:
                repo.init_schema()
                latest = repo.latest_fundamental("000001")
                repo.insert_quotes([QuoteRealtime("000001", "平安银行", "sz", 12.0, 1.0, 2_000_000, 150_000_000)])
                repo.score_latest_quotes()
                html = render_symbol_detail(repo, "000001")
                counts = repo.table_counts()
            finally:
                repo.close()

        self.assertEqual(counts["fundamentals"], 2)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["report_date"], "2026-03-31")
        self.assertEqual(latest["roe"], 12.0)
        self.assertIn("Imported total financial records: 2", output.getvalue())
        self.assertIn("最近财务：2026-03-31", html)
        self.assertIn("金额与比率保持原始 CSV 导出单位", html)

    def test_public_fetch_reads_only_verified_fields_and_notice_date(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "success": True,
                        "result": {
                            "data": [
                                {
                                    "SECURITY_CODE": "000001",
                                    "REPORTDATE": "2026-03-31 00:00:00",
                                    "NOTICE_DATE": "2026-04-25 00:00:00",
                                    "TOTAL_OPERATE_INCOME": 35277000000,
                                    "YSTZ": 4.6515767303,
                                    "PARENT_NETPROFIT": 14523000000,
                                    "SJLTZ": 3.0,
                                    "WEIGHTAVG_ROE": 2.83,
                                }
                            ]
                        },
                    }
                ).encode("utf-8")

        class CashFlowResponse(FakeResponse):
            def read(self) -> bytes:
                return json.dumps(
                    {
                        "success": True,
                        "result": {
                            "data": [
                                {
                                    "SECURITY_CODE": "000001",
                                    "REPORT_DATE": "2026-03-31 00:00:00",
                                    "NOTICE_DATE": "2026-04-25 00:00:00",
                                    "NETCASH_OPERATE": 15500000000,
                                }
                            ]
                        },
                    }
                ).encode("utf-8")

        with patch(
            "ths_stock_picker.fundamentals.urllib.request.urlopen",
            side_effect=[FakeResponse(), CashFlowResponse()],
        ) as mocked_urlopen:
            rows = fetch_eastmoney_fundamentals_one("000001")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].report_date, "2026-03-31")
        self.assertEqual(rows[0].notice_date, "2026-04-25")
        self.assertEqual(rows[0].revenue, 35277000000.0)
        self.assertEqual(rows[0].revenue_yoy, 4.6515767303)
        self.assertEqual(rows[0].net_profit, 14523000000.0)
        self.assertEqual(rows[0].net_profit_yoy, 3.0)
        self.assertEqual(rows[0].operating_cash_flow, 15500000000.0)
        requests = [call.args[0].full_url for call in mocked_urlopen.call_args_list]
        self.assertIn("YSTZ", requests[0])
        self.assertIn("SJLTZ", requests[0])
        self.assertIn("RPT_DMSK_FN_CASHFLOW", requests[1])
        self.assertIn("NETCASH_OPERATE", requests[1])

    def test_public_fetch_keeps_main_report_when_cashflow_supplement_is_unavailable(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "result": {
                            "data": [
                                {
                                    "SECURITY_CODE": "000001",
                                    "REPORTDATE": "2026-03-31 00:00:00",
                                    "NOTICE_DATE": "2026-04-25 00:00:00",
                                    "TOTAL_OPERATE_INCOME": 100,
                                    "PARENT_NETPROFIT": 10,
                                }
                            ]
                        }
                    }
                ).encode("utf-8")

        with patch(
            "ths_stock_picker.fundamentals.urllib.request.urlopen",
            side_effect=[FakeResponse(), OSError("cashflow temporarily unavailable")],
        ):
            rows = fetch_eastmoney_fundamentals_one("000001")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].revenue, 100.0)
        self.assertIsNone(rows[0].operating_cash_flow)

    def test_cli_imports_public_financial_records_without_overwriting_richer_csv_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "picker.db"
            csv_source = Path(temp_dir) / "manual.csv"
            csv_source.write_text(
                "代码,报告期,公告日期,营业收入,净利润,ROE,经营现金流,PE TTM,PB\n000001,2026-03-31,2026-04-20,100,10,5,20,8,1\n",
                encoding="utf-8-sig",
            )
            self.assertEqual(main(["--db", str(db), "import-fundamentals", str(csv_source)]), 0)
            public_record = FundamentalRecord(
                symbol="000001",
                report_date="2026-03-31",
                notice_date="2026-04-25",
                revenue=110.0,
                net_profit=11.0,
                roe=5.5,
                operating_cash_flow=None,
                pe_ttm=None,
                pb=None,
                source_file=Path("eastmoney://RPT_LICO_FN_CPD/000001"),
                revenue_yoy=4.0,
                net_profit_yoy=3.0,
            )
            output = io.StringIO()
            with patch("ths_stock_picker.cli.fetch_eastmoney_fundamentals_one", return_value=[public_record]), redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "import-public-fundamentals", "000001"]), 0)
            repo = Repository(db)
            try:
                repo.init_schema()
                latest = repo.latest_fundamental("000001")
                disclosed = repo.disclosed_fundamental_as_of("000001", "2026-04-28")
            finally:
                repo.close()

        self.assertIsNotNone(latest)
        self.assertEqual(latest["operating_cash_flow"], 20.0)
        self.assertEqual(latest["pe_ttm"], 8.0)
        self.assertEqual(disclosed["operating_cash_flow"], 20.0)
        self.assertEqual(disclosed["revenue_yoy"], 4.0)
        self.assertEqual(disclosed["net_profit_yoy"], 3.0)
        self.assertIn("Fetched public financial records: 1", output.getvalue())

    def test_fundamental_health_reports_cashflow_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Repository(Path(temp_dir) / "picker.db")
            try:
                repo.init_schema()
                repo.upsert_fundamentals(
                    [
                        FundamentalRecord(
                            symbol="000001",
                            report_date="2026-03-31",
                            notice_date="2026-04-25",
                            revenue=100.0,
                            net_profit=10.0,
                            roe=8.0,
                            operating_cash_flow=12.0,
                            pe_ttm=None,
                            pb=None,
                            source_file=Path("public"),
                        ),
                        FundamentalRecord(
                            symbol="000002",
                            report_date="2026-03-31",
                            notice_date="2026-04-25",
                            revenue=100.0,
                            net_profit=10.0,
                            roe=8.0,
                            operating_cash_flow=None,
                            pe_ttm=None,
                            pb=None,
                            source_file=Path("public"),
                        ),
                    ]
                )
                health = repo.fundamental_health(as_of=date(2026, 5, 1))
            finally:
                repo.close()

        self.assertEqual(health["operating_cash_flow_symbols"], 1)
        self.assertEqual(health["operating_cash_flow_records"], 1)
