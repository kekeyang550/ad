from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ai_decision import analyze_symbol, decisions_to_rows, rank_candidates
from .data_readiness import summarize_data_readiness
from .factor_engine import factor_definitions
from .fundamentals import fetch_eastmoney_fundamentals_one, load_fundamentals_csv
from .field_inference import (
    compare_capture_payloads,
    load_observations,
    match_observations,
    read_capture,
    summarize_matches,
    write_capture,
)
from .history_import import fetch_tencent_daily_bars, load_daily_bars_csv
from .news_import import classify_news, fetch_eastmoney_announcements, load_default_ths_news
from .public_industries import fetch_eastmoney_industry_one
from .quote_observer import fetch_tencent_observations, write_observations_csv
from .scoring_profile import load_scoring_profile, write_default_scoring_profile
from .storage import DEFAULT_DB_PATH, Repository, summarize_ai_decision_outcomes
from .ths_local import DEFAULT_THS_ROOT, THSLocalAdapter
from .ths_monitor import inspect_ths_source
from .tdx_local import DEFAULT_TDX_ROOT, inspect_tdx_daily_status, load_tdx_daily_bars
from .tdx_blocks import discover_tdx_block_files, load_tdx_theme_memberships
from .time_utils import display_shanghai_time
from .web_panel import serve_dashboard


DAILY_AUDIT_EXPORT_TABLES = [
    "securities",
    "market_snapshots",
    "quotes_realtime",
    "watchlists",
    "scores",
    "score_runs",
    "stock_themes",
    "stock_industries",
    "stock_notes",
    "ai_decisions",
    "news_items",
    "strategy_validation_runs",
    "daily_runs",
]

