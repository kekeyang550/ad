from __future__ import annotations

import html
import json
import csv
from io import StringIO
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from .ai_decision import AIDecision, analyze_symbol, decisions_to_rows, rank_candidates
from .factor_engine import factor_definitions
from .storage import DEFAULT_DB_PATH, Repository
from .ths_local import DEFAULT_THS_ROOT
from .ths_monitor import THSMonitorSnapshot, inspect_ths_source


@dataclass(frozen=True)
class DashboardFilters:
    query: str = ""
    board: str = ""
    min_score: float = 1.0
    sort: str = "score"

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> "DashboardFilters":
        sort = _first(query, "sort") or "score"
        if sort not in {"score", "amount", "pct", "turnover", "market_cap"}:
            sort = "score"
        return cls(
            query=(_first(query, "q") or "").strip(),
            board=(_first(query, "board") or "").strip(),
            min_score=_to_float(_first(query, "min_score"), 1.0),
            sort=sort,
        )

    def to_query_string(self, **overrides: object) -> str:
        values = {
            "q": self.query,
            "board": self.board,
            "min_score": f"{self.min_score:g}",
            "sort": self.sort,
        }
        for key, value in overrides.items():
            values[key] = "" if value is None else str(value)
        cleaned = {key: value for key, value in values.items() if value not in {"", "score"}}
        return urlencode(cleaned)


@dataclass(frozen=True)
class NotesFilters:
    query: str = ""
    status: str = ""
    sort: str = "updated"

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> "NotesFilters":
        status = (_first(query, "status") or "").strip()
        if status not in {"", "watch", "hold", "avoid", "review"}:
            status = ""
        sort = (_first(query, "sort") or "updated").strip()
        if sort not in {"updated", "score", "pct", "price", "symbol"}:
            sort = "updated"
        return cls(
            query=(_first(query, "q") or "").strip(),
            status=status,
            sort=sort,
        )

    def to_query_string(self, **overrides: object) -> str:
        values = {"q": self.query, "status": self.status, "sort": self.sort}
        for key, value in overrides.items():
            values[key] = "" if value is None else str(value)
        cleaned = {key: value for key, value in values.items() if value not in {"", "updated"}}
        return urlencode(cleaned)


def render_dashboard(repo: Repository, limit: int = 30, filters: DashboardFilters | None = None) -> str:
    active_filters = filters or DashboardFilters()
    counts = repo.table_counts()
    candidates = _filter_candidate_rows(
        repo.latest_candidates(limit=max(limit * 4, 100), min_score=active_filters.min_score),
        active_filters,
    )[:limit]
    scores = _filter_score_rows(repo.latest_scores(limit=max(limit * 4, 100), positive_only=True), active_filters)[:limit]
    run_changes = repo.compare_score_runs(limit=12, min_score=1.0)
    snapshots = repo.latest_snapshots(limit=12)
    latest_run = repo.latest_score_runs(1)
    profile_note = f" · 当前评分 {latest_run[0]['profile_name']}" if latest_run else ""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>A 股选股面板</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            '<main class="shell">',
            '<section class="topbar">',
            "<div>",
            "<h1>A 股选股面板</h1>",
            f"<p>生成时间 {html.escape(generated_at)}{html.escape(profile_note)} · 数据只读 · 不构成投资建议</p>",
            "</div>",
            '<div class="actions"><a class="refresh" href="/">刷新</a><a class="refresh" href="/ths">同花顺</a><a class="refresh" href="/news">资讯</a><a class="refresh" href="/factors">因子</a><a class="refresh" href="/ai">AI 选股</a><a class="refresh" href="/notes">观察池</a></div>',
            "</section>",
            _render_counts(counts),
            _render_filters(active_filters),
            _render_candidates(candidates),
            _render_run_changes(run_changes),
            _render_scores(scores),
            _render_snapshots(snapshots),
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def render_candidates_csv(repo: Repository, limit: int = 500, filters: DashboardFilters | None = None) -> str:
    active_filters = filters or DashboardFilters()
    rows = _filter_candidate_rows(
        repo.latest_candidates(limit=max(limit * 2, 500), min_score=active_filters.min_score),
        active_filters,
    )[:limit]
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["symbol", "name", "board", "total_score", "latest_price", "pct_change", "amount", "turnover_rate", "rules"])
    for row in rows:
        writer.writerow(
            [
                row["symbol"],
                row["name"],
                row["board"],
                row["total_score"],
                row["latest_price"],
                row["pct_change"],
                row["amount"],
                row["turnover_rate"],
                " | ".join(json.loads(row["triggered_rules_json"])),
            ]
        )
    return output.getvalue()


def render_notes_page(
    repo: Repository,
    status: str = "",
    limit: int = 100,
    filters: NotesFilters | None = None,
) -> str:
    active_filters = filters or NotesFilters(status=status if status in {"watch", "hold", "avoid", "review"} else "")
    rows = repo.list_stock_notes(
        limit=limit,
        status=active_filters.status or None,
        query=active_filters.query,
        sort=active_filters.sort,
    )
    return _page(
        "本地观察池",
        [
            _topbar("本地观察池", "本项目本地保存的观察状态、标签和备注", back_link="/"),
            _render_notes_filter(active_filters),
            _render_notes_table(rows, active_filters),
        ],
    )


def render_notes_csv(repo: Repository, limit: int = 1000, filters: NotesFilters | None = None) -> str:
    active_filters = filters or NotesFilters()
    rows = repo.list_stock_notes(
        limit=limit,
        status=active_filters.status or None,
        query=active_filters.query,
        sort=active_filters.sort,
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["symbol", "name", "board", "status", "tags", "note", "total_score", "latest_price", "pct_change", "updated_at"])
    for row in rows:
        writer.writerow(
            [
                row["symbol"],
                row["name"],
                row["board"],
                row["status"],
                row["tags"],
                row["note"],
                row["total_score"],
                row["latest_price"],
                row["pct_change"],
                row["updated_at"],
            ]
        )
    return output.getvalue()


def render_ai_page(repo: Repository, limit: int = 20, min_score: float = 1.0) -> str:
    decisions = rank_candidates(repo, limit=limit, min_score=min_score)
    return _page(
        "AI 选股",
        [
            _topbar("AI 选股", "基于评分、行情、日线和本地备注生成的结构化选股观点", back_link="/"),
            _render_ai_controls(limit, min_score),
            _render_ai_decisions(decisions),
        ],
    )


def render_factors_page(repo: Repository, limit: int = 50, horizon: int = 5) -> str:
    matrix_horizons = [3, 5, 10]
    if horizon not in matrix_horizons:
        matrix_horizons.append(horizon)
    matrix_horizons = sorted(matrix_horizons)
    return _page(
        "公式因子",
        [
            _topbar("公式因子", "借鉴公式思路，转成可解释因子并用真实日线回测", back_link="/"),
            _render_factor_controls(limit, horizon),
            _render_factor_definitions(),
            _render_factor_signals(repo.factor_scan(limit=limit)),
            _render_factor_matrix(repo.factor_backtest_matrix(horizons=matrix_horizons, limit_symbols=300), matrix_horizons),
        ],
    )


