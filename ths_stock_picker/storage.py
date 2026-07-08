from __future__ import annotations

import json
import sqlite3
import csv
import math
from datetime import date
from pathlib import Path

from .factor_engine import evaluate_factors, factor_definitions
from .history_import import DailyBar
from .models import NewsItem, QuoteRealtime, Security, SnapshotDiagnostics, WatchlistEntry
from .quote_observer import QuoteObservation
from .scoring_profile import ScoringProfile, default_scoring_profile

DEFAULT_DB_PATH = Path("work/ths_stock_picker.db")


class Repository:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
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
            self.conn.execute("DELETE FROM quotes_realtime")
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
        quotes = [
            QuoteRealtime(
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
        ]
        with self.conn:
            self.conn.execute("DELETE FROM quotes_realtime WHERE quote_status = 'public_quote'")
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
        rows = self.conn.execute(
            """
            SELECT trade_date,
                   AVG(close) AS close,
                   AVG(high) AS high,
                   AVG(low) AS low,
                   AVG(volume) AS volume
            FROM daily_bars
            WHERE symbol = ? AND close IS NOT NULL
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT 30
            """,
            (symbol,),
        ).fetchall()
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
        tables = ["securities", "market_snapshots", "quotes_realtime", "watchlists", "scores", "score_runs", "daily_bars", "stock_notes", "ai_decisions", "news_items"]
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def export_table_csv(self, table: str, output_path: Path) -> int:
        allowed = {"securities", "market_snapshots", "quotes_realtime", "watchlists", "scores", "score_runs", "daily_bars", "stock_notes", "ai_decisions", "news_items"}
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
        return len(bars)

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
        return self.conn.execute(
            """
            SELECT trade_date,
                   AVG(open) AS open,
                   AVG(high) AS high,
                   AVG(low) AS low,
                   AVG(close) AS close,
                   AVG(volume) AS volume
            FROM daily_bars
            WHERE symbol = ?
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()

    def daily_bars_for_symbol(self, symbol: str, limit: int | None = None) -> list[sqlite3.Row]:
        limit_sql = "" if limit is None else "LIMIT ?"
        params: tuple[object, ...] = (symbol,) if limit is None else (symbol, limit)
        rows = self.conn.execute(
            f"""
            SELECT trade_date,
                   AVG(open) AS open,
                   AVG(high) AS high,
                   AVG(low) AS low,
                   AVG(close) AS close,
                   AVG(volume) AS volume
            FROM daily_bars
            WHERE symbol = ?
            GROUP BY trade_date
            ORDER BY trade_date DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return list(reversed(rows))

    def symbols_with_daily_bars(self, limit: int | None = None) -> list[str]:
        limit_sql = "" if limit is None else "LIMIT ?"
        params: tuple[object, ...] = () if limit is None else (limit,)
        rows = self.conn.execute(
            f"""
            SELECT symbol, COUNT(DISTINCT trade_date) AS bars
            FROM daily_bars
            GROUP BY symbol
            HAVING bars >= 20
            ORDER BY bars DESC, symbol
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [row["symbol"] for row in rows]

    def factor_scan(self, limit: int = 50, symbols: list[str] | None = None) -> list[dict[str, object]]:
        selected = symbols or self.symbols_with_daily_bars(limit=limit * 3)
        results: list[dict[str, object]] = []
        for symbol in selected:
            bars = self.daily_bars_for_symbol(symbol, limit=80)
            signals = evaluate_factors(bars)
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
        return sorted(results, key=lambda row: (row["direction"] == "positive", float(row["strength"])), reverse=True)[:limit]

    def factor_backtest(self, horizon_days: int = 5, min_bars: int = 40, limit_symbols: int | None = None) -> list[dict[str, object]]:
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
            bars = self.daily_bars_for_symbol(symbol)
            if len(bars) < max(min_bars, 20 + horizon_days):
                continue
            for index in range(19, len(bars) - horizon_days):
                window = bars[: index + 1]
                close_now = window[-1]["close"]
                close_future = bars[index + horizon_days]["close"]
                if close_now in (None, 0) or close_future is None:
                    continue
                forward_return = (float(close_future) / float(close_now) - 1) * 100
                for signal in evaluate_factors(window):
                    row = stats[signal.factor_id]
                    row["direction"] = signal.direction
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

    def factor_backtest_matrix(self, horizons: list[int] | None = None, limit_symbols: int | None = None) -> list[dict[str, object]]:
        selected_horizons = horizons or [3, 5, 10]
        by_factor: dict[str, dict[str, object]] = {}
        definitions = {item.factor_id: item for item in factor_definitions()}
        for horizon in selected_horizons:
            for row in self.factor_backtest(horizon_days=horizon, limit_symbols=limit_symbols):
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
        return sorted(rows, key=lambda row: (float(row["effectiveness_score"]), int(row["total_samples"])), reverse=True)

    def latest_quote_for_symbol(self, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT symbol, name, latest_price, pct_change, amount, turnover_rate, board, observed_at
            FROM quotes_realtime
            WHERE symbol = ?
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

    def insert_ai_decisions(self, rows: list[dict[str, object]]) -> int:
        with self.conn:
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
        for row in rows:
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
            "rules",
            "components",
        ]
        with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in rows:
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
                        " | ".join(json.loads(row["triggered_rules_json"])),
                        row["components_json"],
                    ]
                )
        return len(rows)

    def write_daily_report(self, output_path: Path, limit: int = 20, min_score: float = 1.0) -> int:
        rows = self.latest_candidates(limit=limit, min_score=min_score)
        counts = self.table_counts()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# A 股选股日报",
            "",
            f"- 生成日期: {date.today().isoformat()}",
            f"- 候选数量: {len(rows)}",
            f"- 证券基础表: {counts['securities']}",
            f"- 实时行情记录: {counts['quotes_realtime']}",
            f"- 评分记录: {counts['scores']}",
            "",
            "## 候选榜",
            "",
            "| 排名 | 代码 | 名称 | 板块 | 分数 | 现价 | 涨跌幅 | 成交额 | 总市值 | 换手率 | 主要理由 |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for index, row in enumerate(rows, start=1):
            rules = json.loads(row["triggered_rules_json"])
            lines.append(
                "| {rank} | {symbol} | {name} | {board} | {score:.2f} | {price:.2f} | {pct:.2f}% | {amount:.0f} | {market_cap:.2f}亿 | {turnover:.2f}% | {rules} |".format(
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
                    rules=", ".join(rules[:4]).replace("|", "/"),
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
            ]
        )
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(rows)


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