DAILY_STRATEGY_SNAPSHOT_OPTIONS: dict[str, object] = {
    "horizon_days": 5,
    "top_n": 10,
    "min_signal_score": 60.0,
    "limit_symbols": 300,
    "cost_bps": 5.0,
    "slippage_bps": 5.0,
    "benchmark_symbol": "sh000300",
    "max_bars": 260,
    "execution_mode": "next_open",
    "position_mode": "non_overlapping",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ths-picker")
    parser.add_argument("--ths-root", type=Path, default=DEFAULT_THS_ROOT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Check local TongHuaShun paths and data files.")
    subparsers.add_parser("ths-monitor", help="Inspect TongHuaShun process and realtime cache freshness.")
    news_import_parser = subparsers.add_parser("import-ths-news", help="Import local TongHuaShun news cache.")
    news_import_parser.add_argument("--limit-per-file", type=int)
    public_news_parser = subparsers.add_parser("import-public-announcements", help="Import public Eastmoney announcements for selected A-share symbols.")
    public_news_parser.add_argument("symbols", nargs="*")
    public_news_parser.add_argument("--universe", choices=["auto", "watchlist", "securities", "cache"], default="auto")
    public_news_parser.add_argument("--limit", type=int, default=30)
    public_news_parser.add_argument("--per-symbol", type=int, default=3)
    public_news_parser.add_argument("--timeout", type=float, default=10.0)
    news_parser = subparsers.add_parser("news", help="Show imported news items.")
    news_parser.add_argument("--limit", type=int, default=30)
    news_parser.add_argument("--q", default="")
    news_parser.add_argument("--tag", default="")
    subparsers.add_parser("reclassify-news", help="Rebuild imported news tags with the current deterministic classifier.")
    subparsers.add_parser("factors", help="Show formula-inspired factor definitions.")
    factor_scan_parser = subparsers.add_parser("factor-scan", help="Scan latest daily bars for formula-inspired factor signals.")
    factor_scan_parser.add_argument("--limit", type=int, default=50)
    factor_scan_parser.add_argument("--symbol", action="append", default=None)
    factor_scan_cache_parser = subparsers.add_parser("refresh-factor-scan-cache", help="Rebuild cached latest factor signals.")
    factor_scan_cache_parser.add_argument("--limit", type=int, default=50)
    factor_scan_cache_parser.add_argument("--symbol", action="append", default=None)
    factor_backtest_parser = subparsers.add_parser("factor-backtest", help="Backtest formula-inspired factors on stored daily bars.")
    factor_backtest_parser.add_argument("--horizon", type=int, default=5)
    factor_backtest_parser.add_argument("--limit-symbols", type=int)
    factor_matrix_parser = subparsers.add_parser("factor-matrix", help="Show multi-horizon factor effectiveness.")
    factor_matrix_parser.add_argument("--horizons", default="3,5,10")
    factor_matrix_parser.add_argument("--limit-symbols", type=int)
    factor_matrix_parser.add_argument("--max-bars", type=int, default=0, help="Recent bars per symbol to use. Pass 0 for all history.")
    factor_cache_parser = subparsers.add_parser("refresh-factor-cache", help="Rebuild cached multi-horizon factor effectiveness.")
    factor_cache_parser.add_argument("--horizons", default="3,5,10")
    factor_cache_parser.add_argument("--limit-symbols", type=int, default=30)
    factor_cache_parser.add_argument("--max-bars", type=int, default=150, help="Recent bars per symbol to use. Pass 0 for all history.")
    strategy_backtest_parser = subparsers.add_parser("strategy-backtest", help="Backtest a factor-composite stock-picking strategy.")
    strategy_backtest_parser.add_argument("--horizon", type=int, default=5)
    strategy_backtest_parser.add_argument("--top-n", type=int, default=10)
    strategy_backtest_parser.add_argument("--min-signal-score", type=float, default=60.0)
    strategy_backtest_parser.add_argument("--limit-symbols", type=int)
    strategy_backtest_parser.add_argument("--cost-bps", type=float, default=0.0, help="One-way transaction cost in basis points.")
    strategy_backtest_parser.add_argument("--slippage-bps", type=float, default=0.0, help="One-way slippage assumption in basis points.")
    strategy_backtest_parser.add_argument("--benchmark-symbol", help="Optional benchmark symbol with stored daily bars, such as sh000300.")
    strategy_backtest_parser.add_argument("--max-bars", type=int, default=260, help="Recent bars per symbol to use. Pass 0 for all history.")
    strategy_backtest_parser.add_argument(
        "--execution",
        choices=["next_open", "signal_close"],
        default="next_open",
        help="Entry timing. next_open avoids using the signal-day close as an executable price.",
    )
    strategy_backtest_parser.add_argument(
        "--position-mode",
        choices=["non_overlapping", "daily_batches"],
        default="non_overlapping",
        help="non_overlapping waits for a batch to exit before the next entry; daily_batches is research-only overlapping cohorts.",
    )
    strategy_backtest_parser.add_argument("--save", action="store_true", help="Save the parameters and result as a local research record.")
    strategy_backtest_runs_parser = subparsers.add_parser(
        "strategy-backtest-runs",
        help="List saved strategy backtest records.",
    )
    strategy_backtest_runs_parser.add_argument("--limit", type=int, default=20)
    walk_forward_parser = subparsers.add_parser("strategy-walkforward", help="Run rolling out-of-sample strategy validation.")
    walk_forward_parser.add_argument("--train-days", type=int, default=252)
    walk_forward_parser.add_argument("--test-days", type=int, default=63)
    walk_forward_parser.add_argument("--max-folds", type=int)
    walk_forward_parser.add_argument("--horizon", type=int, default=5)
    walk_forward_parser.add_argument("--top-n", type=int, default=10)
    walk_forward_parser.add_argument("--min-signal-score", type=float, default=60.0)
    walk_forward_parser.add_argument("--limit-symbols", type=int)
    walk_forward_parser.add_argument("--cost-bps", type=float, default=0.0)
    walk_forward_parser.add_argument("--slippage-bps", type=float, default=0.0)
    walk_forward_parser.add_argument("--benchmark-symbol")
    walk_forward_parser.add_argument("--execution", choices=["next_open", "signal_close"], default="next_open")
    walk_forward_parser.add_argument("--position-mode", choices=["non_overlapping", "daily_batches"], default="non_overlapping")
    strategy_validate_parser = subparsers.add_parser(
        "strategy-validate",
        help="Run, assess, and save rolling out-of-sample strategy validation.",
    )
    strategy_validate_parser.add_argument("--train-days", type=int, default=252)
    strategy_validate_parser.add_argument("--test-days", type=int, default=63)
    strategy_validate_parser.add_argument("--max-folds", type=int)
    strategy_validate_parser.add_argument("--horizon", type=int, default=5)
    strategy_validate_parser.add_argument("--top-n", type=int, default=10)
    strategy_validate_parser.add_argument("--min-signal-score", type=float, default=60.0)
    strategy_validate_parser.add_argument("--limit-symbols", type=int)
    strategy_validate_parser.add_argument("--cost-bps", type=float, default=0.0)
    strategy_validate_parser.add_argument("--slippage-bps", type=float, default=0.0)
    strategy_validate_parser.add_argument("--benchmark-symbol")
    strategy_validate_parser.add_argument("--execution", choices=["next_open", "signal_close"], default="next_open")
    strategy_validate_parser.add_argument("--position-mode", choices=["non_overlapping", "daily_batches"], default="non_overlapping")
    strategy_validate_parser.add_argument("--min-folds", type=int, default=3)
    strategy_validate_parser.add_argument("--min-trades", type=int, default=60)
    strategy_validate_parser.add_argument("--min-positive-fold-ratio", type=float, default=0.6)
    strategy_validate_parser.add_argument("--max-drawdown", type=float, default=-20.0)
    strategy_validate_parser.add_argument("--min-benchmark-excess-return", type=float, default=0.0)
    strategy_validation_runs_parser = subparsers.add_parser(
        "strategy-validation-runs",
        help="List saved walk-forward strategy validation conclusions.",
    )
    strategy_validation_runs_parser.add_argument("--limit", type=int, default=20)
    subparsers.add_parser("import", help="Import read-only local cache metadata into SQLite.")
    score_parser = subparsers.add_parser("score", help="Score parsed realtime quotes when fields are available.")
    score_parser.add_argument("--profile", type=Path, help="Optional JSON scoring profile.")
    profile_parser = subparsers.add_parser("write-default-profile", help="Write a default scoring profile JSON file.")
    profile_parser.add_argument("--out", type=Path, default=Path("configs/scoring.default.json"))
    scores_parser = subparsers.add_parser("scores", help="Show latest stock score ranking.")
    scores_parser.add_argument("--limit", type=int, default=50)
    scores_parser.add_argument("--positive-only", action="store_true")
    score_runs_parser = subparsers.add_parser("score-runs", help="Show recent scoring runs and profiles.")
    score_runs_parser.add_argument("--limit", type=int, default=20)
    compare_runs_parser = subparsers.add_parser("compare-runs", help="Compare two scoring runs.")
    compare_runs_parser.add_argument("--base-run", type=int)
    compare_runs_parser.add_argument("--target-run", type=int)
    compare_runs_parser.add_argument("--limit", type=int, default=50)
    compare_runs_parser.add_argument("--min-score", type=float, default=1.0)
    explain_parser = subparsers.add_parser("explain", help="Explain latest score for one symbol.")
    explain_parser.add_argument("symbol")
    explain_parser.add_argument("--bars", type=int, default=8)
    diagnose_parser = subparsers.add_parser("diagnose", help="Run a one-symbol stock diagnosis from score, AI thesis, notes, and recent bars.")
    diagnose_parser.add_argument("symbol")
    diagnose_parser.add_argument("--bars", type=int, default=8)
    note_parser = subparsers.add_parser("note", help="Create or update a local stock note.")
    note_parser.add_argument("symbol")
    note_parser.add_argument("--status", default="watch", choices=["watch", "hold", "avoid", "review"])
    note_parser.add_argument("--tags", default="")
    note_parser.add_argument("--text", default="")
    notes_parser = subparsers.add_parser("notes", help="List local stock notes.")
    notes_parser.add_argument("--limit", type=int, default=50)
    notes_parser.add_argument("--status", choices=["watch", "hold", "avoid", "review"])
    notes_parser.add_argument("--q", default="", help="Search symbol, name, tags, or note text.")
    notes_parser.add_argument("--sort", choices=["updated", "score", "pct", "price", "symbol"], default="updated")
    delete_note_parser = subparsers.add_parser("delete-note", help="Delete a local stock note.")
    delete_note_parser.add_argument("symbol")
    ai_pick_parser = subparsers.add_parser("ai-pick", help="Generate AI-assisted stock selection theses.")
    ai_pick_parser.add_argument("--limit", type=int, default=20)
    ai_pick_parser.add_argument("--min-score", type=float, default=1.0)
    ai_pick_parser.add_argument("--save", action="store_true", help="Save generated AI decisions into SQLite.")
    ai_explain_parser = subparsers.add_parser("ai-explain", help="Generate an AI-assisted thesis for one symbol.")
    ai_explain_parser.add_argument("symbol")
    ai_explain_parser.add_argument("--save", action="store_true")
    ai_history_parser = subparsers.add_parser("ai-history", help="Show saved AI-assisted stock decisions.")
    ai_history_parser.add_argument("--limit", type=int, default=30)
    ai_history_parser.add_argument("--symbol")
    ai_changes_parser = subparsers.add_parser("ai-changes", help="Compare latest saved AI decisions per symbol.")
    ai_changes_parser.add_argument("--limit", type=int, default=50)
    ai_outcomes_parser = subparsers.add_parser("ai-outcomes", help="Review forward daily-bar performance for saved AI decisions.")
    ai_outcomes_parser.add_argument("--limit", type=int, default=50)
    ai_outcomes_parser.add_argument("--horizon", type=int, default=5)
    ai_outcomes_parser.add_argument("--symbol")
    candidates_parser = subparsers.add_parser("candidates", help="Export and show filtered stock candidates.")
    candidates_parser.add_argument("--limit", type=int, default=50)
    candidates_parser.add_argument("--min-score", type=float, default=1.0)
    candidates_parser.add_argument("--out", type=Path, default=Path("outputs/candidates.csv"))
    report_parser = subparsers.add_parser("report", help="Write a Markdown daily stock-picking report.")
    report_parser.add_argument("--limit", type=int, default=20)
    report_parser.add_argument("--min-score", type=float, default=1.0)
    report_parser.add_argument("--out", type=Path, default=Path("outputs/daily_report.md"))
    subparsers.add_parser("db-info", help="Show SQLite table counts.")
    subparsers.add_parser("data-health", help="Show daily-bar source coverage and source conflicts.")
    snapshots_parser = subparsers.add_parser("snapshots", help="Show latest snapshot diagnostics.")
    snapshots_parser.add_argument("--limit", type=int, default=20)
    export_parser = subparsers.add_parser("export", help="Export SQLite tables to CSV files.")
    export_parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("outputs"),
        help="Directory for exported CSV files.",
    )
    export_parser.add_argument(
                "--table",
        action="append",
        choices=[
            "securities",
            "market_snapshots",
            "quotes_realtime",
            "watchlists",
            "scores",
            "score_runs",
            "daily_bars",
            "stock_notes",
            "ai_decisions",
            "news_items",
            "factor_backtest_cache",
            "strategy_validation_runs",
            "strategy_backtest_runs",
            "daily_runs",
        ],
        help="Table to export. Can be passed multiple times. Defaults to all tables.",
    )
    inspect_parser = subparsers.add_parser(
        "inspect-symbol",
        help="Inspect raw stocknow.dat records for one 6-digit symbol.",
    )
    inspect_parser.add_argument("symbol")
    inspect_parser.add_argument("--json-out", type=Path, help="Optional JSON diagnostics output path.")
    capture_parser = subparsers.add_parser(
        "capture-symbols",
        help="Capture raw stocknow.dat records for selected symbols.",
    )
    capture_parser.add_argument("symbols", nargs="+")
    capture_parser.add_argument(
        "--out",
        type=Path,
        default=Path("outputs/stocknow_capture.json"),
        help="Capture JSON output path.",
    )
    compare_parser = subparsers.add_parser(
        "compare-captures",
        help="Compare two capture JSON files and show changed byte offsets.",
    )
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)
    compare_parser.add_argument("--json-out", type=Path)
    match_parser = subparsers.add_parser(
        "match-observations",
        help="Match manual quote observations against raw record numeric candidates.",
    )
    match_parser.add_argument("--capture", type=Path, required=True)
    match_parser.add_argument("--observations", type=Path, required=True)
    match_parser.add_argument("--tolerance", type=float, default=0.01)
    match_parser.add_argument(
        "--field",
        action="append",
        default=None,
        help="Observation CSV field to match. Defaults to common quote fields.",
    )
    match_parser.add_argument("--json-out", type=Path, default=Path("outputs/field_matches.json"))
    template_parser = subparsers.add_parser(
        "observation-template",
        help="Create a CSV template for manual quote observations.",
    )
    template_parser.add_argument("symbols", nargs="+")
    template_parser.add_argument("--out", type=Path, default=Path("outputs/observations_template.csv"))
    history_parser = subparsers.add_parser(
        "import-history",
        help="Import daily bar CSV files exported from TongHuaShun or another data source.",
    )
    history_parser.add_argument("files", nargs="+", type=Path)
    history_parser.add_argument("--symbol", help="Default symbol when a CSV file does not contain a code column.")
    fundamentals_parser = subparsers.add_parser(
        "import-fundamentals",
        help="Import local financial CSV rows (revenue, profit, ROE, operating cash flow, PE/PB).",
    )
    fundamentals_parser.add_argument("files", nargs="+", type=Path)
    fundamentals_parser.add_argument("--symbol", help="Default symbol when a CSV file does not contain a code column.")
    public_fundamentals_parser = subparsers.add_parser(
        "import-public-fundamentals",
        help="Fetch disclosed revenue, parent net profit, ROE, and operating cash flow from public Eastmoney reports.",
    )
    public_fundamentals_parser.add_argument("symbols", nargs="*")
    public_fundamentals_parser.add_argument("--universe", choices=["auto", "watchlist", "securities", "cache"])
    public_fundamentals_parser.add_argument("--limit", type=int, default=100)
    public_fundamentals_parser.add_argument("--reports", type=int, default=8)
    public_industries_parser = subparsers.add_parser(
        "import-public-industries",
        help="Fetch current A-share industry labels from public Eastmoney company profiles.",
    )
    public_industries_parser.add_argument("symbols", nargs="*")
    public_industries_parser.add_argument("--universe", choices=["auto", "watchlist", "securities", "cache"], default="auto")
    public_industries_parser.add_argument("--limit", type=int, default=100)
    public_history_parser = subparsers.add_parser(
        "import-public-history",
        help="Fetch recent daily bars from public Tencent kline endpoint.",
    )
    public_history_parser.add_argument("symbols", nargs="*")
    public_history_parser.add_argument("--universe", choices=["auto", "watchlist", "securities", "cache"])
    public_history_parser.add_argument("--limit", type=int, default=100)
    public_history_parser.add_argument("--days", type=int, default=80)
    tdx_history_parser = subparsers.add_parser(
        "import-tdx-history",
        help="Import local TongDaXin .day daily bars into SQLite.",
    )
    tdx_history_parser.add_argument("symbols", nargs="*", help="Optional 6-digit symbols to import.")
    tdx_history_parser.add_argument("--tdx-root", type=Path, default=DEFAULT_TDX_ROOT)
    tdx_history_parser.add_argument(
        "--include-indices",
        action="store_true",
        help="Also import recognized broad and industry index .day files.",
    )
    tdx_history_parser.add_argument("--limit-symbols", type=int)
    tdx_history_parser.add_argument("--start-date", default="", help="YYYY-MM-DD lower bound.")
    tdx_history_parser.add_argument("--end-date", default="", help="YYYY-MM-DD upper bound.")
    tdx_history_parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Delete existing daily_bars for imported symbols before inserting TDX bars.",
    )
    tdx_status_parser = subparsers.add_parser("tdx-status", help="Inspect local TDX stock and index daily-bar freshness.")
    tdx_status_parser.add_argument("--tdx-root", type=Path, default=DEFAULT_TDX_ROOT)
    tdx_blocks_parser = subparsers.add_parser(
        "import-tdx-blocks",
        help="Import read-only local TongDaXin concept and style block memberships.",
    )
    tdx_blocks_parser.add_argument("--tdx-root", type=Path, default=DEFAULT_TDX_ROOT)
    tdx_blocks_parser.add_argument(
        "--kind",
        action="append",
        choices=["concept", "style"],
        help="Repeat to import only selected block kinds. Defaults to both.",
    )
    tdx_blocks_status_parser = subparsers.add_parser(
        "tdx-block-status",
        help="Inspect available local TongDaXin concept and style block files.",
    )
    tdx_blocks_status_parser.add_argument("--tdx-root", type=Path, default=DEFAULT_TDX_ROOT)
    themes_parser = subparsers.add_parser("themes", help="List theme heat from local TDX memberships and the latest score run.")
    themes_parser.add_argument("--limit", type=int, default=50)
    themes_parser.add_argument("--category", choices=["概念", "风格"])
    themes_parser.add_argument("--min-scored", type=int, default=3, help="Minimum latest-score coverage required for ranking.")
    industries_parser = subparsers.add_parser("industries", help="List current industry score heat from imported public labels.")
    industries_parser.add_argument("--limit", type=int, default=50)
    industries_parser.add_argument("--min-scored", type=int, default=3)
    observe_parser = subparsers.add_parser(
        "auto-observe",
        help="Fetch public quote observations for selected symbols.",
    )
    observe_parser.add_argument("symbols", nargs="+")
    observe_parser.add_argument("--out", type=Path, default=Path("outputs/observations_auto.csv"))
    public_parser = subparsers.add_parser(
        "import-public-quotes",
        help="Fetch public quotes and write price fields into quotes_realtime.",
    )
    public_parser.add_argument("symbols", nargs="*")
    public_parser.add_argument("--from-cache", action="store_true", help="Use symbols already parsed from THS stocknow.dat.")
    public_parser.add_argument(
        "--universe",
        choices=["auto", "watchlist", "securities", "cache"],
        help="Choose a symbol universe when explicit symbols are not provided.",
    )
    public_parser.add_argument("--limit", type=int, default=200)
    public_parser.add_argument("--observations-out", type=Path, default=Path("outputs/observations_public.csv"))
    universe_parser = subparsers.add_parser("universe", help="Preview symbols used for public quote fetching.")
    universe_parser.add_argument("--source", choices=["auto", "watchlist", "securities", "cache"], default="auto")
    universe_parser.add_argument("--limit", type=int, default=50)
    daily_parser = subparsers.add_parser(
        "run-daily",
        help="Run the daily pipeline: THS import, public quotes, score, export.",
    )
    daily_parser.add_argument("--limit", type=int, default=200)
    daily_parser.add_argument("--out-dir", type=Path, default=Path("outputs"))
    daily_parser.add_argument("--universe", choices=["auto", "watchlist", "securities", "cache"], default="auto")
    daily_parser.add_argument("--history-days", type=int, default=80)
    daily_parser.add_argument(
        "--public-fundamentals",
        action="store_true",
        help="Also fetch disclosed public financial reports for the selected universe.",
    )
    daily_parser.add_argument(
        "--public-fundamental-reports",
        type=int,
        default=8,
        help="Maximum report periods to fetch per symbol when --public-fundamentals is enabled.",
    )
    daily_parser.add_argument(
        "--public-fundamental-limit",
        type=int,
        default=100,
        help="Maximum symbols to fetch when --public-fundamentals is enabled.",
    )
    daily_parser.add_argument(
        "--public-industries",
        action="store_true",
        help="Also fetch current public industry labels for the selected universe.",
    )
    daily_parser.add_argument(
        "--public-industry-limit",
        type=int,
        default=100,
        help="Maximum symbols to fetch when --public-industries is enabled.",
    )
    daily_parser.add_argument("--profile", type=Path, help="Optional JSON scoring profile.")
    daily_parser.add_argument("--tdx-root", type=Path, help="Optional local TongDaXin root for daily-bar synchronization.")
    daily_parser.add_argument("--tdx-include-indices", action="store_true", help="Also synchronize recognized TDX indices.")
    daily_parser.add_argument(
        "--tdx-import-themes",
        action="store_true",
        help="Also synchronize local TongDaXin concept and style block memberships.",
    )
    daily_parser.add_argument(
        "--tdx-start-date",
        default="",
        help="Optional inclusive TDX start date (YYYY-MM-DD). Defaults to the latest stored TDX date.",
    )
    daily_parser.add_argument("--public-announcements", action="store_true", help="Also import public Eastmoney announcements for the daily universe.")
    daily_parser.add_argument("--public-announcement-limit", type=int, default=30)
    daily_parser.add_argument("--public-announcements-per-symbol", type=int, default=3)
    daily_parser.add_argument(
        "--strategy-snapshot",
        action="store_true",
        help="Also save a conservative, reproducible research strategy backtest after scoring. Failures do not block the daily update.",
    )
    daily_runs_parser = subparsers.add_parser("daily-runs", help="List saved daily pipeline runs and failures.")
    daily_runs_parser.add_argument("--limit", type=int, default=20)
    serve_parser = subparsers.add_parser("serve", help="Start a read-only local web dashboard.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--limit", type=int, default=30)
    infer_parser = subparsers.add_parser(
        "auto-infer-fields",
        help="Capture THS records, fetch public observations, and match candidate field offsets.",
    )
    infer_parser.add_argument("symbols", nargs="+")
    infer_parser.add_argument("--capture-out", type=Path, default=Path("outputs/capture_auto.json"))
    infer_parser.add_argument("--observations-out", type=Path, default=Path("outputs/observations_auto.csv"))
    infer_parser.add_argument("--matches-out", type=Path, default=Path("outputs/field_matches_auto.json"))
    infer_parser.add_argument("--tolerance", type=float, default=0.01)
    args = parser.parse_args(argv)

    adapter = THSLocalAdapter(args.ths_root)
    repo = Repository(args.db)
    try:
        repo.init_schema()
        if args.command == "status":
            return _status(adapter)
        if args.command == "ths-monitor":
            return _ths_monitor(args.ths_root)
        if args.command == "import-ths-news":
            return _import_ths_news(repo, args.ths_root, args.limit_per_file)
        if args.command == "import-public-announcements":
            return _import_public_announcements(repo, args.symbols, args.universe, args.limit, args.per_symbol, args.timeout)
        if args.command == "news":
            return _news(repo, args.limit, args.q, args.tag)
        if args.command == "reclassify-news":
            return _reclassify_news(repo)
        if args.command == "factors":
            return _factors()
        if args.command == "factor-scan":
            return _factor_scan(repo, args.limit, args.symbol)
        if args.command == "refresh-factor-scan-cache":
            return _refresh_factor_scan_cache(repo, args.limit, args.symbol)
        if args.command == "factor-backtest":
            return _factor_backtest(repo, args.horizon, args.limit_symbols)
        if args.command == "factor-matrix":
            return _factor_matrix(repo, args.horizons, args.limit_symbols, args.max_bars)
        if args.command == "refresh-factor-cache":
            return _refresh_factor_cache(repo, args.horizons, args.limit_symbols, args.max_bars)
        if args.command == "strategy-backtest":
            return _strategy_backtest(
                repo,
                args.horizon,
                args.top_n,
                args.min_signal_score,
                args.limit_symbols,
                args.cost_bps,
                args.slippage_bps,
                args.benchmark_symbol,
                args.max_bars,
                args.execution,
                args.position_mode,
                args.save,
            )
        if args.command == "strategy-backtest-runs":
            return _strategy_backtest_runs(repo, args.limit)
        if args.command == "strategy-walkforward":
            return _strategy_walk_forward(repo, args)
        if args.command == "strategy-validate":
            return _strategy_validate(repo, args)
        if args.command == "strategy-validation-runs":
            return _strategy_validation_runs(repo, args.limit)
        if args.command == "import":
            return _import(adapter, repo)
        if args.command == "score":
            return _score(repo, args.profile)
        if args.command == "write-default-profile":
            return _write_default_profile(args.out)
        if args.command == "scores":
            return _scores(repo, args.limit, args.positive_only)
        if args.command == "score-runs":
            return _score_runs(repo, args.limit)
        if args.command == "compare-runs":
            return _compare_runs(repo, args.base_run, args.target_run, args.limit, args.min_score)
        if args.command == "explain":
            return _explain(repo, args.symbol, args.bars)
        if args.command == "diagnose":
            return _diagnose(repo, args.symbol, args.bars)
        if args.command == "note":
            return _note(repo, args.symbol, args.status, args.tags, args.text)
        if args.command == "notes":
            return _notes(repo, args.limit, args.status, args.q, args.sort)
        if args.command == "delete-note":
            return _delete_note(repo, args.symbol)
        if args.command == "ai-pick":
            return _ai_pick(repo, args.limit, args.min_score, args.save)
        if args.command == "ai-explain":
            return _ai_explain(repo, args.symbol, args.save)
        if args.command == "ai-history":
            return _ai_history(repo, args.limit, args.symbol)
        if args.command == "ai-changes":
            return _ai_changes(repo, args.limit)
        if args.command == "ai-outcomes":
            return _ai_outcomes(repo, args.limit, args.horizon, args.symbol)
        if args.command == "candidates":
            return _candidates(repo, args.limit, args.min_score, args.out)
        if args.command == "report":
            return _report(repo, args.limit, args.min_score, args.out)
        if args.command == "db-info":
            return _db_info(repo)
        if args.command == "data-health":
            return _data_health(repo)
        if args.command == "snapshots":
            return _snapshots(repo, args.limit)
        if args.command == "export":
            return _export(repo, args.out_dir, args.table)
        if args.command == "inspect-symbol":
            return _inspect_symbol(adapter, args.symbol, args.json_out)
        if args.command == "capture-symbols":
            return _capture_symbols(adapter, args.symbols, args.out)
        if args.command == "compare-captures":
            return _compare_captures(args.before, args.after, args.json_out)
        if args.command == "match-observations":
            return _match_observations(
                args.capture,
                args.observations,
                args.field,
                args.tolerance,
                args.json_out,
            )
        if args.command == "observation-template":
            return _observation_template(args.symbols, args.out)
        if args.command == "import-history":
            return _import_history(repo, args.files, args.symbol)
        if args.command == "import-fundamentals":
            return _import_fundamentals(repo, args.files, args.symbol)
        if args.command == "import-public-fundamentals":
            return _import_public_fundamentals(repo, args.symbols, args.universe, args.limit, args.reports)
        if args.command == "import-public-industries":
            return _import_public_industries(repo, args.symbols, args.universe, args.limit)
        if args.command == "import-public-history":
            return _import_public_history(repo, args.symbols, args.universe, args.limit, args.days)
        if args.command == "import-tdx-history":
            return _import_tdx_history(
                repo,
                args.tdx_root,
                args.symbols,
                args.include_indices,
                args.limit_symbols,
                args.start_date,
                args.end_date,
                args.replace_existing,
            )
        if args.command == "tdx-status":
            return _tdx_status(args.tdx_root)
        if args.command == "import-tdx-blocks":
            return _import_tdx_blocks(repo, args.tdx_root, args.kind)
        if args.command == "tdx-block-status":
            return _tdx_block_status(args.tdx_root)
        if args.command == "themes":
            return _themes(repo, args.limit, args.category or "", args.min_scored)
        if args.command == "industries":
            return _industries(repo, args.limit, args.min_scored)
        if args.command == "auto-observe":
            return _auto_observe(args.symbols, args.out)
        if args.command == "import-public-quotes":
            return _import_public_quotes(repo, args.symbols, args.from_cache, args.universe, args.limit, args.observations_out)
        if args.command == "universe":
            return _universe(repo, args.source, args.limit)
        if args.command == "run-daily":
            return _run_daily(
                adapter,
                repo,
                args.limit,
                args.out_dir,
                args.universe,
                args.history_days,
                args.public_fundamentals,
                args.public_fundamental_reports,
                args.public_fundamental_limit,
                args.public_industries,
                args.public_industry_limit,
                args.profile,
                args.tdx_root,
                args.tdx_include_indices,
                args.tdx_import_themes,
                args.tdx_start_date,
                args.public_announcements,
                args.public_announcement_limit,
                args.public_announcements_per_symbol,
                args.strategy_snapshot,
            )
        if args.command == "daily-runs":
            return _daily_runs(repo, args.limit)
        if args.command == "serve":
            repo.close()
            serve_dashboard(args.db, host=args.host, port=args.port, limit=args.limit, ths_root=args.ths_root)
            return 0
        if args.command == "auto-infer-fields":
            return _auto_infer_fields(
                adapter,
                args.symbols,
                args.capture_out,
                args.observations_out,
                args.matches_out,
                args.tolerance,
            )
    finally:
        repo.close()
    return 1