def render_ai_history_page(repo: Repository, limit: int = 50, symbol: str = "") -> str:
    selected_symbol = symbol if len(symbol) == 6 and symbol.isdigit() else ""
    rows = repo.latest_ai_decisions(limit=limit, symbol=selected_symbol or None)
    return _page(
        "AI 历史",
        [
            _topbar("AI 历史", "已保存的 AI 选股观点，用于留痕和复盘", back_link="/ai"),
            _render_ai_history_controls(limit, selected_symbol),
            _render_ai_history(rows),
        ],
    )


def render_ai_changes_page(repo: Repository, limit: int = 50) -> str:
    rows = repo.ai_decision_changes(limit=limit)
    return _page(
        "AI 变化",
        [
            _topbar("AI 变化", "对比每只股票最近两次保存的 AI 观点", back_link="/ai"),
            _render_ai_changes_controls(limit),
            _render_ai_changes(rows),
        ],
    )


def render_ths_monitor_page(ths_root: Path = DEFAULT_THS_ROOT) -> str:
    snapshot = inspect_ths_source(ths_root)
    return _page(
        "同花顺数据源",
        [
            _topbar("同花顺数据源", "只读监控同花顺进程和本地实时缓存状态", back_link="/"),
            _render_ths_summary(snapshot),
            _render_ths_processes(snapshot),
            _render_ths_files(snapshot),
        ],
    )


def render_news_page(repo: Repository, query: str = "", tag: str = "", limit: int = 50) -> str:
    rows = repo.latest_news(limit=limit, query=query, tag=tag)
    return _page(
        "资讯",
        [
            _topbar("资讯", "同花顺本地资讯缓存解析结果", back_link="/"),
            _render_news_filters(query, tag, limit),
            _render_news_table(rows),
        ],
    )


def render_symbol_detail(repo: Repository, symbol: str, bars_limit: int = 20) -> str:
    row = repo.score_explanation(symbol)
    if row is None:
        return _page(
            "个股详情",
            [
                _topbar("个股详情", f"{symbol} 暂无最新评分", back_link="/"),
                '<section class="panel"><p class="empty">暂无评分，请先运行 run-daily 或 score。</p></section>',
            ],
        )

    components = json.loads(row["components_json"])
    rules = json.loads(row["triggered_rules_json"])
    bars = repo.recent_daily_bars(symbol, limit=bars_limit)
    chart_bars = repo.recent_daily_bars(symbol, limit=60)
    note = repo.stock_note(symbol)
    ai_decision = analyze_symbol(repo, symbol)
    related_news = repo.related_news_for_symbol(symbol, name=row["name"], limit=8)
    if not related_news and ai_decision is not None:
        related_news = ai_decision.evidence.get("news", [])
    title = f"{row['symbol']} {row['name'] or ''}".strip()
    return _page(
        title,
        [
            _topbar(title, f"评分日期 {row['score_date']} · 数据只读 · 不构成投资建议", back_link="/"),
            _render_quote_summary(row),
            _render_ai_symbol_decision(ai_decision),
            _render_stock_note(symbol, note),
            _render_related_news(related_news),
            _render_price_chart(chart_bars),
            _render_components(components),
            _render_rules(rules),
            _render_daily_bars(bars),
        ],
    )


