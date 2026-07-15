from __future__ import annotations

import json
import sqlite3
import csv
import hashlib
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .factor_engine import FACTOR_ENGINE_VERSION, FactorSignal, evaluate_disclosed_fundamental, evaluate_factors, factor_definitions
from .fundamentals import FundamentalRecord
from .history_import import DailyBar
from .models import NewsItem, QuoteRealtime, Security, SnapshotDiagnostics, WatchlistEntry
from .quote_observer import QuoteObservation
from .public_industries import IndustryClassification
from .scoring_profile import ScoringProfile, default_scoring_profile
from .tdx_blocks import ThemeMembership

DEFAULT_DB_PATH = Path("work/ths_stock_picker.db")
SQLITE_BUSY_TIMEOUT_SECONDS = 30
CANONICAL_DAILY_BAR_POLICY_VERSION = 1
BENCHMARK_INDEX_LABELS = {
    "sh000001": "上证指数",
    "sh000016": "上证50",
    "sh000300": "沪深300",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}


class Repository:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        self.conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS securities (
                ths_code TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                market_id TEXT NOT NULL,
                source_file TEXT NOT NULL,
                security_type TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (market_id, ths_code)
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file TEXT NOT NULL,
                market TEXT NOT NULL,
                read_at TEXT NOT NULL,
                file_mtime TEXT,
                file_size INTEGER NOT NULL,
                status TEXT NOT NULL,
                format_version TEXT,
                header_hex TEXT NOT NULL,
                message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quotes_realtime (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT,
                market TEXT NOT NULL,
                latest_price REAL,
                pct_change REAL,
                volume REAL,
                amount REAL,
                open REAL,
                high REAL,
                low REAL,
                previous_close REAL,
                market_cap REAL,
                float_market_cap REAL,
                turnover_rate REAL,
                board TEXT,
                observed_at TEXT,
                quote_source TEXT,
                quote_status TEXT,
                source_snapshot_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_snapshot_id) REFERENCES market_snapshots(id)
            );

            CREATE TABLE IF NOT EXISTS watchlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                symbol TEXT,
                raw_value TEXT NOT NULL,
                source_file TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                score_run_id INTEGER,
                score_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                profile_name TEXT,
                total_score REAL NOT NULL,
                components_json TEXT NOT NULL,
                triggered_rules_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (score_run_id) REFERENCES score_runs(id)
            );

            CREATE TABLE IF NOT EXISTS score_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                score_date TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_bars (
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                source_file TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, trade_date, source_file)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_bars_trade_date_symbol
                ON daily_bars(trade_date DESC, symbol);

            CREATE TABLE IF NOT EXISTS fundamentals (
                symbol TEXT NOT NULL,
                report_date TEXT NOT NULL,
                notice_date TEXT,
                revenue REAL,
                revenue_yoy REAL,
                net_profit REAL,
                net_profit_yoy REAL,
                roe REAL,
                operating_cash_flow REAL,
                pe_ttm REAL,
                pb REAL,
                source_file TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, report_date, source_file)
            );

            CREATE INDEX IF NOT EXISTS idx_fundamentals_symbol_report_date
                ON fundamentals(symbol, report_date DESC);

            CREATE INDEX IF NOT EXISTS idx_fundamentals_symbol_notice_date
                ON fundamentals(symbol, notice_date DESC);

            CREATE TABLE IF NOT EXISTS stock_themes (
                symbol TEXT NOT NULL,
                category TEXT NOT NULL,
                theme TEXT NOT NULL,
                source_file TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, category, theme, source_file)
            );

            CREATE INDEX IF NOT EXISTS idx_stock_themes_symbol
                ON stock_themes(symbol, category, theme);

            CREATE TABLE IF NOT EXISTS stock_industries (
                symbol TEXT PRIMARY KEY,
                industry TEXT NOT NULL,
                source_file TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_stock_industries_industry
                ON stock_industries(industry, symbol);

            CREATE TABLE IF NOT EXISTS stock_notes (
                symbol TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'watch',
                tags TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT NOT NULL,
                name TEXT,
                decision TEXT NOT NULL,
                confidence REAL NOT NULL,
                summary TEXT NOT NULL,
                thesis_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS news_items (
                news_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                source TEXT,
                event_time TEXT,
                importance INTEGER,
                tags TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS factor_backtest_cache (
                cache_key TEXT PRIMARY KEY,
                horizons_json TEXT NOT NULL,
                limit_symbols INTEGER,
                max_bars INTEGER,
                source_fingerprint TEXT NOT NULL,
                rows_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS factor_scan_cache (
                cache_key TEXT PRIMARY KEY,
                limit_value INTEGER NOT NULL,
                symbols_json TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                rows_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS theme_price_cache (
                cache_key TEXT PRIMARY KEY,
                source_fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_bar_health_cache (
                cache_key TEXT PRIMARY KEY,
                source_fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS data_versions (
                dataset TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS strategy_validation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                parameters_json TEXT NOT NULL,
                data_fingerprint TEXT NOT NULL,
                verdict TEXT NOT NULL,
                summary TEXT NOT NULL,
                assessment_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_strategy_validation_runs_run_at
                ON strategy_validation_runs(run_at DESC);

            CREATE TABLE IF NOT EXISTS strategy_backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                parameters_json TEXT NOT NULL,
                data_fingerprint TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_strategy_backtest_runs_run_at
                ON strategy_backtest_runs(run_at DESC);

            CREATE TABLE IF NOT EXISTS daily_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                status TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_daily_runs_started_at
                ON daily_runs(started_at DESC);

            INSERT OR IGNORE INTO data_versions (dataset, version) VALUES ('daily_bars', 0);
            INSERT OR IGNORE INTO data_versions (dataset, version) VALUES ('stock_themes', 0);
            INSERT OR IGNORE INTO data_versions (dataset, version) VALUES ('fundamentals', 0);
            """
        )
        self._ensure_columns(
            "quotes_realtime",
            {
                "open": "REAL",
                "high": "REAL",
                "low": "REAL",
                "previous_close": "REAL",
                "market_cap": "REAL",
                "float_market_cap": "REAL",
                "turnover_rate": "REAL",
                "board": "TEXT",
                "observed_at": "TEXT",
                "quote_source": "TEXT",
                "quote_status": "TEXT",
            },
        )
        self._ensure_columns(
            "scores",
            {
                "score_run_id": "INTEGER",
                "profile_name": "TEXT",
            },
        )
        self._ensure_columns(
            "fundamentals",
            {
                "notice_date": "TEXT",
                "revenue_yoy": "REAL",
                "net_profit_yoy": "REAL",
            },
        )
        self.conn.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, column_type in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")

    def replace_securities(self, securities: list[Security]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM securities")
            self.conn.executemany(
                """
                INSERT INTO securities
                    (ths_code, symbol, name, market_id, source_file, security_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.ths_code,
                        item.symbol,
                        item.name,
                        item.market_id,
                        str(item.source_file),
                        item.security_type,
                    )
                    for item in securities
                ],
            )

    def insert_snapshots(self, snapshots: list[SnapshotDiagnostics]) -> list[int]:
        ids: list[int] = []
        with self.conn:
            for item in snapshots:
                cursor = self.conn.execute(
                    """
                    INSERT INTO market_snapshots
                        (source_file, market, read_at, file_mtime, file_size, status,
                         format_version, header_hex, message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(item.source_file),
                        item.market,
                        item.read_at.isoformat(timespec="seconds"),
                        item.file_mtime.isoformat(timespec="seconds") if item.file_mtime else None,
                        item.file_size,
                        item.status,
                        item.format_version,
                        item.header_hex,
                        item.message,
                    ),
                )
                ids.append(int(cursor.lastrowid))
        return ids

    def replace_watchlists(self, entries: list[WatchlistEntry]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM watchlists")
            self.conn.executemany(
                """
                INSERT INTO watchlists (name, symbol, raw_value, source_file, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (item.name, item.symbol, item.raw_value, str(item.source_file), item.status)
                    for item in entries
                ],
            )

    def insert_quotes(self, quotes: list[QuoteRealtime]) -> None:
        with self.conn:
            # Local cache rows can be code-only; keep the separately refreshed public prices.
            self.conn.execute(
                "DELETE FROM quotes_realtime WHERE COALESCE(quote_status, '') != 'public_quote'"
            )
            self.conn.executemany(
                """
                INSERT INTO quotes_realtime
                    (symbol, name, market, latest_price, pct_change, volume, amount,
                     open, high, low, previous_close, market_cap, float_market_cap, turnover_rate, board,
                     observed_at, quote_source, quote_status,
                     source_snapshot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        quote.symbol,
                        quote.name,
                        quote.market,
                        quote.latest_price,
                        quote.pct_change,
                        quote.volume,
                        quote.amount,
                        quote.open,
                        quote.high,
                        quote.low,
                        quote.previous_close,
                        quote.market_cap,
                        quote.float_market_cap,
                        quote.turnover_rate,
                        quote.board,
                        quote.observed_at,
                        quote.quote_source,
                        quote.quote_status,
                        quote.source_snapshot_id,
                    )
                    for quote in quotes
                ],
            )

    def upsert_public_quotes(self, observations: list[QuoteObservation]) -> int:
        quotes_by_symbol = {
            item.symbol: QuoteRealtime(
                symbol=item.symbol,
                name=item.name,
                market=_market_for_symbol(item.symbol),
                latest_price=item.latest_price,
                pct_change=item.pct_change,
                volume=item.volume,
                amount=item.amount,
                open=item.open,
                high=item.high,
                low=item.low,
                previous_close=item.previous_close,
                market_cap=item.market_cap,
                float_market_cap=item.float_market_cap,
                turnover_rate=item.turnover_rate,
                board=item.board,
                observed_at=item.observed_at,
                quote_source=item.source,
                quote_status="public_quote",
            )
            for item in observations
        }
        quotes = list(quotes_by_symbol.values())
        if not quotes:
            return 0
        with self.conn:
            self.conn.executemany(
                "DELETE FROM quotes_realtime WHERE quote_status = 'public_quote' AND symbol = ?",
                [(quote.symbol,) for quote in quotes],
            )
            self.conn.executemany(
                """
                INSERT INTO quotes_realtime
                    (symbol, name, market, latest_price, pct_change, volume, amount,
                     open, high, low, previous_close, market_cap, float_market_cap, turnover_rate, board,
                     observed_at, quote_source, quote_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        quote.symbol,
                        quote.name,
                        quote.market,
                        quote.latest_price,
                        quote.pct_change,
                        quote.volume,
                        quote.amount,
                        quote.open,
                        quote.high,
                        quote.low,
                        quote.previous_close,
                        quote.market_cap,
                        quote.float_market_cap,
                        quote.turnover_rate,
                        quote.board,
                        quote.observed_at,
                        quote.quote_source,
                        quote.quote_status,
                    )
                    for quote in quotes
                ],
            )
        return len(quotes)

    def list_realtime_symbols(self, limit: int | None = None) -> list[str]:
        sql = """
            SELECT DISTINCT symbol
            FROM quotes_realtime
            WHERE symbol GLOB '[036][0-9][0-9][0-9][0-9][0-9]'
            ORDER BY symbol
        """
        if limit is not None:
            sql += " LIMIT ?"
            rows = self.conn.execute(sql, (limit,)).fetchall()
        else:
            rows = self.conn.execute(sql).fetchall()
        return [row["symbol"] for row in rows]

    def list_watchlist_symbols(self, limit: int | None = None) -> list[str]:
        sql = """
            SELECT DISTINCT symbol
            FROM watchlists
            WHERE status = 'parsed'
              AND symbol IS NOT NULL
              AND (
                  symbol GLOB '60[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '68[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '00[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '30[0-9][0-9][0-9][0-9]'
              )
              AND symbol != '000000'
            ORDER BY symbol
        """
        if limit is not None:
            sql += " LIMIT ?"
            rows = self.conn.execute(sql, (limit,)).fetchall()
        else:
            rows = self.conn.execute(sql).fetchall()
        return [row["symbol"] for row in rows]

    def list_security_universe_symbols(self, limit: int | None = None) -> list[str]:
        prefixes = ["300", "301", "688", "603", "605", "601", "600", "002", "003", "001", "000"]
        if limit is None:
            per_prefix_limit = None
        else:
            per_prefix_limit = max(1, (limit + len(prefixes) - 1) // len(prefixes))
        symbols: list[str] = []
        seen: set[str] = set()
        for prefix in prefixes:
            for symbol in self._list_security_symbols_for_prefix(prefix, per_prefix_limit):
                if symbol in seen:
                    continue
                seen.add(symbol)
                symbols.append(symbol)
                if limit is not None and len(symbols) >= limit:
                    return symbols
        return symbols

    def _list_security_symbols_for_prefix(self, prefix: str, limit: int | None) -> list[str]:
        sql = """
            SELECT DISTINCT symbol
            FROM securities
            WHERE security_type = 'stock'
              AND symbol LIKE ?
              AND symbol != '000000'
              AND UPPER(COALESCE(name, '')) NOT LIKE '%ST%'
              AND UPPER(COALESCE(name, '')) NOT LIKE '%PT%'
              AND COALESCE(name, '') NOT LIKE '%退%'
            ORDER BY symbol DESC
        """
        if limit is not None:
            sql += " LIMIT ?"
            rows = self.conn.execute(sql, (f"{prefix}%", limit)).fetchall()
        else:
            rows = self.conn.execute(sql, (f"{prefix}%",)).fetchall()
        return [row["symbol"] for row in rows]

    def list_auto_universe_symbols(self, limit: int | None = None) -> tuple[str, list[str]]:
        watchlist_symbols = self.list_watchlist_symbols(limit=limit)
        if len(watchlist_symbols) >= 5:
            return "watchlist", watchlist_symbols
        return "securities", self.list_security_universe_symbols(limit=limit)

    def score_latest_quotes(self, profile: ScoringProfile | None = None) -> int:
        scoring_profile = profile or default_scoring_profile()
        rows = self.conn.execute(
            """
            SELECT symbol, name, latest_price, pct_change, volume, amount, open, high, low,
                   previous_close, market_cap, float_market_cap, turnover_rate, board
            FROM quotes_realtime
            WHERE latest_price IS NOT NULL OR pct_change IS NOT NULL
            """
        ).fetchall()
        with self.conn:
            if not rows:
                return 0
            today = date.today().isoformat()
            profile_json = json.dumps(
                {
                    "name": scoring_profile.name,
                    "component_weights": scoring_profile.component_weights,
                    "disabled_components": sorted(scoring_profile.disabled_components),
                },
                ensure_ascii=False,
            )
            cursor = self.conn.execute(
                """
                INSERT INTO score_runs (score_date, profile_name, profile_json)
                VALUES (?, ?, ?)
                """,
                (today, scoring_profile.name, profile_json),
            )
            score_run_id = int(cursor.lastrowid)
            for row in rows:
                components, rules = _score_row(row)
                technical_components, technical_rules = self._technical_score(row["symbol"])
                components.update(technical_components)
                rules.extend(technical_rules)
                components = scoring_profile.apply(components)
                total = sum(components.values())
                self.conn.execute(
                    """
                    INSERT INTO scores
                        (score_run_id, score_date, symbol, profile_name,
                         total_score, components_json, triggered_rules_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        score_run_id,
                        today,
                        row["symbol"],
                        scoring_profile.name,
                        total,
                        json.dumps(components, ensure_ascii=False),
                        json.dumps(rules, ensure_ascii=False),
                    ),
                )
        return len(rows)

    def _technical_score(self, symbol: str) -> tuple[dict[str, float], list[str]]:
        rows = self._canonical_daily_bar_rows(symbol, limit=30, require_close=True)
        if len(rows) < 5:
            return {}, []
        bars = list(reversed(rows))
        closes = [float(row["close"]) for row in bars if row["close"] is not None]
        if len(closes) < 5:
            return {}, []

        components: dict[str, float] = {}
        rules: list[str] = []
        latest = closes[-1]
        ma5 = sum(closes[-5:]) / 5
        if latest >= ma5:
            components["trend_ma5"] = 10.0
            rules.append("close_above_ma5")
        else:
            components["trend_ma5"] = -8.0
            rules.append("close_below_ma5")

        pct_5d = (latest / closes[-5] - 1) * 100 if closes[-5] else 0.0
        if 0 <= pct_5d <= 8:
            components["momentum_5d"] = 12.0
            rules.append("healthy_5d_momentum")
        elif pct_5d > 12:
            components["momentum_5d"] = -8.0
            rules.append("overextended_5d_momentum")
        elif pct_5d < -8:
            components["momentum_5d"] = -10.0
            rules.append("weak_5d_momentum")

        if len(closes) >= 20:
            ma20 = sum(closes[-20:]) / 20
            if latest >= ma20 and ma5 >= ma20:
                components["trend_ma20"] = 15.0
                rules.append("ma5_above_ma20_and_close_above_ma20")
            elif latest < ma20:
                components["trend_ma20"] = -12.0
                rules.append("close_below_ma20")

            pct_20d = (latest / closes[-20] - 1) * 100 if closes[-20] else 0.0
            if 0 <= pct_20d <= 20:
                components["momentum_20d"] = 10.0
                rules.append("healthy_20d_momentum")
            elif pct_20d > 30:
                components["momentum_20d"] = -8.0
                rules.append("overextended_20d_momentum")
            elif pct_20d < -12:
                components["momentum_20d"] = -10.0
                rules.append("weak_20d_momentum")

            returns = [
                closes[index] / closes[index - 1] - 1
                for index in range(len(closes) - 19, len(closes))
                if closes[index - 1]
            ]
            if len(returns) >= 10:
                avg = sum(returns) / len(returns)
                volatility = math.sqrt(sum((item - avg) ** 2 for item in returns) / len(returns)) * 100
                if volatility <= 2.5:
                    components["volatility"] = 8.0
                    rules.append("controlled_20d_volatility")
                elif volatility > 6:
                    components["volatility"] = -10.0
                    rules.append("high_20d_volatility")

        return components, rules

    def table_counts(self) -> dict[str, int]:
        tables = [
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
            "factor_backtest_cache",
            "factor_scan_cache",
            "theme_price_cache",
            "daily_bar_health_cache",
            "strategy_backtest_runs",
            "daily_runs",
        ]
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def daily_bar_freshness(self, as_of: date | None = None) -> dict[str, object]:
        latest_any_row = self.conn.execute(
            "SELECT trade_date FROM daily_bars ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        latest_any_trade_date = str(latest_any_row["trade_date"] or "") if latest_any_row is not None else ""
        latest_stock_trade_date = ""
        stock_pattern_sql = """
            symbol GLOB '60[0-9][0-9][0-9][0-9]'
            OR symbol GLOB '68[0-9][0-9][0-9][0-9]'
            OR symbol GLOB '00[0-9][0-9][0-9][0-9]'
            OR symbol GLOB '30[0-9][0-9][0-9][0-9]'
        """
        for row in self.conn.execute("SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date DESC"):
            has_stock = self.conn.execute(
                f"SELECT 1 FROM daily_bars WHERE trade_date = ? AND ({stock_pattern_sql}) LIMIT 1",
                (row["trade_date"],),
            ).fetchone()
            if has_stock is not None:
                latest_stock_trade_date = str(row["trade_date"])
                break
        latest_trade_date = latest_stock_trade_date or latest_any_trade_date
        checked_on = as_of or date.today()
        weekday_lag_days = _weekday_lag_days(latest_trade_date, checked_on)
        freshness_status = (
            "empty"
            if not latest_trade_date
            else "unknown"
            if weekday_lag_days is None
            else "current"
            if weekday_lag_days <= 1
            else "lagging"
        )
        return {
            "latest_trade_date": latest_trade_date or None,
            "latest_stock_trade_date": latest_stock_trade_date or None,
            "weekday_lag_days": weekday_lag_days,
            "freshness_status": freshness_status,
            "freshness_checked_on": checked_on.isoformat(),
        }

    def quote_health(self, as_of: date | None = None) -> dict[str, object]:
        row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT symbol) AS priced_symbols,
                   COUNT(*) AS priced_rows,
                   COALESCE(SUM(CASE WHEN quote_status = 'public_quote' THEN 1 ELSE 0 END), 0) AS public_quote_rows,
                   MAX(observed_at) AS latest_observed_at
            FROM quotes_realtime
            WHERE latest_price > 0
            """
        ).fetchone()
        symbol_rows = self.conn.execute(
            """
            SELECT symbol, MAX(observed_at) AS latest_observed_at
            FROM quotes_realtime
            WHERE latest_price > 0
            GROUP BY symbol
            """
        ).fetchall()
        latest_observed_at = str(row["latest_observed_at"] or "")
        latest_price_date = latest_observed_at[:10]
        checked_on = as_of or date.today()
        weekday_lag_days = _weekday_lag_days(latest_price_date, checked_on)
        priced_symbols = int(row["priced_symbols"] or 0)
        current_priced_symbols = 0
        stale_priced_symbols = 0
        unknown_priced_symbols = 0
        for symbol_row in symbol_rows:
            lag_days = _weekday_lag_days(str(symbol_row["latest_observed_at"] or "")[:10], checked_on)
            if lag_days is None:
                unknown_priced_symbols += 1
            elif lag_days <= 1:
                current_priced_symbols += 1
            else:
                stale_priced_symbols += 1
        freshness_status = (
            "empty"
            if priced_symbols == 0
            else "current"
            if stale_priced_symbols == 0 and unknown_priced_symbols == 0
            else "partial"
            if current_priced_symbols > 0
            else "lagging"
            if stale_priced_symbols > 0
            else "unknown"
        )
        return {
            "priced_symbols": priced_symbols,
            "priced_rows": int(row["priced_rows"] or 0),
            "public_quote_rows": int(row["public_quote_rows"] or 0),
            "current_priced_symbols": current_priced_symbols,
            "stale_priced_symbols": stale_priced_symbols,
            "unknown_priced_symbols": unknown_priced_symbols,
            "latest_observed_at": latest_observed_at or None,
            "latest_price_date": latest_price_date or None,
            "weekday_lag_days": weekday_lag_days,
            "freshness_status": freshness_status,
            "freshness_checked_on": checked_on.isoformat(),
        }

    def fundamental_health(self, as_of: date | None = None) -> dict[str, object]:
        checked_on = as_of or date.today()
        as_of_date = checked_on.isoformat()
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS total_records,
                   COUNT(DISTINCT symbol) AS total_symbols,
                   COUNT(DISTINCT CASE WHEN notice_date IS NOT NULL AND notice_date < ? THEN symbol END) AS disclosed_symbols,
                   COUNT(CASE WHEN notice_date IS NOT NULL AND notice_date < ? THEN 1 END) AS disclosed_records,
                   COUNT(DISTINCT CASE WHEN operating_cash_flow IS NOT NULL AND notice_date IS NOT NULL AND notice_date < ? THEN symbol END) AS operating_cash_flow_symbols,
                   COUNT(CASE WHEN operating_cash_flow IS NOT NULL AND notice_date IS NOT NULL AND notice_date < ? THEN 1 END) AS operating_cash_flow_records,
                   MAX(report_date) AS latest_imported_report_date,
                   MAX(CASE WHEN notice_date IS NOT NULL AND notice_date < ? THEN report_date END) AS latest_disclosed_report_date,
                   MAX(CASE WHEN notice_date IS NOT NULL AND notice_date < ? THEN notice_date END) AS latest_disclosed_notice_date,
                   COUNT(DISTINCT source_file) AS source_count
            FROM fundamentals
            """,
            (as_of_date, as_of_date, as_of_date, as_of_date, as_of_date, as_of_date),
        ).fetchone()
        return {
            "as_of_date": as_of_date,
            "total_records": int(row["total_records"] or 0),
            "total_symbols": int(row["total_symbols"] or 0),
            "disclosed_symbols": int(row["disclosed_symbols"] or 0),
            "disclosed_records": int(row["disclosed_records"] or 0),
            "operating_cash_flow_symbols": int(row["operating_cash_flow_symbols"] or 0),
            "operating_cash_flow_records": int(row["operating_cash_flow_records"] or 0),
            "latest_imported_report_date": row["latest_imported_report_date"],
            "latest_disclosed_report_date": row["latest_disclosed_report_date"],
            "latest_disclosed_notice_date": row["latest_disclosed_notice_date"],
            "source_count": int(row["source_count"] or 0),
        }

    def daily_bar_health(self, as_of: date | None = None) -> dict[str, object]:
        checked_on = as_of or date.today()
        cached = self._load_daily_bar_health_cache(checked_on)
        if cached is not None:
            return cached
        source_rows = self.conn.execute(
            """
            SELECT CASE
                       WHEN lower(source_file) LIKE 'tdx:%' THEN 'tdx_unadjusted'
                       WHEN lower(source_file) LIKE 'tencent:%' THEN 'tencent_qfq'
                       ELSE 'csv_or_other'
                   END AS source_kind,
                   COUNT(*) AS bars,
                   COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS first_trade_date,
                   MAX(trade_date) AS last_trade_date
            FROM daily_bars
            GROUP BY source_kind
            """
        ).fetchall()
        source_priority = {"tdx_unadjusted": 0, "csv_or_other": 1, "tencent_qfq": 2}
        sources = [dict(row) for row in source_rows]
        sources.sort(key=lambda row: (source_priority.get(str(row["source_kind"]), 99), str(row["source_kind"])))
        duplicate_row = self.conn.execute(
            """
            SELECT COUNT(*) AS duplicate_symbol_days,
                   COALESCE(SUM(source_count), 0) AS duplicate_rows
            FROM (
                SELECT symbol, trade_date, COUNT(*) AS source_count
                FROM daily_bars
                GROUP BY symbol, trade_date
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        total_bars = sum(int(row["bars"]) for row in sources)
        total_symbols = int(self.conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bars").fetchone()[0])
        duplicate_symbol_days = int(duplicate_row["duplicate_symbol_days"] or 0)
        latest_any_trade_date = max(
            (str(row["last_trade_date"]) for row in sources if row["last_trade_date"]),
            default="",
        )
        latest_stock_row = self.conn.execute(
            """
            SELECT MAX(trade_date) AS latest_trade_date
            FROM daily_bars
            WHERE symbol GLOB '60[0-9][0-9][0-9][0-9]'
               OR symbol GLOB '68[0-9][0-9][0-9][0-9]'
               OR symbol GLOB '00[0-9][0-9][0-9][0-9]'
               OR symbol GLOB '30[0-9][0-9][0-9][0-9]'
            """
        ).fetchone()
        latest_stock_trade_date = str(latest_stock_row["latest_trade_date"] or "")
        latest_trade_date = latest_stock_trade_date or latest_any_trade_date
        weekday_lag_days = _weekday_lag_days(latest_trade_date, checked_on)
        freshness_status = (
            "empty"
            if total_bars == 0
            else "unknown"
            if weekday_lag_days is None
            else "current"
            if weekday_lag_days <= 1
            else "lagging"
        )
        health = {
            "status": "empty" if total_bars == 0 else "attention" if duplicate_symbol_days else "clean",
            "total_bars": total_bars,
            "total_symbols": total_symbols,
            "duplicate_symbol_days": duplicate_symbol_days,
            "duplicate_rows": int(duplicate_row["duplicate_rows"] or 0),
            "latest_trade_date": latest_trade_date or None,
            "latest_stock_trade_date": latest_stock_trade_date or None,
            "latest_any_trade_date": latest_any_trade_date or None,
            "weekday_lag_days": weekday_lag_days,
            "freshness_status": freshness_status,
            "freshness_checked_on": checked_on.isoformat(),
            "canonical_source_policy": "tdx_unadjusted > csv_or_other > tencent_qfq",
            "sources": sources,
        }
        self._save_daily_bar_health_cache(checked_on, health)
        return health

    def _load_daily_bar_health_cache(self, checked_on: date) -> dict[str, object] | None:
        row = self.conn.execute(
            "SELECT payload_json, source_fingerprint FROM daily_bar_health_cache WHERE cache_key = ?",
            (self._daily_bar_health_cache_key(checked_on),),
        ).fetchone()
        if row is None or row["source_fingerprint"] != self._daily_bar_health_fingerprint():
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
            return None
        return payload

    def _save_daily_bar_health_cache(self, checked_on: date, health: dict[str, object]) -> None:
        fingerprint = self._daily_bar_health_fingerprint()
        with self.conn:
            self.conn.execute(
                "DELETE FROM daily_bar_health_cache WHERE source_fingerprint != ?",
                (fingerprint,),
            )
            self.conn.execute(
                """
                INSERT INTO daily_bar_health_cache (cache_key, source_fingerprint, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    source_fingerprint = excluded.source_fingerprint,
                    payload_json = excluded.payload_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    self._daily_bar_health_cache_key(checked_on),
                    fingerprint,
                    json.dumps(health, ensure_ascii=False),
                ),
            )

    def _daily_bar_health_cache_key(self, checked_on: date) -> str:
        return f"checked_on={checked_on.isoformat()}"

    def _daily_bar_health_fingerprint(self) -> str:
        row = self.conn.execute("SELECT version FROM data_versions WHERE dataset = 'daily_bars'").fetchone()
        version = int(row["version"]) if row is not None else 0
        return f"daily_bars_v={version}|canonical_policy_v={CANONICAL_DAILY_BAR_POLICY_VERSION}|health_cache_v=1"

    def export_table_csv(self, table: str, output_path: Path) -> int:
        allowed = {
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
            "factor_backtest_cache",
            "factor_scan_cache",
            "theme_price_cache",
            "daily_bar_health_cache",
            "strategy_validation_runs",
            "strategy_backtest_runs",
            "daily_runs",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            columns = rows[0].keys()
        else:
            columns = [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row[column] for column in columns])
        return len(rows)

    def upsert_daily_bars(self, bars: list[DailyBar]) -> int:
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO daily_bars
                    (symbol, trade_date, open, high, low, close, volume, amount, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date, source_file) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    amount = excluded.amount,
                    imported_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        item.symbol,
                        item.trade_date,
                        item.open,
                        item.high,
                        item.low,
                        item.close,
                        item.volume,
                        item.amount,
                        str(item.source_file),
                    )
                    for item in bars
                ],
            )
            if bars:
                self._bump_data_version("daily_bars")
        return len(bars)

    def upsert_fundamentals(self, records: list[FundamentalRecord]) -> int:
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO fundamentals
                    (symbol, report_date, notice_date, revenue, revenue_yoy, net_profit, net_profit_yoy, roe, operating_cash_flow, pe_ttm, pb, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, report_date, source_file) DO UPDATE SET
                    notice_date = excluded.notice_date,
                    revenue = excluded.revenue,
                    revenue_yoy = excluded.revenue_yoy,
                    net_profit = excluded.net_profit,
                    net_profit_yoy = excluded.net_profit_yoy,
                    roe = excluded.roe,
                    operating_cash_flow = excluded.operating_cash_flow,
                    pe_ttm = excluded.pe_ttm,
                    pb = excluded.pb,
                    imported_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        item.symbol,
                        item.report_date,
                        item.notice_date,
                        item.revenue,
                        item.revenue_yoy,
                        item.net_profit,
                        item.net_profit_yoy,
                        item.roe,
                        item.operating_cash_flow,
                        item.pe_ttm,
                        item.pb,
                        str(item.source_file),
                    )
                    for item in records
                ],
            )
            if records:
                self._bump_data_version("fundamentals")
        return len(records)

    def latest_fundamental(self, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT symbol, report_date, notice_date, revenue, revenue_yoy, net_profit, net_profit_yoy, roe, operating_cash_flow, pe_ttm, pb, source_file, imported_at
            FROM fundamentals
            WHERE symbol = ?
            ORDER BY
                report_date DESC,
                (revenue IS NOT NULL) + (revenue_yoy IS NOT NULL) + (net_profit IS NOT NULL) + (net_profit_yoy IS NOT NULL) + (roe IS NOT NULL)
                    + (operating_cash_flow IS NOT NULL) + (pe_ttm IS NOT NULL) + (pb IS NOT NULL) DESC,
                notice_date DESC,
                imported_at DESC,
                source_file
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

    def disclosed_fundamental_as_of(self, symbol: str, signal_date: str) -> dict[str, object] | None:
        """Return the latest report known before a date-only market signal, merged across sources."""
        return _disclosed_fundamental_at(self._disclosed_fundamentals_for_symbol(symbol), signal_date)

    def _disclosed_fundamentals_for_symbol(self, symbol: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT symbol, report_date, notice_date, revenue, revenue_yoy, net_profit, net_profit_yoy, roe, operating_cash_flow, pe_ttm, pb, source_file, imported_at
            FROM fundamentals
            WHERE symbol = ?
              AND notice_date IS NOT NULL
            """,
            (symbol,),
        ).fetchall()

    def replace_stock_themes(self, memberships: list[ThemeMembership], source_files: list[Path]) -> int:
        source_values = sorted({str(path) for path in source_files})
        with self.conn:
            if source_values:
                placeholders = ", ".join("?" for _ in source_values)
                self.conn.execute(f"DELETE FROM stock_themes WHERE source_file IN ({placeholders})", tuple(source_values))
            if memberships:
                self.conn.executemany(
                    """
                    INSERT INTO stock_themes (symbol, category, theme, source_file)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(symbol, category, theme, source_file) DO UPDATE SET
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    [(item.symbol, item.category, item.theme, str(item.source_file)) for item in memberships],
                )
            if source_values:
                self._bump_data_version("stock_themes")
        return len(memberships)

    def themes_for_symbol(self, symbol: str, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT category, theme, source_file, MAX(updated_at) AS updated_at
            FROM stock_themes
            WHERE symbol = ?
            GROUP BY category, theme, source_file
            ORDER BY CASE category WHEN '概念' THEN 0 WHEN '风格' THEN 1 ELSE 9 END, theme
            LIMIT ?
            """,
            (symbol, max(1, limit)),
        ).fetchall()

    def replace_stock_industries(self, records: list[IndustryClassification]) -> int:
        if not records:
            return 0
        source_values = sorted({str(record.source_file) for record in records})
        with self.conn:
            placeholders = ", ".join("?" for _ in source_values)
            self.conn.execute(f"DELETE FROM stock_industries WHERE source_file IN ({placeholders})", tuple(source_values))
            self.conn.executemany(
                """
                INSERT INTO stock_industries (symbol, industry, source_file)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    industry = excluded.industry,
                    source_file = excluded.source_file,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [(record.symbol, record.industry, str(record.source_file)) for record in records],
            )
        return len(records)

    def upsert_stock_industries(self, records: list[IndustryClassification]) -> int:
        if not records:
            return 0
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO stock_industries (symbol, industry, source_file)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    industry = excluded.industry,
                    source_file = excluded.source_file,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [(record.symbol, record.industry, str(record.source_file)) for record in records],
            )
        return len(records)

    def industry_for_symbol(self, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT symbol, industry, source_file, updated_at FROM stock_industries WHERE symbol = ?",
            (symbol,),
        ).fetchone()

    def industry_refresh_symbols(self, symbols: list[str], limit: int) -> list[str]:
        """Pick missing labels first, then refresh the oldest existing labels."""
        candidates = list(dict.fromkeys(symbols))
        if not candidates:
            return []
        existing: dict[str, str] = {}
        for offset in range(0, len(candidates), 900):
            chunk = candidates[offset : offset + 900]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT symbol, updated_at FROM stock_industries WHERE symbol IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            existing.update({str(row["symbol"]): str(row["updated_at"] or "") for row in rows})
        ranked = sorted(
            enumerate(candidates),
            key=lambda item: (
                0 if item[1] not in existing else 1,
                existing.get(item[1], ""),
                item[0],
            ),
        )
        return [symbol for _, symbol in ranked[: max(1, limit)]]

    def industry_health(self) -> dict[str, object]:
        latest_run = self.conn.execute("SELECT id, score_date FROM score_runs ORDER BY id DESC LIMIT 1").fetchone()
        score_run_id = int(latest_run["id"]) if latest_run is not None else -1
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS label_records,
                   COUNT(DISTINCT i.industry) AS industry_count,
                   COUNT(s.symbol) AS scored_symbols,
                   MAX(i.updated_at) AS latest_updated_at
            FROM stock_industries i
            LEFT JOIN scores s ON s.symbol = i.symbol AND s.score_run_id = ?
            """,
            (score_run_id,),
        ).fetchone()
        return {
            "label_records": int(row["label_records"] or 0),
            "industry_count": int(row["industry_count"] or 0),
            "scored_symbols": int(row["scored_symbols"] or 0),
            "latest_updated_at": row["latest_updated_at"],
            "score_date": str(latest_run["score_date"]) if latest_run is not None else None,
        }

    def industry_heat(self, limit: int = 50, min_scored: int = 3) -> dict[str, object]:
        latest_run = self.conn.execute("SELECT id, score_date FROM score_runs ORDER BY id DESC LIMIT 1").fetchone()
        score_run_id = int(latest_run["id"]) if latest_run is not None else -1
        rows = self.conn.execute(
            """
            SELECT i.industry,
                   COUNT(*) AS member_count,
                   COUNT(s.symbol) AS scored_count,
                   AVG(s.total_score) AS average_score,
                   SUM(CASE WHEN s.total_score > 0 THEN 1 ELSE 0 END) AS positive_count
            FROM stock_industries i
            LEFT JOIN scores s ON s.symbol = i.symbol AND s.score_run_id = ?
            GROUP BY i.industry
            HAVING COUNT(s.symbol) >= ?
            ORDER BY AVG(s.total_score) DESC, COUNT(s.symbol) DESC, COUNT(*) DESC, i.industry
            LIMIT ?
            """,
            (score_run_id, max(1, min_scored), max(1, limit)),
        ).fetchall()
        items = []
        for row in rows:
            member_count = int(row["member_count"] or 0)
            scored_count = int(row["scored_count"] or 0)
            positive_count = int(row["positive_count"] or 0)
            items.append(
                {
                    "industry": str(row["industry"]),
                    "member_count": member_count,
                    "scored_count": scored_count,
                    "coverage_rate": scored_count / member_count * 100 if member_count else None,
                    "average_score": float(row["average_score"]) if row["average_score"] is not None else None,
                    "positive_rate": positive_count / scored_count * 100 if scored_count else None,
                }
            )
        return {
            "score_date": str(latest_run["score_date"]) if latest_run is not None else None,
            "min_scored": max(1, min_scored),
            "items": items,
        }

    def theme_heat(self, limit: int = 50, category: str = "", min_scored: int = 3) -> dict[str, object]:
        latest_run = self.conn.execute(
            "SELECT id, score_date FROM score_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        score_run_id = int(latest_run["id"]) if latest_run is not None else -1
        price_as_of_date, price_performance = self._theme_price_performance(category)
        where_clause = "WHERE t.category = ?" if category else ""
        parameters: list[object] = [score_run_id]
        if category:
            parameters.append(category)
        parameters.append(max(1, min_scored))
        parameters.append(max(1, limit))
        rows = self.conn.execute(
            f"""
            SELECT
                t.category,
                t.theme,
                COUNT(DISTINCT t.symbol) AS member_count,
                COUNT(DISTINCT s.symbol) AS scored_count,
                AVG(s.total_score) AS average_score,
                SUM(CASE WHEN s.total_score > 0 THEN 1 ELSE 0 END) AS positive_count
            FROM stock_themes t
            LEFT JOIN scores s ON s.symbol = t.symbol AND s.score_run_id = ?
            {where_clause}
            GROUP BY t.category, t.theme
            HAVING COUNT(DISTINCT s.symbol) >= ?
            ORDER BY
                CASE WHEN AVG(s.total_score) IS NULL THEN 1 ELSE 0 END,
                AVG(s.total_score) DESC,
                COUNT(DISTINCT s.symbol) DESC,
                COUNT(DISTINCT t.symbol) DESC,
                t.theme
            LIMIT ?
            """,
            tuple(parameters),
        ).fetchall()
        items = []
        for row in rows:
            scored_count = int(row["scored_count"] or 0)
            positive_count = int(row["positive_count"] or 0)
            items.append(
                {
                    "category": str(row["category"]),
                    "theme": str(row["theme"]),
                    "member_count": int(row["member_count"] or 0),
                    "scored_count": scored_count,
                    "average_score": float(row["average_score"]) if row["average_score"] is not None else None,
                    "positive_rate": positive_count / scored_count * 100 if scored_count else None,
                    "coverage_rate": scored_count / int(row["member_count"] or 1) * 100,
                    **price_performance.get((str(row["category"]), str(row["theme"])), {}),
                }
            )
        return {
            "score_date": str(latest_run["score_date"]) if latest_run is not None else None,
            "price_as_of_date": price_as_of_date,
            "min_scored": max(1, min_scored),
            "items": items,
        }

    def _theme_price_performance(self, category: str = "") -> tuple[str | None, dict[tuple[str, str], dict[str, object]]]:
        cached = self._load_theme_price_cache(category)
        if cached is not None:
            return cached
        dates = self._recent_stock_trade_dates(21)
        if len(dates) < 21:
            return None, {}
        category_clause = "WHERE category = ?" if category else ""
        params: list[object] = []
        if category:
            params.append(category)
        params.extend(dates)
        params.extend((dates[0], dates[1], dates[5], dates[20]))
        date_placeholders = ", ".join("?" for _ in dates)
        rows = self.conn.execute(
            f"""
            WITH theme_members AS (
                SELECT DISTINCT symbol, category, theme
                FROM stock_themes
                {category_clause}
            ),
            theme_symbols AS (
                SELECT DISTINCT symbol FROM theme_members
            ),
            ranked AS (
                SELECT b.symbol,
                       b.trade_date,
                       b.close,
                       ROW_NUMBER() OVER (
                           PARTITION BY b.symbol, b.trade_date
                           ORDER BY CASE
                                        WHEN lower(b.source_file) LIKE 'tdx:%' THEN 0
                                        WHEN lower(b.source_file) LIKE 'tencent:%' THEN 2
                                        ELSE 1
                                    END,
                                    b.imported_at DESC,
                                    b.source_file ASC
                       ) AS source_rank
                FROM daily_bars b
                INNER JOIN theme_symbols symbols ON symbols.symbol = b.symbol
                WHERE b.trade_date IN ({date_placeholders})
                  AND b.close IS NOT NULL
            ),
            canonical AS (
                SELECT symbol, trade_date, close
                FROM ranked
                WHERE source_rank = 1
            ),
            price_snapshot AS (
                SELECT symbol,
                       MAX(CASE WHEN trade_date = ? THEN close END) AS close_now,
                       MAX(CASE WHEN trade_date = ? THEN close END) AS close_1d,
                       MAX(CASE WHEN trade_date = ? THEN close END) AS close_5d,
                       MAX(CASE WHEN trade_date = ? THEN close END) AS close_20d
                FROM canonical
                GROUP BY symbol
            )
            SELECT t.category,
                   t.theme,
                   COUNT(*) AS member_count,
                   COUNT(CASE WHEN p.close_now > 0 THEN 1 END) AS priced_count,
                   COUNT(CASE WHEN p.close_now > 0 AND p.close_1d > 0 THEN 1 END) AS return_1d_count,
                   COUNT(CASE WHEN p.close_now > 0 AND p.close_5d > 0 THEN 1 END) AS return_5d_count,
                   COUNT(CASE WHEN p.close_now > 0 AND p.close_20d > 0 THEN 1 END) AS return_20d_count,
                   AVG(CASE WHEN p.close_now > 0 AND p.close_1d > 0 THEN (p.close_now / p.close_1d - 1) * 100 END) AS return_1d,
                   AVG(CASE WHEN p.close_now > 0 AND p.close_5d > 0 THEN (p.close_now / p.close_5d - 1) * 100 END) AS return_5d,
                   AVG(CASE WHEN p.close_now > 0 AND p.close_20d > 0 THEN (p.close_now / p.close_20d - 1) * 100 END) AS return_20d
            FROM theme_members t
            LEFT JOIN price_snapshot p ON p.symbol = t.symbol
            GROUP BY t.category, t.theme
            """,
            tuple(params),
        ).fetchall()
        performance: dict[tuple[str, str], dict[str, object]] = {}
        for row in rows:
            member_count = int(row["member_count"] or 0)
            priced_count = int(row["priced_count"] or 0)
            performance[(str(row["category"]), str(row["theme"]))] = {
                "priced_count": priced_count,
                "price_coverage_rate": priced_count / member_count * 100 if member_count else None,
                "return_1d": float(row["return_1d"]) if row["return_1d"] is not None else None,
                "return_1d_count": int(row["return_1d_count"] or 0),
                "return_5d": float(row["return_5d"]) if row["return_5d"] is not None else None,
                "return_5d_count": int(row["return_5d_count"] or 0),
                "return_20d": float(row["return_20d"]) if row["return_20d"] is not None else None,
                "return_20d_count": int(row["return_20d_count"] or 0),
            }
        self._save_theme_price_cache(category, dates[0], performance)
        return dates[0], performance

    def _load_theme_price_cache(self, category: str) -> tuple[str | None, dict[tuple[str, str], dict[str, object]]] | None:
        row = self.conn.execute(
            "SELECT payload_json, source_fingerprint FROM theme_price_cache WHERE cache_key = ?",
            (self._theme_price_cache_key(category),),
        ).fetchone()
        if row is None or row["source_fingerprint"] != self._theme_price_fingerprint():
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return None
        performance: dict[tuple[str, str], dict[str, object]] = {}
        for item in payload["items"]:
            if not isinstance(item, dict):
                continue
            item_category = item.get("category")
            item_theme = item.get("theme")
            if isinstance(item_category, str) and isinstance(item_theme, str):
                performance[(item_category, item_theme)] = {
                    key: value for key, value in item.items() if key not in {"category", "theme"}
                }
        return payload.get("price_as_of_date") if isinstance(payload.get("price_as_of_date"), str) else None, performance

    def _save_theme_price_cache(
        self,
        category: str,
        price_as_of_date: str,
        performance: dict[tuple[str, str], dict[str, object]],
    ) -> None:
        items = [
            {"category": item_category, "theme": item_theme, **values}
            for (item_category, item_theme), values in performance.items()
        ]
        payload = {"price_as_of_date": price_as_of_date, "items": items}
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO theme_price_cache (cache_key, source_fingerprint, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    source_fingerprint = excluded.source_fingerprint,
                    payload_json = excluded.payload_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    self._theme_price_cache_key(category),
                    self._theme_price_fingerprint(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def _theme_price_cache_key(self, category: str) -> str:
        return f"category={category or 'all'}|horizons=1,5,20"

    def _theme_price_fingerprint(self) -> str:
        versions = {
            str(row["dataset"]): int(row["version"])
            for row in self.conn.execute(
                "SELECT dataset, version FROM data_versions WHERE dataset IN ('daily_bars', 'stock_themes')"
            ).fetchall()
        }
        return (
            f"daily_bars_v={versions.get('daily_bars', 0)}|stock_themes_v={versions.get('stock_themes', 0)}"
            f"|canonical_policy_v={CANONICAL_DAILY_BAR_POLICY_VERSION}|theme_price_v=1"
        )

    def _recent_stock_trade_dates(self, limit: int) -> list[str]:
        recent_rows = self.conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM daily_bars
            WHERE trade_date >= date((SELECT MAX(trade_date) FROM daily_bars), '-45 days')
              AND close IS NOT NULL
              AND (
                  symbol GLOB '60[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '68[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '00[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '30[0-9][0-9][0-9][0-9]'
              )
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        if len(recent_rows) >= limit:
            return [str(row["trade_date"]) for row in recent_rows]
        rows = self.conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM daily_bars
            WHERE close IS NOT NULL
              AND (
                  symbol GLOB '60[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '68[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '00[0-9][0-9][0-9][0-9]' OR
                  symbol GLOB '30[0-9][0-9][0-9][0-9]'
              )
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        return [str(row["trade_date"]) for row in rows]

    def delete_daily_bars_for_symbols(self, symbols: list[str]) -> int:
        selected = sorted({str(symbol) for symbol in symbols if symbol})
        if not selected:
            return 0
        placeholders = ",".join("?" for _ in selected)
        with self.conn:
            cursor = self.conn.execute(
                f"DELETE FROM daily_bars WHERE symbol IN ({placeholders})",
                tuple(selected),
            )
            if cursor.rowcount:
                self._bump_data_version("daily_bars")
        return int(cursor.rowcount or 0)

    def latest_snapshots(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, market, status, format_version, file_size, read_at, message, source_file
            FROM market_snapshots
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def latest_score_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, score_date, profile_name, created_at, profile_json
            FROM score_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def score_run_scores(self, score_run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT s.score_run_id, s.symbol, q.name, q.board, q.latest_price, q.pct_change,
                   q.amount, s.total_score, s.components_json, s.triggered_rules_json
            FROM scores s
            LEFT JOIN quotes_realtime q ON q.symbol = s.symbol AND q.latest_price IS NOT NULL
            WHERE s.score_run_id = ?
            ORDER BY s.total_score DESC, q.amount DESC
            """,
            (score_run_id,),
        ).fetchall()

    def compare_score_runs(
        self,
        base_run_id: int | None = None,
        target_run_id: int | None = None,
        limit: int = 50,
        min_score: float = 1.0,
    ) -> list[dict[str, object]]:
        base_run_id, target_run_id = self._resolve_score_run_pair(base_run_id, target_run_id)
        if base_run_id is None or target_run_id is None:
            return []

        base_rows = self.score_run_scores(base_run_id)
        target_rows = self.score_run_scores(target_run_id)
        base = {row["symbol"]: row for row in base_rows}
        target = {row["symbol"]: row for row in target_rows}
        base_rank = {row["symbol"]: index for index, row in enumerate(base_rows, start=1)}
        target_rank = {row["symbol"]: index for index, row in enumerate(target_rows, start=1)}

        comparisons: list[dict[str, object]] = []
        for symbol in sorted(set(base) | set(target)):
            before = base.get(symbol)
            after = target.get(symbol)
            base_score = float(before["total_score"]) if before is not None else None
            target_score = float(after["total_score"]) if after is not None else None
            if (base_score is None or base_score < min_score) and (target_score is None or target_score < min_score):
                continue
            if before is None:
                status = "new"
                delta = target_score or 0.0
            elif after is None:
                status = "dropped"
                delta = -(base_score or 0.0)
            else:
                delta = (target_score or 0.0) - (base_score or 0.0)
                status = "up" if delta > 0 else "down" if delta < 0 else "flat"
            source = after or before
            comparisons.append(
                {
                    "symbol": symbol,
                    "name": source["name"] if source is not None else None,
                    "board": source["board"] if source is not None else None,
                    "base_run_id": base_run_id,
                    "target_run_id": target_run_id,
                    "base_rank": base_rank.get(symbol),
                    "target_rank": target_rank.get(symbol),
                    "base_score": base_score,
                    "target_score": target_score,
                    "delta": delta,
                    "status": status,
                }
            )

        comparisons.sort(
            key=lambda item: (
                0 if item["status"] in {"new", "up"} else 1,
                -abs(float(item["delta"] or 0)),
                -(float(item["target_score"] or item["base_score"] or 0)),
                str(item["symbol"]),
            )
        )
        return comparisons[:limit]

    def _resolve_score_run_pair(
        self, base_run_id: int | None, target_run_id: int | None
    ) -> tuple[int | None, int | None]:
        if base_run_id is not None and target_run_id is not None:
            return base_run_id, target_run_id
        runs = self.latest_score_runs(2)
        if target_run_id is None and runs:
            target_run_id = int(runs[0]["id"])
        if base_run_id is None:
            for run in runs:
                run_id = int(run["id"])
                if run_id != target_run_id:
                    base_run_id = run_id
                    break
        return base_run_id, target_run_id

    def latest_scores(self, limit: int = 50, positive_only: bool = False) -> list[sqlite3.Row]:
        score_filter = "AND s.total_score > 0" if positive_only else ""
        return self.conn.execute(
            f"""
            SELECT s.score_date, s.profile_name, s.symbol, q.name, q.board, q.latest_price, q.pct_change,
                   q.amount, q.market_cap, q.turnover_rate, s.total_score,
                   s.components_json, s.triggered_rules_json
            FROM scores s
            LEFT JOIN quotes_realtime q ON q.symbol = s.symbol AND q.latest_price IS NOT NULL
            WHERE {_latest_score_filter_sql("s")}
            {score_filter}
            ORDER BY s.total_score DESC, q.amount DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def latest_score_quote_coverage(self) -> dict[str, object]:
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS score_count,
                   COALESCE(SUM(
                       CASE WHEN EXISTS (
                           SELECT 1
                           FROM quotes_realtime q
                           WHERE q.symbol = s.symbol
                             AND q.latest_price > 0
                       ) THEN 1 ELSE 0 END
                   ), 0) AS priced_score_count,
                   MAX(s.score_date) AS score_date
            FROM scores s
            WHERE {_latest_score_filter_sql("s")}
            """
        ).fetchone()
        return {
            "score_count": int(row["score_count"] or 0),
            "priced_score_count": int(row["priced_score_count"] or 0),
            "score_date": row["score_date"],
        }

    def latest_candidates(self, limit: int = 50, min_score: float = 1.0) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"""
            SELECT s.score_date, s.profile_name, s.symbol, q.name, q.market, q.board, q.latest_price, q.pct_change,
                   q.volume, q.amount, q.open, q.high, q.low, q.previous_close,
                   q.market_cap, q.float_market_cap, q.turnover_rate, q.observed_at,
                   s.total_score, s.components_json, s.triggered_rules_json
            FROM scores s
            JOIN quotes_realtime q ON q.symbol = s.symbol AND q.latest_price IS NOT NULL
            WHERE {_latest_score_filter_sql("s")}
              AND s.total_score >= ?
              AND q.latest_price > 0
              AND UPPER(COALESCE(q.name, '')) NOT LIKE '%ST%'
              AND UPPER(COALESCE(q.name, '')) NOT LIKE '%PT%'
              AND COALESCE(q.name, '') NOT LIKE '%退%'
            ORDER BY s.total_score DESC, q.amount DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()

    def score_explanation(self, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT s.score_date, s.profile_name, s.symbol, q.name, q.market, q.board, q.latest_price, q.pct_change,
                   q.volume, q.amount, q.open, q.high, q.low, q.previous_close,
                   q.market_cap, q.float_market_cap, q.turnover_rate, q.observed_at,
                   s.total_score, s.components_json, s.triggered_rules_json
            FROM scores s
            LEFT JOIN quotes_realtime q ON q.symbol = s.symbol AND q.latest_price IS NOT NULL
            WHERE s.symbol = ?
              AND (
                  s.score_run_id = (SELECT MAX(id) FROM score_runs)
                  OR (
                      s.score_run_id IS NULL
                      AND NOT EXISTS (SELECT 1 FROM score_runs)
                      AND s.score_date = (SELECT MAX(score_date) FROM scores WHERE symbol = ?)
                  )
              )
            ORDER BY q.observed_at DESC
            LIMIT 1
            """,
            (symbol, symbol),
        ).fetchone()

    def recent_daily_bars(self, symbol: str, limit: int = 10) -> list[sqlite3.Row]:
        return self._canonical_daily_bar_rows(symbol, limit=limit)

    def daily_bars_for_symbol(self, symbol: str, limit: int | None = None, end_date: str = "") -> list[sqlite3.Row]:
        return list(reversed(self._canonical_daily_bar_rows(symbol, limit=limit, end_date=end_date)))

    def _canonical_daily_bar_rows(
        self,
        symbol: str,
        limit: int | None = None,
        require_close: bool = False,
        end_date: str = "",
    ) -> list[sqlite3.Row]:
        limit_sql = "" if limit is None else "LIMIT ?"
        date_filter = "AND trade_date <= ?" if end_date else ""
        params_list: list[object] = [symbol]
        if end_date:
            params_list.append(end_date)
        if limit is not None:
            params_list.append(limit)
        close_filter = "AND close IS NOT NULL" if require_close else ""
        return self.conn.execute(
            f"""
            WITH ranked AS (
                SELECT trade_date,
                       open,
                       high,
                       low,
                       close,
                       volume,
                       ROW_NUMBER() OVER (
                           PARTITION BY trade_date
                           ORDER BY CASE
                                        WHEN lower(source_file) LIKE 'tdx:%' THEN 0
                                        WHEN lower(source_file) LIKE 'tencent:%' THEN 2
                                        ELSE 1
                                    END,
                                    imported_at DESC,
                                    source_file ASC
                       ) AS source_rank
                FROM daily_bars
                WHERE symbol = ? {date_filter} {close_filter}
            )
            SELECT trade_date, open, high, low, close, volume
            FROM ranked
            WHERE source_rank = 1
            ORDER BY trade_date DESC
            {limit_sql}
            """,
            tuple(params_list),
        ).fetchall()

    def symbols_with_daily_bars(self, limit: int | None = None) -> list[str]:
        limit_sql = "" if limit is None else "LIMIT ?"
        params: tuple[object, ...] = () if limit is None else (limit,)
        rows = self.conn.execute(
            f"""
            SELECT symbol, COUNT(DISTINCT trade_date) AS bars
            FROM daily_bars
            WHERE (
                symbol GLOB '60[0-9][0-9][0-9][0-9]' OR
                symbol GLOB '68[0-9][0-9][0-9][0-9]' OR
                symbol GLOB '00[0-9][0-9][0-9][0-9]' OR
                symbol GLOB '30[0-9][0-9][0-9][0-9]'
            )
            GROUP BY symbol
            HAVING bars >= 20
            ORDER BY bars DESC, symbol
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [row["symbol"] for row in rows]

    def available_benchmark_indices(self) -> list[dict[str, object]]:
        symbols = list(BENCHMARK_INDEX_LABELS)
        placeholders = ", ".join("?" for _ in symbols)
        rows = self.conn.execute(
            f"""
            SELECT symbol, MAX(trade_date) AS latest_trade_date
            FROM daily_bars
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
            ORDER BY symbol
            """,
            tuple(symbols),
        ).fetchall()
        return [
            {
                "symbol": str(row["symbol"]),
                "name": BENCHMARK_INDEX_LABELS[str(row["symbol"])],
                "latest_trade_date": str(row["latest_trade_date"]),
            }
            for row in rows
        ]

    def symbols_without_tdx_daily_bars(self, symbols: list[str]) -> list[str]:
        selected = list(dict.fromkeys(symbol.strip() for symbol in symbols if symbol.strip()))
        if not selected:
            return []
        tdx_symbols: set[str] = set()
        for offset in range(0, len(selected), 900):
            batch = selected[offset : offset + 900]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.conn.execute(
                f"""
                SELECT DISTINCT symbol
                FROM daily_bars
                WHERE lower(source_file) LIKE 'tdx:%'
                  AND symbol IN ({placeholders})
                """,
                tuple(batch),
            ).fetchall()
            tdx_symbols.update(str(row["symbol"]) for row in rows)
        return [symbol for symbol in selected if symbol not in tdx_symbols]

    def factor_scan(
        self,
        limit: int = 50,
        symbols: list[str] | None = None,
        use_cache: bool = False,
    ) -> list[dict[str, object]]:
        cache_symbols = symbols or None
        if use_cache:
            cached = self._load_factor_scan_cache(limit, cache_symbols)
            if cached is not None:
                return cached
        selected = cache_symbols or self.symbols_with_daily_bars(limit=max(limit * 4, 120))
        results: list[dict[str, object]] = []
        for symbol in selected:
            bars = self.daily_bars_for_symbol(symbol, limit=80)
            signals = evaluate_factors(bars)
            if bars:
                signal_date = str(bars[-1]["trade_date"])
                signals.extend(evaluate_disclosed_fundamental(self.disclosed_fundamental_as_of(symbol, signal_date), signal_date))
            if not signals:
                continue
            quote = self.latest_quote_for_symbol(symbol)
            for signal in signals:
                results.append(
                    {
                        "symbol": symbol,
                        "name": quote["name"] if quote is not None else self._security_name(symbol),
                        "factor_id": signal.factor_id,
                        "factor_name": signal.name,
                        "category": signal.category,
                        "direction": signal.direction,
                        "strength": signal.strength,
                        "reason": signal.reason,
                    }
                )
        results.extend(self._relative_strength_scan_rows(selected))
        rows = sorted(results, key=lambda row: (row["direction"] == "positive", float(row["strength"])), reverse=True)[:limit]
        if use_cache:
            self._save_factor_scan_cache(limit, cache_symbols, selected, rows)
        return rows

    def refresh_factor_scan_cache(self, limit: int = 50, symbols: list[str] | None = None) -> list[dict[str, object]]:
        cache_symbols = symbols or None
        with self.conn:
            self.conn.execute("DELETE FROM factor_scan_cache WHERE cache_key = ?", (self._factor_scan_cache_key(limit, cache_symbols),))
        return self.factor_scan(limit=limit, symbols=cache_symbols, use_cache=True)

    def factor_backtest(
        self,
        horizon_days: int = 5,
        min_bars: int = 40,
        limit_symbols: int | None = None,
        max_bars: int | None = None,
        end_date: str = "",
    ) -> list[dict[str, object]]:
        stats: dict[str, dict[str, object]] = {
            item.factor_id: {
                "factor_id": item.factor_id,
                "factor_name": item.name,
                "category": item.category,
                "direction": "",
                "samples": 0,
                "wins": 0,
                "total_return": 0.0,
                "best_return": None,
                "worst_return": None,
            }
            for item in factor_definitions()
        }
        for symbol in self.symbols_with_daily_bars(limit=limit_symbols):
            bars = self.daily_bars_for_symbol(symbol, limit=max_bars, end_date=end_date)
            fundamentals = self._disclosed_fundamentals_for_symbol(symbol)
            if len(bars) < max(min_bars, 20 + horizon_days):
                continue
            for index in range(19, len(bars) - horizon_days):
                window = bars[: index + 1]
                signal_date = str(window[-1]["trade_date"])
                close_now = window[-1]["close"]
                close_future = bars[index + horizon_days]["close"]
                if close_now in (None, 0) or close_future is None:
                    continue
                forward_return = (float(close_future) / float(close_now) - 1) * 100
                signals = evaluate_factors(window)
                signals.extend(evaluate_disclosed_fundamental(_disclosed_fundamental_at(fundamentals, signal_date), signal_date))
                for signal in signals:
                    row = stats[signal.factor_id]
                    row["direction"] = signal.direction
                    row["samples"] = int(row["samples"]) + 1
                    row["wins"] = int(row["wins"]) + (1 if forward_return > 0 else 0)
                    row["total_return"] = float(row["total_return"]) + forward_return
                    row["best_return"] = forward_return if row["best_return"] is None else max(float(row["best_return"]), forward_return)
                    row["worst_return"] = forward_return if row["worst_return"] is None else min(float(row["worst_return"]), forward_return)
        for sample in self._relative_strength_backtest_samples(horizon_days, min_bars, limit_symbols, max_bars, end_date=end_date):
            row = stats[sample["factor_id"]]
            forward_return = float(sample["forward_return"])
            row["direction"] = sample["direction"]
            row["samples"] = int(row["samples"]) + 1
            row["wins"] = int(row["wins"]) + (1 if forward_return > 0 else 0)
            row["total_return"] = float(row["total_return"]) + forward_return
            row["best_return"] = forward_return if row["best_return"] is None else max(float(row["best_return"]), forward_return)
            row["worst_return"] = forward_return if row["worst_return"] is None else min(float(row["worst_return"]), forward_return)
        rows = []
        for row in stats.values():
            samples = int(row["samples"])
            if samples == 0:
                continue
            rows.append(
                {
                    **row,
                    "horizon_days": horizon_days,
                    "win_rate": int(row["wins"]) / samples * 100,
                    "avg_return": float(row["total_return"]) / samples,
                }
            )
        return sorted(rows, key=lambda row: (float(row["avg_return"]), float(row["win_rate"]), int(row["samples"])), reverse=True)

    def _relative_strength_scan_rows(self, symbols: list[str]) -> list[dict[str, object]]:
        metrics: list[dict[str, object]] = []
        bars_by_symbol: dict[str, list[sqlite3.Row]] = {}
        for symbol in symbols:
            bars = self.daily_bars_for_symbol(symbol, limit=130)
            bars_by_symbol[symbol] = bars
            close = _close_at(bars, len(bars) - 1)
            ret60 = _window_return(bars, len(bars) - 1, 60)
            ret120 = _window_return(bars, len(bars) - 1, 120)
            if close is not None and (ret60 is not None or ret120 is not None):
                metrics.append({"symbol": symbol, "ret60": ret60, "ret120": ret120})
        rps60 = _percentile_rank({str(row["symbol"]): row["ret60"] for row in metrics if row["ret60"] is not None})
        rps120 = _percentile_rank({str(row["symbol"]): row["ret120"] for row in metrics if row["ret120"] is not None})
        rows: list[dict[str, object]] = []
        for item in metrics:
            symbol = str(item["symbol"])
            quote = self.latest_quote_for_symbol(symbol)
            name = quote["name"] if quote is not None else self._security_name(symbol)
            trade_date = bars_by_symbol[symbol][-1]["trade_date"] if bars_by_symbol[symbol] else ""
            score60 = rps60.get(symbol)
            score120 = rps120.get(symbol)
            ret60 = item["ret60"]
            ret120 = item["ret120"]
            if score60 is not None and score60 >= 85:
                rows.append(_relative_strength_row(symbol, name, "rps_60_strength", "RPS60 相对强势", "positive", score60, trade_date, 60, ret60))
            if score120 is not None and score120 >= 85:
                rows.append(_relative_strength_row(symbol, name, "rps_120_strength", "RPS120 长期强势", "positive", score120, trade_date, 120, ret120))
            if score60 is not None and score60 <= 15:
                rows.append(_relative_strength_row(symbol, name, "rps_60_weakness", "RPS60 相对弱势", "risk", 100 - score60, trade_date, 60, ret60))
        return rows

    def _relative_strength_backtest_samples(
        self,
        horizon_days: int,
        min_bars: int,
        limit_symbols: int | None,
        max_bars: int | None,
        end_date: str = "",
    ) -> list[dict[str, object]]:
        bars_by_symbol: dict[str, list[sqlite3.Row]] = {}
        for symbol in self.symbols_with_daily_bars(limit=limit_symbols):
            bars = self.daily_bars_for_symbol(symbol, limit=max_bars, end_date=end_date)
            if len(bars) >= max(min_bars, 120 + horizon_days):
                bars_by_symbol[symbol] = bars
        by_date: dict[str, list[dict[str, object]]] = {}
        for symbol, bars in bars_by_symbol.items():
            for index in range(120, len(bars) - horizon_days):
                ret60 = _window_return(bars, index, 60)
                ret120 = _window_return(bars, index, 120)
                close_now = _close_at(bars, index)
                close_future = _close_at(bars, index + horizon_days)
                if close_now in (None, 0) or close_future is None:
                    continue
                trade_date = str(bars[index]["trade_date"])
                by_date.setdefault(trade_date, []).append(
                    {
                        "symbol": symbol,
                        "ret60": ret60,
                        "ret120": ret120,
                        "forward_return": (float(close_future) / float(close_now) - 1) * 100,
                    }
                )
        samples: list[dict[str, object]] = []
        for items in by_date.values():
            rps60 = _percentile_rank({str(row["symbol"]): row["ret60"] for row in items if row["ret60"] is not None})
            rps120 = _percentile_rank({str(row["symbol"]): row["ret120"] for row in items if row["ret120"] is not None})
            for item in items:
                symbol = str(item["symbol"])
                score60 = rps60.get(symbol)
                score120 = rps120.get(symbol)
                if score60 is not None and score60 >= 85:
                    samples.append({"factor_id": "rps_60_strength", "direction": "positive", "forward_return": item["forward_return"]})
                if score120 is not None and score120 >= 85:
                    samples.append({"factor_id": "rps_120_strength", "direction": "positive", "forward_return": item["forward_return"]})
                if score60 is not None and score60 <= 15:
                    samples.append({"factor_id": "rps_60_weakness", "direction": "risk", "forward_return": item["forward_return"]})
        return samples

    def factor_backtest_matrix(
        self,
        horizons: list[int] | None = None,
        limit_symbols: int | None = None,
        max_bars: int | None = None,
        use_cache: bool = False,
        end_date: str = "",
    ) -> list[dict[str, object]]:
        selected_horizons = horizons or [3, 5, 10]
        if use_cache and not end_date:
            cached = self._load_factor_backtest_cache(selected_horizons, limit_symbols, max_bars)
            if cached is not None:
                return cached
        by_factor: dict[str, dict[str, object]] = {}
        definitions = {item.factor_id: item for item in factor_definitions()}
        for horizon in selected_horizons:
            for row in self.factor_backtest(horizon_days=horizon, limit_symbols=limit_symbols, max_bars=max_bars, end_date=end_date):
                factor_id = str(row["factor_id"])
                item = by_factor.setdefault(
                    factor_id,
                    {
                        "factor_id": factor_id,
                        "factor_name": row["factor_name"],
                        "category": row["category"],
                        "direction": row["direction"],
                        "source": definitions[factor_id].source if factor_id in definitions else "",
                        "horizons": {},
                    },
                )
                item["horizons"][int(row["horizon_days"])] = {
                    "samples": row["samples"],
                    "win_rate": row["win_rate"],
                    "avg_return": row["avg_return"],
                    "best_return": row["best_return"],
                    "worst_return": row["worst_return"],
                }
        rows = []
        for item in by_factor.values():
            effectiveness = _factor_effectiveness(item["direction"], item["horizons"])
            rows.append({**item, **effectiveness})
        rows = sorted(rows, key=lambda row: (float(row["effectiveness_score"]), int(row["total_samples"])), reverse=True)
        if use_cache and not end_date:
            self._save_factor_backtest_cache(selected_horizons, limit_symbols, max_bars, rows)
        return rows

    def refresh_factor_backtest_cache(
        self,
        horizons: list[int] | None = None,
        limit_symbols: int | None = None,
        max_bars: int | None = None,
    ) -> list[dict[str, object]]:
        selected_horizons = horizons or [3, 5, 10]
        cache_key = self._factor_backtest_cache_key(selected_horizons, limit_symbols, max_bars)
        with self.conn:
            self.conn.execute("DELETE FROM factor_backtest_cache WHERE cache_key = ?", (cache_key,))
        return self.factor_backtest_matrix(
            horizons=selected_horizons,
            limit_symbols=limit_symbols,
            max_bars=max_bars,
            use_cache=True,
        )

    def _load_factor_backtest_cache(
        self,
        horizons: list[int],
        limit_symbols: int | None,
        max_bars: int | None,
    ) -> list[dict[str, object]] | None:
        row = self.conn.execute(
            """
            SELECT rows_json, source_fingerprint
            FROM factor_backtest_cache
            WHERE cache_key = ?
            """,
            (self._factor_backtest_cache_key(horizons, limit_symbols, max_bars),),
        ).fetchone()
        if row is None or row["source_fingerprint"] != self._daily_bar_fingerprint():
            return None
        try:
            rows = json.loads(row["rows_json"])
        except json.JSONDecodeError:
            return None
        if not isinstance(rows, list):
            return None
        for item in rows:
            if isinstance(item, dict) and isinstance(item.get("horizons"), dict):
                item["horizons"] = {int(key): value for key, value in item["horizons"].items()}
        return rows

    def _save_factor_backtest_cache(
        self,
        horizons: list[int],
        limit_symbols: int | None,
        max_bars: int | None,
        rows: list[dict[str, object]],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO factor_backtest_cache
                    (cache_key, horizons_json, limit_symbols, max_bars, source_fingerprint, rows_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    horizons_json = excluded.horizons_json,
                    limit_symbols = excluded.limit_symbols,
                    max_bars = excluded.max_bars,
                    source_fingerprint = excluded.source_fingerprint,
                    rows_json = excluded.rows_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    self._factor_backtest_cache_key(horizons, limit_symbols, max_bars),
                    json.dumps(sorted(int(item) for item in horizons), ensure_ascii=False),
                    limit_symbols,
                    max_bars,
                    self._daily_bar_fingerprint(),
                    json.dumps(rows, ensure_ascii=False),
                ),
            )

    def _factor_backtest_cache_key(self, horizons: list[int], limit_symbols: int | None, max_bars: int | None) -> str:
        normalized_horizons = ",".join(str(item) for item in sorted(int(item) for item in horizons))
        return f"h={normalized_horizons}|limit={limit_symbols or 'all'}|bars={max_bars or 'all'}"

    def _load_factor_scan_cache(self, limit: int, symbols: list[str] | None) -> list[dict[str, object]] | None:
        row = self.conn.execute(
            """
            SELECT rows_json, source_fingerprint
            FROM factor_scan_cache
            WHERE cache_key = ?
            """,
            (self._factor_scan_cache_key(limit, symbols),),
        ).fetchone()
        if row is None or row["source_fingerprint"] != self._daily_bar_fingerprint():
            return None
        try:
            rows = json.loads(row["rows_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return rows if isinstance(rows, list) else None

    def _save_factor_scan_cache(
        self,
        limit: int,
        cache_symbols: list[str] | None,
        selected_symbols: list[str],
        rows: list[dict[str, object]],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO factor_scan_cache
                    (cache_key, limit_value, symbols_json, source_fingerprint, rows_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    limit_value = excluded.limit_value,
                    symbols_json = excluded.symbols_json,
                    source_fingerprint = excluded.source_fingerprint,
                    rows_json = excluded.rows_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    self._factor_scan_cache_key(limit, cache_symbols),
                    limit,
                    json.dumps(selected_symbols, ensure_ascii=False),
                    self._daily_bar_fingerprint(),
                    json.dumps(rows, ensure_ascii=False),
                ),
            )

    def _factor_scan_cache_key(self, limit: int, symbols: list[str] | None) -> str:
        if symbols is None:
            return f"limit={limit}|symbols=auto"
        serialized_symbols = json.dumps(symbols, ensure_ascii=False, separators=(",", ":"))
        symbols_digest = hashlib.sha256(serialized_symbols.encode("utf-8")).hexdigest()
        return f"limit={limit}|symbols={symbols_digest}"

    def _daily_bar_fingerprint(self) -> str:
        row = self.conn.execute("SELECT version FROM data_versions WHERE dataset = 'daily_bars'").fetchone()
        version = int(row["version"]) if row is not None else 0
        fundamentals_row = self.conn.execute("SELECT version FROM data_versions WHERE dataset = 'fundamentals'").fetchone()
        fundamentals_version = int(fundamentals_row["version"]) if fundamentals_row is not None else 0
        return (
            f"daily_bars_v={version}|canonical_policy_v={CANONICAL_DAILY_BAR_POLICY_VERSION}"
            f"|fundamentals_v={fundamentals_version}|factor_engine_v={FACTOR_ENGINE_VERSION}"
        )

    def _bump_data_version(self, dataset: str) -> None:
        self.conn.execute(
            """
            INSERT INTO data_versions (dataset, version)
            VALUES (?, 1)
            ON CONFLICT(dataset) DO UPDATE SET
                version = data_versions.version + 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (dataset,),
        )

    def strategy_backtest(
        self,
        horizon_days: int = 5,
        top_n: int = 10,
        min_signal_score: float = 60.0,
        limit_symbols: int | None = None,
        cost_bps: float = 0.0,
        slippage_bps: float = 0.0,
        benchmark_symbol: str | None = None,
        max_bars: int | None = None,
        execution_mode: str = "next_open",
        position_mode: str = "non_overlapping",
        factor_training_end_date: str = "",
        signal_start_date: str = "",
        signal_end_date: str = "",
    ) -> dict[str, object]:
        selected_execution_mode = _strategy_execution_mode(execution_mode)
        selected_position_mode = _strategy_position_mode(position_mode)
        round_trip_cost_pct = max(0.0, (cost_bps + slippage_bps) * 2 / 100)
        candidates_by_date: dict[str, list[dict[str, object]]] = {}
        relative_strength_signals = self._relative_strength_signal_map(limit_symbols=limit_symbols, max_bars=max_bars)
        factor_quality = {
            str(row["factor_id"]): row
            for row in self.factor_backtest_matrix(
                horizons=[horizon_days],
                limit_symbols=limit_symbols,
                max_bars=max_bars,
                use_cache=True,
                end_date=factor_training_end_date,
            )
        }
        benchmark_code = (benchmark_symbol or "").strip()
        skipped_locked_entries = 0
        skipped_locked_exits = 0
        for symbol in self.symbols_with_daily_bars(limit=limit_symbols):
            if benchmark_code and symbol == benchmark_code:
                continue
            bars = self.daily_bars_for_symbol(symbol, limit=max_bars)
            fundamentals = self._disclosed_fundamentals_for_symbol(symbol)
            if len(bars) < 20 + horizon_days:
                continue
            for index in range(19, len(bars) - horizon_days):
                window = bars[: index + 1]
                signal_date = str(window[-1]["trade_date"])
                if signal_start_date and signal_date < signal_start_date:
                    continue
                if signal_end_date and signal_date > signal_end_date:
                    continue
                signal_close = window[-1]["close"]
                entry_index = index + 1 if selected_execution_mode == "next_open" else index
                exit_index = index + horizon_days
                entry_price = bars[entry_index]["open"] if selected_execution_mode == "next_open" else signal_close
                exit_price = bars[exit_index]["close"]
                entry_volume = bars[entry_index]["volume"]
                exit_volume = bars[exit_index]["volume"]
                if (
                    entry_price in (None, 0)
                    or exit_price is None
                    or entry_volume is None
                    or exit_volume is None
                    or float(entry_volume) <= 0
                    or float(exit_volume) <= 0
                ):
                    continue
                if _locked_limit_direction(symbol, bars, entry_index) is not None:
                    skipped_locked_entries += 1
                    continue
                if _locked_limit_direction(symbol, bars, exit_index) is not None:
                    skipped_locked_exits += 1
                    continue
                entry_date = str(bars[entry_index]["trade_date"])
                signals = evaluate_factors(window)
                signals.extend(evaluate_disclosed_fundamental(_disclosed_fundamental_at(fundamentals, signal_date), signal_date))
                signals.extend(relative_strength_signals.get((signal_date, symbol), []))
                positive = [item for item in signals if item.direction == "positive"]
                risk = [item for item in signals if item.direction == "risk"]
                if not positive:
                    continue
                signal_score = 0.0
                factor_names = []
                for signal in positive:
                    quality = factor_quality.get(signal.factor_id, {})
                    effectiveness = float(quality.get("effectiveness_score") or 0.0)
                    verdict = str(quality.get("verdict") or "")
                    verdict_adjustment = {"有效": 12.0, "观察": 4.0, "反向": -18.0, "样本不足": -6.0}.get(verdict, 0.0)
                    signal_score += _strategy_signal_strength(signal) + effectiveness + verdict_adjustment
                    factor_names.append(signal.name)
                for signal in risk:
                    quality = factor_quality.get(signal.factor_id, {})
                    effectiveness = abs(float(quality.get("effectiveness_score") or 0.0))
                    signal_score -= min(35.0, _strategy_signal_strength(signal) * 0.35 + effectiveness)
                    factor_names.append(signal.name)
                signal_score = signal_score / max(len(positive), 1)
                if signal_score < min_signal_score:
                    continue
                gross_return = (float(exit_price) / float(entry_price) - 1) * 100
                net_return = gross_return - round_trip_cost_pct
                quote = self.latest_quote_for_symbol(symbol)
                candidates_by_date.setdefault(signal_date, []).append(
                    {
                        "signal_date": signal_date,
                        "trade_date": entry_date,
                        "entry_date": entry_date,
                        "exit_date": bars[exit_index]["trade_date"],
                        "symbol": symbol,
                        "name": quote["name"] if quote is not None else self._security_name(symbol),
                        "entry_price": float(entry_price),
                        "exit_price": float(exit_price),
                        "entry_close": float(signal_close) if signal_close is not None else None,
                        "exit_close": float(exit_price),
                        "gross_return_pct": gross_return,
                        "return_pct": net_return,
                        "cost_pct": round_trip_cost_pct,
                        "signal_score": signal_score,
                        "factors": ", ".join(factor_names[:4]),
                    }
                )
        trades = []
        next_available_signal_date = ""
        for trade_date, rows in sorted(candidates_by_date.items()):
            if selected_position_mode == "non_overlapping" and next_available_signal_date and trade_date < next_available_signal_date:
                continue
            selected = sorted(rows, key=lambda row: float(row["signal_score"]), reverse=True)[:top_n]
            if not selected:
                continue
            trades.extend(selected)
            if selected_position_mode == "non_overlapping":
                next_available_signal_date = max(str(row["exit_date"]) for row in selected)
        return _strategy_summary(
            trades,
            horizon_days=horizon_days,
            top_n=top_n,
            min_signal_score=min_signal_score,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
            execution_mode=selected_execution_mode,
            position_mode=selected_position_mode,
            skipped_locked_entries=skipped_locked_entries,
            skipped_locked_exits=skipped_locked_exits,
            benchmark=self._benchmark_backtest(
                benchmark_symbol,
                sorted({str(row["signal_date"]) for row in trades}),
                horizon_days,
                max_bars=max_bars,
                execution_mode=selected_execution_mode,
            ),
        )

    def strategy_walk_forward(
        self,
        train_days: int = 252,
        test_days: int = 63,
        max_folds: int | None = None,
        **strategy_options: object,
    ) -> dict[str, object]:
        if train_days < 20 or test_days < 1:
            raise ValueError("train_days must be at least 20 and test_days must be positive")
        rows = self.conn.execute("SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date").fetchall()
        dates = [str(row["trade_date"]) for row in rows]
        folds = []
        cursor = train_days
        while cursor < len(dates) and (max_folds is None or len(folds) < max_folds):
            test_end_index = min(cursor + test_days - 1, len(dates) - 1)
            if test_end_index < cursor:
                break
            result = self.strategy_backtest(
                **strategy_options,
                factor_training_end_date=dates[cursor - 1],
                signal_start_date=dates[cursor],
                signal_end_date=dates[test_end_index],
            )
            folds.append(
                {
                    "fold": len(folds) + 1,
                    "train_start_date": dates[max(0, cursor - train_days)],
                    "train_end_date": dates[cursor - 1],
                    "test_start_date": dates[cursor],
                    "test_end_date": dates[test_end_index],
                    "trade_count": result["trade_count"],
                    "test_days": result["day_count"],
                    "avg_return": result["avg_return"],
                    "portfolio_avg_return": result["portfolio_avg_return"],
                    "max_drawdown": result["max_drawdown"],
                    "benchmark": result["benchmark"],
                }
            )
            cursor += test_days
        return {
            "validation_mode": "walk_forward",
            "train_days": train_days,
            "test_days": test_days,
            "folds": folds,
            "total_test_days": sum(int(row["test_days"]) for row in folds),
            "total_trades": sum(int(row["trade_count"]) for row in folds),
        }

    def validate_strategy_walk_forward(
        self,
        train_days: int = 252,
        test_days: int = 63,
        max_folds: int | None = None,
        min_folds: int = 3,
        min_trades: int = 60,
        min_positive_fold_ratio: float = 0.6,
        max_drawdown: float = -20.0,
        min_benchmark_excess_return: float = 0.0,
        **strategy_options: object,
    ) -> dict[str, object]:
        result = self.strategy_walk_forward(
            train_days=train_days,
            test_days=test_days,
            max_folds=max_folds,
            **strategy_options,
        )
        assessment = assess_strategy_walk_forward(
            result,
            min_folds=min_folds,
            min_trades=min_trades,
            min_positive_fold_ratio=min_positive_fold_ratio,
            max_drawdown=max_drawdown,
            min_benchmark_excess_return=min_benchmark_excess_return,
        )
        return {**result, "assessment": assessment}

    def save_strategy_validation_run(
        self,
        parameters: dict[str, object],
        result: dict[str, object],
        assessment: dict[str, object],
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO strategy_validation_runs
                    (parameters_json, data_fingerprint, verdict, summary, assessment_json, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                    self._daily_bar_fingerprint(),
                    str(assessment.get("verdict") or "样本不足"),
                    str(assessment.get("summary") or "未生成验证结论。"),
                    json.dumps(assessment, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                ),
            )
        return int(cursor.lastrowid)

    def strategy_validation_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, run_at, data_fingerprint, verdict, summary, parameters_json, assessment_json
            FROM strategy_validation_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()

    def save_strategy_backtest_run(self, parameters: dict[str, object], result: dict[str, object]) -> int:
        summary = _strategy_backtest_run_summary(result)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO strategy_backtest_runs
                    (parameters_json, data_fingerprint, summary_json, result_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                    self._daily_bar_fingerprint(),
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                ),
            )
        return int(cursor.lastrowid)

    def strategy_backtest_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, run_at, data_fingerprint, parameters_json, summary_json
            FROM strategy_backtest_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()

    def strategy_backtest_run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT id, run_at, data_fingerprint, parameters_json, summary_json, result_json
            FROM strategy_backtest_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()

    def start_daily_run(self, parameters: dict[str, object]) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO daily_runs (status, parameters_json)
                VALUES ('running', ?)
                """,
                (json.dumps(parameters, ensure_ascii=False, sort_keys=True),),
            )
        return int(cursor.lastrowid)

    def finish_daily_run(
        self,
        run_id: int,
        status: str,
        summary: dict[str, object] | None = None,
        error_text: str = "",
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("daily run status must be succeeded or failed")
        with self.conn:
            self.conn.execute(
                """
                UPDATE daily_runs
                SET finished_at = CURRENT_TIMESTAMP,
                    status = ?,
                    summary_json = ?,
                    error_text = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(summary or {}, ensure_ascii=False, sort_keys=True),
                    error_text,
                    run_id,
                ),
            )

    def daily_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, started_at, finished_at, status, parameters_json, summary_json, error_text
            FROM daily_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()

    def _relative_strength_signal_map(
        self,
        limit_symbols: int | None,
        max_bars: int | None,
    ) -> dict[tuple[str, str], list[FactorSignal]]:
        bars_by_symbol: dict[str, list[sqlite3.Row]] = {}
        for symbol in self.symbols_with_daily_bars(limit=limit_symbols):
            bars = self.daily_bars_for_symbol(symbol, limit=max_bars)
            if len(bars) >= 121:
                bars_by_symbol[symbol] = bars
        by_date: dict[str, list[dict[str, object]]] = {}
        for symbol, bars in bars_by_symbol.items():
            for index in range(120, len(bars)):
                ret60 = _window_return(bars, index, 60)
                ret120 = _window_return(bars, index, 120)
                if ret60 is None and ret120 is None:
                    continue
                trade_date = str(bars[index]["trade_date"])
                by_date.setdefault(trade_date, []).append({"symbol": symbol, "ret60": ret60, "ret120": ret120})
        result: dict[tuple[str, str], list[FactorSignal]] = {}
        for trade_date, items in by_date.items():
            rps60 = _percentile_rank({str(row["symbol"]): row["ret60"] for row in items if row["ret60"] is not None})
            rps120 = _percentile_rank({str(row["symbol"]): row["ret120"] for row in items if row["ret120"] is not None})
            for item in items:
                symbol = str(item["symbol"])
                signals: list[FactorSignal] = []
                score60 = rps60.get(symbol)
                score120 = rps120.get(symbol)
                if score60 is not None and score60 >= 85:
                    signals.append(
                        FactorSignal(
                            "rps_60_strength",
                            "RPS60 相对强势",
                            "相对强弱",
                            score60,
                            "positive",
                            f"{trade_date} 近 60 日涨幅 {float(item['ret60']):.2f}%，RPS={score60:.1f}。",
                        )
                    )
                if score120 is not None and score120 >= 85:
                    signals.append(
                        FactorSignal(
                            "rps_120_strength",
                            "RPS120 长期强势",
                            "相对强弱",
                            score120,
                            "positive",
                            f"{trade_date} 近 120 日涨幅 {float(item['ret120']):.2f}%，RPS={score120:.1f}。",
                        )
                    )
                if score60 is not None and score60 <= 15:
                    signals.append(
                        FactorSignal(
                            "rps_60_weakness",
                            "RPS60 相对弱势",
                            "风险",
                            100 - score60,
                            "risk",
                            f"{trade_date} 近 60 日涨幅 {float(item['ret60']):.2f}%，RPS 位于后 15%。",
                        )
                    )
                if signals:
                    result[(trade_date, symbol)] = signals
        return result

    def _benchmark_backtest(
        self,
        benchmark_symbol: str | None,
        signal_dates: list[str],
        horizon_days: int,
        max_bars: int | None = None,
        execution_mode: str = "next_open",
    ) -> dict[str, object] | None:
        symbol = (benchmark_symbol or "").strip()
        if not symbol or not signal_dates:
            return None
        selected_execution_mode = _strategy_execution_mode(execution_mode)
        bars = self.daily_bars_for_symbol(symbol, limit=max_bars)
        if len(bars) <= horizon_days:
            return {
                "symbol": symbol,
                "sample_count": 0,
                "avg_return": 0.0,
                "cumulative_return": 0.0,
                "max_drawdown": 0.0,
                "daily_returns": [],
            }
        by_date = {str(row["trade_date"]): index for index, row in enumerate(bars)}
        daily_returns = []
        for signal_date in signal_dates:
            index = by_date.get(signal_date)
            if index is None or index + horizon_days >= len(bars):
                continue
            entry_index = index + 1 if selected_execution_mode == "next_open" else index
            exit_index = index + horizon_days
            entry = bars[entry_index]["open"] if selected_execution_mode == "next_open" else bars[index]["close"]
            exit_price = bars[exit_index]["close"]
            entry_volume = bars[entry_index]["volume"]
            exit_volume = bars[exit_index]["volume"]
            if (
                entry in (None, 0)
                or exit_price is None
                or entry_volume is None
                or exit_volume is None
                or float(entry_volume) <= 0
                or float(exit_volume) <= 0
            ):
                continue
            if _locked_limit_direction(symbol, bars, entry_index) is not None:
                continue
            if _locked_limit_direction(symbol, bars, exit_index) is not None:
                continue
            daily_returns.append(
                {
                    "signal_date": signal_date,
                    "trade_date": bars[entry_index]["trade_date"],
                    "exit_date": bars[exit_index]["trade_date"],
                    "return_pct": (float(exit_price) / float(entry) - 1) * 100,
                }
            )
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for row in daily_returns:
            equity *= 1 + float(row["return_pct"]) / 100
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, (equity / peak - 1) * 100 if peak else 0.0)
        return {
            "symbol": symbol,
            "sample_count": len(daily_returns),
            "avg_return": sum(float(row["return_pct"]) for row in daily_returns) / len(daily_returns) if daily_returns else 0.0,
            "cumulative_return": (equity - 1) * 100,
            "max_drawdown": max_drawdown,
            "daily_returns": daily_returns,
        }

    def latest_quote_for_symbol(self, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT symbol, name, latest_price, pct_change, amount, turnover_rate, board, observed_at
            FROM quotes_realtime
            WHERE symbol = ? AND latest_price IS NOT NULL
            ORDER BY observed_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

    def stock_note(self, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT symbol, status, tags, note, updated_at
            FROM stock_notes
            WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()

    def upsert_stock_note(self, symbol: str, status: str, tags: str, note: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stock_notes (symbol, status, tags, note, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                    status = excluded.status,
                    tags = excluded.tags,
                    note = excluded.note,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (symbol, status, tags, note),
            )

    def list_stock_notes(
        self,
        limit: int = 100,
        status: str | None = None,
        query: str = "",
        sort: str = "updated",
    ) -> list[sqlite3.Row]:
        conditions = []
        params: list[object] = []
        if status:
            conditions.append("n.status = ?")
            params.append(status)
        if query:
            pattern = f"%{query}%"
            conditions.append(
                "(n.symbol LIKE ? OR COALESCE(q.name, '') LIKE ? OR n.tags LIKE ? OR n.note LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        order_by = {
            "updated": "n.updated_at DESC, n.symbol",
            "score": "COALESCE(s.total_score, -999999) DESC, n.updated_at DESC",
            "pct": "COALESCE(q.pct_change, -999999) DESC, n.updated_at DESC",
            "price": "COALESCE(q.latest_price, -999999) DESC, n.updated_at DESC",
            "symbol": "n.symbol ASC",
        }.get(sort, "n.updated_at DESC, n.symbol")
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT n.symbol, n.status, n.tags, n.note, n.updated_at,
                   q.name, q.board, q.latest_price, q.pct_change,
                   q.observed_at,
                   s.total_score
            FROM stock_notes n
            LEFT JOIN quotes_realtime q ON q.symbol = n.symbol AND q.latest_price IS NOT NULL
            LEFT JOIN scores s ON s.symbol = n.symbol AND {_latest_score_filter_sql("s")}
            {where}
            ORDER BY {order_by}
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    def delete_stock_note(self, symbol: str) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM stock_notes WHERE symbol = ?", (symbol,))
        return cursor.rowcount > 0

    def insert_ai_decisions(self, rows: list[dict[str, object]], replace_same_signal: bool = False) -> int:
        signal_keys = {
            (str(row["symbol"]), _score_date_from_thesis(row.get("thesis_json")))
            for row in rows
            if _score_date_from_thesis(row.get("thesis_json"))
        }
        with self.conn:
            if replace_same_signal and signal_keys:
                symbols = sorted({symbol for symbol, _ in signal_keys})
                placeholders = ", ".join("?" for _ in symbols)
                existing = self.conn.execute(
                    f"""
                    SELECT id, symbol, thesis_json
                    FROM ai_decisions
                    WHERE symbol IN ({placeholders})
                    """,
                    tuple(symbols),
                ).fetchall()
                stale_ids = [
                    int(item["id"])
                    for item in existing
                    if (str(item["symbol"]), _score_date_from_thesis(item["thesis_json"])) in signal_keys
                ]
                if stale_ids:
                    stale_placeholders = ", ".join("?" for _ in stale_ids)
                    self.conn.execute(f"DELETE FROM ai_decisions WHERE id IN ({stale_placeholders})", tuple(stale_ids))
            self.conn.executemany(
                """
                INSERT INTO ai_decisions
                    (symbol, name, decision, confidence, summary, thesis_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["symbol"],
                        row["name"],
                        row["decision"],
                        row["confidence"],
                        row["summary"],
                        row["thesis_json"],
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def latest_ai_decisions(self, limit: int = 50, symbol: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE symbol = ?" if symbol else ""
        params: tuple[object, ...] = (symbol, limit) if symbol else (limit,)
        return self.conn.execute(
            f"""
            SELECT id, run_at, symbol, name, decision, confidence, summary, thesis_json
            FROM ai_decisions
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def upsert_news_items(self, items: list[NewsItem]) -> int:
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO news_items
                    (news_id, title, summary, source, event_time, importance, tags, source_file, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(news_id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    source = excluded.source,
                    event_time = excluded.event_time,
                    importance = excluded.importance,
                    tags = excluded.tags,
                    source_file = excluded.source_file,
                    imported_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        item.news_id,
                        item.title,
                        item.summary,
                        item.source,
                        item.event_time,
                        item.importance,
                        item.tags,
                        str(item.source_file),
                    )
                    for item in items
                ],
            )
        return len(items)

    def latest_news(self, limit: int = 50, query: str = "", tag: str = "") -> list[sqlite3.Row]:
        conditions = []
        params: list[object] = []
        if query:
            pattern = f"%{query}%"
            conditions.append("(title LIKE ? OR summary LIKE ? OR source LIKE ?)")
            params.extend([pattern, pattern, pattern])
        if tag:
            conditions.append("tags LIKE ?")
            params.append(f"%{tag}%")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT news_id, title, summary, source, event_time, importance, tags, source_file, imported_at
            FROM news_items
            {where}
            ORDER BY COALESCE(event_time, imported_at) DESC, news_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    def reclassify_news_tags(self, classifier: Callable[[str, str], list[str]]) -> int:
        rows = self.conn.execute("SELECT news_id, title, summary, tags FROM news_items").fetchall()
        updates = []
        for row in rows:
            tags = ",".join(classifier(str(row["title"] or ""), str(row["summary"] or "")))
            if tags != str(row["tags"] or ""):
                updates.append((tags, row["news_id"]))
        if not updates:
            return 0
        with self.conn:
            self.conn.executemany("UPDATE news_items SET tags = ? WHERE news_id = ?", updates)
        return len(updates)

    def related_news_for_symbol(self, symbol: str, name: str | None = None, limit: int = 5) -> list[sqlite3.Row]:
        security_name = name or self._security_name(symbol) or ""
        tokens = [symbol]
        if security_name:
            tokens.append(security_name)
            for suffix in ("股份", "集团", "科技", "控股", "有限"):
                short_name = security_name.replace(suffix, "")
                if short_name and short_name != security_name:
                    tokens.append(short_name)
        conditions = []
        params: list[object] = []
        for token in dict.fromkeys(item for item in tokens if item):
            pattern = f"%{token}%"
            conditions.append("(title LIKE ? OR summary LIKE ?)")
            params.extend([pattern, pattern])
        if not conditions:
            return []
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT news_id, title, summary, source, event_time, importance, tags, source_file, imported_at
            FROM news_items
            WHERE {" OR ".join(conditions)}
            ORDER BY COALESCE(event_time, imported_at) DESC, news_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    def _security_name(self, symbol: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT name
            FROM securities
            WHERE symbol = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        return row["name"] if row is not None else None

    def ai_decision_changes(self, limit: int = 50) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT id, run_at, symbol, name, decision, confidence, summary, thesis_json
            FROM ai_decisions
            ORDER BY symbol, id DESC
            """
        ).fetchall()
        by_symbol: dict[str, list[sqlite3.Row]] = {}
        seen_signal_keys: set[tuple[str, str]] = set()
        for row in rows:
            score_date = _score_date_from_thesis(row["thesis_json"])
            signal_key = (str(row["symbol"]), score_date)
            if score_date and signal_key in seen_signal_keys:
                continue
            if score_date:
                seen_signal_keys.add(signal_key)
            by_symbol.setdefault(row["symbol"], []).append(row)

        changes: list[dict[str, object]] = []
        for symbol, items in by_symbol.items():
            latest = items[0]
            previous = items[1] if len(items) > 1 else None
            if previous is None:
                status = "new"
                confidence_delta = float(latest["confidence"] or 0)
            else:
                confidence_delta = float(latest["confidence"] or 0) - float(previous["confidence"] or 0)
                if latest["decision"] != previous["decision"]:
                    status = "changed"
                elif abs(confidence_delta) >= 5:
                    status = "confidence"
                else:
                    status = "stable"
            changes.append(
                {
                    "symbol": symbol,
                    "name": latest["name"],
                    "latest_id": latest["id"],
                    "previous_id": previous["id"] if previous is not None else None,
                    "latest_run_at": latest["run_at"],
                    "previous_run_at": previous["run_at"] if previous is not None else None,
                    "latest_decision": latest["decision"],
                    "previous_decision": previous["decision"] if previous is not None else None,
                    "latest_confidence": latest["confidence"],
                    "previous_confidence": previous["confidence"] if previous is not None else None,
                    "confidence_delta": confidence_delta,
                    "status": status,
                    "summary": latest["summary"],
                }
            )
        changes.sort(
            key=lambda item: (
                {"changed": 0, "new": 1, "confidence": 2, "stable": 3}.get(str(item["status"]), 9),
                -abs(float(item["confidence_delta"] or 0)),
                -int(item["latest_id"]),
            )
        )
        return changes[:limit]

    def ai_decision_outcomes(
        self,
        limit: int = 50,
        horizon_days: int = 5,
        symbol: str | None = None,
    ) -> list[dict[str, object]]:
        selected_horizon = max(1, min(horizon_days, 60))
        selected_limit = max(1, limit)
        scan_limit = max(100, min(selected_limit * 20, 2_000))
        rows = self.latest_ai_decisions(limit=scan_limit, symbol=symbol)
        outcomes: list[dict[str, object]] = []
        seen_signal_keys: set[tuple[str, str]] = set()
        for row in rows:
            score_date = _score_date_from_thesis(row["thesis_json"])
            signal_key = (str(row["symbol"]), score_date)
            if score_date and signal_key in seen_signal_keys:
                continue
            if score_date:
                seen_signal_keys.add(signal_key)
            outcome: dict[str, object] = {
                "id": int(row["id"]),
                "run_at": row["run_at"],
                "symbol": row["symbol"],
                "name": row["name"],
                "decision": row["decision"],
                "confidence": row["confidence"],
                "score_date": score_date,
                "horizon_days": selected_horizon,
                "entry_date": None,
                "entry_price": None,
                "exit_date": None,
                "exit_price": None,
                "return_pct": None,
                "available_days": 0,
                "status": "unavailable",
                "status_label": "缺少评分日期",
            }
            if not score_date:
                outcomes.append(outcome)
                if len(outcomes) >= selected_limit:
                    break
                continue

            bars = self.daily_bars_for_symbol(str(row["symbol"]))
            future_bars = [bar for bar in bars if str(bar["trade_date"]) > score_date]
            outcome["available_days"] = len(future_bars)
            entry_index = next(
                (
                    index
                    for index, bar in enumerate(future_bars)
                    if bar["open"] is not None and float(bar["open"]) > 0
                ),
                None,
            )
            if entry_index is None:
                outcome.update({"status": "pending", "status_label": "待下一交易日"})
                outcomes.append(outcome)
                if len(outcomes) >= selected_limit:
                    break
                continue

            entry_bar = future_bars[entry_index]
            outcome.update(
                {
                    "entry_date": entry_bar["trade_date"],
                    "entry_price": float(entry_bar["open"]),
                }
            )
            exit_index = entry_index + selected_horizon - 1
            if exit_index >= len(future_bars):
                outcome.update({"status": "pending", "status_label": "待观察"})
                outcomes.append(outcome)
                if len(outcomes) >= selected_limit:
                    break
                continue

            exit_bar = future_bars[exit_index]
            exit_price = exit_bar["close"]
            if exit_price is None or float(exit_price) <= 0:
                outcome.update({"status": "pending", "status_label": "待有效收盘"})
                outcomes.append(outcome)
                if len(outcomes) >= selected_limit:
                    break
                continue

            entry_price = float(entry_bar["open"])
            exit_price_value = float(exit_price)
            outcome.update(
                {
                    "exit_date": exit_bar["trade_date"],
                    "exit_price": exit_price_value,
                    "return_pct": (exit_price_value / entry_price - 1) * 100,
                    "status": "evaluated",
                    "status_label": "已完成",
                }
            )
            outcomes.append(outcome)
            if len(outcomes) >= selected_limit:
                break
        return outcomes

    def write_candidates_csv(self, output_path: Path, limit: int = 50, min_score: float = 1.0) -> int:
        rows = self.latest_candidates(limit=limit, min_score=min_score)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "score_date",
            "symbol",
            "name",
            "market",
            "board",
            "total_score",
            "latest_price",
            "pct_change",
            "amount",
            "market_cap",
            "turnover_rate",
            "volume",
            "open",
            "high",
            "low",
            "observed_at",
            "news_count",
            "latest_news_time",
            "latest_news_title",
            "latest_news_tags",
            "latest_news_source",
            "rules",
            "components",
        ]
        with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in rows:
                news_summary = _news_export_summary(
                    self.related_news_for_symbol(str(row["symbol"]), name=row["name"], limit=3)
                )
                writer.writerow(
                    [
                        row["score_date"],
                        row["symbol"],
                        row["name"],
                        row["market"],
                        row["board"],
                        row["total_score"],
                        row["latest_price"],
                        row["pct_change"],
                        row["amount"],
                        row["market_cap"],
                        row["turnover_rate"],
                        row["volume"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["observed_at"],
                        news_summary["news_count"],
                        news_summary["latest_news_time"],
                        news_summary["latest_news_title"],
                        news_summary["latest_news_tags"],
                        news_summary["latest_news_source"],
                        " | ".join(json.loads(row["triggered_rules_json"])),
                        row["components_json"],
                    ]
                )
        return len(rows)

    def write_daily_report(self, output_path: Path, limit: int = 20, min_score: float = 1.0) -> int:
        rows = self.latest_candidates(limit=limit, min_score=min_score)
        counts = self.table_counts()
        industry_heat = self.industry_heat(limit=5, min_scored=2)
        daily_bar_health = self.daily_bar_health()
        quote_health = self.quote_health()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# A 股选股日报",
            "",
            f"- 生成日期: {date.today().isoformat()}",
            f"- 候选数量: {len(rows)}",
            f"- 证券基础表: {counts['securities']}",
            f"- 实时行情记录: {counts['quotes_realtime']}",
            f"- 评分记录: {counts['scores']}",
            f"- 已导入行业归属: {counts['stock_industries']}",
            f"- 日线时效: {_daily_report_daily_bar_freshness(daily_bar_health)}",
            f"- 实时行情时效: {_daily_report_quote_freshness(quote_health)}",
            "",
            "## 候选榜",
            "",
            "| 排名 | 代码 | 名称 | 板块 | 分数 | 现价 | 涨跌幅 | 成交额 | 总市值 | 换手率 | 行情时间 | 主要理由 | 消息面 |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
        for index, row in enumerate(rows, start=1):
            rules = json.loads(row["triggered_rules_json"])
            news_text = _daily_report_news_summary(
                self.related_news_for_symbol(str(row["symbol"]), name=row["name"], limit=3)
            )
            lines.append(
                "| {rank} | {symbol} | {name} | {board} | {score:.2f} | {price:.2f} | {pct:.2f}% | {amount:.0f} | {market_cap:.2f}亿 | {turnover:.2f}% | {observed_at} | {rules} | {news} |".format(
                    rank=index,
                    symbol=row["symbol"],
                    name=(row["name"] or "").replace("|", "/"),
                    board=row["board"] or "-",
                    score=row["total_score"],
                    price=row["latest_price"],
                    pct=row["pct_change"],
                    amount=row["amount"] or 0,
                    market_cap=(row["market_cap"] or 0) / 100_000_000,
                    turnover=row["turnover_rate"] or 0,
                    observed_at=str(row["observed_at"] or "-").replace("|", "/"),
                    rules=", ".join(rules[:4]).replace("|", "/"),
                    news=news_text,
                )
            )
        if industry_heat["items"]:
            lines.extend(
                [
                    "",
                    "## 当前行业热度",
                    "",
                    f"- 评分日期: {industry_heat['score_date'] or '-'}",
                    "- 仅展示当前评分批次覆盖至少 2 只股票的行业；行业标签仅作当前研究上下文，不参与历史回测。",
                    "",
                    "| 行业 | 成员 | 评分覆盖 | 平均分 | 正分占比 |",
                    "| --- | ---: | ---: | ---: | ---: |",
                ]
            )
            for item in industry_heat["items"]:
                lines.append(
                    "| {industry} | {members} | {scored} | {average:.2f} | {positive:.1f}% |".format(
                        industry=str(item["industry"]).replace("|", "/"),
                        members=int(item["member_count"]),
                        scored=int(item["scored_count"]),
                        average=float(item["average_score"] or 0),
                        positive=float(item["positive_rate"] or 0),
                    )
                )
        lines.extend(
            [
                "",
                "## 说明",
                "",
                "- 本报告用于个人投研辅助，不构成投资建议。",
                "- 候选池已排除 ST/PT/退市名称风险、0 价格和非正分股票。",
                "- 实时价格来自公开行情补充源；同花顺本地缓存用于证券池、名称、市场和诊断。",
                "- 候选榜的行情时间为单只报价观测时间，应结合数据时效判断，不等同于报告生成时间。",
                "- 消息面列来自同花顺本地资讯缓存和公开公告兜底，只作复核线索。",
            ]
        )
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(rows)


def _daily_report_news_summary(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "-"
    latest = str(rows[0]["title"] or "").replace("|", "/")
    if len(latest) > 38:
        latest = latest[:35] + "..."
    return f"{len(rows)}条：{latest}"


def _daily_report_daily_bar_freshness(health: dict[str, object]) -> str:
    latest_date = str(health.get("latest_trade_date") or "-")
    freshness = str(health.get("freshness_status") or "unknown")
    lag_days = health.get("weekday_lag_days")
    if freshness == "current":
        return f"近 1 个工作日（最近股票日线 {latest_date}）"
    if freshness == "lagging":
        return f"可能滞后 {lag_days if lag_days is not None else '-'} 个工作日（最近股票日线 {latest_date}）"
    if freshness == "empty":
        return "暂无日线"
    return f"无法判断（最近股票日线 {latest_date}）"


def _daily_report_quote_freshness(health: dict[str, object]) -> str:
    latest_date = str(health.get("latest_price_date") or "-")
    freshness = str(health.get("freshness_status") or "unknown")
    priced_symbols = int(health.get("priced_symbols") or 0)
    current_symbols = int(health.get("current_priced_symbols") or 0)
    stale_symbols = int(health.get("stale_priced_symbols") or 0)
    lag_days = health.get("weekday_lag_days")
    if freshness == "current":
        return f"近 1 个工作日（带价格 {priced_symbols} 只，最近价格日期 {latest_date}）"
    if freshness == "partial":
        return f"部分过期（近 1 个工作日 {current_symbols}/{priced_symbols} 只，可能过期 {stale_symbols} 只，最近价格日期 {latest_date}）"
    if freshness == "lagging":
        return f"可能滞后 {lag_days if lag_days is not None else '-'} 个工作日（带价格 {priced_symbols} 只，最近价格日期 {latest_date}）"
    if freshness == "empty":
        return "暂无带价格行情"
    return f"无法判断（带价格 {priced_symbols} 只，最近价格日期 {latest_date}）"


def _news_export_summary(rows: list[sqlite3.Row]) -> dict[str, object]:
    if not rows:
        return {
            "news_count": 0,
            "latest_news_time": "",
            "latest_news_title": "",
            "latest_news_tags": "",
            "latest_news_source": "",
        }
    latest = rows[0]
    return {
        "news_count": len(rows),
        "latest_news_time": latest["event_time"] or "",
        "latest_news_title": latest["title"] or "",
        "latest_news_tags": latest["tags"] or "",
        "latest_news_source": latest["source"] or "",
    }


def _score_date_from_thesis(raw: object) -> str:
    try:
        thesis = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(thesis, dict):
        return ""
    evidence = thesis.get("evidence")
    if not isinstance(evidence, dict):
        return ""
    score_date = str(evidence.get("score_date") or "")
    try:
        date.fromisoformat(score_date)
    except ValueError:
        return ""
    return score_date


def summarize_ai_decision_outcomes(rows: list[dict[str, object]]) -> dict[str, object]:
    def metrics(items: list[dict[str, object]]) -> dict[str, object]:
        evaluated = [item for item in items if item.get("status") == "evaluated" and item.get("return_pct") is not None]
        positive = [item for item in evaluated if float(item["return_pct"]) > 0]
        returns = [float(item["return_pct"]) for item in evaluated]
        return {
            "total": len(items),
            "evaluated": len(evaluated),
            "pending": sum(1 for item in items if item.get("status") == "pending"),
            "unavailable": sum(1 for item in items if item.get("status") == "unavailable"),
            "positive": len(positive),
            "hit_rate": len(positive) / len(evaluated) * 100 if evaluated else None,
            "average_return": sum(returns) / len(returns) if returns else None,
        }

    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("decision") or "未分类"), []).append(row)
    order = {"重点观察": 0, "观察": 1, "等待回踩": 2, "谨慎复盘": 3, "回避": 4}
    by_decision = []
    for decision in sorted(grouped, key=lambda item: (order.get(item, 9), item)):
        by_decision.append({"decision": decision, **metrics(grouped[decision])})
    return {**metrics(rows), "by_decision": by_decision}


def _strategy_backtest_run_summary(result: dict[str, object]) -> dict[str, object]:
    summary_keys = [
        "trade_count",
        "day_count",
        "win_rate",
        "avg_return",
        "gross_avg_return",
        "portfolio_avg_return",
        "max_drawdown",
        "best_return",
        "worst_return",
        "round_trip_cost_pct",
        "execution_mode",
        "position_mode",
        "skipped_locked_entries",
        "skipped_locked_exits",
        "excess_portfolio_avg_return",
    ]
    summary = {key: result.get(key) for key in summary_keys}
    benchmark = result.get("benchmark")
    if isinstance(benchmark, dict):
        summary["benchmark"] = {
            key: benchmark.get(key)
            for key in ("symbol", "sample_count", "avg_return", "cumulative_return", "max_drawdown")
        }
    else:
        summary["benchmark"] = None
    return summary


def _score_row(row: sqlite3.Row) -> tuple[dict[str, float], list[str]]:
    components: dict[str, float] = {}
    rules: list[str] = []
    pct_change = row["pct_change"]
    amount = row["amount"]
    volume = row["volume"]
    latest_price = row["latest_price"]
    name = row["name"] or ""
    high = row["high"]
    low = row["low"]
    previous_close = row["previous_close"]
    market_cap = row["market_cap"]
    turnover_rate = row["turnover_rate"]
    board = row["board"] or _board_for_symbol(row["symbol"])

    if any(token in name.upper() for token in ("ST", "PT", "退")):
        components["name_risk"] = -50.0
        rules.append("st_pt_or_delisting_name_risk")

    if latest_price is not None:
        if latest_price <= 0:
            components["invalid_quote"] = -100.0
            rules.append("invalid_zero_price")
        elif 3 <= latest_price <= 80:
            components["price_band"] = 10.0
            rules.append("price_in_operable_band")
        elif latest_price < 3:
            components["price_band"] = -15.0
            rules.append("very_low_price_risk")
        elif latest_price > 300:
            components["price_band"] = -5.0
            rules.append("high_price_liquidity_discount")

    if pct_change is not None:
        if 0.2 <= pct_change <= 3:
            components["intraday_momentum"] = 30.0
            rules.append("constructive_positive_pct_change")
        elif 3 < pct_change <= 7:
            components["intraday_momentum"] = 18.0
            rules.append("strong_move_watch_chase_risk")
        elif pct_change > 7:
            components["intraday_momentum"] = -20.0
            rules.append("near_limit_up_chase_risk")
        elif -2 <= pct_change < 0:
            components["intraday_momentum"] = -5.0
            rules.append("negative_pct_change")
        elif pct_change < -2:
            components["intraday_momentum"] = -20.0
            rules.append("large_intraday_decline")

    if amount is not None and amount >= 100_000_000:
        components["liquidity"] = 30.0
        rules.append("active_amount")
    elif amount is not None and amount >= 30_000_000:
        components["liquidity"] = 18.0
        rules.append("acceptable_amount")
    elif volume is not None and volume > 0:
        components["liquidity"] = 8.0
        rules.append("has_volume")

    if latest_price is not None and high is not None and low is not None and high > low:
        close_position = (latest_price - low) / (high - low)
        if close_position >= 0.75:
            components["intraday_position"] = 15.0
            rules.append("close_near_intraday_high")
        elif close_position <= 0.25:
            components["intraday_position"] = -10.0
            rules.append("close_near_intraday_low")

    if previous_close is not None and latest_price is not None and previous_close > 0:
        gap = (row["open"] - previous_close) / previous_close * 100 if row["open"] is not None else 0.0
        if abs(gap) > 5:
            components["gap_risk"] = -10.0
            rules.append("large_open_gap")

    if market_cap is not None and market_cap > 0:
        market_cap_yi = market_cap / 100_000_000
        if 50 <= market_cap_yi <= 3000:
            components["market_cap_tier"] = 12.0
            rules.append("market_cap_in_preferred_range")
        elif market_cap_yi < 30:
            components["market_cap_tier"] = -15.0
            rules.append("micro_cap_risk")
        elif market_cap_yi > 8000:
            components["market_cap_tier"] = -6.0
            rules.append("mega_cap_lower_elasticity")

    if turnover_rate is not None and turnover_rate > 0:
        if 0.5 <= turnover_rate <= 8:
            components["turnover_quality"] = 12.0
            rules.append("healthy_turnover_rate")
        elif turnover_rate < 0.2:
            components["turnover_quality"] = -8.0
            rules.append("low_turnover_rate")
        elif turnover_rate > 15:
            components["turnover_quality"] = -12.0
            rules.append("overheated_turnover_rate")

    if board == "科创板":
        components["board_risk"] = -3.0
        rules.append("star_market_volatility_discount")
    elif board == "创业板":
        components["board_risk"] = -2.0
        rules.append("chinext_volatility_discount")
    elif board in {"沪主板", "深主板"}:
        components["board_quality"] = 3.0
        rules.append("main_board")

    return components, rules


def _latest_score_filter_sql(alias: str) -> str:
    return f"""
    (
        {alias}.score_run_id = (SELECT MAX(id) FROM score_runs)
        OR (
            {alias}.score_run_id IS NULL
            AND NOT EXISTS (SELECT 1 FROM score_runs)
            AND {alias}.score_date = (SELECT MAX(score_date) FROM scores)
        )
    )
    """


def _weekday_lag_days(latest_trade_date: str, as_of: date) -> int | None:
    if not latest_trade_date:
        return None
    try:
        latest = date.fromisoformat(latest_trade_date)
    except ValueError:
        return None
    if latest >= as_of:
        return 0
    lag = 0
    current = latest + timedelta(days=1)
    while current <= as_of:
        if current.weekday() < 5:
            lag += 1
        current += timedelta(days=1)
    return lag


def _factor_effectiveness(direction: object, horizons: object) -> dict[str, object]:
    if not isinstance(horizons, dict):
        return {"effectiveness_score": 0.0, "total_samples": 0, "verdict": "样本不足"}
    total_samples = 0
    weighted_score = 0.0
    weight_sum = 0.0
    direction_text = str(direction or "")
    for horizon, stats in horizons.items():
        if not isinstance(stats, dict):
            continue
        samples = int(stats.get("samples") or 0)
        if samples <= 0:
            continue
        avg_return = float(stats.get("avg_return") or 0.0)
        win_rate = float(stats.get("win_rate") or 0.0)
        if direction_text == "risk":
            return_edge = -avg_return
            win_edge = 50.0 - win_rate
        else:
            return_edge = avg_return
            win_edge = win_rate - 50.0
        sample_weight = min(samples / 30.0, 1.0)
        horizon_weight = 1.2 if int(horizon) == 5 else 1.0
        score = return_edge * 8.0 + win_edge * 0.6 + sample_weight * 10.0
        weighted_score += score * horizon_weight
        weight_sum += horizon_weight
        total_samples += samples
    effectiveness_score = weighted_score / weight_sum if weight_sum else 0.0
    if total_samples < 10:
        verdict = "样本不足"
    elif effectiveness_score >= 18:
        verdict = "有效"
    elif effectiveness_score >= 5:
        verdict = "观察"
    elif effectiveness_score <= -8:
        verdict = "反向"
    else:
        verdict = "中性"
    return {
        "effectiveness_score": effectiveness_score,
        "total_samples": total_samples,
        "verdict": verdict,
    }


def assess_strategy_walk_forward(
    result: dict[str, object],
    min_folds: int = 3,
    min_trades: int = 60,
    min_positive_fold_ratio: float = 0.6,
    max_drawdown: float = -20.0,
    min_benchmark_excess_return: float = 0.0,
) -> dict[str, object]:
    if min_folds < 1 or min_trades < 1:
        raise ValueError("min_folds and min_trades must be positive")
    if not 0 < min_positive_fold_ratio <= 1:
        raise ValueError("min_positive_fold_ratio must be between 0 and 1")
    if max_drawdown > 0:
        raise ValueError("max_drawdown must be zero or negative")

    folds = [row for row in result.get("folds", []) if isinstance(row, dict)]
    total_trades = int(result.get("total_trades") or sum(int(row.get("trade_count") or 0) for row in folds))
    portfolio_returns = [float(row.get("portfolio_avg_return") or 0.0) for row in folds]
    fold_drawdowns = [float(row.get("max_drawdown") or 0.0) for row in folds]
    positive_fold_count = sum(1 for value in portfolio_returns if value > 0)
    positive_fold_ratio = positive_fold_count / len(folds) if folds else 0.0
    portfolio_avg_return = sum(portfolio_returns) / len(portfolio_returns) if portfolio_returns else 0.0
    observed_max_drawdown = min(fold_drawdowns) if fold_drawdowns else 0.0

    benchmark_returns = []
    benchmark_fold_count = 0
    for row in folds:
        benchmark = row.get("benchmark")
        if not isinstance(benchmark, dict) or int(benchmark.get("sample_count") or 0) <= 0:
            continue
        benchmark_fold_count += 1
        benchmark_returns.append(float(benchmark.get("avg_return") or 0.0))
    benchmark_avg_return = sum(benchmark_returns) / len(benchmark_returns) if benchmark_returns else None
    benchmark_excess_return = (
        portfolio_avg_return - benchmark_avg_return if benchmark_avg_return is not None else None
    )
    benchmark_complete = bool(folds) and benchmark_fold_count == len(folds)

    reasons: list[str] = []
    if len(folds) < min_folds:
        reasons.append(f"样本外折数 {len(folds)} 少于最低要求 {min_folds}。")
    if total_trades < min_trades:
        reasons.append(f"样本外交易数 {total_trades} 少于最低要求 {min_trades}。")

    if reasons:
        verdict = "样本不足"
    else:
        if portfolio_avg_return <= 0:
            reasons.append("样本外组合平均收益未为正。")
        if positive_fold_ratio < min_positive_fold_ratio:
            reasons.append(
                f"正收益折占比 {positive_fold_ratio * 100:.1f}% 低于最低要求 {min_positive_fold_ratio * 100:.1f}%。"
            )
        if observed_max_drawdown < max_drawdown:
            reasons.append(f"最大回撤 {observed_max_drawdown:.2f}% 超出允许下限 {max_drawdown:.2f}%。")
        if benchmark_complete and benchmark_excess_return is not None and benchmark_excess_return <= min_benchmark_excess_return:
            reasons.append(
                f"相对基准超额 {benchmark_excess_return:.2f}% 未超过最低要求 {min_benchmark_excess_return:.2f}%。"
            )
        if reasons:
            verdict = "未通过"
        elif not benchmark_complete:
            verdict = "观察"
            reasons.append("基准覆盖不完整，暂不授予通过结论。")
        else:
            verdict = "通过"
            reasons.append("样本外收益、正收益折占比、回撤和基准超额均达到当前门槛。")

    benchmark_text = "基准缺失" if benchmark_avg_return is None else f"基准平均 {benchmark_avg_return:.2f}%"
    summary = (
        f"样本外 {len(folds)} 折、{total_trades} 笔；组合平均 {portfolio_avg_return:.2f}%，"
        f"正收益折 {positive_fold_count}/{len(folds)}，最大回撤 {observed_max_drawdown:.2f}%，{benchmark_text}。"
    )
    return {
        "verdict": verdict,
        "summary": summary,
        "reasons": reasons,
        "fold_count": len(folds),
        "total_trades": total_trades,
        "positive_fold_count": positive_fold_count,
        "positive_fold_ratio": positive_fold_ratio,
        "portfolio_avg_return": portfolio_avg_return,
        "max_drawdown": observed_max_drawdown,
        "benchmark_fold_count": benchmark_fold_count,
        "benchmark_avg_return": benchmark_avg_return,
        "benchmark_excess_return": benchmark_excess_return,
        "benchmark_complete": benchmark_complete,
        "thresholds": {
            "min_folds": min_folds,
            "min_trades": min_trades,
            "min_positive_fold_ratio": min_positive_fold_ratio,
            "max_drawdown": max_drawdown,
            "min_benchmark_excess_return": min_benchmark_excess_return,
        },
    }


def _strategy_summary(
    trades: list[dict[str, object]],
    horizon_days: int,
    top_n: int,
    min_signal_score: float,
    cost_bps: float = 0.0,
    slippage_bps: float = 0.0,
    execution_mode: str = "next_open",
    position_mode: str = "non_overlapping",
    skipped_locked_entries: int = 0,
    skipped_locked_exits: int = 0,
    benchmark: dict[str, object] | None = None,
) -> dict[str, object]:
    round_trip_cost_pct = max(0.0, (cost_bps + slippage_bps) * 2 / 100)
    if not trades:
        return {
            "horizon_days": horizon_days,
            "top_n": top_n,
            "min_signal_score": min_signal_score,
            "cost_bps": cost_bps,
            "slippage_bps": slippage_bps,
            "execution_mode": execution_mode,
            "position_mode": position_mode,
            "skipped_locked_entries": skipped_locked_entries,
            "skipped_locked_exits": skipped_locked_exits,
            "round_trip_cost_pct": round_trip_cost_pct,
            "benchmark": benchmark,
            "excess_portfolio_avg_return": None,
            "trade_count": 0,
            "day_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "gross_avg_return": 0.0,
            "best_return": None,
            "worst_return": None,
            "portfolio_avg_return": 0.0,
            "gross_portfolio_avg_return": 0.0,
            "return_std": 0.0,
            "profit_loss_ratio": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown": 0.0,
            "equity_curve": [],
            "trades": [],
            "daily_returns": [],
            "period_stats": {"monthly": [], "yearly": []},
        }
    returns = [float(row["return_pct"]) for row in trades]
    gross_returns = [float(row.get("gross_return_pct", row["return_pct"])) for row in trades]
    by_date: dict[str, list[tuple[float, float]]] = {}
    for row in trades:
        by_date.setdefault(str(row["trade_date"]), []).append(
            (float(row["return_pct"]), float(row.get("gross_return_pct", row["return_pct"])))
        )
    daily_returns = [
        {
            "trade_date": trade_date,
            "selected": len(values),
            "avg_return": sum(value[0] for value in values) / len(values),
            "gross_avg_return": sum(value[1] for value in values) / len(values),
        }
        for trade_date, values in sorted(by_date.items())
    ]
    equity_curve = []
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for row in daily_returns:
        equity *= 1 + float(row["avg_return"]) / 100
        peak = max(peak, equity)
        drawdown = (equity / peak - 1) * 100 if peak else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        equity_curve.append(
            {
                "trade_date": row["trade_date"],
                "equity": equity,
                "drawdown": drawdown,
                "avg_return": row["avg_return"],
            }
        )
    avg_return = sum(returns) / len(returns)
    variance = sum((item - avg_return) ** 2 for item in returns) / len(returns)
    return_std = math.sqrt(variance)
    winners = [item for item in returns if item > 0]
    losers = [abs(item) for item in returns if item < 0]
    avg_win = sum(winners) / len(winners) if winners else 0.0
    avg_loss = sum(losers) / len(losers) if losers else 0.0
    profit_loss_ratio = avg_win / avg_loss if avg_loss else 0.0
    portfolio_avg_return = sum(float(row["avg_return"]) for row in daily_returns) / len(daily_returns)
    period_stats = _strategy_period_stats(daily_returns)
    benchmark_avg_return = None
    if benchmark is not None and int(benchmark.get("sample_count") or 0) > 0:
        benchmark_avg_return = float(benchmark.get("avg_return") or 0.0)
    return {
        "horizon_days": horizon_days,
        "top_n": top_n,
        "min_signal_score": min_signal_score,
        "cost_bps": cost_bps,
        "slippage_bps": slippage_bps,
        "execution_mode": execution_mode,
        "position_mode": position_mode,
        "skipped_locked_entries": skipped_locked_entries,
        "skipped_locked_exits": skipped_locked_exits,
        "round_trip_cost_pct": round_trip_cost_pct,
        "benchmark": benchmark,
        "excess_portfolio_avg_return": None if benchmark_avg_return is None else portfolio_avg_return - benchmark_avg_return,
        "trade_count": len(trades),
        "day_count": len(daily_returns),
        "win_rate": sum(1 for item in returns if item > 0) / len(returns) * 100,
        "avg_return": avg_return,
        "gross_avg_return": sum(gross_returns) / len(gross_returns),
        "best_return": max(returns),
        "worst_return": min(returns),
        "portfolio_avg_return": portfolio_avg_return,
        "gross_portfolio_avg_return": sum(float(row["gross_avg_return"]) for row in daily_returns) / len(daily_returns),
        "return_std": return_std,
        "profit_loss_ratio": profit_loss_ratio,
        "sharpe_like": avg_return / return_std if return_std else 0.0,
        "max_drawdown": max_drawdown,
        "equity_curve": equity_curve,
        "trades": sorted(trades, key=lambda row: (str(row["trade_date"]), float(row["signal_score"])), reverse=True),
        "daily_returns": daily_returns,
        "period_stats": period_stats,
    }


def _strategy_period_stats(daily_returns: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    return {
        "monthly": _strategy_period_rows(daily_returns, width=7),
        "yearly": _strategy_period_rows(daily_returns, width=4),
    }


def _strategy_period_rows(daily_returns: list[dict[str, object]], width: int) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in daily_returns:
        period = str(row["trade_date"])[:width]
        grouped.setdefault(period, []).append(row)
    result = []
    for period, rows in sorted(grouped.items()):
        net_returns = [float(row["avg_return"]) for row in rows]
        gross_returns = [float(row["gross_avg_return"]) for row in rows]
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for return_pct in net_returns:
            equity *= 1 + return_pct / 100
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, (equity / peak - 1) * 100 if peak else 0.0)
        result.append(
            {
                "period": period,
                "batches": len(rows),
                "trades": sum(int(row["selected"]) for row in rows),
                "win_rate": sum(1 for return_pct in net_returns if return_pct > 0) / len(net_returns) * 100,
                "avg_return": sum(net_returns) / len(net_returns),
                "gross_avg_return": sum(gross_returns) / len(gross_returns),
                "cumulative_return": (equity - 1) * 100,
                "max_drawdown": max_drawdown,
            }
        )
    return result


def _strategy_execution_mode(value: str) -> str:
    if value not in {"next_open", "signal_close"}:
        raise ValueError(f"unsupported strategy execution mode: {value}")
    return value


def _strategy_position_mode(value: str) -> str:
    if value not in {"non_overlapping", "daily_batches"}:
        raise ValueError(f"unsupported strategy position mode: {value}")
    return value


def _locked_limit_direction(symbol: str, bars: list[sqlite3.Row], index: int) -> str | None:
    if index <= 0 or symbol.lower().startswith(("sh", "sz")):
        return None
    bar = bars[index]
    previous_close = bars[index - 1]["close"]
    prices = [bar["open"], bar["high"], bar["low"], bar["close"]]
    if previous_close in (None, 0) or any(price is None for price in prices):
        return None
    numeric_prices = [float(price) for price in prices]
    close = float(bar["close"])
    if max(numeric_prices) - min(numeric_prices) > max(0.01, abs(close) * 0.001):
        return None
    change_pct = (close / float(previous_close) - 1) * 100
    limit_pct = _daily_price_limit_pct(symbol)
    if change_pct >= limit_pct - 0.5:
        return "up"
    if change_pct <= -limit_pct + 0.5:
        return "down"
    return None


def _daily_price_limit_pct(symbol: str) -> float:
    code = symbol.lower().strip()
    if code.startswith(("300", "301", "688")):
        return 20.0
    if code.startswith(("4", "8")):
        return 30.0
    return 10.0


def _relative_strength_row(
    symbol: str,
    name: object,
    factor_id: str,
    factor_name: str,
    direction: str,
    strength: float,
    trade_date: object,
    window: int,
    return_pct: object,
) -> dict[str, object]:
    ret = float(return_pct or 0.0)
    if direction == "risk":
        reason = f"{trade_date} 近 {window} 日涨幅 {ret:.2f}%，RPS 排名处于后 15%，相对弱势。"
        category = "风险"
    else:
        reason = f"{trade_date} 近 {window} 日涨幅 {ret:.2f}%，RPS={strength:.1f}，位于可比股票池前列。"
        category = "相对强弱"
    return {
        "symbol": symbol,
        "name": name,
        "factor_id": factor_id,
        "factor_name": factor_name,
        "category": category,
        "direction": direction,
        "strength": strength,
        "reason": reason,
    }


def _strategy_signal_strength(signal: FactorSignal) -> float:
    strength = float(signal.strength)
    if str(signal.factor_id).startswith("rps_"):
        if signal.direction == "risk":
            return _bounded(45.0 + max(0.0, strength - 85.0) * 0.8, 45.0, 65.0)
        return _bounded(50.0 + max(0.0, strength - 85.0) * 0.9, 50.0, 70.0)
    return strength


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _disclosed_fundamental_at(rows: list[sqlite3.Row], signal_date: str) -> dict[str, object] | None:
    eligible = [
        row
        for row in rows
        if str(row["notice_date"] or "") < signal_date and str(row["report_date"] or "") <= signal_date
    ]
    if not eligible:
        return None
    report_date = max(str(row["report_date"] or "") for row in eligible)
    latest_period_rows = [row for row in eligible if str(row["report_date"] or "") == report_date]
    fields = ("revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "roe", "operating_cash_flow", "pe_ttm", "pb")
    preferred = sorted(
        latest_period_rows,
        key=lambda row: (
            sum(row[key] is not None for key in fields),
            str(row["notice_date"] or ""),
            str(row["imported_at"] or ""),
            str(row["source_file"] or ""),
        ),
        reverse=True,
    )
    snapshot: dict[str, object] = {
        "symbol": preferred[0]["symbol"],
        "report_date": report_date,
        "notice_date": max(str(row["notice_date"] or "") for row in latest_period_rows),
    }
    for field in fields:
        snapshot[field] = next((row[field] for row in preferred if row[field] is not None), None)
    return snapshot


def _percentile_rank(values: dict[str, object]) -> dict[str, float]:
    clean = [(symbol, float(value)) for symbol, value in values.items() if value is not None]
    if not clean:
        return {}
    clean.sort(key=lambda item: item[1])
    if len(clean) == 1:
        return {clean[0][0]: 100.0}
    return {symbol: index / (len(clean) - 1) * 100 for index, (symbol, _) in enumerate(clean)}


def _window_return(bars: list[sqlite3.Row], index: int, window: int) -> float | None:
    current = _close_at(bars, index)
    previous = _close_at(bars, index - window)
    if current is None or previous in (None, 0):
        return None
    return (float(current) / float(previous) - 1) * 100


def _close_at(bars: list[sqlite3.Row], index: int) -> float | None:
    if index < 0 or index >= len(bars):
        return None
    value = bars[index]["close"]
    return None if value is None else float(value)


def _market_for_symbol(symbol: str) -> str:
    if symbol.startswith(("6", "5", "9")):
        return "shase"
    return "sznse"


def _board_for_symbol(symbol: str) -> str:
    if symbol.startswith(("300", "301")):
        return "创业板"
    if symbol.startswith("688"):
        return "科创板"
    if symbol.startswith(("600", "601", "603", "605")):
        return "沪主板"
    if symbol.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "其他"