def _status(adapter: THSLocalAdapter) -> int:
    missing = adapter.validate()
    if missing:
        print("Status: invalid")
        for item in missing:
            print(f"- {item}")
        return 2

    stockname_files = list(adapter.stockname_dir.glob("stockname_*.txt"))
    stocknow_files = list(adapter.realtime_dir.glob("*/stocknow.dat"))
    print("Status: ok")
    print(f"Root: {adapter.root}")
    print(f"Stockname files: {len(stockname_files)}")
    print(f"Realtime stocknow files: {len(stocknow_files)}")
    print(f"Market config: {adapter.realtime_dir / 'market.txt'}")
    return 0


def _ths_monitor(ths_root: Path) -> int:
    snapshot = inspect_ths_source(ths_root)
    print(f"THS root: {snapshot.root}")
    print(f"Checked at: {snapshot.checked_at:%Y-%m-%d %H:%M:%S}")
    print(f"Overall: {snapshot.overall_status} - {snapshot.message}")
    print("Processes:")
    for item in snapshot.processes:
        pid = item.pid if item.pid is not None else "-"
        print(f"- {item.name}: {'running' if item.running else 'stopped'} pid={pid} path={item.path or '-'}")
    print("Realtime stocknow.dat:")
    for item in snapshot.files:
        age = "-" if item.age_seconds is None else f"{item.age_seconds:.0f}s"
        mtime = "-" if item.mtime is None else item.mtime.strftime("%Y-%m-%d %H:%M:%S")
        print(f"- {item.market}: {item.status} size={item.size} mtime={mtime} age={age} path={item.path}")
    return 0


def _import_ths_news(repo: Repository, ths_root: Path, limit_per_file: int | None) -> int:
    items = load_default_ths_news(ths_root, limit_per_file=limit_per_file)
    count = repo.upsert_news_items(items)
    print(f"Imported THS news items: {count}")
    for item in items[:10]:
        print(f"- {item.event_time or '-'} {item.title} [{item.tags}]")
    return 0


def _import_public_announcements(
    repo: Repository,
    symbols: list[str],
    universe: str,
    limit: int,
    per_symbol: int,
    timeout: float,
) -> int:
    selected = sorted({symbol.strip() for symbol in symbols if symbol.strip()})
    if not selected:
        source, selected = _select_universe_symbols(repo, universe, limit)
        print(f"Using {source} universe for announcements: {len(selected)} symbols")
    if not selected:
        print("No symbols available for public announcements. Pass symbols or import/score a universe first.")
        return 1
    items = fetch_eastmoney_announcements(selected[:limit], per_symbol=per_symbol, timeout=timeout)
    imported = repo.upsert_news_items(items)
    print(f"Fetched public announcements: symbols={len(selected[:limit])} items={len(items)}")
    print(f"Imported public announcements: {imported}")
    for item in items[:10]:
        print(f"- {item.event_time or '-'} {item.title} [{item.tags}]")
    return 0 if items else 1


def _news(repo: Repository, limit: int, query: str, tag: str) -> int:
    rows = repo.latest_news(limit=limit, query=query, tag=tag)
    if not rows:
        print("No news found. Run import-ths-news or import-public-announcements first.")
        return 0
    for row in rows:
        print(f"{row['event_time'] or '-'} {row['title']} [{row['tags']}] {row['source'] or '-'}")
        if row["summary"]:
            print(f"   {row['summary'][:120]}")
    return 0


def _reclassify_news(repo: Repository) -> int:
    updated = repo.reclassify_news_tags(classify_news)
    print(f"Reclassified news tags: {updated}")
    return 0


def _factors() -> int:
    for item in factor_definitions():
        print(f"{item.factor_id} {item.name} [{item.category}] risk={item.future_function_risk}")
        print(f"   {item.description}")
        print(f"   source: {item.source}")
    return 0


def _factor_scan(repo: Repository, limit: int, symbols: list[str] | None) -> int:
    rows = repo.factor_scan(limit=limit, symbols=symbols, use_cache=True)
    if not rows:
        print("No factor signals found. Import enough daily bars first.")
        return 0
    for row in rows:
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['factor_name']} "
            f"{row['direction']} strength={float(row['strength']):.1f}"
        )
        print(f"   {row['reason']}")
    return 0


def _refresh_factor_scan_cache(repo: Repository, limit: int, symbols: list[str] | None) -> int:
    rows = repo.refresh_factor_scan_cache(limit=limit, symbols=symbols)
    print(f"Refreshed factor signal cache limit={limit} signals={len(rows)}")
    return 0


def _factor_backtest(repo: Repository, horizon: int, limit_symbols: int | None) -> int:
    rows = repo.factor_backtest(horizon_days=horizon, limit_symbols=limit_symbols)
    if not rows:
        print("No factor backtest samples found. Import more historical daily bars first.")
        return 0
    print(f"Factor backtest horizon={horizon} trading days")
    for row in rows:
        print(
            f"{row['factor_id']} {row['factor_name']} samples={row['samples']} "
            f"win_rate={float(row['win_rate']):.1f}% avg={float(row['avg_return']):.2f}% "
            f"best={float(row['best_return']):.2f}% worst={float(row['worst_return']):.2f}%"
        )
    return 0


def _factor_matrix(repo: Repository, horizons_text: str, limit_symbols: int | None, max_bars: int = 0) -> int:
    horizons = _parse_horizons(horizons_text)
    selected_max_bars = None if max_bars <= 0 else max_bars
    rows = repo.factor_backtest_matrix(horizons=horizons, limit_symbols=limit_symbols, max_bars=selected_max_bars, use_cache=True)
    if not rows:
        print("No factor matrix samples found. Import more historical daily bars first.")
        return 0
    print(
        f"Factor matrix horizons={','.join(str(item) for item in horizons)} "
        f"limit_symbols={limit_symbols or 'all'} max_bars={'all' if selected_max_bars is None else selected_max_bars}"
    )
    for row in rows:
        parts = []
        horizon_stats = row["horizons"]
        for horizon in horizons:
            stats = horizon_stats.get(horizon, {}) if isinstance(horizon_stats, dict) else {}
            if stats:
                parts.append(
                    f"{horizon}d: n={stats['samples']} win={float(stats['win_rate']):.1f}% avg={float(stats['avg_return']):.2f}%"
                )
            else:
                parts.append(f"{horizon}d: -")
        print(
            f"{row['factor_id']} {row['factor_name']} verdict={row['verdict']} "
            f"score={float(row['effectiveness_score']):.1f} samples={row['total_samples']} | "
            + " | ".join(parts)
        )
    return 0