def serve_dashboard(
    db_path: Path = DEFAULT_DB_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    limit: int = 30,
    ths_root: Path = DEFAULT_THS_ROOT,
) -> None:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._write_text("ok\n", "text/plain; charset=utf-8")
                return
            if parsed.path == "/":
                filters = DashboardFilters.from_query(parse_qs(parsed.query))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_dashboard(repo, limit=limit, filters=filters)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/export/candidates.csv":
                filters = DashboardFilters.from_query(parse_qs(parsed.query))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    payload = render_candidates_csv(repo, filters=filters)
                finally:
                    repo.close()
                self._write_text(payload, "text/csv; charset=utf-8-sig")
                return
            if parsed.path == "/export/notes.csv":
                filters = NotesFilters.from_query(parse_qs(parsed.query))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    payload = render_notes_csv(repo, filters=filters)
                finally:
                    repo.close()
                self._write_text(payload, "text/csv; charset=utf-8-sig")
                return
            if parsed.path == "/notes":
                filters = NotesFilters.from_query(parse_qs(parsed.query))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_notes_page(repo, filters=filters)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/ths":
                page = render_ths_monitor_page(ths_root)
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/news":
                query = parse_qs(parsed.query)
                q = (_first(query, "q") or "").strip()
                tag = (_first(query, "tag") or "").strip()
                page_limit = int(_to_float(_first(query, "limit"), 50.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_news_page(repo, query=q, tag=tag, limit=page_limit)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/factors":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 50.0))
                horizon = int(_to_float(_first(query, "horizon"), 5.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_factors_page(repo, limit=page_limit, horizon=horizon)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/ai":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), float(limit)))
                min_score = _to_float(_first(query, "min_score"), 1.0)
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_ai_page(repo, limit=page_limit, min_score=min_score)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/ai/history":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 50.0))
                symbol = (_first(query, "symbol") or "").strip()
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_ai_history_page(repo, limit=page_limit, symbol=symbol)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/ai/changes":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 50.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_ai_changes_page(repo, limit=page_limit)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/symbol/"):
                symbol = parsed.path.rsplit("/", 1)[-1]
                if not (len(symbol) == 6 and symbol.isdigit()):
                    self.send_error(404)
                    return
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_symbol_detail(repo, symbol)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            self.send_error(404)
            return

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/symbol/") and parsed.path.endswith("/note"):
                symbol = parsed.path.split("/")[-2]
                if not (len(symbol) == 6 and symbol.isdigit()):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = self.rfile.read(length).decode("utf-8")
                form = parse_qs(payload)
                status = (_first(form, "status") or "watch").strip()
                if status not in {"watch", "hold", "avoid", "review"}:
                    status = "watch"
                tags = (_first(form, "tags") or "").strip()
                note = (_first(form, "note") or "").strip()
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    repo.upsert_stock_note(symbol, status=status, tags=tags, note=note)
                finally:
                    repo.close()
                self.send_response(303)
                self.send_header("Location", f"/symbol/{symbol}")
                self.end_headers()
                return
            if parsed.path == "/notes/delete":
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = self.rfile.read(length).decode("utf-8")
                form = parse_qs(payload)
                symbol = (_first(form, "symbol") or "").strip()
                return_to = (_first(form, "return_to") or "/notes").strip()
                if not (len(symbol) == 6 and symbol.isdigit()):
                    self.send_error(400)
                    return
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    repo.delete_stock_note(symbol)
                finally:
                    repo.close()
                self.send_response(303)
                self.send_header("Location", _safe_local_redirect(return_to, "/notes"))
                self.end_headers()
                return
            if parsed.path == "/ai/save":
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = self.rfile.read(length).decode("utf-8")
                form = parse_qs(payload)
                page_limit = int(_to_float(_first(form, "limit"), float(limit)))
                min_score = _to_float(_first(form, "min_score"), 1.0)
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    decisions = rank_candidates(repo, limit=page_limit, min_score=min_score)
                    repo.insert_ai_decisions(decisions_to_rows(decisions))
                finally:
                    repo.close()
                self.send_response(303)
                self.send_header("Location", f"/ai/history?limit={page_limit}")
                self.end_headers()
                return
            self.send_error(404)
            return

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_text(self, payload: str, content_type: str) -> None:
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Serving dashboard at http://{host}:{port}/")
    server.serve_forever()


def _render_counts(counts: dict[str, int]) -> str:
    labels = {
        "securities": "证券",
        "market_snapshots": "快照",
        "quotes_realtime": "行情",
        "watchlists": "股票池",
        "scores": "评分",
        "score_runs": "批次",
        "daily_bars": "日线",
    }
    cards = []
    for key, label in labels.items():
        cards.append(
            '<article class="metric">'
            f"<span>{html.escape(label)}</span>"
            f"<strong>{counts.get(key, 0):,}</strong>"
            "</article>"
        )
    return '<section class="metrics">' + "".join(cards) + "</section>"


def _filter_candidate_rows(rows: list[object], filters: DashboardFilters) -> list[object]:
    filtered = [row for row in rows if _row_matches(row, filters)]
    return _sort_rows(filtered, filters.sort)


def _filter_score_rows(rows: list[object], filters: DashboardFilters) -> list[object]:
    filtered = [
        row
        for row in rows
        if _row_matches(row, filters) and float(row["total_score"] or 0) >= filters.min_score
    ]
    return _sort_rows(filtered, filters.sort)


def _row_matches(row: object, filters: DashboardFilters) -> bool:
    if filters.board and (row["board"] or "") != filters.board:
        return False
    if filters.query:
        haystack = f"{row['symbol']} {row['name'] or ''}".lower()
        if filters.query.lower() not in haystack:
            return False
    return True


def _sort_rows(rows: list[object], sort: str) -> list[object]:
    columns = {
        "score": "total_score",
        "amount": "amount",
        "pct": "pct_change",
        "turnover": "turnover_rate",
        "market_cap": "market_cap",
    }
    column = columns.get(sort, "total_score")
    return sorted(rows, key=lambda row: float(row[column] or 0), reverse=True)


def _render_filters(filters: DashboardFilters) -> str:
    board_options = ["", "沪主板", "深主板", "创业板", "科创板"]
    sort_options = [
        ("score", "分数"),
        ("amount", "成交额"),
        ("pct", "涨跌幅"),
        ("turnover", "换手率"),
        ("market_cap", "总市值"),
    ]
    boards = "".join(
        f'<option value="{_e(board)}"{" selected" if board == filters.board else ""}>{_e(board or "全部板块")}</option>'
        for board in board_options
    )
    sorts = "".join(
        f'<option value="{_e(value)}"{" selected" if value == filters.sort else ""}>{_e(label)}</option>'
        for value, label in sort_options
    )
    export_query = filters.to_query_string()
    export_href = "/export/candidates.csv" + (f"?{export_query}" if export_query else "")
    return (
        '<section class="panel filter-panel">'
        '<form class="filters" method="get" action="/">'
        '<label><span>搜索</span>'
        f'<input name="q" value="{_e(filters.query)}" placeholder="代码或名称">'
        "</label>"
        '<label><span>板块</span>'
        f'<select name="board">{boards}</select>'
        "</label>"
        '<label><span>最低分</span>'
        f'<input name="min_score" type="number" step="1" value="{filters.min_score:g}">'
        "</label>"
        '<label><span>排序</span>'
        f'<select name="sort">{sorts}</select>'
        "</label>"
        '<button type="submit">应用</button>'
        '<a class="ghost" href="/">重置</a>'
        f'<a class="ghost" href="{_e(export_href)}">导出 CSV</a>'
        "</form>"
        "</section>"
    )


def _render_candidates(rows: list[object]) -> str:
    body = []
    for index, row in enumerate(rows, start=1):
        rules = _top_rules(row["triggered_rules_json"])
        body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'])}</td>"
            f"<td>{_e(row['board'] or '-')}</td>"
            f"<td class=\"num strong\">{row['total_score']:.2f}</td>"
            f"<td class=\"num\">{_fmt(row['latest_price'])}</td>"
            f"<td class=\"num\">{_fmt(row['pct_change'])}%</td>"
            f"<td class=\"num\">{_fmt_yi(row['amount'])}亿</td>"
            f"<td>{_e(rules)}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="9" class="empty">暂无候选，请先运行 run-daily。</td></tr>')
    return _table_section(
        "候选池",
        ["#", "代码", "名称", "板块", "分数", "现价", "涨跌幅", "成交额", "主要理由"],
        body,
    )


def _render_scores(rows: list[object]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'])}</td>"
            f"<td>{_e(row['board'] or '-')}</td>"
            f"<td class=\"num strong\">{row['total_score']:.2f}</td>"
            f"<td class=\"num\">{_fmt(row['latest_price'])}</td>"
            f"<td class=\"num\">{_fmt(row['turnover_rate'])}%</td>"
            f"<td class=\"num\">{_fmt_yi(row['market_cap'])}亿</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="7" class="empty">暂无评分。</td></tr>')
    return _table_section("评分榜", ["代码", "名称", "板块", "分数", "现价", "换手率", "总市值"], body)


def _render_news_filters(query: str, tag: str, limit: int) -> str:
    tag_options = ["", "业绩预告", "退市风险", "并购投资", "AI算力", "政策监管", "消费", "新能源", "公告"]
    options = "".join(
        f'<option value="{_e(item)}"{" selected" if item == tag else ""}>{_e(item or "全部标签")}</option>'
        for item in tag_options
    )
    return (
        '<section class="panel filter-panel">'
        '<form class="filters news-filters" method="get" action="/news">'
        '<label><span>搜索</span>'
        f'<input name="q" value="{_e(query)}" placeholder="股票名、关键词、来源">'
        "</label>"
        '<label><span>标签</span>'
        f'<select name="tag">{options}</select>'
        "</label>"
        '<label><span>数量</span>'
        f'<input name="limit" type="number" min="1" max="300" value="{limit}">'
        "</label>"
        '<button type="submit">筛选</button>'
        '<a class="ghost" href="/news">重置</a>'
        "</form>"
        "</section>"
    )


def _render_news_table(rows: list[object]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_e(row['event_time'] or '-')}</td>"
            f"<td>{_e(row['title'])}</td>"
            f"<td>{_e(row['tags'] or '-')}</td>"
            f"<td>{_e(row['source'] or '-')}</td>"
            f"<td>{_e(row['summary'] or '-')}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="5" class="empty">暂无资讯，请先运行 import-ths-news。</td></tr>')
    return _table_section("资讯列表", ["时间", "标题", "标签", "来源", "摘要"], body)


def _render_related_news(rows: list[object]) -> str:
    if not rows:
        return '<section class="panel"><h2>相关新闻</h2><p class="empty">暂无匹配相关新闻。</p></section>'
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_e(row['event_time'] or '-')}</td>"
            f"<td>{_e(row['title'])}</td>"
            f"<td>{_e(row['tags'] or '-')}</td>"
            f"<td>{_e(row['summary'] or '-')}</td>"
            "</tr>"
        )
    return _table_section("相关新闻", ["时间", "标题", "标签", "摘要"], body)


def _render_ths_summary(snapshot: THSMonitorSnapshot) -> str:
    status_class = {
        "active": "ok",
        "stale": "warn",
        "offline": "warn",
        "invalid": "danger",
    }.get(snapshot.overall_status, "warn")
    cards = [
        ("状态", f'<span class="pill {status_class}">{_e(snapshot.overall_status)}</span>'),
        ("检查时间", _e(snapshot.checked_at.strftime("%Y-%m-%d %H:%M:%S"))),
        ("同花顺路径", _e(snapshot.root)),
        ("说明", _e(snapshot.message)),
    ]
    rendered = []
    for label, value in cards:
        rendered.append(
            '<article class="metric wide-metric">'
            f"<span>{_e(label)}</span>"
            f"<strong>{value}</strong>"
            "</article>"
        )
    return '<section class="metrics ths-metrics">' + "".join(rendered) + "</section>"


def _render_ths_processes(snapshot: THSMonitorSnapshot) -> str:
    body = []
    for item in snapshot.processes:
        status = "running" if item.running else "stopped"
        pill_class = "ok" if item.running else "warn"
        body.append(
            "<tr>"
            f"<td>{_e(item.name)}</td>"
            f'<td><span class="pill {pill_class}">{_e(status)}</span></td>'
            f"<td>{_e(item.pid or '-')}</td>"
            f"<td>{_e(item.path or '-')}</td>"
            "</tr>"
        )
    return _table_section("进程状态", ["进程", "状态", "PID", "路径"], body)


def _render_ths_files(snapshot: THSMonitorSnapshot) -> str:
    body = []
    for item in snapshot.files:
        pill_class = {
            "active": "ok",
            "stale": "warn",
            "old": "warn",
            "missing": "danger",
        }.get(item.status, "warn")
        age = "-" if item.age_seconds is None else _format_age(item.age_seconds)
        mtime = "-" if item.mtime is None else item.mtime.strftime("%Y-%m-%d %H:%M:%S")
        body.append(
            "<tr>"
            f"<td>{_e(item.market)}</td>"
            f'<td><span class="pill {pill_class}">{_e(item.status)}</span></td>'
            f"<td class=\"num\">{item.size:,}</td>"
            f"<td>{_e(mtime)}</td>"
            f"<td>{_e(age)}</td>"
            f"<td>{_e(item.path)}</td>"
            "</tr>"
        )
    return _table_section("A 股实时缓存", ["市场", "状态", "大小", "更新时间", "距今", "文件"], body)


def _render_factor_controls(limit: int, horizon: int) -> str:
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-filters" method="get" action="/factors">'
        '<label><span>信号数量</span>'
        f'<input name="limit" type="number" min="1" max="200" value="{limit}">'
        "</label>"
        '<label><span>回测周期</span>'
        f'<input name="horizon" type="number" min="1" max="30" value="{horizon}">'
        "</label>"
        '<button type="submit">刷新</button>'
        '<a class="ghost" href="/factors">重置</a>'
        "</form>"
        "</section>"
    )


def _render_factor_definitions() -> str:
    body = []
    for item in factor_definitions():
        body.append(
            "<tr>"
            f"<td>{_e(item.factor_id)}</td>"
            f"<td>{_e(item.name)}</td>"
            f"<td>{_e(item.category)}</td>"
            f"<td>{_e(item.future_function_risk)}</td>"
            f"<td>{_e(item.description)}</td>"
            f"<td>{_e(item.source)}</td>"
            "</tr>"
        )
    return _table_section("因子定义", ["ID", "名称", "类别", "未来函数风险", "逻辑", "来源"], body)


def _render_factor_signals(rows: list[dict[str, object]]) -> str:
    body = []
    for row in rows:
        pill = "danger" if row["direction"] == "risk" else "ok"
        body.append(
            "<tr>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'] or '-')}</td>"
            f"<td>{_e(row['factor_name'])}</td>"
            f"<td><span class=\"pill {pill}\">{_e(row['direction'])}</span></td>"
            f"<td class=\"num\">{float(row['strength']):.1f}</td>"
            f"<td>{_e(row['reason'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="6" class="empty">暂无因子信号，请先导入更多日线数据。</td></tr>')
    return _table_section("当前因子信号", ["代码", "名称", "因子", "方向", "强度", "原因"], body)


def _render_factor_backtest(rows: list[dict[str, object]], horizon: int) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_e(row['factor_id'])}</td>"
            f"<td>{_e(row['factor_name'])}</td>"
            f"<td>{_e(row['category'])}</td>"
            f"<td class=\"num\">{int(row['samples'])}</td>"
            f"<td class=\"num\">{float(row['win_rate']):.1f}%</td>"
            f"<td class=\"num\">{float(row['avg_return']):.2f}%</td>"
            f"<td class=\"num\">{float(row['best_return']):.2f}%</td>"
            f"<td class=\"num\">{float(row['worst_return']):.2f}%</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="8" class="empty">暂无回测样本，请先导入更多历史日线。</td></tr>')
    return _table_section(f"因子回测：未来 {horizon} 个交易日", ["ID", "名称", "类别", "样本", "胜率", "平均收益", "最好", "最差"], body)


def _render_factor_matrix(rows: list[dict[str, object]], horizons: list[int]) -> str:
    headers = ["ID", "名称", "类别", "结论", "评分", "总样本"]
    for horizon in horizons:
        headers.extend([f"{horizon}日样本", f"{horizon}日胜率", f"{horizon}日均收"])
    body = []
    for row in rows:
        verdict_class = {
            "有效": "ok",
            "观察": "warn",
            "反向": "danger",
            "样本不足": "warn",
        }.get(str(row["verdict"]), "warn")
        cells = [
            f"<td>{_e(row['factor_id'])}</td>",
            f"<td>{_e(row['factor_name'])}</td>",
            f"<td>{_e(row['category'])}</td>",
            f'<td><span class="pill {verdict_class}">{_e(row["verdict"])}</span></td>',
            f"<td class=\"num strong\">{float(row['effectiveness_score']):.1f}</td>",
            f"<td class=\"num\">{int(row['total_samples'])}</td>",
        ]
        horizon_stats = row["horizons"] if isinstance(row["horizons"], dict) else {}
        for horizon in horizons:
            stats = horizon_stats.get(horizon, {})
            if stats:
                cells.extend(
                    [
                        f"<td class=\"num\">{int(stats['samples'])}</td>",
                        f"<td class=\"num\">{float(stats['win_rate']):.1f}%</td>",
                        f"<td class=\"num\">{float(stats['avg_return']):.2f}%</td>",
                    ]
                )
            else:
                cells.extend(['<td class="num">-</td>', '<td class="num">-</td>', '<td class="num">-</td>'])
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f'<tr><td colspan="{len(headers)}" class="empty">暂无回测样本，请先导入更多历史日线。</td></tr>')
    return _table_section("因子多周期回测矩阵", headers, body)


