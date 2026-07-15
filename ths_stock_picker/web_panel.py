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
from .storage import DEFAULT_DB_PATH, Repository, summarize_ai_decision_outcomes
from .ths_local import DEFAULT_THS_ROOT
from .ths_monitor import THSMonitorSnapshot, inspect_ths_source
from .time_utils import display_shanghai_time


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
    quote_coverage = repo.latest_score_quote_coverage()
    quote_health = repo.quote_health()
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
            '<div class="actions"><a class="refresh" href="/">刷新</a><a class="refresh primary-action" href="/ai">AI 选股</a></div>',
            "</section>",
            _render_app_nav("/"),
            _render_diagnose_search(),
            _render_daily_data_freshness_notice(repo),
            _render_quote_freshness_notice(repo),
            _render_ai_candidate_availability(quote_coverage, bool(candidates)),
            _render_dashboard_guide(counts, quote_health, len(candidates), len(run_changes)),
            _render_counts(counts),
            _render_filters(active_filters),
            _render_candidates(repo, candidates),
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
    writer.writerow(
        [
            "symbol",
            "name",
            "board",
            "total_score",
            "latest_price",
            "pct_change",
            "amount",
            "turnover_rate",
            "news_count",
            "latest_news_time",
            "latest_news_title",
            "latest_news_tags",
            "latest_news_source",
            "rules",
        ]
    )
    for row in rows:
        news = repo.related_news_for_symbol(str(row["symbol"]), name=row["name"], limit=3)
        news_summary = _csv_news_summary(news)
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
                news_summary["news_count"],
                news_summary["latest_news_time"],
                news_summary["latest_news_title"],
                news_summary["latest_news_tags"],
                news_summary["latest_news_source"],
                " | ".join(json.loads(row["triggered_rules_json"])),
            ]
        )
    return output.getvalue()


def _csv_news_summary(rows: list[object]) -> dict[str, object]:
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
            _render_context_help("notes"),
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
    quote_coverage = repo.latest_score_quote_coverage()
    return _page(
        "AI 选股",
        [
            _topbar("AI 选股", "基于评分、行情、日线和本地备注生成的结构化选股观点", back_link="/"),
            _render_context_help("ai"),
            _render_daily_data_freshness_notice(repo),
            _render_quote_freshness_notice(repo),
            _render_ai_candidate_availability(quote_coverage, bool(decisions)),
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
            _render_context_help("factors"),
            _render_daily_data_freshness_notice(repo),
            _render_factor_controls(limit, horizon),
            _render_factor_definitions(),
            _render_factor_signals(repo.factor_scan(limit=limit, use_cache=True)),
            _render_factor_matrix(
                repo.factor_backtest_matrix(horizons=matrix_horizons, limit_symbols=30, max_bars=150, use_cache=True),
                matrix_horizons,
            ),
        ],
    )


def render_factor_detail_page(repo: Repository, factor_id: str) -> str:
    definition = next((item for item in factor_definitions() if item.factor_id == factor_id), None)
    if definition is None:
        return _page(
            "因子不存在",
            [
                _topbar("因子不存在", "请求的因子定义不存在或已被移除", back_link="/factors"),
                '<section class="panel"><p class="empty">未找到该因子。</p></section>',
            ],
        )
    current_signals = [
        row for row in repo.factor_scan(limit=50, use_cache=True) if row.get("factor_id") == factor_id
    ]
    matrix = next(
        (
            row
            for row in repo.factor_backtest_matrix(
                horizons=[3, 5, 10],
                limit_symbols=30,
                max_bars=150,
                use_cache=True,
            )
            if row.get("factor_id") == factor_id
        ),
        None,
    )
    return _page(
        f"因子详情：{definition.name}",
        [
            _topbar(definition.name, "因子逻辑、当前命中与历史表现", back_link="/factors"),
            _render_daily_data_freshness_notice(repo),
            _render_factor_detail_summary(definition, current_signals, matrix),
            _render_factor_detail_signals(current_signals),
            _render_factor_detail_history(matrix),
        ],
    )