def _refresh_factor_cache(repo: Repository, horizons_text: str, limit_symbols: int | None, max_bars: int) -> int:
    horizons = _parse_horizons(horizons_text)
    selected_max_bars = None if max_bars <= 0 else max_bars
    rows = repo.refresh_factor_backtest_cache(horizons=horizons, limit_symbols=limit_symbols, max_bars=selected_max_bars)
    print(
        f"Refreshed factor cache horizons={','.join(str(item) for item in horizons)} "
        f"limit_symbols={limit_symbols or 'all'} max_bars={'all' if selected_max_bars is None else selected_max_bars} "
        f"factors={len(rows)}"
    )
    return 0


def _strategy_backtest(
    repo: Repository,
    horizon: int,
    top_n: int,
    min_signal_score: float,
    limit_symbols: int | None,
    cost_bps: float,
    slippage_bps: float,
    benchmark_symbol: str | None,
    max_bars: int,
    execution_mode: str,
    position_mode: str,
    save: bool = False,
) -> int:
    selected_max_bars = None if max_bars <= 0 else max_bars
    result = repo.strategy_backtest(
        horizon_days=horizon,
        top_n=top_n,
        min_signal_score=min_signal_score,
        limit_symbols=limit_symbols,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
        benchmark_symbol=benchmark_symbol,
        max_bars=selected_max_bars,
        execution_mode=execution_mode,
        position_mode=position_mode,
    )
    parameters: dict[str, object] = {
        "horizon_days": horizon,
        "top_n": top_n,
        "min_signal_score": min_signal_score,
        "limit_symbols": limit_symbols,
        "cost_bps": cost_bps,
        "slippage_bps": slippage_bps,
        "benchmark_symbol": benchmark_symbol,
        "max_bars": selected_max_bars,
        "execution_mode": execution_mode,
        "position_mode": position_mode,
    }
    print(
        f"Strategy backtest horizon={horizon} top_n={top_n} min_signal_score={min_signal_score:g} "
        f"cost_bps={cost_bps:g} slippage_bps={slippage_bps:g} "
        f"execution={execution_mode} "
        f"position_mode={position_mode} "
        f"max_bars={'all' if selected_max_bars is None else selected_max_bars} "
        f"trades={result['trade_count']} days={result['day_count']}"
    )
    print(
        f"locked_limit_skipped entries={result.get('skipped_locked_entries', 0)} "
        f"exits={result.get('skipped_locked_exits', 0)}"
    )
    saved_run_id = repo.save_strategy_backtest_run(parameters, result) if save else None
    if int(result["trade_count"]) == 0:
        print("No strategy trades found. Import more daily bars or lower min-signal-score.")
        if saved_run_id is not None:
            print(f"saved_backtest_run={saved_run_id}")
        return 0
    print(
        f"win_rate={float(result['win_rate']):.1f}% net_avg={float(result['avg_return']):.2f}% "
        f"gross_avg={float(result.get('gross_avg_return') or 0.0):.2f}% "
        f"net_portfolio_avg={float(result['portfolio_avg_return']):.2f}% "
        f"max_dd={float(result['max_drawdown']):.2f}% "
        f"best={float(result['best_return']):.2f}% worst={float(result['worst_return']):.2f}%"
    )
    benchmark = result.get("benchmark")
    if isinstance(benchmark, dict) and int(benchmark.get("sample_count") or 0) > 0:
        excess = result.get("excess_portfolio_avg_return")
        print(
            f"benchmark={benchmark['symbol']} samples={benchmark['sample_count']} "
            f"avg={float(benchmark['avg_return']):.2f}% cumulative={float(benchmark['cumulative_return']):.2f}% "
            f"max_dd={float(benchmark['max_drawdown']):.2f}% "
            f"excess_portfolio_avg={float(excess):.2f}%"
        )
    elif isinstance(benchmark, dict):
        print(f"benchmark={benchmark['symbol']} samples=0 (import matching daily bars first)")
    period_stats = result.get("period_stats", {})
    if isinstance(period_stats, dict):
        for row in period_stats.get("yearly", []):
            print(
                f"year={row['period']} batches={row['batches']} trades={row['trades']} "
                f"batch_win={float(row['win_rate']):.1f}% net_avg={float(row['avg_return']):.2f}% "
                f"cumulative={float(row['cumulative_return']):.2f}% max_dd={float(row['max_drawdown']):.2f}%"
            )
        for row in list(period_stats.get("monthly", []))[-12:]:
            print(
                f"month={row['period']} batches={row['batches']} trades={row['trades']} "
                f"batch_win={float(row['win_rate']):.1f}% net_avg={float(row['avg_return']):.2f}% "
                f"cumulative={float(row['cumulative_return']):.2f}% max_dd={float(row['max_drawdown']):.2f}%"
            )
    for row in result["trades"][:20]:
        print(
            f"signal={row.get('signal_date', row['trade_date'])} entry={row['trade_date']}->{row['exit_date']} "
            f"{row['symbol']} {row['name'] or '-'} "
            f"score={float(row['signal_score']):.1f} net={float(row['return_pct']):.2f}% "
            f"gross={float(row.get('gross_return_pct', row['return_pct'])):.2f}% factors={row['factors']}"
        )
    if saved_run_id is not None:
        print(f"saved_backtest_run={saved_run_id}")
    return 0


def _strategy_walk_forward(repo: Repository, args: argparse.Namespace) -> int:
    result = repo.strategy_walk_forward(
        train_days=args.train_days,
        test_days=args.test_days,
        max_folds=args.max_folds,
        horizon_days=args.horizon,
        top_n=args.top_n,
        min_signal_score=args.min_signal_score,
        limit_symbols=args.limit_symbols,
        cost_bps=args.cost_bps,
        slippage_bps=args.slippage_bps,
        benchmark_symbol=args.benchmark_symbol,
        execution_mode=args.execution,
        position_mode=args.position_mode,
    )
    _print_strategy_walk_forward(result)
    return 0


def _strategy_validate(repo: Repository, args: argparse.Namespace) -> int:
    parameters: dict[str, object] = {
        "train_days": args.train_days,
        "test_days": args.test_days,
        "max_folds": args.max_folds,
        "horizon_days": args.horizon,
        "top_n": args.top_n,
        "min_signal_score": args.min_signal_score,
        "limit_symbols": args.limit_symbols,
        "cost_bps": args.cost_bps,
        "slippage_bps": args.slippage_bps,
        "benchmark_symbol": args.benchmark_symbol,
        "execution_mode": args.execution,
        "position_mode": args.position_mode,
        "min_folds": args.min_folds,
        "min_trades": args.min_trades,
        "min_positive_fold_ratio": args.min_positive_fold_ratio,
        "max_drawdown": args.max_drawdown,
        "min_benchmark_excess_return": args.min_benchmark_excess_return,
    }
    result = repo.validate_strategy_walk_forward(
        train_days=args.train_days,
        test_days=args.test_days,
        max_folds=args.max_folds,
        horizon_days=args.horizon,
        top_n=args.top_n,
        min_signal_score=args.min_signal_score,
        limit_symbols=args.limit_symbols,
        cost_bps=args.cost_bps,
        slippage_bps=args.slippage_bps,
        benchmark_symbol=args.benchmark_symbol,
        execution_mode=args.execution,
        position_mode=args.position_mode,
        min_folds=args.min_folds,
        min_trades=args.min_trades,
        min_positive_fold_ratio=args.min_positive_fold_ratio,
        max_drawdown=args.max_drawdown,
        min_benchmark_excess_return=args.min_benchmark_excess_return,
    )
    _print_strategy_walk_forward(result)
    assessment = result["assessment"]
    print(f"validation_verdict={assessment['verdict']} {assessment['summary']}")
    for reason in assessment["reasons"]:
        print(f"reason={reason}")
    run_id = repo.save_strategy_validation_run(parameters, result, assessment)
    print(f"saved_validation_run={run_id}")
    return 0


def _print_strategy_walk_forward(result: dict[str, object]) -> None:
    print(
        f"Walk-forward train_days={result['train_days']} test_days={result['test_days']} "
        f"folds={len(result['folds'])} test_batches={result['total_test_days']} trades={result['total_trades']}"
    )
    for row in result["folds"]:
        print(
            f"fold={row['fold']} train={row['train_start_date']}..{row['train_end_date']} "
            f"test={row['test_start_date']}..{row['test_end_date']} batches={row['test_days']} "
            f"trades={row['trade_count']} net_avg={float(row['avg_return']):.2f}% "
            f"portfolio_avg={float(row['portfolio_avg_return']):.2f}% max_dd={float(row['max_drawdown']):.2f}%"
        )


def _strategy_validation_runs(repo: Repository, limit: int) -> int:
    rows = repo.strategy_validation_runs(limit=limit)
    if not rows:
        print("No saved strategy validation runs.")
        return 0
    for row in rows:
        print(
            f"id={row['id']} run_at={display_shanghai_time(row['run_at'])} verdict={row['verdict']} "
            f"data={row['data_fingerprint']} summary={row['summary']}"
        )
    return 0


def _strategy_backtest_runs(repo: Repository, limit: int) -> int:
    rows = repo.strategy_backtest_runs(limit=limit)
    if not rows:
        print("No saved strategy backtest runs.")
        return 0
    for row in rows:
        try:
            summary = json.loads(str(row["summary_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        print(
            f"id={row['id']} run_at={display_shanghai_time(row['run_at'])} "
            f"trades={int(summary.get('trade_count') or 0)} "
            f"portfolio_avg={_fmt_number(summary.get('portfolio_avg_return'))}% "
            f"max_dd={_fmt_number(summary.get('max_drawdown'))}% "
            f"data={row['data_fingerprint']}"
        )
    return 0


def _parse_horizons(value: str) -> list[int]:
    horizons: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            horizon = int(item)
        except ValueError:
            continue
        if 1 <= horizon <= 60 and horizon not in horizons:
            horizons.append(horizon)
    return horizons or [3, 5, 10]


def _import(adapter: THSLocalAdapter, repo: Repository) -> int:
    missing = adapter.validate()
    if missing:
        for item in missing:
            print(item)
        return 2

    securities = adapter.read_stocknames()
    market_config = adapter.read_market_config()
    snapshots = adapter.read_realtime_snapshots()
    watchlists = adapter.read_watchlists()

    repo.replace_securities(securities)
    snapshot_ids = repo.insert_snapshots(snapshots)
    snapshot_ids_by_file = {
        snapshot.source_file: snapshot_id
        for snapshot, snapshot_id in zip(snapshots, snapshot_ids, strict=True)
    }
    quotes = adapter.read_stocknow_quotes(snapshot_ids_by_file)
    repo.insert_quotes(quotes)
    repo.replace_watchlists(watchlists)

    recognized = sum(1 for item in snapshots if item.format_version)
    print(f"Imported securities: {len(securities)}")
    print(f"Markets in config: {len(market_config.markets)}")
    print(f"Realtime snapshots: {len(snapshots)} ({recognized} recognized headers)")
    print(f"Watchlist entries: {len(watchlists)}")
    print(f"Quotes parsed: {len(quotes)} code-only records (price fields are not implemented yet)")
    return 0


def _score(repo: Repository, profile_path: Path | None = None) -> int:
    profile = load_scoring_profile(profile_path)
    scored = repo.score_latest_quotes(profile=profile)
    if scored == 0:
        print("Scored 0 symbols: no parsed realtime price fields are available yet.")
    else:
        suffix = f" with profile {profile.name}" if profile_path else ""
        print(f"Scored {scored} symbols{suffix}.")
    return 0


def _write_default_profile(out: Path) -> int:
    write_default_scoring_profile(out)
    print(f"Wrote default scoring profile: {out}")
    return 0


def _scores(repo: Repository, limit: int, positive_only: bool = False) -> int:
    rows = repo.latest_scores(limit, positive_only=positive_only)
    if not rows:
        print("No scores found. Run score after importing quotes.")
        return 0
    for row in rows:
        market_cap = row["market_cap"]
        market_cap_text = "-" if market_cap is None else f"{float(market_cap) / 100000000:.2f}亿"
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['board'] or '-'} "
            f"score={_fmt_number(row['total_score'])} price={_fmt_number(row['latest_price'])} "
            f"pct={_fmt_number(row['pct_change'])}% amount={_fmt_number(row['amount'], 0)} "
            f"mcap={market_cap_text} turnover={_fmt_number(row['turnover_rate'])}%"
        )
    return 0


def _score_runs(repo: Repository, limit: int) -> int:
    rows = repo.latest_score_runs(limit)
    if not rows:
        print("No score runs found. Run score or run-daily first.")
        return 0
    for row in rows:
        print(
            f"#{row['id']} date={row['score_date']} profile={row['profile_name']} "
            f"created_at={row['created_at']}"
        )
    return 0