def _render_ai_controls(limit: int, min_score: float) -> str:
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-filters" method="get" action="/ai">'
        '<label><span>数量</span>'
        f'<input name="limit" type="number" min="1" max="100" value="{limit}">'
        "</label>"
        '<label><span>最低评分</span>'
        f'<input name="min_score" type="number" step="1" value="{min_score:g}">'
        "</label>"
        '<button type="submit">生成</button>'
        '<a class="ghost" href="/ai">重置</a>'
        '<a class="ghost" href="/ai/history">历史</a>'
        '<a class="ghost" href="/ai/changes">变化</a>'
        "</form>"
        '<form class="inline-toolbar" method="post" action="/ai/save">'
        f'<input type="hidden" name="limit" value="{limit}">'
        f'<input type="hidden" name="min_score" value="{min_score:g}">'
        '<button type="submit">保存本次 AI 榜单</button>'
        "</form>"
        "</section>"
    )


def _render_ai_decisions(decisions: list[AIDecision]) -> str:
    body = []
    for index, item in enumerate(decisions, start=1):
        score = item.evidence.get("total_score")
        pct = item.evidence.get("pct_change")
        factor_text = _factor_badges(item.evidence.get("factor_signals", []))
        body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{_symbol_link(item.symbol)}</td>"
            f"<td>{_e(item.name or '-')}</td>"
            f"<td>{_e(item.board or '-')}</td>"
            f"<td><span class=\"pill ai-{_decision_class(item.decision)}\">{_e(item.decision)}</span></td>"
            f"<td class=\"num strong\">{item.confidence:.0f}</td>"
            f"<td class=\"num\">{_fmt(score)}</td>"
            f"<td class=\"num\">{_fmt(pct)}%</td>"
            f"<td>{factor_text}</td>"
            f"<td>{_e(item.summary)}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="10" class="empty">暂无 AI 候选，请先运行 run-daily 或 score。</td></tr>')
    return _table_section("AI 候选观点", ["#", "代码", "名称", "板块", "结论", "置信度", "评分", "涨跌幅", "公式因子", "摘要"], body)


