from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ai_decision import analyze_symbol, decisions_to_rows, rank_candidates
from .factor_engine import factor_definitions
from .field_inference import (
    compare_capture_payloads,
    load_observations,
    match_observations,
    read_capture,
    summarize_matches,
    write_capture,
)
from .history_import import fetch_tencent_daily_bars, load_daily_bars_csv
from .news_import import load_default_ths_news
from .quote_observer import fetch_tencent_observations, write_observations_csv
from .scoring_profile import load_scoring_profile, write_default_scoring_profile
from .storage import DEFAULT_DB_PATH, Repository
from .ths_local import DEFAULT_THS_ROOT, THSLocalAdapter
from .ths_monitor import inspect_ths_source
from .web_panel import serve_dashboard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ths-picker")
    parser.add_argument("--ths-root", type=Path, default=DEFAULT_THS_ROOT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Check local TongHuaShun paths and data files.")
    subparsers.add_parser("ths-monitor", help="Inspect TongHuaShun process and realtime cache freshness.")
    news_import_parser = subparsers.add_parser("import-ths-news", help="Import local TongHuaShun news cache.")
    news_import_parser.add_argument("--limit-per-file", type=int)
    news_parser = subparsers.add_parser("news", help="Show imported news items.")
    news_parser.add_argument("--limit", type=int, default=30)
    news_parser.add_argument("--q", default="")
    news_parser.add_argument("--tag", default="")
    subparsers.add_parser("factors", help="Show formula-inspired factor definitions.")
    factor_scan_parser = subparsers.add_parser("factor-scan", help="Scan latest daily bars for formula-inspired factor signals.")
    factor_scan_parser.add_argument("--limit", type=int, default=50)
    factor_scan_parser.add_argument("--symbol", action="append", default=None)
    factor_backtest_parser = subparsers.add_parser("factor-backtest", help="Backtest formula-inspired factors on stored daily bars.")
    factor_backtest_parser.add_argument("--horizon", type=int, default=5)
    factor_backtest_parser.add_argument("--limit-symbols", type=int)
    factor_matrix_parser = subparsers.add_parser("factor-matrix", help="Show multi-horizon factor effectiveness.")
    factor_matrix_parser.add_argument("--horizons", default="3,5,10")
    factor_matrix_parser.add_argument("--limit-symbols", type=int)
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
    candidates_parser = subparsers.add_parser("candidates", help="Export and show filtered stock candidates.")
    candidates_parser.add_argument("--limit", type=int, default=50)
    candidates_parser.add_argument("--min-score", type=float, default=1.0)
    candidates_parser.add_argument("--out", type=Path, default=Path("outputs/candidates.csv"))
    report_parser = subparsers.add_parser("report", help="Write a Markdown daily stock-picking report.")
    report_parser.add_argument("--limit", type=int, default=20)
    report_parser.add_argument("--min-score", type=float, default=1.0)
    report_parser.add_argument("--out", type=Path, default=Path("outputs/daily_report.md"))
    subparsers.add_parser("db-info", help="Show SQLite table counts.")
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
        choices=["securities", "market_snapshots", "quotes_realtime", "watchlists", "scores", "score_runs", "daily_bars", "stock_notes", "ai_decisions", "news_items"],
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
    public_history_parser = subparsers.add_parser(
        "import-public-history",
        help="Fetch recent daily bars from public Tencent kline endpoint.",
    )
    public_history_parser.add_argument("symbols", nargs="*")
    public_history_parser.add_argument("--universe", choices=["auto", "watchlist", "securities", "cache"])
    public_history_parser.add_argument("--limit", type=int, default=100)
    public_history_parser.add_argument("--days", type=int, default=80)
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
    daily_parser.add_argument("--profile", type=Path, help="Optional JSON scoring profile.")
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
        if args.command == "news":
            return _news(repo, args.limit, args.q, args.tag)
        if args.command == "factors":
            return _factors()
        if args.command == "factor-scan":
            return _factor_scan(repo, args.limit, args.symbol)
        if args.command == "factor-backtest":
            return _factor_backtest(repo, args.horizon, args.limit_symbols)
        if args.command == "factor-matrix":
            return _factor_matrix(repo, args.horizons, args.limit_symbols)
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
        if args.command == "candidates":
            return _candidates(repo, args.limit, args.min_score, args.out)
        if args.command == "report":
            return _report(repo, args.limit, args.min_score, args.out)
        if args.command == "db-info":
            return _db_info(repo)
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
        if args.command == "import-public-history":
            return _import_public_history(repo, args.symbols, args.universe, args.limit, args.days)
        if args.command == "auto-observe":
            return _auto_observe(args.symbols, args.out)
        if args.command == "import-public-quotes":
            return _import_public_quotes(repo, args.symbols, args.from_cache, args.universe, args.limit, args.observations_out)
        if args.command == "universe":
            return _universe(repo, args.source, args.limit)
        if args.command == "run-daily":
            return _run_daily(adapter, repo, args.limit, args.out_dir, args.universe, args.history_days, args.profile)
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


def _news(repo: Repository, limit: int, query: str, tag: str) -> int:
    rows = repo.latest_news(limit=limit, query=query, tag=tag)
    if not rows:
        print("No news found. Run import-ths-news first.")
        return 0
    for row in rows:
        print(f"{row['event_time'] or '-'} {row['title']} [{row['tags']}] {row['source'] or '-'}")
        if row["summary"]:
            print(f"   {row['summary'][:120]}")
    return 0


def _factors() -> int:
    for item in factor_definitions():
        print(f"{item.factor_id} {item.name} [{item.category}] risk={item.future_function_risk}")
        print(f"   {item.description}")
        print(f"   source: {item.source}")
    return 0


def _factor_scan(repo: Repository, limit: int, symbols: list[str] | None) -> int:
    rows = repo.factor_scan(limit=limit, symbols=symbols)
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


def _factor_matrix(repo: Repository, horizons_text: str, limit_symbols: int | None) -> int:
    horizons = _parse_horizons(horizons_text)
    rows = repo.factor_backtest_matrix(horizons=horizons, limit_symbols=limit_symbols)
    if not rows:
        print("No factor matrix samples found. Import more historical daily bars first.")
        return 0
    print(f"Factor matrix horizons={','.join(str(item) for item in horizons)}")
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
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['board'] or '-'} "
            f"score={row['total_score']:.2f} price={row['latest_price']} "
            f"pct={row['pct_change']:.2f}% amount={row['amount']} "
            f"mcap={((row['market_cap'] or 0) / 100000000):.2f}亿 "
            f"turnover={(row['turnover_rate'] or 0):.2f}%"
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
            f"tags={row['tags'] or '-'} note={row['note'] or '-'} updated_at={row['updated_at']}"
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
        print("No AI candidates found. Run run-daily or score first.")
        return 0
    if save:
        repo.insert_ai_decisions(decisions_to_rows(decisions))
    for index, item in enumerate(decisions, start=1):
        print(f"{index}. {item.symbol} {item.name or '-'} {item.decision} confidence={item.confidence:.0f}")
        print(f"   {item.summary}")
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
    print("Strengths:")
    for item in decision.strengths:
        print(f"- {item}")
    print("Risks:")
    for item in decision.risks:
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
            f"#{row['id']} {row['run_at']} {row['symbol']} {row['name'] or '-'} "
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