def _compare_runs(
    repo: Repository,
    base_run_id: int | None,
    target_run_id: int | None,
    limit: int,
    min_score: float,
) -> int:
    rows = repo.compare_score_runs(base_run_id=base_run_id, target_run_id=target_run_id, limit=limit, min_score=min_score)
    if not rows:
        print("No comparable score runs found. Run score or run-daily at least twice.")
        return 0
    first = rows[0]
    print(f"Comparing run #{first['base_run_id']} -> #{first['target_run_id']}")
    for row in rows:
        base = "-" if row["base_score"] is None else f"{float(row['base_score']):.2f}"
        target = "-" if row["target_score"] is None else f"{float(row['target_score']):.2f}"
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['board'] or '-'} "
            f"{row['status']} base={base} target={target} delta={float(row['delta']):+.2f} "
            f"rank={row['base_rank'] or '-'}->{row['target_rank'] or '-'}"
        )
    return 0


def _explain(repo: Repository, symbol: str, bars: int) -> int:
    row = repo.score_explanation(symbol)
    if row is None:
        print(f"No score found for {symbol}. Run run-daily or score first.")
        return 1

    components = json.loads(row["components_json"])
    rules = json.loads(row["triggered_rules_json"])
    print(f"{row['symbol']} {row['name'] or '-'} {row['board'] or '-'}")
    print(f"Score date: {row['score_date']}  total={row['total_score']:.2f}")
    print(
        f"Quote: price={row['latest_price']} pct={row['pct_change']}% "
        f"amount={row['amount']} turnover={row['turnover_rate']}% observed_at={row['observed_at']}"
    )
    print("Components:")
    for name, value in sorted(components.items(), key=lambda item: (-abs(float(item[1])), item[0])):
        print(f"- {name}: {float(value):.2f}")
    print("Rules:")
    for rule in rules:
        print(f"- {rule}")

    daily_rows = repo.recent_daily_bars(symbol, limit=bars)
    if daily_rows:
        print("Recent daily bars:")
        for item in daily_rows:
            print(
                f"- {item['trade_date']} close={_fmt_number(item['close'])} "
                f"high={_fmt_number(item['high'])} low={_fmt_number(item['low'])} "
                f"volume={_fmt_number(item['volume'], digits=0)}"
            )
    return 0


def _diagnose(repo: Repository, symbol: str, bars: int) -> int:
    if not (len(symbol) == 6 and symbol.isdigit()):
        print(f"Invalid symbol: {symbol}. Pass a 6-digit A-share code.")
        return 1
    row = repo.score_explanation(symbol)
    decision = analyze_symbol(repo, symbol)
    if row is None or decision is None:
        print(f"No diagnosis available for {symbol}. Run run-daily or score first.")
        return 1

    print(f"Diagnosis: {decision.symbol} {decision.name or '-'}")
    print(f"Conclusion: {decision.decision} confidence={decision.confidence:.0f}")
    print(f"Summary: {decision.summary}")
    print(
        f"Score: date={row['score_date']} total={float(row['total_score']):.2f} "
        f"price={_fmt_number(row['latest_price'])} pct={_fmt_number(row['pct_change'])}% "
        f"amount={_fmt_number(row['amount'])} turnover={_fmt_number(row['turnover_rate'])}%"
    )

    note = repo.stock_note(symbol)
    if note is not None:
        print(f"Local note: status={note['status']} tags={note['tags'] or '-'} note={note['note'] or '-'}")

    sections = [
        ("Strengths", decision.strengths),
        ("Risks", decision.risks),
        ("Trigger conditions", decision.trigger_conditions),
        ("Invalidation conditions", decision.invalidation_conditions),
        ("Next actions", decision.next_actions),
    ]
    for title, items in sections:
        print(f"{title}:")
        for item in items:
            print(f"- {item}")

    factor_signals = decision.evidence.get("factor_signals", [])
    if isinstance(factor_signals, list) and factor_signals:
        print("Factor signals:")
        for item in factor_signals[:5]:
            if isinstance(item, dict):
                print(
                    f"- {item.get('name', '-')} direction={item.get('direction', '-')} "
                    f"strength={_fmt_number(item.get('strength'))} verdict={item.get('effectiveness_verdict') or '-'}"
                )

    daily_rows = repo.recent_daily_bars(symbol, limit=bars)
    if daily_rows:
        print("Recent daily bars:")
        for item in daily_rows:
            print(
                f"- {item['trade_date']} close={_fmt_number(item['close'])} "
                f"high={_fmt_number(item['high'])} low={_fmt_number(item['low'])} "
                f"volume={_fmt_number(item['volume'], digits=0)}"
            )
    print("Notice: research assistance only; not investment advice.")
    return 0


def _note(repo: Repository, symbol: str, status: str, tags: str, text: str) -> int:
    repo.upsert_stock_note(symbol, status=status, tags=tags, note=text)
    print(f"Saved note for {symbol}: status={status} tags={tags or '-'}")
    return 0


def _notes(repo: Repository, limit: int, status: str | None = None, query: str = "", sort: str = "updated") -> int:
    rows = repo.list_stock_notes(limit=limit, status=status, query=query, sort=sort)
    if not rows:
        print("No local stock notes found.")
        return 0
    for row in rows:
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['status']} "
            f"score={_fmt_number(row['total_score'])} price={_fmt_number(row['latest_price'])} "
            f"quote_observed_at={row['observed_at'] or '-'} tags={row['tags'] or '-'} "
            f"note={row['note'] or '-'} updated_at={display_shanghai_time(row['updated_at'])}"
        )
    return 0


def _delete_note(repo: Repository, symbol: str) -> int:
    deleted = repo.delete_stock_note(symbol)
    if deleted:
        print(f"Deleted note for {symbol}.")
    else:
        print(f"No note found for {symbol}.")
    return 0


def _ai_pick(repo: Repository, limit: int, min_score: float, save: bool) -> int:
    decisions = rank_candidates(repo, limit=limit, min_score=min_score)
    if not decisions:
        coverage = repo.latest_score_quote_coverage()
        score_count = int(coverage["score_count"])
        priced_score_count = int(coverage["priced_score_count"])
        if score_count and priced_score_count == 0:
            print(
                f"No AI candidates found: latest score batch has {score_count} scores but "
                f"0 price-ready quotes (score_date={coverage['score_date'] or '-'})."
            )
        else:
            print("No AI candidates found. Run run-daily or score first.")
        return 0
    if save:
        repo.insert_ai_decisions(decisions_to_rows(decisions))
    for index, item in enumerate(decisions, start=1):
        print(f"{index}. {item.symbol} {item.name or '-'} {item.decision} confidence={item.confidence:.0f}")
        print(f"   {item.summary}")
        print(f"   Quote observed at: {item.evidence.get('quote_observed_at') or '-'}")
        print(f"   Next: {'; '.join(item.next_actions[:2])}")
    if save:
        print(f"Saved AI decisions: {len(decisions)}")
    return 0


def _ai_explain(repo: Repository, symbol: str, save: bool) -> int:
    decision = analyze_symbol(repo, symbol)
    if decision is None:
        print(f"No score found for {symbol}. Run run-daily or score first.")
        return 1
    if save:
        repo.insert_ai_decisions(decisions_to_rows([decision]))
    print(f"{decision.symbol} {decision.name or '-'} {decision.decision} confidence={decision.confidence:.0f}")
    print(decision.summary)
    print(f"Quote observed at: {decision.evidence.get('quote_observed_at') or '-'}")
    print("Strengths:")
    for item in decision.strengths:
        print(f"- {item}")
    print("Risks:")
    for item in decision.risks:
        print(f"- {item}")
    print("Trigger conditions:")
    for item in decision.trigger_conditions:
        print(f"- {item}")
    print("Invalidation conditions:")
    for item in decision.invalidation_conditions:
        print(f"- {item}")
    print("Next actions:")
    for item in decision.next_actions:
        print(f"- {item}")
    if save:
        print("Saved AI decision.")
    return 0


def _ai_history(repo: Repository, limit: int, symbol: str | None) -> int:
    rows = repo.latest_ai_decisions(limit=limit, symbol=symbol)
    if not rows:
        print("No saved AI decisions found.")
        return 0
    for row in rows:
        print(
            f"#{row['id']} {display_shanghai_time(row['run_at'])} {row['symbol']} {row['name'] or '-'} "
            f"{row['decision']} confidence={row['confidence']:.0f}"
        )
        print(f"   {row['summary']}")
    return 0


def _ai_changes(repo: Repository, limit: int) -> int:
    rows = repo.ai_decision_changes(limit=limit)
    if not rows:
        print("No saved AI decisions found.")
        return 0
    for row in rows:
        previous = row["previous_decision"] or "-"
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['status']} "
            f"{previous}->{row['latest_decision']} "
            f"confidence={float(row['latest_confidence']):.0f} "
            f"delta={float(row['confidence_delta']):+.0f}"
        )
        print(f"   {row['summary']}")
    return 0


def _ai_outcomes(repo: Repository, limit: int, horizon: int, symbol: str | None) -> int:
    selected_symbol = symbol if symbol and len(symbol) == 6 and symbol.isdigit() else None
    rows = repo.ai_decision_outcomes(limit=limit, horizon_days=horizon, symbol=selected_symbol)
    if not rows:
        print("No saved AI decisions found. Run run-daily or ai-pick --save first.")
        return 0
    summary = summarize_ai_decision_outcomes(rows)
    hit_rate = summary["hit_rate"]
    average_return = summary["average_return"]
    print(
        f"AI outcomes: total={summary['total']} completed={summary['evaluated']} "
        f"pending={summary['pending']} unavailable={summary['unavailable']} "
        f"positive={summary['positive']} "
        f"hit_rate={'-' if hit_rate is None else f'{float(hit_rate):.1f}%'} "
        f"average_return={'-' if average_return is None else f'{float(average_return):+.2f}%'}"
    )
    for row in rows:
        entry = "-" if row["entry_date"] is None else f"{row['entry_date']}@{float(row['entry_price']):.2f}"
        if row["status"] == "evaluated":
            exit_text = f"{row['exit_date']}@{float(row['exit_price']):.2f}"
            outcome = f"{float(row['return_pct']):+.2f}%"
        else:
            exit_text = "-"
            outcome = str(row["status_label"])
        print(
            f"#{row['id']} {row['symbol']} {row['name'] or '-'} {row['decision']} "
            f"signal={row['score_date'] or '-'} horizon={row['horizon_days']}d "
            f"entry={entry} exit={exit_text} outcome={outcome}"
        )
    return 0


def _fmt_number(value: object, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _candidates(repo: Repository, limit: int, min_score: float, out: Path) -> int:
    count = repo.write_candidates_csv(out, limit=limit, min_score=min_score)
    rows = repo.latest_candidates(limit=limit, min_score=min_score)
    print(f"Wrote {count} candidates -> {out}")
    for row in rows[:20]:
        market_cap = row["market_cap"]
        market_cap_text = "-" if market_cap is None else f"{float(market_cap) / 100000000:.2f}亿"
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['board'] or '-'} "
            f"score={_fmt_number(row['total_score'])} price={_fmt_number(row['latest_price'])} "
            f"pct={_fmt_number(row['pct_change'])}% amount={_fmt_number(row['amount'], 0)} "
            f"mcap={market_cap_text} turnover={_fmt_number(row['turnover_rate'])}%"
        )
    return 0


def _report(repo: Repository, limit: int, min_score: float, out: Path) -> int:
    count = repo.write_daily_report(out, limit=limit, min_score=min_score)
    print(f"Wrote daily report with {count} candidates -> {out}")
    return 0


def _db_info(repo: Repository) -> int:
    for table, count in repo.table_counts().items():
        print(f"{table}: {count}")
    return 0


def _data_health(repo: Repository) -> int:
    health = repo.daily_bar_health()
    quote_health = repo.quote_health()
    fundamental_health = repo.fundamental_health()
    industry_health = repo.industry_health()
    readiness = summarize_data_readiness(health, quote_health, fundamental_health, industry_health)
    print(f"Daily bar health: {health['status']}")
    print(f"Canonical source precedence: {health['canonical_source_policy']}")
    print(f"Bars: {health['total_bars']}  Symbols: {health['total_symbols']}")
    print(f"Duplicate symbol-days: {health['duplicate_symbol_days']}  Rows in conflicts: {health['duplicate_rows']}")
    print(
        f"Freshness: {health['freshness_status']} latest_trade_date={health['latest_trade_date'] or '-'} "
        f"weekday_lag_days={health['weekday_lag_days'] if health['weekday_lag_days'] is not None else '-'} "
        f"checked_on={health['freshness_checked_on']}"
    )
    if health.get("latest_any_trade_date") != health.get("latest_trade_date"):
        print(f"Latest non-stock-or-any daily bar: {health['latest_any_trade_date'] or '-'}")
    for row in health["sources"]:
        print(
            f"{row['source_kind']}: bars={row['bars']} symbols={row['symbols']} "
            f"range={row['first_trade_date'] or '-'}..{row['last_trade_date'] or '-'}"
        )
    print(
        "Realtime quote health: "
        f"freshness={quote_health['freshness_status']} "
        f"priced_symbols={quote_health['priced_symbols']} "
        f"current_priced_symbols={quote_health['current_priced_symbols']} "
        f"stale_priced_symbols={quote_health['stale_priced_symbols']} "
        f"latest_price_date={quote_health['latest_price_date'] or '-'} "
        f"checked_on={quote_health['freshness_checked_on']}"
    )
    print(
        "Financial disclosure health: "
        f"records={fundamental_health['total_records']} symbols={fundamental_health['total_symbols']} "
        f"disclosed_symbols={fundamental_health['disclosed_symbols']} "
        f"cashflow_symbols={fundamental_health['operating_cash_flow_symbols']} "
        f"latest_notice_date={fundamental_health['latest_disclosed_notice_date'] or '-'}"
    )
    print(
        "Industry label health: "
        f"labels={industry_health['label_records']} industries={industry_health['industry_count']} "
        f"scored_symbols={industry_health['scored_symbols']} "
        f"score_date={industry_health['score_date'] or '-'} "
        f"latest_updated_at={industry_health['latest_updated_at'] or '-'}"
    )
    print(f"Data readiness: {readiness['status']} - {readiness['label']}")
    print(str(readiness["summary"]))
    actions = readiness.get("actions", [])
    if isinstance(actions, list) and actions:
        print("Next data actions:")
        for item in actions:
            if isinstance(item, dict):
                action = str(item.get("action") or "")
                suffix = f" | {action}" if action else ""
                print(f"- [{item.get('status', '-')}] {item.get('label', '-')}: {item.get('message', '-')}{suffix}")
    return 0