def _render_ai_history_controls(limit: int, symbol: str) -> str:
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-history-filters" method="get" action="/ai/history">'
        '<label><span>代码</span>'
        f'<input name="symbol" value="{_e(symbol)}" placeholder="可选，如 688981">'
        "</label>"
        '<label><span>数量</span>'
        f'<input name="limit" type="number" min="1" max="300" value="{limit}">'
        "</label>"
        '<button type="submit">筛选</button>'
        '<a class="ghost" href="/ai/history">重置</a>'
        '<a class="ghost" href="/ai/changes">变化</a>'
        "</form>"
        "</section>"
    )


def _render_ai_history(rows: list[object]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>#{row['id']}</td>"
            f"<td>{_e(row['run_at'])}</td>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'] or '-')}</td>"
            f"<td><span class=\"pill ai-{_decision_class(row['decision'])}\">{_e(row['decision'])}</span></td>"
            f"<td class=\"num strong\">{float(row['confidence']):.0f}</td>"
            f"<td>{_e(row['summary'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="7" class="empty">暂无已保存 AI 观点。</td></tr>')
    return _table_section("历史观点", ["ID", "保存时间", "代码", "名称", "结论", "置信度", "摘要"], body)


def _render_ai_changes_controls(limit: int) -> str:
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-history-filters" method="get" action="/ai/changes">'
        '<label><span>数量</span>'
        f'<input name="limit" type="number" min="1" max="300" value="{limit}">'
        "</label>"
        '<button type="submit">刷新</button>'
        '<a class="ghost" href="/ai/history">历史</a>'
        "</form>"
        "</section>"
    )