def _fmt_number(value: object, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _candidates(repo: Repository, limit: int, min_score: float, out: Path) -> int:
    count = repo.write_candidates_csv(out, limit=limit, min_score=min_score)
    rows = repo.latest_candidates(limit=limit, min_score=min_score)
    print(f"Wrote {count} candidates -> {out}")
    for row in rows[:20]:
        print(
            f"{row['symbol']} {row['name'] or '-'} {row['board'] or '-'} "
            f"score={row['total_score']:.2f} price={row['latest_price']} "
            f"pct={row['pct_change']:.2f}% amount={row['amount']} "
            f"mcap={((row['market_cap'] or 0) / 100000000):.2f}亿 "
            f"turnover={(row['turnover_rate'] or 0):.2f}%"
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
    selected = tables or ["securities", "market_snapshots", "quotes_realtime", "watchlists", "scores", "score_runs", "daily_bars", "stock_notes", "ai_decisions", "news_items"]
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


def _select_universe_symbols(repo: Repository, universe: str, limit: int) -> tuple[str, list[str]]:
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
    profile_path: Path | None = None,
) -> int:
    print("Step 1/6: importing TongHuaShun local cache")
    import_code = _import(adapter, repo)
    if import_code != 0:
        return import_code

    print("Step 2/6: importing TongHuaShun local news")
    _import_ths_news(repo, adapter.root, limit_per_file=300)

    print("Step 3/6: fetching public quote supplement")
    observations_path = out_dir / "observations_public.csv"
    quote_code = _import_public_quotes(repo, [], from_cache=False, universe=universe, limit=limit, observations_out=observations_path)
    if quote_code != 0:
        return quote_code

    print("Step 4/6: fetching public daily bars")
    source, history_symbols = _select_universe_symbols(repo, universe, limit)
    print(f"Using {source} universe for history: {len(history_symbols)} symbols")
    bars = fetch_tencent_daily_bars(history_symbols, count=history_days)
    repo.upsert_daily_bars(bars)
    print(f"Imported daily bars: {len(bars)}")

    print("Step 5/6: scoring")
    _score(repo, profile_path)
    _scores(repo, min(20, limit), positive_only=True)

    print("Step 6/6: exporting")
    _export(repo, out_dir, None)
    _candidates(repo, min(50, limit), 1.0, out_dir / "candidates.csv")
    _report(repo, min(20, limit), 1.0, out_dir / "daily_report.md")
    return 0