def _snapshots(repo: Repository, limit: int) -> int:
    rows = repo.latest_snapshots(limit)
    if not rows:
        print("No snapshots found. Run import first.")
        return 0
    for row in rows:
        print(
            f"#{row['id']} {row['market']} {row['status']} "
            f"{row['format_version'] or '-'} size={row['file_size']} read_at={row['read_at']}"
        )
        print(f"  {row['message']}")
    return 0


def _export(repo: Repository, out_dir: Path, tables: list[str] | None) -> int:
    selected = tables or [
        "securities",
        "market_snapshots",
        "quotes_realtime",
        "watchlists",
        "scores",
        "score_runs",
        "daily_bars",
        "fundamentals",
        "stock_themes",
        "stock_industries",
        "stock_notes",
        "ai_decisions",
        "news_items",
        "strategy_validation_runs",
        "strategy_backtest_runs",
        "daily_runs",
    ]
    for table in selected:
        output_path = out_dir / f"{table}.csv"
        count = repo.export_table_csv(table, output_path)
        print(f"Exported {table}: {count} rows -> {output_path}")
    return 0


def _inspect_symbol(adapter: THSLocalAdapter, symbol: str, json_out: Path | None) -> int:
    inspections = adapter.inspect_symbol(symbol)
    if not inspections:
        print(f"No stocknow.dat record found for symbol {symbol}.")
        return 1
    payload = [
        {
            "symbol": item.symbol,
            "market": item.market,
            "record_index": item.record_index,
            "record_offset": item.record_offset,
            "record_length": item.record_length,
            "name_from_record": item.name_from_record,
            "name_from_master": item.name_from_master,
            "first_bytes_hex": item.first_bytes_hex,
            "ascii_runs": item.ascii_runs,
            "numeric_candidates": item.numeric_candidates,
        }
        for item in inspections
    ]
    for item in inspections:
        print(
            f"{item.symbol} {item.market} index={item.record_index} "
            f"offset={item.record_offset} len={item.record_length}"
        )
        print(f"  name(record): {item.name_from_record or '-'}")
        print(f"  name(master): {item.name_from_master or '-'}")
        print(f"  first_bytes: {item.first_bytes_hex}")
        print(f"  ascii_runs: {item.ascii_runs[:8]}")
        print(f"  numeric_candidates: {item.numeric_candidates[:12]}")
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote diagnostics: {json_out}")
    return 0


def _capture_symbols(adapter: THSLocalAdapter, symbols: list[str], out: Path) -> int:
    captures = adapter.capture_symbols(symbols)
    write_capture(out, captures)
    found = {(item["symbol"], item["market"]) for item in captures}
    print(f"Captured {len(captures)} records -> {out}")
    for symbol, market in sorted(found):
        print(f"- {symbol} {market}")
    missing = sorted(set(symbols) - {str(item["symbol"]) for item in captures})
    if missing:
        print(f"Missing symbols: {', '.join(missing)}")
    return 0 if captures else 1


def _compare_captures(before_path: Path, after_path: Path, json_out: Path | None) -> int:
    rows = compare_capture_payloads(read_capture(before_path), read_capture(after_path))
    rows = sorted(rows, key=lambda row: (-row["changed_byte_count"], row["symbol"], row["market"]))
    for row in rows[:30]:
        print(
            f"{row['symbol']} {row['market']}: "
            f"{row['changed_byte_count']} changed bytes, offsets={row['changed_offsets'][:30]}"
        )
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote comparison: {json_out}")
    return 0


def _match_observations(
    capture_path: Path,
    observations_path: Path,
    fields: list[str] | None,
    tolerance: float,
    json_out: Path,
) -> int:
    payload = read_capture(capture_path)
    selected_fields = fields or ["latest_price", "pct_change", "volume", "amount", "open", "high", "low"]
    matches = match_observations(payload.get("records", []), load_observations(observations_path), selected_fields, tolerance)
    summary = summarize_matches(matches)
    result = {"matches": matches, "summary": summary}
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for field in selected_fields:
        top = summary.get(field, [])[:10]
        print(f"{field}: {len(matches.get(field, []))} raw matches")
        for item in top:
            print(
                f"  offset={item['offset']} kind={item['kind']} scale={item['scale']} "
                f"count={item['match_count']} max_delta={item['max_abs_delta']}"
            )
    print(f"Wrote matches: {json_out}")
    return 0


def _observation_template(symbols: list[str], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    columns = ["symbol", "latest_price", "pct_change", "volume", "amount", "open", "high", "low"]
    lines = [",".join(columns)]
    lines.extend(f"{symbol},,,,,,," for symbol in symbols)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    print(f"Wrote observation template: {out}")
    return 0


def _import_history(repo: Repository, files: list[Path], symbol: str | None) -> int:
    total = 0
    for file_path in files:
        bars = load_daily_bars_csv(file_path, default_symbol=symbol)
        imported = repo.upsert_daily_bars(bars)
        total += imported
        print(f"Imported {imported} daily bars from {file_path}")
    print(f"Imported total daily bars: {total}")
    return 0


def _import_fundamentals(repo: Repository, files: list[Path], symbol: str | None) -> int:
    total = 0
    for file_path in files:
        records = load_fundamentals_csv(file_path, default_symbol=symbol)
        imported = repo.upsert_fundamentals(records)
        total += imported
        print(f"Imported {imported} financial records from {file_path}")
    print("Financial values retain the source CSV unit; no unit conversion was applied.")
    print(f"Imported total financial records: {total}")
    return 0 if total else 1


def _import_public_fundamentals(
    repo: Repository,
    symbols: list[str],
    universe: str | None,
    limit: int,
    reports: int,
) -> int:
    selected = sorted(set(symbols))
    if not selected and universe:
        source, selected = _select_universe_symbols(repo, universe, limit)
        print(f"Using {source} universe: {len(selected)} symbols")
    if not selected:
        print("No symbols provided. Pass symbols or --universe after running import.")
        return 1
    total, failures = _fetch_public_fundamentals(repo, selected[: max(1, limit)], reports)
    print(f"Fetched public financial records: {total}")
    if failures:
        print(f"Public financial fetch failures: {len(failures)} ({'; '.join(failures[:5])})")
    print("Public fields: report/notice date, revenue and revenue YoY, parent net profit and net profit YoY, weighted ROE, operating cash flow. PE/PB remain unchanged.")
    return 0 if total else 1


def _fetch_public_fundamentals(repo: Repository, symbols: list[str], reports: int) -> tuple[int, list[str]]:
    total = 0
    failures: list[str] = []
    for symbol in symbols:
        try:
            records = fetch_eastmoney_fundamentals_one(symbol, reports=reports)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"{symbol}: {type(error).__name__}")
            continue
        total += repo.upsert_fundamentals(records)
    return total, failures


def _import_public_industries(repo: Repository, symbols: list[str], universe: str, limit: int) -> int:
    selected = sorted(set(symbols))
    if not selected:
        source, selected = _select_universe_symbols(repo, universe, None)
        print(f"Using {source} universe: {len(selected)} available symbols")
    if not selected:
        print("No symbols provided. Import securities or pass explicit symbols first.")
        return 1
    refresh_symbols = repo.industry_refresh_symbols(selected, limit)
    print(f"Industry refresh targets: {len(refresh_symbols)} (missing labels first, then oldest)")
    imported, failures = _fetch_public_industries(repo, refresh_symbols)
    print(f"Imported public industry labels: {imported}")
    if failures:
        print(f"Public industry fetch failures: {len(failures)} ({'; '.join(failures[:5])})")
    print("Industry labels are current research context only and are not used in historical factor or strategy backtests.")
    return 0 if imported else 1


def _fetch_public_industries(repo: Repository, symbols: list[str]) -> tuple[int, list[str]]:
    records = []
    failures: list[str] = []
    for symbol in symbols:
        try:
            record = fetch_eastmoney_industry_one(symbol)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"{symbol}: {type(error).__name__}")
            continue
        if record is not None:
            records.append(record)
    return repo.upsert_stock_industries(records), failures


def _import_public_history(
    repo: Repository,
    symbols: list[str],
    universe: str | None,
    limit: int,
    days: int,
) -> int:
    selected = sorted(set(symbols))
    if not selected and universe:
        source, selected = _select_universe_symbols(repo, universe, limit)
        print(f"Using {source} universe: {len(selected)} symbols")
    if not selected:
        print("No symbols provided. Pass symbols or --universe after running import.")
        return 1
    bars = fetch_tencent_daily_bars(selected, count=days)
    imported = repo.upsert_daily_bars(bars)
    print(f"Fetched daily bars: {len(bars)}")
    print(f"Imported daily bars: {imported}")
    return 0 if imported else 1


def _import_tdx_history(
    repo: Repository,
    tdx_root: Path,
    symbols: list[str],
    include_indices: bool,
    limit_symbols: int | None,
    start_date: str,
    end_date: str,
    replace_existing: bool,
) -> int:
    bars, files = load_tdx_daily_bars(
        tdx_root=tdx_root,
        symbols=symbols,
        include_indices=include_indices,
        limit_symbols=limit_symbols,
        start_date=start_date,
        end_date=end_date,
    )
    if not files:
        print(f"No TDX .day files found under {tdx_root}.")
        return 1
    imported_symbols = sorted({item.symbol for item in files})
    deleted = repo.delete_daily_bars_for_symbols(imported_symbols) if replace_existing else 0
    imported = repo.upsert_daily_bars(bars)
    first_date = min((bar.trade_date for bar in bars), default="-")
    last_date = max((bar.trade_date for bar in bars), default="-")
    print(f"TDX root: {tdx_root}")
    print(f"TDX daily files: {len(files)} symbols={len(imported_symbols)}")
    print(f"TDX daily bars: {len(bars)} date_range={first_date}->{last_date}")
    if replace_existing:
        print(f"Deleted existing daily bars: {deleted}")
    print(f"Imported daily bars: {imported}")
    return 0 if imported else 1


def _tdx_status(tdx_root: Path) -> int:
    status = inspect_tdx_daily_status(tdx_root)
    print(f"TDX root: {status.root}")
    print(
        f"stock_files={status.stock_file_count} "
        f"stock_latest_trade_date={status.stock_latest_trade_date or '-'}"
    )
    print(
        f"index_files={status.index_file_count} "
        f"index_latest_trade_date={status.index_latest_trade_date or '-'}"
    )
    print(f"latest_trade_date={status.latest_trade_date or '-'}")
    return 0 if status.stock_file_count or status.index_file_count else 1


def _import_tdx_blocks(repo: Repository, tdx_root: Path, kinds: list[str] | None) -> int:
    memberships, files = load_tdx_theme_memberships(tdx_root=tdx_root, kinds=kinds)
    if not files:
        print(f"No TDX block files found under {tdx_root / 'T0002' / 'hq_cache'}.")
        return 1
    imported = repo.replace_stock_themes(memberships, files)
    by_category: dict[str, set[str]] = {}
    for item in memberships:
        by_category.setdefault(item.category, set()).add(item.theme)
    print(f"TDX root: {tdx_root}")
    print(f"TDX block files: {', '.join(path.name for path in files)}")
    print(
        "Theme memberships: "
        f"{imported} "
        + ", ".join(f"{category}={len(themes)} themes" for category, themes in sorted(by_category.items()))
    )
    return 0 if imported else 1


def _tdx_block_status(tdx_root: Path) -> int:
    files = discover_tdx_block_files(tdx_root)
    print(f"TDX root: {tdx_root}")
    if not files:
        print("No recognized concept/style block files found.")
        return 1
    for kind, category, path in files:
        print(f"{kind} category={category} size={path.stat().st_size} path={path}")
    return 0