def render_backtest_page(
    repo: Repository,
    horizon: int = 5,
    top_n: int = 10,
    min_signal_score: float = 60.0,
    limit_symbols: int = 300,
    cost_bps: float = 0.0,
    slippage_bps: float = 0.0,
    benchmark_symbol: str = "",
    max_bars: int = 260,
    execution_mode: str = "next_open",
    position_mode: str = "non_overlapping",
) -> str:
    selected_max_bars = None if max_bars <= 0 else max_bars
    selected_execution_mode = execution_mode if execution_mode in {"next_open", "signal_close"} else "next_open"
    selected_position_mode = position_mode if position_mode in {"non_overlapping", "daily_batches"} else "non_overlapping"
    result = repo.strategy_backtest(
        horizon_days=horizon,
        top_n=top_n,
        min_signal_score=min_signal_score,
        limit_symbols=limit_symbols,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
        benchmark_symbol=benchmark_symbol,
        max_bars=selected_max_bars,
        execution_mode=selected_execution_mode,
        position_mode=selected_position_mode,
    )
    benchmark_indices = repo.available_benchmark_indices()
    return _page(
        "策略回测",
        [
            _topbar("策略回测", "用真实日线按当日因子信号选股，并统计未来持有收益", back_link="/"),
            _render_context_help("backtest"),
            _render_daily_data_freshness_notice(repo),
            _render_backtest_controls(
                horizon,
                selected_execution_mode,
                selected_position_mode,
                top_n,
                min_signal_score,
                limit_symbols,
                cost_bps,
                slippage_bps,
                benchmark_symbol,
                benchmark_indices,
                max_bars,
            ),
            _render_backtest_summary(result),
            _render_backtest_equity_chart(result),
            _render_backtest_trades(result),
            _render_backtest_daily_returns(result),
            _render_backtest_period_stats(result, "yearly", "年度批次表现"),
            _render_backtest_period_stats(result, "monthly", "月度批次表现"),
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


def render_ai_outcomes_page(
    repo: Repository,
    limit: int = 50,
    horizon: int = 5,
    symbol: str = "",
) -> str:
    selected_symbol = symbol if len(symbol) == 6 and symbol.isdigit() else ""
    selected_horizon = max(1, min(horizon, 60))
    rows = repo.ai_decision_outcomes(
        limit=limit,
        horizon_days=selected_horizon,
        symbol=selected_symbol or None,
    )
    summary = summarize_ai_decision_outcomes(rows)
    return _page(
        "AI 观点复盘",
        [
            _topbar("AI 观点复盘", "按保存时评分日期，观察后续日线表现", back_link="/ai/history"),
            _render_context_help("ai_outcomes"),
            _render_daily_data_freshness_notice(repo),
            _render_ai_outcome_controls(limit, selected_horizon, selected_symbol),
            _render_ai_outcome_summary(summary),
            _render_ai_outcomes(rows),
        ],
    )


def render_ths_monitor_page(ths_root: Path = DEFAULT_THS_ROOT) -> str:
    snapshot = inspect_ths_source(ths_root)
    return _page(
        "同花顺数据源",
        [
            _topbar("同花顺数据源", "只读监控同花顺进程和本地实时缓存状态", back_link="/"),
            _render_context_help("ths"),
            _render_ths_summary(snapshot),
            _render_ths_processes(snapshot),
            _render_ths_files(snapshot),
        ],
    )


def render_data_health_page(repo: Repository) -> str:
    health = repo.daily_bar_health()
    fundamental_health = repo.fundamental_health()
    industry_health = repo.industry_health()
    return _page(
        "日线数据健康",
        [
            _topbar("数据健康", "检查日线来源、财务披露与当前行业归属覆盖", back_link="/"),
            _render_data_health_summary(health),
            _render_data_health_sources(health),
            _render_fundamental_health(fundamental_health),
            _render_industry_health(industry_health),
        ],
    )


def render_themes_page(repo: Repository, limit: int = 50, category: str = "", min_scored: int = 3) -> str:
    selected_category = category if category in {"概念", "风格"} else ""
    selected_min_scored = max(1, min_scored)
    summary = repo.theme_heat(limit=limit, category=selected_category, min_scored=selected_min_scored)
    count = repo.table_counts().get("stock_themes", 0)
    score_date = summary.get("score_date") or "暂无评分批次"
    price_as_of_date = summary.get("price_as_of_date") or "暂无足够日线"
    return _page(
        "主题热度",
        [
            _topbar("主题热度", "基于本地主题成员、评分与等权价格表现汇总，不等同于官方板块指数", back_link="/"),
            _render_theme_controls(limit, selected_category, selected_min_scored),
            _render_theme_summary(count, str(score_date), str(price_as_of_date), selected_min_scored),
            _render_theme_heat(summary.get("items", [])),
        ],
    )


def render_industries_page(repo: Repository, limit: int = 50, min_scored: int = 3) -> str:
    selected_min_scored = max(1, min_scored)
    summary = repo.industry_heat(limit=limit, min_scored=selected_min_scored)
    count = repo.table_counts().get("stock_industries", 0)
    score_date = summary.get("score_date") or "暂无评分批次"
    return _page(
        "行业概览",
        [
            _topbar("行业概览", "公开行业标签与当前本地评分汇总，仅作研究上下文", back_link="/"),
            _render_industry_controls(limit, selected_min_scored),
            _render_industry_summary(count, str(score_date), selected_min_scored),
            _render_industry_heat(summary.get("items", [])),
        ],
    )


def render_strategy_validation_page(repo: Repository, limit: int = 30) -> str:
    rows = repo.strategy_validation_runs(limit=limit)
    return _page(
        "策略样本外验证",
        [
            _topbar("策略样本外验证", "已保存的滚动样本外结论与可复核运行参数", back_link="/backtest"),
            _render_context_help("validation"),
            _render_strategy_validation_runs(rows),
        ],
    )


def render_strategy_backtest_runs_page(
    repo: Repository,
    limit: int = 30,
    saved_run_id: int = 0,
) -> str:
    rows = repo.strategy_backtest_runs(limit=limit)
    saved_notice = ""
    if saved_run_id > 0:
        saved_notice = f'<section class="panel"><p>已保存策略回测记录 #{saved_run_id}。</p></section>'
    return _page(
        "已保存策略回测",
        [
            _topbar("已保存策略回测", "保存参数、结果摘要和日线版本，便于后续复核", back_link="/backtest"),
            _render_context_help("backtest_runs"),
            saved_notice,
            _render_strategy_backtest_runs(rows),
        ],
    )


def render_strategy_backtest_run_detail_page(repo: Repository, run_id: int) -> str:
    row = repo.strategy_backtest_run(run_id)
    if row is None:
        return _page(
            "策略回测记录不存在",
            [
                _topbar("策略回测记录不存在", "请求的保存记录不存在或已被删除", back_link="/strategy-backtest-runs"),
                '<section class="panel"><p class="empty">未找到该策略回测记录。</p></section>',
            ],
        )
    try:
        result = json.loads(str(row["result_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    if not result:
        return _page(
            "策略回测记录",
            [
                _topbar("策略回测记录", "保存结果无法读取", back_link="/strategy-backtest-runs"),
                '<section class="panel"><p class="empty">此记录的完整结果不可用。</p></section>',
            ],
        )
    try:
        parameters = json.loads(str(row["parameters_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        parameters = {}
    if not isinstance(parameters, dict):
        parameters = {}
    parameter_text = (
        f"持有 {parameters.get('horizon_days', '-')} 日，Top {parameters.get('top_n', '-')}，"
        f"最低信号分 {parameters.get('min_signal_score', '-')}，{parameters.get('execution_mode', '-')} / "
        f"{parameters.get('position_mode', '-')}"
    )
    return _page(
        f"策略回测记录 #{row['id']}",
        [
            _topbar(
                f"策略回测记录 #{row['id']}",
                f"保存于 {display_shanghai_time(row['run_at'])} · 日线版本 {row['data_fingerprint']}",
                back_link="/strategy-backtest-runs",
            ),
            f'<section class="panel"><p>{_e(parameter_text)}</p></section>',
            _render_backtest_summary(result),
            _render_backtest_equity_chart(result),
            _render_backtest_trades(result),
            _render_backtest_daily_returns(result),
            _render_backtest_period_stats(result, "yearly", "年度批次表现"),
            _render_backtest_period_stats(result, "monthly", "月度批次表现"),
        ],
    )


def render_daily_runs_page(repo: Repository, limit: int = 30) -> str:
    rows = repo.daily_runs(limit=limit)
    return _page(
        "每日运行记录",
        [
            _topbar("每日运行记录", "数据更新、评分和导出流水线的本地执行记录", back_link="/"),
            _render_context_help("daily_runs"),
            _render_daily_runs(rows),
        ],
    )


def render_news_page(repo: Repository, query: str = "", tag: str = "", limit: int = 50) -> str:
    rows = repo.latest_news(limit=limit, query=query, tag=tag)
    return _page(
        "资讯",
        [
            _topbar("资讯", "同花顺本地资讯缓存解析结果", back_link="/"),
            _render_context_help("news"),
            _render_news_filters(query, tag, limit),
            _render_news_table(rows),
        ],
    )


def render_symbol_detail(repo: Repository, symbol: str, bars_limit: int = 20) -> str:
    detail = _symbol_detail_sections(repo, symbol, bars_limit=bars_limit)
    if detail is None:
        return _page(
            "个股详情",
            [
                _topbar("个股详情", f"{symbol} 暂无最新评分", back_link="/"),
                '<section class="panel"><p class="empty">暂无评分，请先运行 run-daily 或 score。</p></section>',
            ],
        )
    title, sections = detail
    return _page(
        title,
        [
            _topbar(title, sections[0], back_link="/"),
            *sections[1:],
        ],
    )


def render_diagnose_page(repo: Repository, symbol: str = "", bars_limit: int = 20) -> str:
    selected_symbol = symbol.strip()
    body = [
        _topbar("一键诊股", "输入 6 位 A 股代码，汇总评分、AI 观点、触发条件、失效条件和本地备注", back_link="/"),
        _render_diagnose_search(selected_symbol),
    ]
    if not selected_symbol:
        body.append('<section class="panel"><p class="empty">请输入代码后开始诊断。</p></section>')
        return _page("一键诊股", body)
    if not (len(selected_symbol) == 6 and selected_symbol.isdigit()):
        body.append('<section class="panel"><p class="empty">代码格式不正确，请输入 6 位数字。</p></section>')
        return _page("一键诊股", body)

    detail = _symbol_detail_sections(repo, selected_symbol, bars_limit=bars_limit)
    if detail is None:
        body.append('<section class="panel"><p class="empty">暂无评分，请先运行 run-daily 或 score。</p></section>')
        return _page("一键诊股", body)
    _, sections = detail
    body.extend(sections[1:])
    return _page("一键诊股", body)


def _symbol_detail_sections(repo: Repository, symbol: str, bars_limit: int) -> tuple[str, list[str]] | None:
    row = repo.score_explanation(symbol)
    if row is None:
        return None
    components = json.loads(row["components_json"])
    rules = json.loads(row["triggered_rules_json"])
    bars = repo.recent_daily_bars(symbol, limit=bars_limit)
    chart_bars = repo.recent_daily_bars(symbol, limit=60)
    note = repo.stock_note(symbol)
    fundamental = repo.latest_fundamental(symbol)
    industry = repo.industry_for_symbol(symbol)
    ai_decision = analyze_symbol(repo, symbol)
    related_news = repo.related_news_for_symbol(symbol, name=row["name"], limit=8)
    themes = repo.themes_for_symbol(symbol)
    if not related_news and ai_decision is not None:
        related_news = ai_decision.evidence.get("news", [])
    title = f"{row['symbol']} {row['name'] or ''}".strip()
    return (
        title,
        [
            f"评分日期 {row['score_date']} · 数据只读 · 不构成投资建议",
            _render_quote_summary(row),
            _render_diagnosis_data_status(row, chart_bars, related_news, note, ai_decision),
            _render_ai_symbol_decision(ai_decision),
            _render_stock_note(symbol, note),
            _render_fundamental(fundamental),
            _render_industry_context(industry),
            _render_related_news(related_news),
            _render_symbol_themes(themes),
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
            if parsed.path == "/data-health":
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_data_health_page(repo)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/diagnose":
                query = parse_qs(parsed.query)
                symbol = (_first(query, "symbol") or "").strip()
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_diagnose_page(repo, symbol=symbol)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/themes":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 50.0))
                category = _first(query, "category") or ""
                min_scored = int(_to_float(_first(query, "min_scored"), 3.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_themes_page(repo, limit=page_limit, category=category, min_scored=min_scored)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/industries":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 50.0))
                min_scored = int(_to_float(_first(query, "min_scored"), 3.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_industries_page(repo, limit=page_limit, min_scored=min_scored)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/strategy-validation":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 30.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_strategy_validation_page(repo, limit=page_limit)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/strategy-backtest-runs":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 30.0))
                saved_run_id = int(_to_float(_first(query, "saved"), 0.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_strategy_backtest_runs_page(repo, limit=page_limit, saved_run_id=saved_run_id)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/strategy-backtest-runs/"):
                run_id_text = parsed.path.rsplit("/", 1)[-1]
                if not run_id_text.isdigit():
                    self.send_error(404)
                    return
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_strategy_backtest_run_detail_page(repo, int(run_id_text))
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/daily-runs":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 30.0))
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_daily_runs_page(repo, limit=page_limit)
                finally:
                    repo.close()
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
            if parsed.path.startswith("/factors/"):
                factor_id = parsed.path.rsplit("/", 1)[-1]
                if factor_id not in {item.factor_id for item in factor_definitions()}:
                    self.send_error(404)
                    return
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_factor_detail_page(repo, factor_id)
                finally:
                    repo.close()
                self._write_text(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/backtest":
                query = parse_qs(parsed.query)
                horizon = int(_to_float(_first(query, "horizon"), 5.0))
                top_n = int(_to_float(_first(query, "top_n"), 10.0))
                min_signal_score = _to_float(_first(query, "min_signal_score"), 60.0)
                limit_symbols = int(_to_float(_first(query, "limit_symbols"), 300.0))
                cost_bps = _to_float(_first(query, "cost_bps"), 0.0)
                slippage_bps = _to_float(_first(query, "slippage_bps"), 0.0)
                benchmark_symbol = _first(query, "benchmark_symbol")
                max_bars = int(_to_float(_first(query, "max_bars"), 260.0))
                execution_mode = _first(query, "execution") or "next_open"
                position_mode = _first(query, "position_mode") or "non_overlapping"
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_backtest_page(
                        repo,
                        horizon=horizon,
                        top_n=top_n,
                        min_signal_score=min_signal_score,
                        limit_symbols=limit_symbols,
                        cost_bps=cost_bps,
                        slippage_bps=slippage_bps,
                        benchmark_symbol=benchmark_symbol,
                        max_bars=max_bars,
                        execution_mode=execution_mode,
                        position_mode=position_mode,
                    )
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
            if parsed.path == "/ai/outcomes":
                query = parse_qs(parsed.query)
                page_limit = int(_to_float(_first(query, "limit"), 50.0))
                horizon = int(_to_float(_first(query, "horizon"), 5.0))
                symbol = (_first(query, "symbol") or "").strip()
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    page = render_ai_outcomes_page(
                        repo,
                        limit=page_limit,
                        horizon=horizon,
                        symbol=symbol,
                    )
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
            if parsed.path == "/notes/quick-add":
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = self.rfile.read(length).decode("utf-8")
                form = parse_qs(payload)
                symbol = (_first(form, "symbol") or "").strip()
                status = (_first(form, "status") or "watch").strip()
                tags = (_first(form, "tags") or "AI候选").strip()
                note = (_first(form, "note") or "").strip()
                return_to = (_first(form, "return_to") or "/notes").strip()
                if status not in {"watch", "hold", "avoid", "review"}:
                    status = "watch"
                if not (len(symbol) == 6 and symbol.isdigit()):
                    self.send_error(400)
                    return
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    repo.upsert_stock_note(symbol, status=status, tags=tags, note=note)
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
            if parsed.path == "/backtest/save":
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = self.rfile.read(length).decode("utf-8")
                form = parse_qs(payload)
                horizon = int(_to_float(_first(form, "horizon"), 5.0))
                top_n = int(_to_float(_first(form, "top_n"), 10.0))
                min_signal_score = _to_float(_first(form, "min_signal_score"), 60.0)
                limit_symbols = int(_to_float(_first(form, "limit_symbols"), 300.0))
                cost_bps = _to_float(_first(form, "cost_bps"), 0.0)
                slippage_bps = _to_float(_first(form, "slippage_bps"), 0.0)
                benchmark_symbol = (_first(form, "benchmark_symbol") or "").strip()
                max_bars = int(_to_float(_first(form, "max_bars"), 260.0))
                execution_mode = _first(form, "execution") or "next_open"
                position_mode = _first(form, "position_mode") or "non_overlapping"
                selected_max_bars = None if max_bars <= 0 else max_bars
                parameters: dict[str, object] = {
                    "horizon_days": horizon,
                    "top_n": top_n,
                    "min_signal_score": min_signal_score,
                    "limit_symbols": limit_symbols,
                    "cost_bps": cost_bps,
                    "slippage_bps": slippage_bps,
                    "benchmark_symbol": benchmark_symbol or None,
                    "max_bars": selected_max_bars,
                    "execution_mode": execution_mode,
                    "position_mode": position_mode,
                }
                repo = Repository(db_path)
                try:
                    repo.init_schema()
                    result = repo.strategy_backtest(
                        horizon_days=horizon,
                        top_n=top_n,
                        min_signal_score=min_signal_score,
                        limit_symbols=limit_symbols,
                        cost_bps=cost_bps,
                        slippage_bps=slippage_bps,
                        benchmark_symbol=benchmark_symbol or None,
                        max_bars=selected_max_bars,
                        execution_mode=execution_mode,
                        position_mode=position_mode,
                    )
                    run_id = repo.save_strategy_backtest_run(parameters, result)
                finally:
                    repo.close()
                self.send_response(303)
                self.send_header("Location", f"/strategy-backtest-runs?saved={run_id}")
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
        "fundamentals": "财务",
        "stock_themes": "主题",
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


def _render_dashboard_guide(
    counts: dict[str, int],
    quote_health: dict[str, object],
    candidate_count: int,
    change_count: int,
) -> str:
    has_daily_bars = counts.get("daily_bars", 0) > 0
    has_priced_quotes = int(quote_health.get("priced_symbols") or 0) > 0
    if has_daily_bars and has_priced_quotes:
        status_text, status_class = "数据已接入", "ok"
    elif has_daily_bars:
        status_text, status_class = "行情待补齐", "warn"
    elif has_priced_quotes:
        status_text, status_class = "日线待补齐", "warn"
    else:
        status_text, status_class = "等待数据", "warn"
    steps = [
        ("1", "数据更新", "确认同花顺缓存、行情、日线和新闻是否可用", "/ths"),
        ("2", "AI 选股", "查看当前候选、结论、正向证据和风险点", "/ai"),
        ("3", "因子验证", "检查公式思路是否真的有历史表现", "/factors"),
        ("4", "策略回测", "按信号分选股并统计未来持有收益", "/backtest"),
        ("5", "观察复盘", "把要跟踪的股票加入观察池并记录原因", "/notes"),
    ]
    rendered_steps = []
    for number, title, text, href in steps:
        rendered_steps.append(
            f'<a class="workflow-step" href="{_e(href)}">'
            f'<span class="step-index">{_e(number)}</span>'
            "<strong>" + _e(title) + "</strong>"
            "<small>" + _e(text) + "</small>"
            "</a>"
        )
    return (
        '<section class="hero-panel">'
        '<div class="hero-copy">'
        '<span class="eyebrow">A 股 AI 选股工作台</span>'
        "<h2>从真实数据出发，先筛选，再验证，最后复盘。</h2>"
        "<p>当前面板把同花顺本地数据、公开行情、公式型因子、新闻消息和策略回测放在同一条决策链里。</p>"
        '<div class="hero-actions">'
        '<a class="refresh primary-action" href="/ai">查看 AI 选股</a>'
        '<a class="refresh" href="/backtest">运行策略回测</a>'
        '<a class="refresh" href="/factors">验证因子</a>'
        "</div>"
        "</div>"
        '<div class="hero-status">'
        f'<span class="pill {status_class}">{_e(status_text)}</span>'
        f"<strong>{candidate_count}</strong><small>当前候选</small>"
        f"<strong>{change_count}</strong><small>批次变化</small>"
        "</div>"
        "</section>"
        '<section class="workflow">' + "".join(rendered_steps) + "</section>"
    )


def _render_context_help(kind: str) -> str:
    copy = {
        "ai": (
            "看什么",
            "优先看“结论、置信度、公式因子、摘要”。点代码进入详情页，核对趋势、新闻、因子历史结论和本地备注。",
            [("保存榜单", "/ai/history"), ("看策略回测", "/backtest"), ("看因子", "/factors")],
        ),
        "ai_outcomes": (
            "如何复盘",
            "评分日之后以首个有效交易日开盘作为观察起点，第 N 个后续交易日收盘计算收益。同一代码同一评分日只保留最新保存版本，日线未更新或观察期未满时只显示待观察。",
            [("历史观点", "/ai/history"), ("观点变化", "/ai/changes"), ("日线健康", "/data-health")],
        ),
        "factors": (
            "怎么用",
            "公式只作为因子灵感。先看当前命中，再看多周期回测矩阵；历史结论为反向的因子不应盲目加分。",
            [("策略回测", "/backtest"), ("AI 选股", "/ai"), ("资讯", "/news")],
        ),
        "backtest": (
            "怎么回测",
            "设置持有天数、每日入选数量和最低信号分。回测只用当日以前数据选股，未来收益仅用于统计。",
            [("查看因子矩阵", "/factors"), ("样本外验证", "/strategy-validation"), ("已保存回测", "/strategy-backtest-runs")],
        ),
        "backtest_runs": (
            "如何复核",
            "每条记录固定保存回测参数、日线版本指纹和结果摘要。日线更新后版本会变化，旧结果不应当作新数据上的结论。",
            [("运行回测", "/backtest"), ("样本外验证", "/strategy-validation"), ("日线健康", "/data-health")],
        ),
        "validation": (
            "如何解读",
            "每次记录都在独立测试窗口评估策略。未通过会否定当前假设；基准缺失或覆盖不完整时，结论最多为观察。",
            [("策略回测", "/backtest"), ("因子验证", "/factors"), ("日线健康", "/data-health")],
        ),
        "daily_runs": (
            "运行记录",
            "每天更新后先确认状态，再查看日线导入量和输出文件。失败记录会保留停止的步骤与错误信息，便于定位数据源或网络问题。",
            [("数据源", "/ths"), ("日线健康", "/data-health"), ("AI 选股", "/ai")],
        ),
        "notes": (
            "怎么复盘",
            "观察池保存你的人工判断。把 AI 候选、回测表现和新闻催化写成标签或备注，后续对比观点变化。",
            [("AI 选股", "/ai"), ("历史观点", "/ai/history"), ("策略回测", "/backtest")],
        ),
        "ths": (
            "数据源",
            "这里确认同花顺是否正在运行、A 股实时缓存是否活跃。只读监控，不接交易、不读取账号敏感文件。",
            [("资讯", "/news"), ("首页", "/"), ("AI 选股", "/ai")],
        ),
        "news": (
            "消息面",
            "这里查看同花顺本地资讯缓存和公开公告兜底数据。AI 会把个股相关新闻或行业主题新闻作为辅助证据。",
            [("AI 选股", "/ai"), ("因子", "/factors"), ("观察池", "/notes")],
        ),
    }.get(kind)
    if copy is None:
        return ""
    title, text, actions = copy
    links = "".join(f'<a class="ghost" href="{_e(href)}">{_e(label)}</a>' for label, href in actions)
    return (
        '<section class="guide-panel">'
        "<div>"
        f"<h2>{_e(title)}</h2>"
        f"<p>{_e(text)}</p>"
        "</div>"
        f'<div class="guide-actions">{links}</div>'
        "</section>"
    )


def _render_ai_candidate_availability(coverage: dict[str, object], has_candidates: bool) -> str:
    score_count = int(coverage.get("score_count") or 0)
    priced_score_count = int(coverage.get("priced_score_count") or 0)
    if has_candidates or score_count == 0 or priced_score_count > 0:
        return ""
    score_date = _e(coverage.get("score_date") or "-")
    return (
        '<section class="data-freshness-warning">'
        "<div>"
        "<h2>AI 候选暂不可用</h2>"
        f"<p>最新评分批次 {score_date} 有 {score_count} 个评分，带价格行情 {priced_score_count} / {score_count}。"
        "缺少可用价格行情时不会生成候选，以免基于空行情给出结论。</p>"
        "</div>"
        '<a class="ghost" href="/daily-runs">查看每日记录</a>'
        "</section>"
    )


def _render_daily_data_freshness_notice(repo: Repository) -> str:
    freshness = repo.daily_bar_freshness()
    if freshness.get("freshness_status") != "lagging":
        return ""
    latest_trade_date = _e(freshness.get("latest_trade_date") or "-")
    weekday_lag_days = int(freshness.get("weekday_lag_days") or 0)
    return (
        '<section class="data-freshness-warning">'
        "<div>"
        "<h2>日线时效提醒</h2>"
        f"<p>最近股票日线为 {latest_trade_date}，按工作日粗略估算可能滞后 {weekday_lag_days} 个工作日。"
        "当前研究结果基于这批日线，请更新后再作判断。</p>"
        "</div>"
        '<a class="ghost" href="/data-health">查看日线健康</a>'
        "</section>"
    )


def _render_quote_freshness_notice(repo: Repository) -> str:
    freshness = repo.quote_health()
    freshness_status = freshness.get("freshness_status")
    if freshness_status not in {"lagging", "partial"}:
        return ""
    latest_price_date = _e(freshness.get("latest_price_date") or "-")
    weekday_lag_days = int(freshness.get("weekday_lag_days") or 0)
    priced_symbols = int(freshness.get("priced_symbols") or 0)
    current_symbols = int(freshness.get("current_priced_symbols") or 0)
    stale_symbols = int(freshness.get("stale_priced_symbols") or 0)
    if freshness_status == "partial":
        detail = f"带价格行情共 {priced_symbols} 只，其中 {current_symbols} 只近 1 个工作日已刷新，{stale_symbols} 只可能过期。"
    else:
        detail = f"最近带价格行情为 {latest_price_date}，覆盖 {priced_symbols} 只股票，按工作日粗略估算可能滞后 {weekday_lag_days} 个工作日。"
    return (
        '<section class="data-freshness-warning">'
        "<div>"
        "<h2>行情时效提醒</h2>"
        f"<p>{detail}"
        "保留报价可防止网络短暂失败清空候选，但应更新后再作判断。</p>"
        "</div>"
        '<a class="ghost" href="/daily-runs">查看每日记录</a>'
        "</section>"
    )


def _render_app_nav(active: str) -> str:
    items = [
        ("/", "总览"),
        ("/daily-runs", "每日记录"),
        ("/diagnose", "一键诊股"),
        ("/ai", "AI 选股"),
        ("/backtest", "策略回测"),
        ("/strategy-validation", "样本外验证"),
        ("/factors", "因子验证"),
        ("/themes", "主题热度"),
        ("/industries", "行业概览"),
        ("/news", "资讯"),
        ("/notes", "观察池"),
        ("/ths", "数据源"),
        ("/data-health", "日线健康"),
    ]
    links = []
    for href, label in items:
        current = " active" if href == active else ""
        links.append(f'<a class="nav-link{current}" href="{_e(href)}">{_e(label)}</a>')
    return '<nav class="app-nav">' + "".join(links) + "</nav>"


def _render_diagnose_search(symbol: str = "") -> str:
    return (
        '<section class="panel filter-panel diagnose-panel">'
        '<form class="filters diagnose-form" method="get" action="/diagnose">'
        "<label><span>股票代码</span>"
        f'<input name="symbol" value="{_e(symbol)}" placeholder="例如 688981" inputmode="numeric" pattern="[0-9]{{6}}">'
        "</label>"
        '<button type="submit">诊股</button>'
        '<a class="ghost" href="/ai">AI 选股</a>'
        '<a class="ghost" href="/notes">观察池</a>'
        "</form>"
        "</section>"
    )


def _active_path_for_title(title: str) -> str:
    if title.startswith("AI"):
        return "/ai"
    if "诊股" in title:
        return "/diagnose"
    if "每日运行记录" in title:
        return "/daily-runs"
    if "样本外验证" in title:
        return "/strategy-validation"
    if "回测" in title:
        return "/backtest"
    if "因子" in title:
        return "/factors"
    if "主题热度" in title:
        return "/themes"
    if "行业概览" in title:
        return "/industries"
    if "资讯" in title:
        return "/news"
    if "观察" in title:
        return "/notes"
    if "同花顺" in title:
        return "/ths"
    if "日线数据健康" in title:
        return "/data-health"
    return "/"


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


def _render_candidates(repo: Repository, rows: list[object]) -> str:
    body = []
    for index, row in enumerate(rows, start=1):
        rules = _top_rules(row["triggered_rules_json"])
        news_label = _candidate_news_label(repo.related_news_for_symbol(str(row["symbol"]), name=row["name"], limit=3))
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
            f"<td>{_e(news_label)}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="10" class="empty">暂无候选，请先运行 run-daily。</td></tr>')
    return _table_section(
        "候选池",
        ["#", "代码", "名称", "板块", "分数", "现价", "涨跌幅", "成交额", "主要理由", "消息面"],
        body,
    )


def _candidate_news_label(rows: list[object]) -> str:
    if not rows:
        return "-"
    title = str(rows[0]["title"] or "")
    if len(title) > 24:
        title = title[:21] + "..."
    return f"{len(rows)}条：{title}"


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
    tag_options = ["", "业绩利好", "业绩风险", "业绩预告", "减持质押", "回购增持", "中标订单", "退市风险", "并购投资", "AI算力", "政策监管", "消费", "新能源", "公告"]
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
        body.append('<tr><td colspan="5" class="empty">暂无资讯，请先运行 import-ths-news 或 import-public-announcements。</td></tr>')
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


def _render_symbol_themes(rows: list[object]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_e(row['category'])}</td>"
            f"<td>{_e(row['theme'])}</td>"
            f"<td>{_e(row['updated_at'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="3" class="empty">暂无本地主题归属，请先导入通达信板块缓存。</td></tr>')
    return _table_section("概念与风格", ["类别", "主题", "更新时间"], body)


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


def _render_data_health_summary(health: dict[str, object]) -> str:
    status = str(health.get("status") or "empty")
    status_label = {"clean": "正常", "attention": "需检查", "empty": "暂无日线"}.get(status, status)
    freshness_status = str(health.get("freshness_status") or "unknown")
    weekday_lag_days = health.get("weekday_lag_days")
    freshness_label = {
        "current": "近 1 个工作日",
        "lagging": f"可能滞后 {weekday_lag_days} 个工作日",
        "unknown": "无法判断",
        "empty": "暂无日线",
    }.get(freshness_status, freshness_status)
    cards = [
        ("健康状态", status_label),
        ("最近股票日线", health.get("latest_trade_date") or "-"),
        ("任意品种最新", health.get("latest_any_trade_date") or "-"),
        ("数据时效", freshness_label),
        ("日线数量", f"{int(health.get('total_bars') or 0):,}"),
        ("覆盖标的", f"{int(health.get('total_symbols') or 0):,}"),
        ("同日冲突", str(int(health.get("duplicate_symbol_days") or 0))),
        ("冲突记录", str(int(health.get("duplicate_rows") or 0))),
    ]
    note = '<section class="panel"><p>时效按工作日间隔粗略估算，不包含交易所节假日和盘中更新状态。</p></section>'
    return "<section class=\"metrics\">" + "".join(
        '<article class="metric">' + f"<span>{_e(label)}</span><strong>{_e(value)}</strong>" + "</article>"
        for label, value in cards
    ) + "</section>" + note


def _render_data_health_sources(health: dict[str, object]) -> str:
    body = []
    sources = health.get("sources", [])
    if isinstance(sources, list):
        for row in sources:
            body.append(
                "<tr>"
                f"<td>{_e(row['source_kind'])}</td>"
                f"<td class=\"num\">{int(row['bars']):,}</td>"
                f"<td class=\"num\">{int(row['symbols']):,}</td>"
                f"<td>{_e(row['first_trade_date'] or '-')}</td>"
                f"<td>{_e(row['last_trade_date'] or '-')}</td>"
                "</tr>"
            )
    if not body:
        body.append('<tr><td colspan="5" class="empty">暂无已导入日线。</td></tr>')
    policy = _e(str(health.get("canonical_source_policy") or "-"))
    note = f'<section class="panel"><h2>规范来源</h2><p>{policy}</p></section>'
    return note + _table_section("日线来源覆盖", ["来源", "日线", "标的", "最早交易日", "最近交易日"], body)


def _render_fundamental_health(health: dict[str, object]) -> str:
    cards = [
        ("已导入记录", f"{int(health.get('total_records') or 0):,}"),
        ("覆盖股票", f"{int(health.get('total_symbols') or 0):,}"),
        ("已披露股票", f"{int(health.get('disclosed_symbols') or 0):,}"),
        ("已披露记录", f"{int(health.get('disclosed_records') or 0):,}"),
        ("现金流覆盖股票", f"{int(health.get('operating_cash_flow_symbols') or 0):,}"),
        ("现金流记录", f"{int(health.get('operating_cash_flow_records') or 0):,}"),
        ("已披露最新报告期", health.get("latest_disclosed_report_date") or "-"),
        ("已披露最新公告日", health.get("latest_disclosed_notice_date") or "-"),
    ]
    note = (
        '<section class="panel"><h2>财务披露覆盖</h2>'
        f"<p>按 {_e(health.get('as_of_date') or '-')} 的日期边界统计；同日公告从下一交易日才会进入财务因子。"
        f"已导入来源 {int(health.get('source_count') or 0):,} 个，最近已导入报告期 {_e(health.get('latest_imported_report_date') or '-')}。"
        "</p></section>"
    )
    metrics = "".join(
        '<article class="metric"><span>' + _e(label) + "</span><strong>" + _e(value) + "</strong></article>"
        for label, value in cards
    )
    return note + '<section class="metrics">' + metrics + "</section>"


def _render_industry_health(health: dict[str, object]) -> str:
    cards = [
        ("行业归属记录", f"{int(health.get('label_records') or 0):,}"),
        ("行业数量", f"{int(health.get('industry_count') or 0):,}"),
        ("当前评分已覆盖", f"{int(health.get('scored_symbols') or 0):,}"),
        ("对应评分日期", health.get("score_date") or "-"),
        ("最新更新时间", health.get("latest_updated_at") or "-"),
    ]
    note = (
        '<section class="panel"><h2>行业归属覆盖</h2>'
        "<p>行业标签来自公开公司概况，仅用于当前研究展示；不会进入历史因子、策略回测或样本外验证。</p>"
        "</section>"
    )
    metrics = "".join(
        '<article class="metric"><span>' + _e(label) + "</span><strong>" + _e(value) + "</strong></article>"
        for label, value in cards
    )
    return note + '<section class="metrics">' + metrics + "</section>"


def _render_strategy_validation_runs(rows: list[object]) -> str:
    body = []
    verdict_classes = {"通过": "ok", "观察": "warn", "未通过": "danger", "样本不足": "warn"}
    for row in rows:
        try:
            assessment = json.loads(str(row["assessment_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            assessment = {}
        if not isinstance(assessment, dict):
            assessment = {}
        try:
            parameters = json.loads(str(row["parameters_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}
        verdict = str(row["verdict"])
        parameter_text = (
            f"训练 {parameters.get('train_days', '-')} / 测试 {parameters.get('test_days', '-')} 日，"
            f"Top {parameters.get('top_n', '-')}，{parameters.get('execution_mode', '-')}"
        )
        body.append(
            "<tr>"
            f"<td>{_e(display_shanghai_time(row['run_at']))}</td>"
            f'<td><span class="pill {verdict_classes.get(verdict, "warn")}">{_e(verdict)}</span></td>'
            f"<td class=\"num\">{int(assessment.get('fold_count') or 0)}</td>"
            f"<td class=\"num\">{int(assessment.get('total_trades') or 0)}</td>"
            f"<td class=\"num\">{_validation_percent(assessment.get('portfolio_avg_return'))}</td>"
            f"<td class=\"num\">{_validation_percent(assessment.get('benchmark_excess_return'))}</td>"
            f"<td class=\"num\">{_validation_percent(assessment.get('max_drawdown'))}</td>"
            f"<td>{_e(parameter_text)}</td>"
            f"<td>{_e(row['data_fingerprint'])}</td>"
            f"<td>{_e(row['summary'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="10" class="empty">暂无保存的样本外验证。请先运行 strategy-validate。</td></tr>')
    return _table_section(
        "已保存样本外验证",
        ["运行时间", "结论", "折数", "交易", "组合平均", "基准超额", "最大回撤", "参数", "日线版本", "摘要"],
        body,
    )


def _render_strategy_backtest_runs(rows: list[object]) -> str:
    body = []
    for row in rows:
        try:
            summary = json.loads(str(row["summary_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        try:
            parameters = json.loads(str(row["parameters_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}
        excess = summary.get("excess_portfolio_avg_return")
        parameter_text = (
            f"持有 {parameters.get('horizon_days', '-')} 日，Top {parameters.get('top_n', '-')}，"
            f"{parameters.get('execution_mode', '-')} / {parameters.get('position_mode', '-')}"
        )
        body.append(
            "<tr>"
            f'<td><a class="symbol-link" href="/strategy-backtest-runs/{int(row["id"])}">#{int(row["id"])}</a></td>'
            f"<td>{_e(display_shanghai_time(row['run_at']))}</td>"
            f"<td class=\"num\">{int(summary.get('trade_count') or 0)}</td>"
            f"<td class=\"num\">{int(summary.get('day_count') or 0)}</td>"
            f"<td class=\"num\">{_fmt(summary.get('win_rate'))}%</td>"
            f"<td class=\"num\">{_fmt(summary.get('avg_return'))}%</td>"
            f"<td class=\"num\">{_fmt(summary.get('portfolio_avg_return'))}%</td>"
            f"<td class=\"num\">{_fmt(summary.get('max_drawdown'))}%</td>"
            f"<td class=\"num\">{'-' if excess is None else f'{float(excess):+.2f}%'} </td>"
            f"<td>{_e(parameter_text)}</td>"
            f"<td>{_e(row['data_fingerprint'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="11" class="empty">暂无保存的策略回测。请先运行回测并保存结果。</td></tr>')
    return _table_section(
        "已保存策略回测",
        ["ID", "运行时间", "交易", "批次", "胜率", "净均收", "组合平均", "最大回撤", "基准超额", "参数", "日线版本"],
        body,
    )


def _render_daily_runs(rows: list[object]) -> str:
    body = []
    status_classes = {"succeeded": "ok", "failed": "danger", "running": "warn"}
    for row in rows:
        try:
            summary = json.loads(str(row["summary_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        try:
            parameters = json.loads(str(row["parameters_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}
        status = str(row["status"])
        freshness_text = _daily_run_freshness_label(summary)
        quote_freshness_text = _daily_run_quote_freshness_label(summary)
        fundamentals_text = _daily_run_fundamentals_label(summary)
        industries_text = _daily_run_industries_label(summary)
        ai_snapshot_text = _daily_run_ai_snapshot_label(summary)
        strategy_snapshot_text = _daily_run_strategy_snapshot_label(summary)
        announcements_text = _daily_run_public_announcements_label(summary, parameters)
        parameter_text = (
            f"标的上限 {parameters.get('limit', '-')}，"
            f"日线 {parameters.get('history_days', '-')} 日，"
            f"{parameters.get('universe', '-')}"
        )
        body.append(
            "<tr>"
            f"<td>{_e(display_shanghai_time(row['started_at']))}</td>"
            f"<td>{_e(display_shanghai_time(row['finished_at']) if row['finished_at'] else '-')}</td>"
            f'<td><span class="pill {status_classes.get(status, "warn")}">{_e(status)}</span></td>'
            f"<td class=\"num\">{int(summary.get('history_symbols') or 0)}</td>"
            f"<td class=\"num\">{int(summary.get('tdx_covered_symbols') or 0)}</td>"
            f"<td class=\"num\">{int(summary.get('tdx_daily_bars_imported') or 0):,}</td>"
            f"<td class=\"num\">{int(summary.get('history_bars_imported') or 0):,}</td>"
            f"<td>{_e(freshness_text)}</td>"
            f"<td>{_e(quote_freshness_text)}</td>"
            f"<td>{_e(fundamentals_text)}</td>"
            f"<td>{_e(industries_text)}</td>"
            f"<td>{_e(ai_snapshot_text)}</td>"
            f"<td>{_e(strategy_snapshot_text)}</td>"
            f"<td>{_e(announcements_text)}</td>"
            f"<td>{_e(summary.get('failed_step') or '-')}</td>"
            f"<td>{_e(parameter_text)}</td>"
            f"<td>{_e(row['error_text'] or '-')}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="17" class="empty">暂无每日运行记录。请先运行 run-daily。</td></tr>')
    return _table_section(
        "最近每日运行",
        ["开始时间", "结束时间", "状态", "标的", "TDX 已覆盖", "TDX 同步", "公开日线", "日线时效", "行情时效", "公开财报", "公开行业", "AI 快照", "策略快照", "公告", "失败步骤", "参数", "错误"],
        body,
    )


def _daily_run_freshness_label(summary: dict[str, object]) -> str:
    health = summary.get("daily_bar_health")
    if not isinstance(health, dict):
        return "-"
    latest_date = str(health.get("latest_trade_date") or "-")
    freshness = str(health.get("freshness_status") or "unknown")
    lag_days = health.get("weekday_lag_days")
    if freshness == "current":
        return f"近 1 个工作日 · {latest_date}"
    if freshness == "lagging":
        return f"可能滞后 {lag_days if lag_days is not None else '-'} 个工作日 · {latest_date}"
    if freshness == "empty":
        return "暂无日线"
    return f"无法判断 · {latest_date}"


def _daily_run_quote_freshness_label(summary: dict[str, object]) -> str:
    health = summary.get("quote_health")
    if not isinstance(health, dict):
        return "-"
    latest_date = str(health.get("latest_price_date") or "-")
    priced_symbols = int(health.get("priced_symbols") or 0)
    freshness = str(health.get("freshness_status") or "unknown")
    lag_days = health.get("weekday_lag_days")
    if freshness == "current":
        return f"近 1 个工作日 · {latest_date} · {priced_symbols} 只"
    if freshness == "partial":
        current_symbols = int(health.get("current_priced_symbols") or 0)
        stale_symbols = int(health.get("stale_priced_symbols") or 0)
        return f"近 1 日 {current_symbols} 只，可能过期 {stale_symbols} 只 · {latest_date}"
    if freshness == "lagging":
        return f"可能滞后 {lag_days if lag_days is not None else '-'} 个工作日 · {latest_date} · {priced_symbols} 只"
    if freshness == "empty":
        return "暂无带价格行情"
    return f"无法判断 · {latest_date} · {priced_symbols} 只"


def _daily_run_public_announcements_label(summary: dict[str, object], parameters: dict[str, object]) -> str:
    if not parameters.get("public_announcements") and "public_announcement_status" not in summary:
        return "未启用"
    status = str(summary.get("public_announcement_status") or "unknown")
    if status == "saved":
        return f"已保存 {int(summary.get('public_announcements_imported') or 0)} 条"
    if status == "empty":
        return "无新增"
    if status == "failed":
        return "失败"
    imported = summary.get("public_announcements_imported")
    if imported is not None:
        return f"已保存 {int(imported or 0)} 条"
    return "-"


def _daily_run_fundamentals_label(summary: dict[str, object]) -> str:
    health = summary.get("fundamental_health")
    disclosed_symbols = int(health.get("disclosed_symbols") or 0) if isinstance(health, dict) else 0
    if not summary.get("public_fundamentals_enabled"):
        return f"未更新 · {disclosed_symbols} 只已披露" if isinstance(health, dict) else "未启用"
    imported = int(summary.get("public_fundamentals_imported") or 0)
    failures = int(summary.get("public_fundamentals_failures") or 0)
    coverage = f"{imported} 条，{disclosed_symbols} 只已披露"
    if failures:
        return f"{coverage}，失败 {failures} 只"
    return coverage


def _daily_run_industries_label(summary: dict[str, object]) -> str:
    if not summary.get("public_industries_enabled"):
        return "未更新"
    imported = int(summary.get("public_industries_imported") or 0)
    failures = int(summary.get("public_industries_failures") or 0)
    coverage = f"{imported} 条"
    if failures:
        return f"{coverage}，失败 {failures} 只"
    return coverage


def _daily_run_ai_snapshot_label(summary: dict[str, object]) -> str:
    status = str(summary.get("ai_snapshot_status") or "")
    saved_count = int(summary.get("ai_decisions_saved") or 0)
    if status == "saved":
        return f"已保存 {saved_count} 条"
    if status == "empty":
        return "无可保存候选"
    if status == "failed":
        return "生成失败，不影响数据更新"
    if status == "skipped_stale_daily_bars":
        freshness = str(summary.get("ai_snapshot_daily_bar_freshness") or "unknown")
        latest_date = str(summary.get("ai_snapshot_latest_trade_date") or "-")
        if freshness == "lagging":
            return f"日线滞后至 {latest_date}，未保存"
        if freshness == "empty":
            return "暂无日线，未保存"
        return f"日线状态 {freshness}，未保存"
    if status == "skipped_stale_quotes":
        freshness = str(summary.get("ai_snapshot_quote_freshness") or "unknown")
        latest_date = str(summary.get("ai_snapshot_latest_price_date") or "-")
        current_symbols = int(summary.get("ai_snapshot_current_priced_symbols") or 0)
        priced_symbols = int(summary.get("ai_snapshot_priced_symbols") or 0)
        if freshness == "partial":
            return f"行情部分过期（近 1 日 {current_symbols}/{priced_symbols} 只），未保存"
        if freshness == "lagging":
            return f"行情滞后至 {latest_date}，未保存"
        if freshness == "empty":
            return "暂无带价格行情，未保存"
        return f"行情状态 {freshness}，未保存"
    return "-"


def _daily_run_strategy_snapshot_label(summary: dict[str, object]) -> str:
    if not summary.get("strategy_snapshot_enabled") and "strategy_snapshot_status" not in summary:
        return "未启用"
    status = str(summary.get("strategy_snapshot_status") or "unknown")
    if status == "saved":
        run_id = summary.get("strategy_snapshot_run_id")
        trades = int(summary.get("strategy_snapshot_trade_count") or 0)
        average_return = summary.get("strategy_snapshot_portfolio_avg_return")
        parts = [f"#{run_id}" if run_id is not None else "已保存", f"{trades} 笔"]
        if average_return is not None:
            parts.append(f"组合 {float(average_return):+.2f}%")
        return "已保存 " + " · ".join(parts)
    if status == "failed":
        return "失败，不影响数据更新"
    if status == "skipped_stale_daily_bars":
        freshness = str(summary.get("strategy_snapshot_daily_bar_freshness") or "unknown")
        latest_date = str(summary.get("strategy_snapshot_latest_trade_date") or "-")
        if freshness == "lagging":
            return f"日线滞后至 {latest_date}，未保存"
        if freshness == "empty":
            return "暂无日线，未保存"
        return f"日线状态 {freshness}，未保存"
    return "-"


def _validation_percent(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "-"


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


def _render_theme_controls(limit: int, category: str, min_scored: int) -> str:
    options = [("", "全部"), ("概念", "概念"), ("风格", "风格")]
    category_options = "".join(
        f'<option value="{_e(value)}"{" selected" if value == category else ""}>{_e(label)}</option>'
        for value, label in options
    )
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-filters" method="get" action="/themes">'
        '<label><span>类别</span>'
        f'<select name="category">{category_options}</select>'
        "</label>"
        '<label><span>主题数量</span>'
        f'<input name="limit" type="number" min="1" max="300" value="{limit}">'
        "</label>"
        '<label><span>最低评分覆盖</span>'
        f'<input name="min_scored" type="number" min="1" max="100" value="{min_scored}">'
        "</label>"
        '<button type="submit">刷新</button>'
        '<a class="ghost" href="/themes">重置</a>'
        "</form>"
        "</section>"
    )


def _render_industry_controls(limit: int, min_scored: int) -> str:
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-filters" method="get" action="/industries">'
        '<label><span>行业数量</span>'
        f'<input name="limit" type="number" min="1" max="300" value="{limit}">'
        "</label>"
        '<label><span>最低评分覆盖</span>'
        f'<input name="min_scored" type="number" min="1" max="100" value="{min_scored}">'
        "</label>"
        '<button type="submit">刷新</button>'
        '<a class="ghost" href="/industries">重置</a>'
        "</form>"
        "</section>"
    )


def _render_industry_summary(industry_count: int, score_date: str, min_scored: int) -> str:
    return (
        '<section class="panel">'
        "<h2>数据口径</h2>"
        f"<p>当前已导入 {industry_count:,} 条公开行业归属；评分口径：{_e(score_date)}。</p>"
        f"<p>仅展示当前评分批次覆盖至少 {min_scored} 只股票的行业。行业标签会随公开数据源变化，只用于当前研究上下文，不参与历史回测。</p>"
        "</section>"
    )


def _render_industry_heat(rows: object) -> str:
    body = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            coverage_rate = row.get("coverage_rate")
            positive_rate = row.get("positive_rate")
            coverage_text = f"{float(coverage_rate):.1f}%" if coverage_rate is not None else "-"
            positive_text = f"{float(positive_rate):.1f}%" if positive_rate is not None else "-"
            body.append(
                "<tr>"
                f"<td>{_e(row.get('industry'))}</td>"
                f"<td class=\"num\">{int(row.get('member_count') or 0):,}</td>"
                f"<td class=\"num\">{int(row.get('scored_count') or 0):,}</td>"
                f"<td class=\"num\">{_e(coverage_text)}</td>"
                f"<td class=\"num strong\">{_fmt(row.get('average_score'))}</td>"
                f"<td class=\"num\">{_e(positive_text)}</td>"
                "</tr>"
            )
    if not body:
        body.append('<tr><td colspan="6" class="empty">暂无符合覆盖条件的行业，请先导入公开行业标签并运行评分。</td></tr>')
    return _table_section("行业评分汇总", ["行业", "成员", "评分覆盖", "覆盖率", "平均分", "正分占比"], body)


def _render_theme_summary(membership_count: int, score_date: str, price_as_of_date: str, min_scored: int) -> str:
    return (
        '<section class="panel">'
        "<h2>数据口径</h2>"
        f"<p>当前已导入 {membership_count:,} 条通达信本地主题成员关系；评分口径：{_e(score_date)}；价格口径：{_e(price_as_of_date)} 收盘。</p>"
        f"<p>仅展示当前评分批次覆盖至少 {min_scored} 只股票的主题。价格表现为成分股等权均值，缺少两个端点收盘价的成分不计入对应周期，不代表主题指数收益或投资结论。</p>"
        "</section>"
    )


def _render_theme_heat(rows: object) -> str:
    body = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            average_score = row.get("average_score")
            positive_rate = row.get("positive_rate")
            coverage_rate = row.get("coverage_rate")
            price_coverage_rate = row.get("price_coverage_rate")
            positive_rate_text = f"{float(positive_rate):.1f}%" if positive_rate is not None else "-"
            coverage_rate_text = f"{float(coverage_rate):.1f}%" if coverage_rate is not None else "-"
            price_coverage_text = (
                f"{int(row.get('priced_count') or 0)}/{int(row.get('member_count') or 0)} · {float(price_coverage_rate):.1f}%"
                if price_coverage_rate is not None
                else "-"
            )
            body.append(
                "<tr>"
                f"<td>{_e(row.get('category'))}</td>"
                f"<td>{_e(row.get('theme'))}</td>"
                f"<td class=\"num\">{int(row.get('member_count') or 0):,}</td>"
                f"<td class=\"num\">{int(row.get('scored_count') or 0):,}</td>"
                f"<td class=\"num\">{_e(coverage_rate_text)}</td>"
                f"<td class=\"num strong\">{_fmt(average_score)}</td>"
                f"<td class=\"num\">{_e(positive_rate_text)}</td>"
                f"<td class=\"num\">{_e(price_coverage_text)}</td>"
                f"<td class=\"num\">{_theme_return_text(row, 1)}</td>"
                f"<td class=\"num\">{_theme_return_text(row, 5)}</td>"
                f"<td class=\"num\">{_theme_return_text(row, 20)}</td>"
                "</tr>"
            )
    if not body:
        body.append('<tr><td colspan="11" class="empty">暂无符合覆盖条件的主题，请先导入主题或降低最低评分覆盖。</td></tr>')
    return _table_section("主题评分与价格表现", ["类别", "主题", "成员", "评分覆盖", "覆盖率", "平均分", "正分占比", "价格覆盖", "1 日等权", "5 日等权", "20 日等权"], body)


def _theme_return_text(row: dict[str, object], horizon_days: int) -> str:
    value = row.get(f"return_{horizon_days}d")
    count = int(row.get(f"return_{horizon_days}d_count") or 0)
    if value is None:
        return "-"
    return f"{float(value):+.2f}% ({count})"


def _render_factor_definitions() -> str:
    body = []
    for item in factor_definitions():
        body.append(
            "<tr>"
            f"<td>{_factor_link(item.factor_id)}</td>"
            f"<td>{_factor_link(item.factor_id, item.name)}</td>"
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
            f"<td>{_factor_link(str(row['factor_id']), str(row['factor_name']))}</td>"
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
            f"<td>{_factor_link(str(row['factor_id']))}</td>",
            f"<td>{_factor_link(str(row['factor_id']), str(row['factor_name']))}</td>",
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


def _render_factor_detail_summary(definition: object, signals: list[dict[str, object]], matrix: dict[str, object] | None) -> str:
    factor = definition
    verdict = str(matrix.get("verdict") or "样本不足") if matrix else "样本不足"
    effectiveness = float(matrix.get("effectiveness_score") or 0.0) if matrix else 0.0
    samples = int(matrix.get("total_samples") or 0) if matrix else 0
    cards = [
        ("类别", factor.category),
        ("未来函数风险", factor.future_function_risk),
        ("当前命中", str(len(signals))),
        ("历史结论", verdict),
        ("有效性评分", f"{effectiveness:.1f}"),
        ("总样本", f"{samples:,}"),
    ]
    rendered = "".join(
        '<article class="metric">'
        f"<span>{_e(label)}</span><strong>{_e(value)}</strong>"
        "</article>"
        for label, value in cards
    )
    return (
        '<section class="metrics">'
        f"{rendered}"
        "</section>"
        '<section class="panel">'
        "<h2>因子逻辑</h2>"
        f"<p>{_e(factor.description)}</p>"
        f"<p>来源：{_e(factor.source)}</p>"
        "<p>风险边界：信号仅使用当日及以前日线计算；未来收益仅用于历史统计，不能作为交易指令。</p>"
        "</section>"
    )


def _render_factor_detail_signals(rows: list[dict[str, object]]) -> str:
    body = []
    for row in rows:
        pill = "danger" if row["direction"] == "risk" else "ok"
        body.append(
            "<tr>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'] or '-')}</td>"
            f'<td><span class="pill {pill}">{_e(row["direction"])}</span></td>'
            f"<td class=\"num\">{float(row['strength']):.1f}</td>"
            f"<td>{_e(row['reason'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="5" class="empty">当前扫描范围内暂无命中。</td></tr>')
    return _table_section("当前命中", ["代码", "名称", "方向", "强度", "原因"], body)


def _render_factor_detail_history(matrix: dict[str, object] | None) -> str:
    horizon_rows = matrix.get("horizons") if isinstance(matrix, dict) else {}
    body = []
    if isinstance(horizon_rows, dict):
        for horizon, stats in sorted(horizon_rows.items(), key=lambda item: int(item[0])):
            if not isinstance(stats, dict):
                continue
            body.append(
                "<tr>"
                f"<td>{_e(horizon)} 个交易日</td>"
                f"<td class=\"num\">{int(stats.get('samples') or 0):,}</td>"
                f"<td class=\"num\">{float(stats.get('win_rate') or 0.0):.1f}%</td>"
                f"<td class=\"num\">{float(stats.get('avg_return') or 0.0):.2f}%</td>"
                f"<td class=\"num\">{float(stats.get('best_return') or 0.0):.2f}%</td>"
                f"<td class=\"num\">{float(stats.get('worst_return') or 0.0):.2f}%</td>"
                "</tr>"
            )
    if not body:
        body.append('<tr><td colspan="6" class="empty">暂无足够历史样本。</td></tr>')
    return _table_section("多周期历史表现", ["持有周期", "样本", "胜率", "平均收益", "最好", "最差"], body)


def _render_backtest_controls(
    horizon: int,
    execution_mode: str,
    position_mode: str,
    top_n: int,
    min_signal_score: float,
    limit_symbols: int,
    cost_bps: float,
    slippage_bps: float,
    benchmark_symbol: str,
    benchmark_indices: list[dict[str, object]],
    max_bars: int,
) -> str:
    benchmark_options = [("", "不比较基准")]
    benchmark_options.extend(
        (
            str(item["symbol"]),
            f"{item['name']}（{item['symbol']}，至 {item['latest_trade_date']}）",
        )
        for item in benchmark_indices
    )
    if benchmark_symbol and benchmark_symbol not in {item[0] for item in benchmark_options}:
        benchmark_options.append((benchmark_symbol, f"{benchmark_symbol}（手动代码）"))
    benchmark_select = "".join(
        f'<option value="{_e(value)}"{" selected" if value == benchmark_symbol else ""}>{_e(label)}</option>'
        for value, label in benchmark_options
    )
    return (
        '<section class="panel filter-panel">'
        '<form class="filters backtest-filters" method="get" action="/backtest">'
        '<label><span>持有天数</span>'
        f'<input name="horizon" type="number" min="1" max="30" value="{horizon}">'
        "</label>"
        '<label><span>成交方式</span>'
        '<select name="execution">'
        f'<option value="next_open"{" selected" if execution_mode == "next_open" else ""}>次日开盘</option>'
        f'<option value="signal_close"{" selected" if execution_mode == "signal_close" else ""}>信号日收盘</option>'
        "</select>"
        "</label>"
        '<label><span>持仓方式</span>'
        '<select name="position_mode">'
        f'<option value="non_overlapping"{" selected" if position_mode == "non_overlapping" else ""}>不重叠</option>'
        f'<option value="daily_batches"{" selected" if position_mode == "daily_batches" else ""}>每日批次</option>'
        "</select>"
        "</label>"
        '<label><span>每日数量</span>'
        f'<input name="top_n" type="number" min="1" max="50" value="{top_n}">'
        "</label>"
        '<label><span>最低信号分</span>'
        f'<input name="min_signal_score" type="number" step="1" value="{min_signal_score:g}">'
        "</label>"
        '<label><span>股票上限</span>'
        f'<input name="limit_symbols" type="number" min="10" max="5000" value="{limit_symbols}">'
        "</label>"
        '<label><span>成本 bps</span>'
        f'<input name="cost_bps" type="number" min="0" max="200" step="0.1" value="{cost_bps:g}">'
        "</label>"
        '<label><span>滑点 bps</span>'
        f'<input name="slippage_bps" type="number" min="0" max="200" step="0.1" value="{slippage_bps:g}">'
        "</label>"
        '<label><span>基准指数</span>'
        f'<select name="benchmark_symbol">{benchmark_select}</select>'
        "</label>"
        '<label><span>K线数量</span>'
        f'<input name="max_bars" type="number" min="0" max="3000" value="{max_bars}">'
        "</label>"
        '<button type="submit">回测</button>'
        '<a class="ghost" href="/backtest">重置</a>'
        '<a class="ghost" href="/strategy-backtest-runs">已保存回测</a>'
        "</form>"
        '<form class="inline-toolbar" method="post" action="/backtest/save">'
        f'<input type="hidden" name="horizon" value="{horizon}">'
        f'<input type="hidden" name="execution" value="{_e(execution_mode)}">'
        f'<input type="hidden" name="position_mode" value="{_e(position_mode)}">'
        f'<input type="hidden" name="top_n" value="{top_n}">'
        f'<input type="hidden" name="min_signal_score" value="{min_signal_score:g}">'
        f'<input type="hidden" name="limit_symbols" value="{limit_symbols}">'
        f'<input type="hidden" name="cost_bps" value="{cost_bps:g}">'
        f'<input type="hidden" name="slippage_bps" value="{slippage_bps:g}">'
        f'<input type="hidden" name="benchmark_symbol" value="{_e(benchmark_symbol)}">'
        f'<input type="hidden" name="max_bars" value="{max_bars}">'
        '<button type="submit">保存本次回测</button>'
        "</form>"
        "</section>"
    )


def _render_backtest_summary(result: dict[str, object]) -> str:
    benchmark = result.get("benchmark")
    benchmark_label = "基准"
    benchmark_value = "-"
    benchmark_excess = "-"
    if isinstance(benchmark, dict):
        benchmark_label = f"基准 {benchmark.get('symbol')}"
        if int(benchmark.get("sample_count") or 0) > 0:
            benchmark_value = f"{float(benchmark.get('avg_return') or 0.0):.2f}%"
            excess = result.get("excess_portfolio_avg_return")
            benchmark_excess = "-" if excess is None else f"{float(excess):+.2f}%"
        else:
            benchmark_value = "样本不足"
    execution_label = "次日开盘" if result.get("execution_mode") == "next_open" else "信号日收盘"
    position_label = "不重叠" if result.get("position_mode") == "non_overlapping" else "每日批次（重叠）"
    cards = [
        ("成交方式", execution_label),
        ("持仓方式", position_label),
        (
            "一字板跳过",
            f"入场 {int(result.get('skipped_locked_entries') or 0)} / 出场 {int(result.get('skipped_locked_exits') or 0)}",
        ),
        ("交易数", str(result["trade_count"])),
        ("交易日", str(result["day_count"])),
        ("胜率", f"{float(result['win_rate']):.1f}%"),
        ("净平均收益", f"{float(result['avg_return']):.2f}%"),
        ("毛平均收益", f"{float(result.get('gross_avg_return') or 0.0):.2f}%"),
        ("净组合日均", f"{float(result['portfolio_avg_return']):.2f}%"),
        ("最大回撤", f"{float(result.get('max_drawdown') or 0.0):.2f}%"),
        ("收益波动", f"{float(result.get('return_std') or 0.0):.2f}%"),
        ("盈亏比", f"{float(result.get('profit_loss_ratio') or 0.0):.2f}"),
        ("风险收益", f"{float(result.get('sharpe_like') or 0.0):.2f}"),
        ("双边扣减", f"{float(result.get('round_trip_cost_pct') or 0.0):.2f}%"),
        (benchmark_label, benchmark_value),
        ("超额日均", benchmark_excess),
        ("最差单笔", "-" if result["worst_return"] is None else f"{float(result['worst_return']):.2f}%"),
    ]
    rendered = []
    for label, value in cards:
        rendered.append(
            '<article class="metric">'
            f"<span>{_e(label)}</span>"
            f"<strong>{_e(value)}</strong>"
            "</article>"
        )
    position_note = "默认不重叠持仓，上一批到期后才允许下一批入场。"
    if result.get("position_mode") == "daily_batches":
        position_note = "每日批次会允许不同持有期的仓位重叠，仅用于研究信号的分批表现。"
    note = (
        '<section class="panel"><p>'
        "说明：这是研究型回测，选股只使用当日及以前的日线因子信号；未来收益仅用于统计。"
        "默认在下一交易日开盘买入、持有指定交易日后按收盘卖出；无成交量或缺失价格的日线会跳过。"
        f"{position_note}"
        "标准涨跌幅附近的一字涨跌停会跳过入场或出场；ST 历史涨跌幅仍未纳入。"
        "净收益已按设置的买卖双边交易成本和滑点扣减。"
        "</p></section>"
    )
    return '<section class="metrics">' + "".join(rendered) + "</section>" + note


def _render_backtest_equity_chart(result: dict[str, object]) -> str:
    rows = result.get("equity_curve", [])
    if not isinstance(rows, list) or len(rows) < 2:
        return '<section class="panel"><h2>组合权益曲线</h2><p class="empty">回测样本不足，暂无法绘图。</p></section>'

    width = 920
    height = 300
    pad_x = 40
    pad_top = 24
    equity_height = 150
    drawdown_top = 205
    drawdown_height = 50
    equities = [float(row["equity"]) for row in rows]
    drawdowns = [float(row["drawdown"]) for row in rows]
    min_equity = min(equities)
    max_equity = max(equities)
    min_drawdown = min(drawdowns)
    equity_span = max(max_equity - min_equity, 0.001)
    drawdown_span = max(abs(min_drawdown), 0.001)
    step = (width - pad_x * 2) / max(len(rows) - 1, 1)

    equity_points = []
    drawdown_points = []
    for index, row in enumerate(rows):
        x = pad_x + step * index
        equity = float(row["equity"])
        drawdown = float(row["drawdown"])
        equity_y = pad_top + (max_equity - equity) / equity_span * equity_height
        drawdown_y = drawdown_top + abs(drawdown) / drawdown_span * drawdown_height
        equity_points.append(f"{x:.1f},{equity_y:.1f}")
        drawdown_points.append(f"{x:.1f},{drawdown_y:.1f}")

    first_date = rows[0]["trade_date"]
    last_date = rows[-1]["trade_date"]
    final_return = (equities[-1] - 1) * 100
    return (
        '<section class="panel">'
        "<h2>组合权益曲线</h2>"
        '<div class="chart-wrap">'
        f'<svg class="price-chart equity-chart" viewBox="0 0 {width} {height}" role="img" aria-label="组合权益曲线">'
        f'<text x="{pad_x}" y="16">{_e(f"累计 {final_return:+.2f}%")}</text>'
        f'<text x="{width - pad_x - 150}" y="16">{_e(f"最大回撤 {float(result.get("max_drawdown") or 0.0):.2f}%")}</text>'
        f'<line x1="{pad_x}" y1="{pad_top}" x2="{width - pad_x}" y2="{pad_top}" class="grid"></line>'
        f'<line x1="{pad_x}" y1="{pad_top + equity_height}" x2="{width - pad_x}" y2="{pad_top + equity_height}" class="grid"></line>'
        f'<line x1="{pad_x}" y1="{drawdown_top}" x2="{width - pad_x}" y2="{drawdown_top}" class="grid"></line>'
        f'<polyline points="{" ".join(equity_points)}" class="price-line"></polyline>'
        f'<polyline points="{" ".join(drawdown_points)}" class="drawdown-line"></polyline>'
        f'<text x="{pad_x}" y="{drawdown_top - 8}">权益</text>'
        f'<text x="{pad_x}" y="{drawdown_top + drawdown_height + 18}">回撤</text>'
        f'<text x="{pad_x}" y="{height - 10}">{_e(first_date)}</text>'
        f'<text x="{width - pad_x - 90}" y="{height - 10}">{_e(last_date)}</text>'
        "</svg>"
        "</div>"
        "</section>"
    )


def _render_backtest_trades(result: dict[str, object]) -> str:
    rows = result.get("trades", [])
    body = []
    if isinstance(rows, list):
        for row in rows[:80]:
            ret = float(row["return_pct"])
            ret_class = "pos" if ret > 0 else "neg" if ret < 0 else ""
            body.append(
                "<tr>"
                f"<td>{_e(row.get('signal_date', row['trade_date']))}</td>"
                f"<td>{_e(row['trade_date'])}</td>"
                f"<td>{_e(row['exit_date'])}</td>"
                f"<td>{_symbol_link(row['symbol'])}</td>"
                f"<td>{_e(row['name'] or '-')}</td>"
                f"<td class=\"num\">{float(row['signal_score']):.1f}</td>"
                f"<td class=\"num {ret_class}\">{ret:.2f}%</td>"
                f"<td class=\"num\">{float(row.get('gross_return_pct', row['return_pct'])):.2f}%</td>"
                f"<td>{_e(row['factors'])}</td>"
                "</tr>"
            )
    if not body:
        body.append('<tr><td colspan="9" class="empty">暂无交易样本，请导入更多日线或降低最低信号分。</td></tr>')
    return _table_section("策略交易样本", ["信号日", "买入日", "卖出日", "代码", "名称", "信号分", "净收益", "毛收益", "触发因子"], body)


def _render_backtest_daily_returns(result: dict[str, object]) -> str:
    rows = result.get("daily_returns", [])
    body = []
    if isinstance(rows, list):
        for row in list(reversed(rows))[:40]:
            ret = float(row["avg_return"])
            ret_class = "pos" if ret > 0 else "neg" if ret < 0 else ""
            body.append(
                "<tr>"
                f"<td>{_e(row['trade_date'])}</td>"
                f"<td class=\"num\">{int(row['selected'])}</td>"
                f"<td class=\"num {ret_class}\">{ret:.2f}%</td>"
                f"<td class=\"num\">{float(row.get('gross_avg_return', row['avg_return'])):.2f}%</td>"
                "</tr>"
            )
    if not body:
        body.append('<tr><td colspan="4" class="empty">暂无每日组合收益。</td></tr>')
    return _table_section("每日组合平均收益", ["买入日", "入选数", "净平均收益", "毛平均收益"], body)


def _render_backtest_period_stats(result: dict[str, object], key: str, title: str) -> str:
    period_stats = result.get("period_stats", {})
    rows = period_stats.get(key, []) if isinstance(period_stats, dict) else []
    body = []
    if isinstance(rows, list):
        for row in reversed(rows):
            body.append(
                "<tr>"
                f"<td>{_e(row['period'])}</td>"
                f"<td class=\"num\">{int(row['batches'])}</td>"
                f"<td class=\"num\">{int(row['trades'])}</td>"
                f"<td class=\"num\">{float(row['win_rate']):.1f}%</td>"
                f"<td class=\"num\">{float(row['avg_return']):.2f}%</td>"
                f"<td class=\"num\">{float(row['gross_avg_return']):.2f}%</td>"
                f"<td class=\"num\">{float(row['cumulative_return']):.2f}%</td>"
                f"<td class=\"num\">{float(row['max_drawdown']):.2f}%</td>"
                "</tr>"
            )
    if not body:
        body.append('<tr><td colspan="8" class="empty">暂无可归因的独立持仓批次。</td></tr>')
    return _table_section(title, ["期间", "批次", "交易", "批次胜率", "净均收", "毛均收", "累计收益", "最大回撤"], body)


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
        quick_note = f"{item.decision}，置信度 {item.confidence:.0f}。{item.summary}"
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
            '<td class="action-cell">'
            '<form class="inline-form" method="post" action="/notes/quick-add">'
            f'<input type="hidden" name="symbol" value="{_e(item.symbol)}">'
            '<input type="hidden" name="status" value="watch">'
            '<input type="hidden" name="tags" value="AI候选">'
            f'<input type="hidden" name="note" value="{_e(quick_note)}">'
            '<input type="hidden" name="return_to" value="/ai">'
            '<button class="small-action" type="submit">加入观察</button>'
            "</form>"
            "</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="11" class="empty">暂无 AI 候选，请先运行 run-daily 或 score。</td></tr>')
    return _table_section("AI 候选观点", ["#", "代码", "名称", "板块", "结论", "置信度", "评分", "涨跌幅", "公式因子", "摘要", "操作"], body)


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
        '<a class="ghost" href="/ai/outcomes">复盘</a>'
        "</form>"
        "</section>"
    )


def _render_ai_history(rows: list[object]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>#{row['id']}</td>"
            f"<td>{_e(display_shanghai_time(row['run_at']))}</td>"
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


def _render_ai_outcome_controls(limit: int, horizon: int, symbol: str) -> str:
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-history-filters" method="get" action="/ai/outcomes">'
        '<label><span>代码</span>'
        f'<input name="symbol" value="{_e(symbol)}" placeholder="可选，如 688981">'
        "</label>"
        '<label><span>持有日</span>'
        f'<input name="horizon" type="number" min="1" max="60" value="{horizon}">'
        "</label>"
        '<label><span>数量</span>'
        f'<input name="limit" type="number" min="1" max="300" value="{limit}">'
        "</label>"
        '<button type="submit">复盘</button>'
        '<a class="ghost" href="/ai/outcomes">重置</a>'
        '<a class="ghost" href="/ai/history">历史</a>'
        "</form>"
        "</section>"
    )


def _render_ai_outcomes(rows: list[dict[str, object]]) -> str:
    body = []
    for row in rows:
        status = str(row["status"])
        return_pct = row["return_pct"]
        outcome_class = "pos" if return_pct is not None and float(return_pct) > 0 else "neg" if return_pct is not None and float(return_pct) < 0 else ""
        entry = "-"
        if row["entry_date"] is not None and row["entry_price"] is not None:
            entry = f"{row['entry_date']} / {float(row['entry_price']):.2f}"
        exit_text = "-"
        if row["exit_date"] is not None and row["exit_price"] is not None:
            exit_text = f"{row['exit_date']} / {float(row['exit_price']):.2f}"
        outcome_text = f"{float(return_pct):+.2f}%" if return_pct is not None else str(row["status_label"])
        body.append(
            "<tr>"
            f"<td>#{row['id']}</td>"
            f"<td>{_symbol_link(row['symbol'])}</td>"
            f"<td>{_e(row['name'] or '-')}</td>"
            f'<td><span class="pill ai-{_decision_class(row["decision"])}">{_e(row["decision"])}</span></td>'
            f"<td>{_e(row['score_date'] or '-')}</td>"
            f"<td>{_e(entry)}</td>"
            f"<td>{_e(exit_text)}</td>"
            f"<td class=\"num {outcome_class}\">{_e(outcome_text)}</td>"
            f"<td>{_e(row['status_label'])}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="9" class="empty">暂无已保存 AI 观点。</td></tr>')
    return _table_section(
        "AI 观点后续表现",
        ["ID", "代码", "名称", "保存结论", "评分日", "下一日开盘", "第 N 日收盘", "后续收益", "状态"],
        body,
    )


def _render_ai_outcome_summary(summary: dict[str, object]) -> str:
    hit_rate = summary["hit_rate"]
    average_return = summary["average_return"]
    cards = [
        ("独立观点", str(summary["total"])),
        ("已完成", str(summary["evaluated"])),
        ("待观察", str(summary["pending"])),
        ("正收益", str(summary["positive"])),
        ("命中率", "-" if hit_rate is None else f"{float(hit_rate):.1f}%"),
        ("平均收益", "-" if average_return is None else f"{float(average_return):+.2f}%"),
    ]
    metrics = '<section class="metrics">' + "".join(
        f'<article class="metric"><span>{_e(label)}</span><strong>{_e(value)}</strong></article>'
        for label, value in cards
    ) + "</section>"

    body = []
    for row in summary["by_decision"]:
        row_hit_rate = row["hit_rate"]
        row_average_return = row["average_return"]
        body.append(
            "<tr>"
            f'<td><span class="pill ai-{_decision_class(row["decision"])}">{_e(row["decision"])}</span></td>'
            f'<td class="num">{row["total"]}</td>'
            f'<td class="num">{row["evaluated"]}</td>'
            f'<td class="num">{row["positive"]}</td>'
            f'<td class="num">{"-" if row_hit_rate is None else f"{float(row_hit_rate):.1f}%"}</td>'
            f'<td class="num">{"-" if row_average_return is None else f"{float(row_average_return):+.2f}%"}</td>'
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="6" class="empty">暂无可汇总的 AI 观点。</td></tr>')
    return metrics + _table_section(
        "按保存结论汇总",
        ["保存结论", "观点数", "已完成", "正收益", "命中率", "平均收益"],
        body,
    )


def _render_ai_changes_controls(limit: int) -> str:
    return (
        '<section class="panel filter-panel">'
        '<form class="filters ai-history-filters" method="get" action="/ai/changes">'
        '<label><span>数量</span>'
        f'<input name="limit" type="number" min="1" max="300" value="{limit}">'
        "</label>"
        '<button type="submit">刷新</button>'
        '<a class="ghost" href="/ai/history">历史</a>'
        '<a class="ghost" href="/ai/outcomes">复盘</a>'
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
            f"<td>{_e(display_shanghai_time(row['latest_run_at']))}</td>"
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
    trigger_conditions = "".join(f"<li>{_e(item)}</li>" for item in decision.trigger_conditions)
    invalidation_conditions = "".join(f"<li>{_e(item)}</li>" for item in decision.invalidation_conditions)
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
        f'<div><h3>触发条件</h3><ul class="rules">{trigger_conditions}</ul></div>'
        f'<div><h3>失效条件</h3><ul class="rules">{invalidation_conditions}</ul></div>'
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


def _render_diagnosis_data_status(
    row: object,
    daily_rows: list[object],
    news_rows: list[object],
    note: object | None,
    decision: AIDecision | None,
) -> str:
    latest_bar = daily_rows[0]["trade_date"] if daily_rows else "-"
    priced = row["latest_price"] is not None and row["pct_change"] is not None
    factor_count = 0
    if decision is not None and isinstance(decision.evidence.get("factor_signals"), list):
        factor_count = len(decision.evidence.get("factor_signals", []))
    note_status = note["status"] if note is not None else "未记录"
    items = [
        ("行情字段", "已补价" if priced else "缺价格", "ok" if priced else "warn"),
        ("近60日线", f"{len(daily_rows)} 根 · 最新 {latest_bar}", "ok" if len(daily_rows) >= 20 else "warn"),
        ("公式因子", f"{factor_count} 个当前信号", "ok" if factor_count else "warn"),
        ("相关新闻", f"{len(news_rows)} 条", "ok" if news_rows else "warn"),
        ("本地备注", str(note_status), "ok" if note is not None else "warn"),
    ]
    cards = []
    for label, value, status in items:
        cards.append(
            '<article class="metric compact-metric">'
            f"<span>{_e(label)}</span>"
            f"<strong>{_e(value)}</strong>"
            f'<em class="pill {status}">{_e("可用" if status == "ok" else "待补")}</em>'
            "</article>"
        )
    return '<section class="panel data-status-panel"><h2>数据覆盖状态</h2><div class="metrics status-metrics">' + "".join(cards) + "</div></section>"


def _render_stock_note(symbol: str, note: object | None) -> str:
    status = note["status"] if note is not None else "watch"
    tags = note["tags"] if note is not None else ""
    note_text = note["note"] if note is not None else ""
    options = []
    for value, label in [("watch", "观察"), ("hold", "持有"), ("avoid", "回避"), ("review", "复盘")]:
        selected = " selected" if value == status else ""
        options.append(f'<option value="{value}"{selected}>{label}</option>')
    updated = f'<p class="note-updated">更新于 { _e(display_shanghai_time(note["updated_at"])) }</p>' if note is not None else ""
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


def _render_fundamental(row: object | None) -> str:
    if row is None:
        return '<section class="panel"><h2>最近财务</h2><p class="empty">暂无财务数据，请导入报告期 CSV 或抓取公开财报。</p></section>'
    values = [
        ("营业收入", row["revenue"]),
        ("营业收入同比", row["revenue_yoy"]),
        ("净利润", row["net_profit"]),
        ("归母净利同比", row["net_profit_yoy"]),
        ("ROE", row["roe"]),
        ("经营现金流", row["operating_cash_flow"]),
        ("PE TTM", row["pe_ttm"]),
        ("PB", row["pb"]),
    ]
    body = "".join(
        "<tr>"
        f"<td>{_e(label)}</td>"
        f"<td class=\"num\">{_fmt(value)}</td>"
        "</tr>"
        for label, value in values
    )
    return (
        '<section class="panel">'
        f"<h2>最近财务：{_e(row['report_date'])}</h2>"
        f"<p>公告日期：{_e(row['notice_date'] or '-')}；金额与比率保持原始 CSV 导出单位或公开接口原始值。带公告日期的营收、利润、ROE、同比和经营现金流会在公告后的下一交易日参与基本面因子。</p>"
        '<div class="table-wrap"><table><thead><tr><th>指标</th><th>原始值</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
        "</section>"
    )


def _render_industry_context(row: object | None) -> str:
    if row is None:
        return ""
    return (
        '<section class="panel">'
        "<h2>行业归属</h2>"
        f"<p><strong>{_e(row['industry'])}</strong> · 更新于 {_e(display_shanghai_time(row['updated_at']))}</p>"
        "<p>行业标签来自公开公司概况，仅作当前研究上下文，不参与历史因子和策略回测。</p>"
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
            f"<td>{_e(display_shanghai_time(row['updated_at']))}</td>"
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
        + _render_app_nav(_active_path_for_title(title))
    )


def _symbol_link(symbol: object) -> str:
    text = _e(symbol)
    return f'<a class="symbol-link" href="/symbol/{text}">{text}</a>'


def _factor_link(factor_id: str, label: str | None = None) -> str:
    factor_text = _e(factor_id)
    label_text = _e(label if label is not None else factor_id)
    return f'<a class="symbol-link" href="/factors/{factor_text}">{label_text}</a>'


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
  background: #f4f6f8;
  color: #1f2933;
}
* { box-sizing: border-box; }
html { -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
body { margin: 0; }
.shell { width: min(1440px, calc(100% - 32px)); margin: 0 auto; padding: 24px 0 40px; }
.topbar { display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; margin-bottom: 18px; }
h1 { margin: 0; font-size: 28px; font-weight: 700; text-wrap: balance; }
h2 { margin: 0 0 12px; font-size: 18px; font-weight: 700; text-wrap: balance; }
h3 { margin: 0 0 8px; font-size: 14px; font-weight: 700; color: #334155; }
p { margin: 8px 0 0; color: #5f6c7b; font-size: 13px; line-height: 1.7; text-wrap: pretty; }
.refresh {
  display: inline-flex; align-items: center; justify-content: center;
  min-height: 40px; padding: 0 14px; border-radius: 6px;
  color: #0f766e; background: #dff6f1; text-decoration: none; font-weight: 600;
  transition-property: transform, background-color, box-shadow;
  transition-duration: 160ms;
  transition-timing-function: ease-out;
}
.refresh:hover { background: #c8efe7; box-shadow: 0 1px 0 rgba(15, 118, 110, 0.12); }
.refresh:active { transform: scale(0.98); }
.primary-action { color: #ffffff; background: #0f766e; }
.primary-action:hover { background: #115e59; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.symbol-link { color: #0f766e; font-weight: 700; text-decoration: none; }
.symbol-link:hover, .refresh:hover { text-decoration: underline; }
.app-nav {
  display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
  padding: 8px; margin: -4px 0 16px; border: 1px solid #d8e0e8;
  border-radius: 8px; background: #ffffff;
}
.nav-link {
  min-height: 38px; display: inline-flex; align-items: center; justify-content: center;
  padding: 0 12px; border-radius: 6px; color: #475569; text-decoration: none; font-weight: 700;
  transition-property: background-color, color, transform;
  transition-duration: 160ms;
  transition-timing-function: ease-out;
}
.nav-link:hover { color: #0f766e; background: #eef7f5; }
.nav-link:active { transform: scale(0.98); }
.nav-link.active { color: #ffffff; background: #0f766e; }
.hero-panel {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(180px, 240px);
  gap: 18px; align-items: stretch; padding: 20px; margin-bottom: 14px;
  background: #ffffff; border: 1px solid #d8e0e8; border-radius: 8px;
  box-shadow: 0 10px 28px rgba(31, 41, 51, 0.06);
}
.hero-copy h2 { margin: 4px 0 8px; font-size: 24px; line-height: 1.25; }
.eyebrow { color: #0f766e; font-size: 12px; font-weight: 800; letter-spacing: 0; }
.hero-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
.hero-status {
  display: grid; align-content: center; justify-items: start; gap: 6px;
  padding: 16px; border: 1px solid #d8e0e8; border-radius: 6px; background: #f8fafc;
}
.hero-status strong { font-size: 30px; line-height: 1; font-variant-numeric: tabular-nums; }
.hero-status small { color: #64748b; font-weight: 700; }
.workflow { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.workflow-step {
  display: grid; grid-template-columns: 34px minmax(0, 1fr); column-gap: 10px; row-gap: 4px;
  min-height: 92px; padding: 12px; border: 1px solid #d8e0e8; border-radius: 8px;
  background: #ffffff; color: #334155; text-decoration: none;
  transition-property: transform, border-color, box-shadow;
  transition-duration: 160ms;
  transition-timing-function: ease-out;
}
.workflow-step:hover { border-color: #0f766e; box-shadow: 0 8px 18px rgba(31, 41, 51, 0.08); transform: translateY(-1px); }
.workflow-step strong { align-self: center; }
.workflow-step small { grid-column: 2; color: #64748b; line-height: 1.45; text-wrap: pretty; }
.step-index {
  width: 34px; height: 34px; display: inline-flex; align-items: center; justify-content: center;
  border-radius: 999px; color: #0f766e; background: #dff6f1; font-weight: 800;
}
.guide-panel {
  display: flex; justify-content: space-between; gap: 14px; align-items: center;
  padding: 14px 16px; margin-top: 14px; border: 1px solid #cfe8e2;
  border-radius: 8px; background: #f2fbf8;
}
.guide-panel h2 { margin-bottom: 4px; font-size: 16px; }
.guide-panel p { margin: 0; max-width: 820px; }
.guide-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.data-freshness-warning {
  display: flex; justify-content: space-between; gap: 14px; align-items: center;
  padding: 14px 16px; margin: 14px 0; border: 1px solid #fdba74;
  border-radius: 8px; background: #fff7ed;
}
.data-freshness-warning h2 { margin-bottom: 4px; font-size: 16px; color: #9a3412; }
.data-freshness-warning p { margin: 0; max-width: 820px; color: #7c2d12; }
.metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.detail-metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.status-metrics { grid-template-columns: repeat(5, minmax(0, 1fr)); margin-bottom: 0; }
.ths-metrics { grid-template-columns: minmax(120px, 0.8fr) minmax(180px, 1fr) minmax(240px, 1.4fr) minmax(260px, 1.8fr); }
.metric { background: #ffffff; border: 1px solid #d8e0e8; border-radius: 8px; padding: 14px; }
.metric span { display: block; color: #64748b; font-size: 12px; margin-bottom: 8px; }
.metric strong { font-size: 24px; line-height: 1; font-variant-numeric: tabular-nums; }
.compact-metric { display: grid; gap: 8px; align-content: start; }
.compact-metric strong { font-size: 15px; line-height: 1.35; overflow-wrap: anywhere; }
.compact-metric em { width: fit-content; font-style: normal; }
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
  min-height: 40px; border: 0; border-radius: 6px; padding: 0 14px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center; text-decoration: none;
  transition-property: transform, background-color, box-shadow;
  transition-duration: 160ms;
  transition-timing-function: ease-out;
}
.filters button { background: #0f766e; color: #ffffff; cursor: pointer; }
.ghost { color: #475569; background: #e2e8f0; }
.filters button:hover, .ghost:hover { box-shadow: 0 1px 0 rgba(31, 41, 51, 0.12); }
.filters button:active, .ghost:active { transform: scale(0.98); }
.ai-filters { grid-template-columns: minmax(120px, 180px) minmax(120px, 180px) auto auto auto; }
.ai-history-filters { grid-template-columns: minmax(160px, 220px) minmax(120px, 160px) auto auto; }
.backtest-filters { grid-template-columns: repeat(10, minmax(100px, 155px)) auto auto; }
.news-filters { grid-template-columns: minmax(220px, 1fr) minmax(130px, 180px) minmax(100px, 140px) auto auto; }
.diagnose-form { grid-template-columns: minmax(180px, 260px) auto auto auto; }
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
tbody tr:hover { background: #fbfdfd; }
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
.small-action {
  min-height: 32px; border: 0; border-radius: 6px; padding: 0 10px; cursor: pointer;
  background: #dff6f1; color: #0f766e; font-weight: 800;
  transition-property: transform, background-color, box-shadow;
  transition-duration: 160ms;
  transition-timing-function: ease-out;
}
.small-action:hover { background: #c8efe7; box-shadow: 0 1px 0 rgba(15, 118, 110, 0.12); }
.small-action:active { transform: scale(0.98); }
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
.drawdown-line { fill: none; stroke: #b42318; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }
.equity-chart .price-line { stroke: #0f766e; }
.volume-bars rect { fill: #94a3b8; opacity: 0.55; }
@media (max-width: 900px) {
  .hero-panel { grid-template-columns: 1fr; }
  .workflow { grid-template-columns: 1fr; }
  .guide-panel { align-items: flex-start; flex-direction: column; }
  .data-freshness-warning { align-items: flex-start; flex-direction: column; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ai-grid { grid-template-columns: 1fr; }
  .note-form { grid-template-columns: 1fr; }
  .topbar { align-items: flex-start; flex-direction: column; }
  h1 { font-size: 24px; }
}
""".strip()