def _render_ai_changes(rows: list[dict[str, object]]) -> str:
    body = []
    for row in rows:
        previous = row["previous_decision"] or "-"
        delta = float(row["confidence_delta"] or 0)
        delta_class = "pos" if delta > 0 else "neg" if delta < 0 else ""
        body.append(
            "<tr>"
            f"<td><span class=\"pill change-{_e(row['status'])}\">{_ai_change_label(row['status'])}</span></td>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'] or '-')}</td>"
            f"<td>{_e(previous)} -> {_e(row['latest_decision'])}</td>"
            f"<td class=\"num strong\">{float(row['latest_confidence']):.0f}</td>"
            f"<td class=\"num {delta_class}\">{delta:+.0f}</td>"
            f"<td>{_e(row['latest_run_at'])}</td>"
            f"<td>{_e(row['summary'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="8" class="empty">暂无可比较的 AI 观点。</td></tr>')
    return _table_section("AI 观点变化", ["状态", "代码", "名称", "结论变化", "置信度", "变化", "最新保存", "摘要"], body)


def _ai_change_label(status: object) -> str:
    return {
        "changed": "结论变化",
        "new": "新增",
        "confidence": "置信变化",
        "stable": "稳定",
    }.get(str(status), str(status))


def _render_ai_symbol_decision(decision: AIDecision | None) -> str:
    if decision is None:
        return '<section class="panel"><h2>AI 观点</h2><p class="empty">暂无 AI 观点，请先运行评分。</p></section>'
    strengths = "".join(f"<li>{_e(item)}</li>" for item in decision.strengths)
    risks = "".join(f"<li>{_e(item)}</li>" for item in decision.risks)
    actions = "".join(f"<li>{_e(item)}</li>" for item in decision.next_actions)
    factor_table = _render_ai_factor_signals(decision.evidence.get("factor_signals", []))
    return (
        '<section class="panel ai-panel">'
        '<div class="panel-title-row">'
        "<h2>AI 观点</h2>"
        f'<span class="pill ai-{_decision_class(decision.decision)}">{_e(decision.decision)} · 置信度 {decision.confidence:.0f}</span>'
        "</div>"
        f'<p class="ai-summary">{_e(decision.summary)}</p>'
        '<div class="ai-grid">'
        f'<div><h3>正向证据</h3><ul class="rules">{strengths}</ul></div>'
        f'<div><h3>风险点</h3><ul class="rules">{risks}</ul></div>'
        f'<div><h3>下一步</h3><ul class="rules">{actions}</ul></div>'
        "</div>"
        f"{factor_table}"
        "</section>"
    )


def _render_ai_factor_signals(rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return '<div class="subtable"><h3>公式型因子</h3><p class="empty">暂无当前因子信号。</p></div>'
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pill = "danger" if row.get("direction") == "risk" else "ok"
        body.append(
            "<tr>"
            f"<td>{_e(row.get('name', '-'))}</td>"
            f"<td>{_e(row.get('category', '-'))}</td>"
            f"<td><span class=\"pill {pill}\">{_e(row.get('direction', '-'))}</span></td>"
            f"<td class=\"num\">{_fmt(row.get('strength'))}</td>"
            f"<td>{_e(row.get('effectiveness_verdict') or '-')}</td>"
            f"<td class=\"num\">{_fmt(row.get('effectiveness_score'))}</td>"
            f"<td>{_e(row.get('reason', '-'))}</td>"
            "</tr>"
        )
    if not body:
        return '<div class="subtable"><h3>公式型因子</h3><p class="empty">暂无当前因子信号。</p></div>'
    return (
        '<div class="subtable">'
        "<h3>公式型因子</h3>"
        '<div class="table-wrap"><table>'
        "<thead><tr><th>因子</th><th>类别</th><th>方向</th><th>强度</th><th>历史结论</th><th>有效性</th><th>原因</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table></div>"
        "</div>"
    )


def _factor_badges(rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return '<span class="muted">-</span>'
    badges = []
    for row in rows[:3]:
        if not isinstance(row, dict):
            continue
        pill = "danger" if row.get("direction") == "risk" else "ok"
        badges.append(f'<span class="pill {pill}">{_e(row.get("name", "-"))}</span>')
    return "".join(badges) or '<span class="muted">-</span>'


def _decision_class(decision: str) -> str:
    return {
        "重点观察": "strong",
        "观察": "watch",
        "等待回踩": "wait",
        "谨慎复盘": "review",
        "回避": "avoid",
    }.get(decision, "review")


def _render_run_changes(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    body = []
    for row in rows:
        base = "-" if row["base_score"] is None else f"{float(row['base_score']):.2f}"
        target = "-" if row["target_score"] is None else f"{float(row['target_score']):.2f}"
        delta = float(row["delta"] or 0.0)
        delta_class = "pos" if delta > 0 else "neg" if delta < 0 else ""
        body.append(
            "<tr>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'])}</td>"
            f"<td>{_e(row['board'] or '-')}</td>"
            f"<td><span class=\"pill change-{_e(row['status'])}\">{_e(row['status'])}</span></td>"
            f"<td class=\"num\">{base}</td>"
            f"<td class=\"num strong\">{target}</td>"
            f"<td class=\"num {delta_class}\">{delta:+.2f}</td>"
            f"<td class=\"num\">{_e(row['base_rank'] or '-')} -> {_e(row['target_rank'] or '-')}</td>"
            "</tr>"
        )
    first = rows[0]
    title = f"批次变化 #{first['base_run_id']} -> #{first['target_run_id']}"
    return _table_section(title, ["代码", "名称", "板块", "状态", "原分", "新分", "变化", "排名"], body)


def _render_quote_summary(row: object) -> str:
    items = [
        ("总分", f"{row['total_score']:.2f}"),
        ("板块", row["board"] or "-"),
        ("现价", _fmt(row["latest_price"])),
        ("涨跌幅", f"{_fmt(row['pct_change'])}%"),
        ("成交额", f"{_fmt_yi(row['amount'])}亿"),
        ("总市值", f"{_fmt_yi(row['market_cap'])}亿"),
        ("换手率", f"{_fmt(row['turnover_rate'])}%"),
        ("观察时间", row["observed_at"] or "-"),
    ]
    cards = []
    for label, value in items:
        cards.append(
            '<article class="metric">'
            f"<span>{_e(label)}</span>"
            f"<strong>{_e(value)}</strong>"
            "</article>"
        )
    return '<section class="metrics detail-metrics">' + "".join(cards) + "</section>"


def _render_stock_note(symbol: str, note: object | None) -> str:
    status = note["status"] if note is not None else "watch"
    tags = note["tags"] if note is not None else ""
    note_text = note["note"] if note is not None else ""
    options = []
    for value, label in [("watch", "观察"), ("hold", "持有"), ("avoid", "回避"), ("review", "复盘")]:
        selected = " selected" if value == status else ""
        options.append(f'<option value="{value}"{selected}>{label}</option>')
    updated = f'<p class="note-updated">更新于 { _e(note["updated_at"]) }</p>' if note is not None else ""
    return (
        '<section class="panel">'
        "<h2>本地观察记录</h2>"
        f'<form class="note-form" method="post" action="/symbol/{_e(symbol)}/note">'
        "<label><span>状态</span>"
        f'<select name="status">{"".join(options)}</select>'
        "</label>"
        "<label><span>标签</span>"
        f'<input name="tags" value="{_e(tags)}" placeholder="例如：半导体, 放量, 等回踩">'
        "</label>"
        "<label class=\"note-text\"><span>备注</span>"
        f'<textarea name="note" rows="4" placeholder="记录你的观察、触发条件、风险点">{_e(note_text)}</textarea>'
        "</label>"
        '<button type="submit">保存记录</button>'
        f"{updated}"
        "</form>"
        "</section>"
    )


def _render_notes_filter(filters: NotesFilters) -> str:
    items = [("", "全部"), ("watch", "观察"), ("hold", "持有"), ("avoid", "回避"), ("review", "复盘")]
    links = []
    for value, label in items:
        query = filters.to_query_string(status=value)
        href = "/notes" + (f"?{query}" if query else "")
        active = " active" if value == filters.status else ""
        links.append(f'<a class="segment{active}" href="{href}">{label}</a>')
    sort_options = [
        ("updated", "更新时间"),
        ("score", "评分"),
        ("pct", "涨跌幅"),
        ("price", "现价"),
        ("symbol", "代码"),
    ]
    sorts = "".join(
        f'<option value="{_e(value)}"{" selected" if value == filters.sort else ""}>{_e(label)}</option>'
        for value, label in sort_options
    )
    export_query = filters.to_query_string()
    export_href = "/export/notes.csv" + (f"?{export_query}" if export_query else "")
    return (
        '<section class="panel filter-panel">'
        '<nav class="segments">' + "".join(links) + "</nav>"
        '<form class="filters notes-filters" method="get" action="/notes">'
        f'<input type="hidden" name="status" value="{_e(filters.status)}">'
        '<label><span>搜索</span>'
        f'<input name="q" value="{_e(filters.query)}" placeholder="代码、名称、标签或备注">'
        "</label>"
        '<label><span>排序</span>'
        f'<select name="sort">{sorts}</select>'
        "</label>"
        '<button type="submit">应用</button>'
        '<a class="ghost" href="/notes">重置</a>'
        f'<a class="ghost" href="{_e(export_href)}">导出 CSV</a>'
        "</form>"
        "</section>"
    )


def _render_notes_table(rows: list[object], filters: NotesFilters) -> str:
    body = []
    return_query = filters.to_query_string()
    return_to = "/notes" + (f"?{return_query}" if return_query else "")
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'] or '-')}</td>"
            f"<td>{_e(row['board'] or '-')}</td>"
            f"<td><span class=\"pill note-{_e(row['status'])}\">{_status_label(row['status'])}</span></td>"
            f"<td>{_e(row['tags'] or '-')}</td>"
            f"<td>{_e(row['note'] or '-')}</td>"
            f"<td class=\"num\">{_fmt(row['total_score'])}</td>"
            f"<td class=\"num\">{_fmt(row['latest_price'])}</td>"
            f"<td class=\"num\">{_fmt(row['pct_change'])}%</td>"
            f"<td>{_e(row['updated_at'])}</td>"
            '<td class="action-cell">'
            '<form class="inline-form" method="post" action="/notes/delete">'
            f'<input type="hidden" name="symbol" value="{_e(row["symbol"])}">'
            f'<input type="hidden" name="return_to" value="{_e(return_to)}">'
            '<button class="danger-link" type="submit">删除</button>'
            "</form>"
            "</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="11" class="empty">暂无观察记录。</td></tr>')
    return _table_section("观察记录", ["代码", "名称", "板块", "状态", "标签", "备注", "分数", "现价", "涨跌幅", "更新时间", "操作"], body)


def _status_label(status: object) -> str:
    return {
        "watch": "观察",
        "hold": "持有",
        "avoid": "回避",
        "review": "复盘",
    }.get(str(status), str(status))


def _render_components(components: dict[str, float]) -> str:
    rows = []
    for name, value in sorted(components.items(), key=lambda item: (-abs(float(item[1])), item[0])):
        rows.append(
            "<tr>"
            f"<td>{_e(name)}</td>"
            f"<td class=\"num strong\">{float(value):.2f}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="2" class="empty">暂无分项。</td></tr>')
    return _table_section("分项分", ["分项", "分数"], rows)


def _render_rules(rules: list[object]) -> str:
    items = "".join(f"<li>{_e(item)}</li>" for item in rules)
    if not items:
        items = '<li class="empty">暂无触发规则。</li>'
    return '<section class="panel"><h2>触发规则</h2><ul class="rules">' + items + "</ul></section>"


def _render_price_chart(rows: list[object]) -> str:
    bars = list(reversed(rows))
    points = [
        (row["trade_date"], float(row["close"]), float(row["volume"] or 0))
        for row in bars
        if row["close"] is not None
    ]
    if len(points) < 2:
        return '<section class="panel"><h2>日线走势</h2><p class="empty">日线数量不足，暂无法绘图。</p></section>'

    width = 920
    height = 260
    pad_x = 34
    pad_top = 20
    price_height = 150
    volume_top = 192
    volume_height = 46
    closes = [item[1] for item in points]
    volumes = [item[2] for item in points]
    min_close = min(closes)
    max_close = max(closes)
    max_volume = max(volumes) if max(volumes) > 0 else 1.0
    price_span = max(max_close - min_close, 0.01)
    step = (width - pad_x * 2) / max(len(points) - 1, 1)

    line_points = []
    volume_bars = []
    for index, (_, close, volume) in enumerate(points):
        x = pad_x + step * index
        y = pad_top + (max_close - close) / price_span * price_height
        line_points.append(f"{x:.1f},{y:.1f}")
        bar_height = volume / max_volume * volume_height
        volume_bars.append(
            f'<rect x="{x - 2:.1f}" y="{volume_top + volume_height - bar_height:.1f}" '
            f'width="4" height="{bar_height:.1f}" rx="1"></rect>'
        )

    first_date = points[0][0]
    last_date = points[-1][0]
    return (
        '<section class="panel">'
        "<h2>日线走势</h2>"
        '<div class="chart-wrap">'
        f'<svg class="price-chart" viewBox="0 0 {width} {height}" role="img" aria-label="日线走势">'
        f'<text x="{pad_x}" y="14">{_e(f"最高 {max_close:.2f}")}</text>'
        f'<text x="{pad_x}" y="{pad_top + price_height + 16}">{_e(f"最低 {min_close:.2f}")}</text>'
        f'<line x1="{pad_x}" y1="{pad_top}" x2="{width - pad_x}" y2="{pad_top}" class="grid"></line>'
        f'<line x1="{pad_x}" y1="{pad_top + price_height}" x2="{width - pad_x}" y2="{pad_top + price_height}" class="grid"></line>'
        f'<g class="volume-bars">{"".join(volume_bars)}</g>'
        f'<polyline points="{" ".join(line_points)}" class="price-line"></polyline>'
        f'<text x="{pad_x}" y="{height - 10}">{_e(first_date)}</text>'
        f'<text x="{width - pad_x - 90}" y="{height - 10}">{_e(last_date)}</text>'
        "</svg>"
        "</div>"
        "</section>"
    )


def _render_daily_bars(rows: list[object]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_e(row['trade_date'])}</td>"
            f"<td class=\"num\">{_fmt(row['open'])}</td>"
            f"<td class=\"num\">{_fmt(row['high'])}</td>"
            f"<td class=\"num\">{_fmt(row['low'])}</td>"
            f"<td class=\"num\">{_fmt(row['close'])}</td>"
            f"<td class=\"num\">{_fmt(row['volume'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="6" class="empty">暂无日线。</td></tr>')
    return _table_section("最近日线", ["日期", "开盘", "最高", "最低", "收盘", "成交量"], body)


def _render_snapshots(rows: list[object]) -> str:
    body = []
    for row in rows:
        status_class = "ok" if row["status"] == "ok" else "warn"
        body.append(
            "<tr>"
            f"<td>{_e(row['market'])}</td>"
            f'<td><span class="pill {status_class}">{_e(row["status"])}</span></td>'
            f"<td>{_e(row['format_version'] or '-')}</td>"
            f"<td class=\"num\">{row['file_size']}</td>"
            f"<td>{_e(row['read_at'])}</td>"
            f"<td>{_e(row['message'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="6" class="empty">暂无快照诊断。</td></tr>')
    return _table_section("解析诊断", ["市场", "状态", "格式", "大小", "读取时间", "信息"], body)


def _table_section(title: str, headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    return (
        '<section class="panel">'
        f"<h2>{html.escape(title)}</h2>"
        '<div class="table-wrap"><table>'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
        "</section>"
    )


def _page(title: str, body: list[str]) -> str:
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_e(title)}</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            '<main class="shell">',
            *body,
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _topbar(title: str, subtitle: str, back_link: str | None = None) -> str:
    action = f'<a class="refresh" href="{_e(back_link)}">返回</a>' if back_link else '<a class="refresh" href="/">刷新</a>'
    return (
        '<section class="topbar">'
        "<div>"
        f"<h1>{_e(title)}</h1>"
        f"<p>{_e(subtitle)}</p>"
        "</div>"
        f"{action}"
        "</section>"
    )


def _symbol_link(symbol: object) -> str:
    text = _e(symbol)
    return f'<a class="symbol-link" href="/symbol/{text}">{text}</a>'


def _top_rules(raw: str) -> str:
    try:
        rules = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:80]
    return ", ".join(str(item) for item in rules[:4])


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_yi(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) / 100_000_000:.2f}"
    except (TypeError, ValueError):
        return str(value)


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _safe_local_redirect(value: str, fallback: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def _to_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


_CSS = """
:root {
  color-scheme: light;
  font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
  background: #f5f7fa;
  color: #1f2933;
}
* { box-sizing: border-box; }
body { margin: 0; }
.shell { width: min(1440px, calc(100% - 32px)); margin: 0 auto; padding: 24px 0 40px; }
.topbar { display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; margin-bottom: 18px; }
h1 { margin: 0; font-size: 28px; font-weight: 700; }
h2 { margin: 0 0 12px; font-size: 18px; font-weight: 700; }
h3 { margin: 0 0 8px; font-size: 14px; font-weight: 700; color: #334155; }
p { margin: 8px 0 0; color: #5f6c7b; font-size: 13px; }
.refresh {
  display: inline-flex; align-items: center; justify-content: center;
  min-height: 36px; padding: 0 14px; border-radius: 6px;
  color: #0f766e; background: #dff6f1; text-decoration: none; font-weight: 600;
}
.actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.symbol-link { color: #0f766e; font-weight: 700; text-decoration: none; }
.symbol-link:hover, .refresh:hover { text-decoration: underline; }
.metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.detail-metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.ths-metrics { grid-template-columns: minmax(120px, 0.8fr) minmax(180px, 1fr) minmax(240px, 1.4fr) minmax(260px, 1.8fr); }
.metric { background: #ffffff; border: 1px solid #d8e0e8; border-radius: 8px; padding: 14px; }
.metric span { display: block; color: #64748b; font-size: 12px; margin-bottom: 8px; }
.metric strong { font-size: 24px; line-height: 1; }
.wide-metric strong { font-size: 15px; line-height: 1.35; overflow-wrap: anywhere; }
.panel { background: #ffffff; border: 1px solid #d8e0e8; border-radius: 8px; padding: 16px; margin-top: 14px; }
.filter-panel { padding: 12px 16px; }
.filters { display: grid; grid-template-columns: minmax(160px, 1.4fr) repeat(3, minmax(120px, 1fr)) auto auto; gap: 10px; align-items: end; }
.filters label { display: grid; gap: 5px; color: #52616f; font-size: 12px; font-weight: 700; }
.filters input, .filters select {
  width: 100%; height: 36px; border: 1px solid #cbd5e1; border-radius: 6px;
  padding: 0 10px; background: #ffffff; color: #1f2933; font: inherit;
}
.filters button, .ghost {
  height: 36px; border: 0; border-radius: 6px; padding: 0 14px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center; text-decoration: none;
}
.filters button { background: #0f766e; color: #ffffff; cursor: pointer; }
.ghost { color: #475569; background: #e2e8f0; }
.ai-filters { grid-template-columns: minmax(120px, 180px) minmax(120px, 180px) auto auto auto; }
.ai-history-filters { grid-template-columns: minmax(160px, 220px) minmax(120px, 160px) auto auto; }
.news-filters { grid-template-columns: minmax(220px, 1fr) minmax(130px, 180px) minmax(100px, 140px) auto auto; }
.note-form { display: grid; grid-template-columns: minmax(120px, 0.5fr) minmax(220px, 1fr) auto; gap: 10px; align-items: end; }
.note-form label { display: grid; gap: 5px; color: #52616f; font-size: 12px; font-weight: 700; }
.note-form .note-text { grid-column: 1 / -1; }
.note-form input, .note-form select, .note-form textarea {
  width: 100%; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px 10px;
  background: #ffffff; color: #1f2933; font: inherit;
}
.note-form select, .note-form input { height: 36px; }
.note-form textarea { resize: vertical; min-height: 88px; }
.note-form button {
  height: 36px; border: 0; border-radius: 6px; padding: 0 14px;
  background: #0f766e; color: #ffffff; font-weight: 700; cursor: pointer;
}
.note-updated { grid-column: 1 / -1; margin: 0; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 8px; border-bottom: 1px solid #e6edf3; text-align: left; white-space: nowrap; }
th { color: #52616f; font-weight: 700; background: #f8fafc; }
td:last-child { white-space: normal; min-width: 220px; }
.action-cell { white-space: nowrap !important; min-width: 0 !important; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.strong { color: #0f766e; font-weight: 700; }
.pos { color: #0f766e; font-weight: 700; }
.neg { color: #b42318; font-weight: 700; }
.pill { display: inline-flex; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; }
.pill + .pill { margin-left: 4px; }
.pill.ok { color: #166534; background: #dcfce7; }
.pill.warn { color: #92400e; background: #fef3c7; }
.pill.danger { color: #9f1239; background: #ffe4e6; }
.pill.change-new, .pill.change-up { color: #166534; background: #dcfce7; }
.pill.change-down, .pill.change-dropped { color: #9f1239; background: #ffe4e6; }
.pill.change-flat { color: #334155; background: #e2e8f0; }
.pill.change-changed { color: #7c2d12; background: #ffedd5; }
.pill.change-confidence { color: #075985; background: #e0f2fe; }
.pill.change-stable { color: #334155; background: #e2e8f0; }
.pill.note-watch { color: #075985; background: #e0f2fe; }
.pill.note-hold { color: #166534; background: #dcfce7; }
.pill.note-avoid { color: #9f1239; background: #ffe4e6; }
.pill.note-review { color: #7c2d12; background: #ffedd5; }
.pill.ai-strong { color: #166534; background: #dcfce7; }
.pill.ai-watch { color: #075985; background: #e0f2fe; }
.pill.ai-wait { color: #92400e; background: #fef3c7; }
.pill.ai-review { color: #334155; background: #e2e8f0; }
.pill.ai-avoid { color: #9f1239; background: #ffe4e6; }
.segments { display: flex; gap: 8px; flex-wrap: wrap; }
.segment {
  display: inline-flex; align-items: center; justify-content: center; min-height: 34px;
  padding: 0 12px; border-radius: 6px; background: #e2e8f0; color: #334155;
  text-decoration: none; font-weight: 700; font-size: 13px;
}
.segment.active { background: #0f766e; color: #ffffff; }
.notes-filters { margin-top: 12px; grid-template-columns: minmax(220px, 1fr) minmax(130px, 180px) auto auto auto; }
.inline-form { margin: 0; }
.inline-toolbar { display: flex; gap: 8px; margin: 12px 0 0; }
.inline-toolbar button {
  height: 34px; border: 0; border-radius: 6px; padding: 0 12px; cursor: pointer;
  background: #0f766e; color: #ffffff; font-weight: 700;
}
.danger-link {
  height: 30px; border: 0; border-radius: 6px; padding: 0 10px; cursor: pointer;
  color: #9f1239; background: #ffe4e6; font-weight: 700;
}
.panel-title-row { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 8px; }
.ai-summary { color: #334155; font-size: 14px; line-height: 1.7; }
.ai-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }
.subtable { margin-top: 16px; }
.subtable h3 { margin-top: 0; }
.muted { color: #94a3b8; }
.empty { color: #64748b; text-align: center; padding: 28px 8px; }
.rules { margin: 0; padding-left: 18px; color: #334155; line-height: 1.8; }
.chart-wrap { overflow-x: auto; }
.price-chart { width: 100%; min-width: 680px; height: auto; display: block; }
.price-chart text { fill: #64748b; font-size: 12px; }
.price-chart .grid { stroke: #d8e0e8; stroke-width: 1; }
.price-line { fill: none; stroke: #0f766e; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.volume-bars rect { fill: #94a3b8; opacity: 0.55; }
@media (max-width: 900px) {
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ai-grid { grid-template-columns: 1fr; }
  .note-form { grid-template-columns: 1fr; }
  .topbar { align-items: flex-start; flex-direction: column; }
  h1 { font-size: 24px; }
}
""".strip()