def _themes(repo: Repository, limit: int, category: str, min_scored: int) -> int:
    summary = repo.theme_heat(limit=limit, category=category, min_scored=min_scored)
    score_date = summary["score_date"] or "-"
    price_as_of_date = summary.get("price_as_of_date") or "-"
    print(
        f"Theme heat score_date={score_date} price_as_of={price_as_of_date} "
        f"category={category or '全部'} min_scored={max(1, min_scored)}"
    )
    rows = summary["items"]
    if not isinstance(rows, list) or not rows:
        print("No theme memberships found. Run import-tdx-blocks first.")
        return 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        average_score = row["average_score"]
        positive_rate = row["positive_rate"]
        coverage_rate = row["coverage_rate"]
        average_text = f"{float(average_score):.2f}" if average_score is not None else "-"
        positive_text = f"{float(positive_rate):.1f}%" if positive_rate is not None else "-"
        returns = " ".join(
            f"r{days}={float(row[f'return_{days}d']):+.2f}%/{int(row.get(f'return_{days}d_count') or 0)}"
            if row.get(f"return_{days}d") is not None
            else f"r{days}=-"
            for days in (1, 5, 20)
        )
        print(
            f"{row['category']} {row['theme']} members={row['member_count']} scored={row['scored_count']} "
            f"coverage={float(coverage_rate):.1f}% avg_score={average_text} positive_rate={positive_text} "
            f"priced={int(row.get('priced_count') or 0)} {returns}"
        )
    return 0


def _industries(repo: Repository, limit: int, min_scored: int) -> int:
    summary = repo.industry_heat(limit=limit, min_scored=min_scored)
    score_date = summary["score_date"] or "-"
    print(f"Industry heat score_date={score_date} min_scored={max(1, min_scored)}")
    rows = summary["items"]
    if not isinstance(rows, list) or not rows:
        print("No industry heat found. Run import-public-industries and score first.")
        return 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        average_score = row["average_score"]
        positive_rate = row["positive_rate"]
        coverage_rate = row["coverage_rate"]
        average_text = f"{float(average_score):.2f}" if average_score is not None else "-"
        positive_text = f"{float(positive_rate):.1f}%" if positive_rate is not None else "-"
        coverage_text = f"{float(coverage_rate):.1f}%" if coverage_rate is not None else "-"
        print(
            f"{row['industry']} members={row['member_count']} scored={row['scored_count']} "
            f"coverage={coverage_text} avg_score={average_text} positive_rate={positive_text}"
        )
    return 0


def _auto_observe(symbols: list[str], out: Path) -> int:
    observations = fetch_tencent_observations(symbols)
    write_observations_csv(out, observations)
    print(f"Fetched {len(observations)} observations -> {out}")
    for item in observations:
        print(
            f"- {item.symbol} {item.name} latest={item.latest_price} "
            f"pct={item.pct_change:.4f} volume={item.volume} amount={item.amount}"
        )
    return 0 if observations else 1


def _import_public_quotes(
    repo: Repository,
    symbols: list[str],
    from_cache: bool,
    universe: str | None,
    limit: int,
    observations_out: Path,
) -> int:
    selected = list(symbols)
    if from_cache:
        selected.extend(repo.list_realtime_symbols(limit=limit))
    selected_universe = universe
    if not selected and selected_universe:
        source, universe_symbols = _select_universe_symbols(repo, selected_universe, limit)
        selected.extend(universe_symbols)
        print(f"Using {source} universe: {len(universe_symbols)} symbols")
    selected = sorted(set(selected))
    if not selected:
        print("No symbols provided. Pass symbols or use --from-cache after running import.")
        return 1
    observations = fetch_tencent_observations(selected)
    imported = repo.upsert_public_quotes(observations)
    write_observations_csv(observations_out, observations)
    print(f"Fetched {len(observations)} observations -> {observations_out}")
    print(f"Imported public quotes: {imported}")
    return 0 if imported else 1


def _select_universe_symbols(repo: Repository, universe: str, limit: int | None) -> tuple[str, list[str]]:
    if universe == "auto":
        return repo.list_auto_universe_symbols(limit=limit)
    if universe == "watchlist":
        return "watchlist", repo.list_watchlist_symbols(limit=limit)
    if universe == "securities":
        return "securities", repo.list_security_universe_symbols(limit=limit)
    if universe == "cache":
        return "cache", repo.list_realtime_symbols(limit=limit)
    raise ValueError(f"unsupported universe: {universe}")


def _universe(repo: Repository, source: str, limit: int) -> int:
    resolved_source, symbols = _select_universe_symbols(repo, source, limit)
    print(f"Universe source: {resolved_source}")
    print(f"Symbols: {len(symbols)}")
    for symbol in symbols:
        print(symbol)
    return 0


def _auto_infer_fields(
    adapter: THSLocalAdapter,
    symbols: list[str],
    capture_out: Path,
    observations_out: Path,
    matches_out: Path,
    tolerance: float,
) -> int:
    captures = adapter.capture_symbols(symbols)
    write_capture(capture_out, captures)
    observations = fetch_tencent_observations(symbols)
    write_observations_csv(observations_out, observations)
    fields = ["latest_price", "pct_change", "volume", "amount", "open", "high", "low", "previous_close"]
    matches = match_observations(captures, load_observations(observations_out), fields, tolerance)
    summary = summarize_matches(matches)
    result = {"matches": matches, "summary": summary}
    matches_out.parent.mkdir(parents=True, exist_ok=True)
    matches_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Captured {len(captures)} THS records -> {capture_out}")
    print(f"Fetched {len(observations)} public observations -> {observations_out}")
    for field in fields:
        top = summary.get(field, [])[:5]
        print(f"{field}: {len(matches.get(field, []))} raw matches")
        for item in top:
            print(
                f"  offset={item['offset']} kind={item['kind']} scale={item['scale']} "
                f"count={item['match_count']} max_delta={item['max_abs_delta']}"
            )
    print(f"Wrote matches: {matches_out}")
    return 0


def _run_daily(
    adapter: THSLocalAdapter,
    repo: Repository,
    limit: int,
    out_dir: Path,
    universe: str,
    history_days: int,
    public_fundamentals: bool = False,
    public_fundamental_reports: int = 8,
    public_fundamental_limit: int = 100,
    public_industries: bool = False,
    public_industry_limit: int = 100,
    profile_path: Path | None = None,
    tdx_root: Path | None = None,
    tdx_include_indices: bool = False,
    tdx_import_themes: bool = False,
    tdx_start_date: str = "",
    public_announcements: bool = False,
    public_announcement_limit: int = 30,
    public_announcements_per_symbol: int = 3,
    strategy_snapshot: bool = False,
) -> int:
    parameters = {
        "limit": limit,
        "out_dir": str(out_dir),
        "universe": universe,
        "history_days": history_days,
        "public_fundamentals": public_fundamentals,
        "public_fundamental_reports": public_fundamental_reports if public_fundamentals else None,
        "public_fundamental_limit": public_fundamental_limit if public_fundamentals else None,
        "public_industries": public_industries,
        "public_industry_limit": public_industry_limit if public_industries else None,
        "profile": str(profile_path) if profile_path is not None else None,
        "tdx_root": str(tdx_root) if tdx_root is not None else None,
        "tdx_include_indices": tdx_include_indices,
        "tdx_import_themes": tdx_import_themes,
        "tdx_start_date": tdx_start_date or None,
        "public_announcements": public_announcements,
        "public_announcement_limit": public_announcement_limit,
        "public_announcements_per_symbol": public_announcements_per_symbol,
        "strategy_snapshot": strategy_snapshot,
    }
    run_id = repo.start_daily_run(parameters)
    summary: dict[str, object] = {"run_id": run_id}
    total_steps = (
        7
        + int(public_fundamentals)
        + int(public_industries)
        + int(public_announcements)
        + int(strategy_snapshot)
        + int(tdx_root is not None)
        + int(tdx_root is not None and tdx_import_themes)
    )
    next_step = 1
    current_step = "import_tdx_history" if tdx_root is not None else "import_local_cache"
    try:
        if tdx_root is not None:
            print(f"Step {next_step}/{total_steps}: importing TongDaXin local daily bars")
            tdx_code, tdx_summary = _sync_tdx_daily_bars(repo, tdx_root, tdx_include_indices, tdx_start_date)
            summary.update(tdx_summary)
            if tdx_code != 0:
                summary.update({"failed_step": current_step, "return_code": tdx_code})
                repo.finish_daily_run(run_id, "failed", summary, f"{current_step} returned {tdx_code}")
                return tdx_code
            next_step += 1

        if tdx_root is not None and tdx_import_themes:
            current_step = "import_tdx_themes"
            print(f"Step {next_step}/{total_steps}: importing TongDaXin local themes")
            themes_code, themes_summary = _sync_tdx_themes(repo, tdx_root)
            summary.update(themes_summary)
            if themes_code != 0:
                summary.update({"failed_step": current_step, "return_code": themes_code})
                repo.finish_daily_run(run_id, "failed", summary, f"{current_step} returned {themes_code}")
                return themes_code
            next_step += 1

        current_step = "import_local_cache"
        print(f"Step {next_step}/{total_steps}: importing TongHuaShun local cache")
        import_code = _import(adapter, repo)
        if import_code != 0:
            summary.update({"failed_step": current_step, "return_code": import_code})
            repo.finish_daily_run(run_id, "failed", summary, f"{current_step} returned {import_code}")
            return import_code

        current_step = "import_local_news"
        next_step += 1
        print(f"Step {next_step}/{total_steps}: importing TongHuaShun local news")
        _import_ths_news(repo, adapter.root, limit_per_file=300)

        if public_announcements:
            current_step = "import_public_announcements"
            next_step += 1
            print(f"Step {next_step}/{total_steps}: importing public announcements")
            try:
                announcement_source, announcement_symbols = _select_universe_symbols(
                    repo,
                    universe,
                    min(limit, public_announcement_limit),
                )
                announcement_items = fetch_eastmoney_announcements(
                    announcement_symbols,
                    per_symbol=public_announcements_per_symbol,
                )
                announcement_imported = repo.upsert_news_items(announcement_items)
                summary.update(
                    {
                        "public_announcement_status": "saved" if announcement_imported else "empty",
                        "public_announcement_source": announcement_source,
                        "public_announcement_symbols": len(announcement_symbols),
                        "public_announcements_imported": announcement_imported,
                    }
                )
                print(
                    f"Imported public announcements: symbols={len(announcement_symbols)} "
                    f"items={announcement_imported}"
                )
            except Exception as error:
                summary.update(
                    {
                        "public_announcement_status": "failed",
                        "public_announcements_imported": 0,
                        "public_announcement_error": f"{type(error).__name__}: {error}",
                    }
                )
                print(f"Public announcements skipped: {type(error).__name__}: {error}")

        current_step = "import_public_quotes"
        next_step += 1
        print(f"Step {next_step}/{total_steps}: fetching public quote supplement")
        observations_path = out_dir / "observations_public.csv"
        quote_code = _import_public_quotes(repo, [], from_cache=False, universe=universe, limit=limit, observations_out=observations_path)
        if quote_code != 0:
            summary.update({"failed_step": current_step, "return_code": quote_code})
            repo.finish_daily_run(run_id, "failed", summary, f"{current_step} returned {quote_code}")
            return quote_code

        current_step = "import_public_history"
        next_step += 1
        print(f"Step {next_step}/{total_steps}: fetching public daily bars")
        source, history_symbols = _select_universe_symbols(repo, universe, limit)
        print(f"Using {source} universe for history: {len(history_symbols)} symbols")
        public_history_symbols = repo.symbols_without_tdx_daily_bars(history_symbols)
        tdx_covered_symbols = len(history_symbols) - len(public_history_symbols)
        if public_history_symbols:
            bars = fetch_tencent_daily_bars(public_history_symbols, count=history_days)
            repo.upsert_daily_bars(bars)
            print(f"Imported daily bars: {len(bars)}")
        else:
            bars = []
            print("Skipped public daily bars: all selected symbols already have TDX history.")

        public_fundamentals_imported = 0
        public_fundamentals_failures: list[str] = []
        if public_fundamentals:
            current_step = "import_public_fundamentals"
            next_step += 1
            print(f"Step {next_step}/{total_steps}: fetching public financial reports")
            public_fundamental_symbols = history_symbols[: max(1, public_fundamental_limit)]
            print(f"Using {len(public_fundamental_symbols)} of {len(history_symbols)} selected symbols for public financial reports")
            public_fundamentals_imported, public_fundamentals_failures = _fetch_public_fundamentals(
                repo,
                public_fundamental_symbols,
                public_fundamental_reports,
            )
            print(f"Fetched public financial records: {public_fundamentals_imported}")
            if public_fundamentals_failures:
                print(
                    f"Public financial fetch failures: {len(public_fundamentals_failures)} "
                    f"({'; '.join(public_fundamentals_failures[:5])})"
                )

        public_industries_imported = 0
        public_industries_failures: list[str] = []
        if public_industries:
            current_step = "import_public_industries"
            next_step += 1
            print(f"Step {next_step}/{total_steps}: fetching public industry labels")
            public_industry_symbols = repo.industry_refresh_symbols(history_symbols, public_industry_limit)
            print(f"Using {len(public_industry_symbols)} of {len(history_symbols)} selected symbols for public industry labels")
            public_industries_imported, public_industries_failures = _fetch_public_industries(repo, public_industry_symbols)
            print(f"Imported public industry labels: {public_industries_imported}")
            if public_industries_failures:
                print(
                    f"Public industry fetch failures: {len(public_industries_failures)} "
                    f"({'; '.join(public_industries_failures[:5])})"
                )

        current_step = "score"
        next_step += 1
        print(f"Step {next_step}/{total_steps}: scoring")
        score_code = _score(repo, profile_path)
        _scores(repo, min(20, limit), positive_only=True)

        current_step = "save_ai_snapshot"
        next_step += 1
        print(f"Step {next_step}/{total_steps}: saving AI snapshot")
        try:
            summary.update(_save_daily_ai_snapshot(repo, limit))
        except Exception as error:
            summary.update(
                {
                    "ai_snapshot_status": "failed",
                    "ai_decisions_saved": 0,
                    "ai_snapshot_error": f"{type(error).__name__}: {error}",
                }
            )
            print(f"AI snapshot skipped: {type(error).__name__}: {error}")

        if strategy_snapshot:
            current_step = "save_strategy_snapshot"
            next_step += 1
            print(f"Step {next_step}/{total_steps}: saving strategy research snapshot")
            try:
                summary.update(_save_daily_strategy_snapshot(repo))
            except Exception as error:
                summary.update(
                    {
                        "strategy_snapshot_status": "failed",
                        "strategy_snapshot_error": f"{type(error).__name__}: {error}",
                    }
                )
                print(f"Strategy snapshot skipped: {type(error).__name__}: {error}")

        current_step = "export"
        next_step += 1
        print(f"Step {next_step}/{total_steps}: exporting")
        _export(repo, out_dir, DAILY_AUDIT_EXPORT_TABLES)
        candidates_path = out_dir / "candidates.csv"
        report_path = out_dir / "daily_report.md"
        _candidates(repo, min(50, limit), 1.0, candidates_path)
        _report(repo, min(20, limit), 1.0, report_path)
        health = repo.daily_bar_health()
        quote_health = repo.quote_health()
        fundamental_health = repo.fundamental_health()
        summary.update(
            {
                "universe_source": source,
                "history_symbols": len(history_symbols),
                "tdx_covered_symbols": tdx_covered_symbols,
                "public_history_symbols": len(public_history_symbols),
                "history_bars_imported": len(bars),
                "public_fundamentals_enabled": public_fundamentals,
                "public_fundamental_symbols_requested": len(public_fundamental_symbols) if public_fundamentals else 0,
                "public_fundamentals_imported": public_fundamentals_imported,
                "public_fundamentals_failures": len(public_fundamentals_failures),
                "public_fundamentals_failure_samples": public_fundamentals_failures[:5],
                "public_industries_enabled": public_industries,
                "public_industry_symbols_requested": len(public_industry_symbols) if public_industries else 0,
                "public_industries_imported": public_industries_imported,
                "public_industries_failures": len(public_industries_failures),
                "public_industries_failure_samples": public_industries_failures[:5],
                "score_return_code": score_code,
                "daily_audit_export_tables": DAILY_AUDIT_EXPORT_TABLES,
                "artifacts": [str(observations_path), str(candidates_path), str(report_path)],
                "table_counts": repo.table_counts(),
                "daily_bar_health": {
                    "status": health["status"],
                    "latest_trade_date": health["latest_trade_date"],
                    "freshness_status": health["freshness_status"],
                    "weekday_lag_days": health["weekday_lag_days"],
                    "freshness_checked_on": health["freshness_checked_on"],
                },
                "quote_health": {
                    "priced_symbols": quote_health["priced_symbols"],
                    "current_priced_symbols": quote_health["current_priced_symbols"],
                    "stale_priced_symbols": quote_health["stale_priced_symbols"],
                    "latest_price_date": quote_health["latest_price_date"],
                    "freshness_status": quote_health["freshness_status"],
                    "weekday_lag_days": quote_health["weekday_lag_days"],
                    "freshness_checked_on": quote_health["freshness_checked_on"],
                },
                "fundamental_health": fundamental_health,
            }
        )
        repo.finish_daily_run(run_id, "succeeded", summary)
        print(f"Saved daily run: {run_id}")
        return 0
    except Exception as error:
        summary["failed_step"] = current_step
        repo.finish_daily_run(run_id, "failed", summary, f"{type(error).__name__}: {error}")
        raise


def _save_daily_ai_snapshot(repo: Repository, limit: int) -> dict[str, object]:
    health = repo.daily_bar_health()
    freshness = str(health.get("freshness_status") or "unknown")
    latest_trade_date = str(health.get("latest_trade_date") or "-")
    if freshness != "current":
        print(
            f"AI snapshot skipped: daily bars freshness={freshness} "
            f"latest_trade_date={latest_trade_date}"
        )
        return {
            "ai_snapshot_status": "skipped_stale_daily_bars",
            "ai_decisions_saved": 0,
            "ai_snapshot_daily_bar_freshness": freshness,
            "ai_snapshot_latest_trade_date": latest_trade_date,
        }
    quote_health = repo.quote_health()
    quote_freshness = str(quote_health.get("freshness_status") or "unknown")
    latest_price_date = str(quote_health.get("latest_price_date") or "-")
    if quote_freshness != "current":
        print(
            f"AI snapshot skipped: quotes freshness={quote_freshness} "
            f"latest_price_date={latest_price_date}"
        )
        return {
            "ai_snapshot_status": "skipped_stale_quotes",
            "ai_decisions_saved": 0,
            "ai_snapshot_quote_freshness": quote_freshness,
            "ai_snapshot_latest_price_date": latest_price_date,
            "ai_snapshot_current_priced_symbols": int(quote_health.get("current_priced_symbols") or 0),
            "ai_snapshot_priced_symbols": int(quote_health.get("priced_symbols") or 0),
        }
    decisions = rank_candidates(repo, limit=min(30, limit), min_score=1.0)
    saved_ai_decisions = (
        repo.insert_ai_decisions(decisions_to_rows(decisions), replace_same_signal=True) if decisions else 0
    )
    print(f"Saved AI snapshot decisions: {saved_ai_decisions}")
    return {
        "ai_snapshot_status": "saved" if saved_ai_decisions else "empty",
        "ai_decisions_saved": saved_ai_decisions,
    }


def _save_daily_strategy_snapshot(repo: Repository) -> dict[str, object]:
    health = repo.daily_bar_health()
    freshness = str(health.get("freshness_status") or "unknown")
    latest_trade_date = str(health.get("latest_trade_date") or "-")
    if freshness != "current":
        print(
            f"Strategy snapshot skipped: daily bars freshness={freshness} "
            f"latest_trade_date={latest_trade_date}"
        )
        return {
            "strategy_snapshot_enabled": True,
            "strategy_snapshot_status": "skipped_stale_daily_bars",
            "strategy_snapshot_daily_bar_freshness": freshness,
            "strategy_snapshot_latest_trade_date": latest_trade_date,
        }
    options = dict(DAILY_STRATEGY_SNAPSHOT_OPTIONS)
    result = repo.strategy_backtest(**options)
    parameters = {**options, "source": "daily_run_strategy_snapshot"}
    saved_run_id = repo.save_strategy_backtest_run(parameters, result)
    trade_count = int(result.get("trade_count") or 0)
    portfolio_avg_return = result.get("portfolio_avg_return")
    max_drawdown = result.get("max_drawdown")
    print(
        f"Saved strategy snapshot: run={saved_run_id} trades={trade_count} "
        f"portfolio_avg={_fmt_number(portfolio_avg_return)}% max_dd={_fmt_number(max_drawdown)}%"
    )
    return {
        "strategy_snapshot_enabled": True,
        "strategy_snapshot_status": "saved",
        "strategy_snapshot_run_id": saved_run_id,
        "strategy_snapshot_trade_count": trade_count,
        "strategy_snapshot_portfolio_avg_return": portfolio_avg_return,
        "strategy_snapshot_max_drawdown": max_drawdown,
    }


def _sync_tdx_daily_bars(
    repo: Repository,
    tdx_root: Path,
    include_indices: bool,
    requested_start_date: str,
) -> tuple[int, dict[str, object]]:
    existing_tdx_date = next(
        (
            str(source["last_trade_date"])
            for source in repo.daily_bar_health()["sources"]
            if source["source_kind"] == "tdx_unadjusted" and source["last_trade_date"]
        ),
        "",
    )
    start_date = requested_start_date or existing_tdx_date
    bars, files = load_tdx_daily_bars(
        tdx_root=tdx_root,
        include_indices=include_indices,
        start_date=start_date,
    )
    summary = {
        "tdx_root": str(tdx_root),
        "tdx_include_indices": include_indices,
        "tdx_start_date": start_date or None,
        "tdx_daily_files": len(files),
        "tdx_daily_bars_loaded": len(bars),
        "tdx_daily_bars_imported": 0,
    }
    if not files:
        print(f"No TDX .day files found under {tdx_root}.")
        return 1, summary
    imported = repo.upsert_daily_bars(bars) if bars else 0
    summary["tdx_daily_bars_imported"] = imported
    first_date = min((bar.trade_date for bar in bars), default="-")
    last_date = max((bar.trade_date for bar in bars), default="-")
    print(
        f"TDX daily files: {len(files)} bars={len(bars)} imported={imported} "
        f"date_range={first_date}->{last_date} start_date={start_date or '-'}"
    )
    return 0, summary


def _sync_tdx_themes(repo: Repository, tdx_root: Path) -> tuple[int, dict[str, object]]:
    memberships, files = load_tdx_theme_memberships(tdx_root=tdx_root)
    summary: dict[str, object] = {
        "tdx_theme_files": [str(path) for path in files],
        "tdx_theme_memberships_loaded": len(memberships),
        "tdx_theme_memberships_imported": 0,
    }
    if not files:
        print(f"No TDX block files found under {tdx_root / 'T0002' / 'hq_cache'}.")
        return 1, summary
    imported = repo.replace_stock_themes(memberships, files)
    summary["tdx_theme_memberships_imported"] = imported
    categories = sorted({item.category for item in memberships})
    print(
        f"TDX themes: files={len(files)} memberships={len(memberships)} imported={imported} "
        f"categories={','.join(categories) or '-'}"
    )
    return 0 if imported else 1, summary


def _daily_runs(repo: Repository, limit: int) -> int:
    rows = repo.daily_runs(limit=limit)
    if not rows:
        print("No saved daily runs.")
        return 0
    for row in rows:
        try:
            summary = json.loads(row["summary_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        detail = ""
        if isinstance(summary, dict):
            if row["status"] == "succeeded":
                health = summary.get("daily_bar_health")
                freshness = "-"
                if isinstance(health, dict):
                    freshness = str(health.get("freshness_status") or "-")
                quote_health = summary.get("quote_health")
                quote_freshness = "-"
                if isinstance(quote_health, dict):
                    quote_freshness = str(quote_health.get("freshness_status") or "-")
                ai_snapshot = str(summary.get("ai_snapshot_status") or "-")
                ai_saved = int(summary.get("ai_decisions_saved") or 0)
                ai_snapshot_latest_date = str(summary.get("ai_snapshot_latest_trade_date") or "-")
                ai_snapshot_quote_freshness = str(summary.get("ai_snapshot_quote_freshness") or "-")
                announcement_status = str(summary.get("public_announcement_status") or "-")
                announcement_count = int(summary.get("public_announcements_imported") or 0)
                strategy_snapshot_status = str(summary.get("strategy_snapshot_status") or "-")
                strategy_snapshot_run_id = summary.get("strategy_snapshot_run_id") or "-"
                strategy_snapshot_trades = int(summary.get("strategy_snapshot_trade_count") or 0)
                detail = (
                    f"history_bars={summary.get('history_bars_imported', 0)} "
                    f"history_symbols={summary.get('history_symbols', 0)} "
                    f"tdx_covered={summary.get('tdx_covered_symbols', 0)} "
                    f"tdx_sync_bars={summary.get('tdx_daily_bars_imported', 0)} "
                    f"daily_freshness={freshness} quote_freshness={quote_freshness} "
                    f"ai_snapshot={ai_snapshot}:{ai_saved}:{ai_snapshot_latest_date}:{ai_snapshot_quote_freshness} "
                    f"strategy_snapshot={strategy_snapshot_status}:{strategy_snapshot_run_id}:{strategy_snapshot_trades} "
                    f"announcements={announcement_status}:{announcement_count}"
                )
            else:
                detail = f"failed_step={summary.get('failed_step', '-')}"
        print(
            f"id={row['id']} started_at={display_shanghai_time(row['started_at'])} "
            f"finished_at={display_shanghai_time(row['finished_at']) if row['finished_at'] else '-'} "
            f"status={row['status']} {detail} error={row['error_text'] or '-'}"
        )
    return 0
