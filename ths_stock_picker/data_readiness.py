from __future__ import annotations


def summarize_data_readiness(
    daily_bar_health: dict[str, object],
    quote_health: dict[str, object],
    fundamental_health: dict[str, object],
    industry_health: dict[str, object],
) -> dict[str, object]:
    items = [
        _daily_bar_item(daily_bar_health),
        _quote_item(quote_health),
        _fundamental_item(fundamental_health),
        _industry_item(industry_health),
    ]
    status_order = {"ok": 0, "warn": 1, "block": 2}
    worst = max(items, key=lambda item: status_order.get(str(item["status"]), 0)) if items else {"status": "ok"}
    status = "ready" if worst["status"] == "ok" else "blocked" if worst["status"] == "block" else "attention"
    label = {"ready": "数据准备充分", "attention": "数据需补充", "blocked": "关键数据不足"}[status]
    summary = {
        "ready": "核心数据状态良好，可以继续生成候选、诊股和复盘。",
        "attention": "核心链路可用，但部分增强数据缺失或过期，建议先补齐后再做严肃复盘。",
        "blocked": "关键行情或日线不满足当前研究前提，自动 AI/策略快照可能会跳过。",
    }[status]
    actions = [item for item in items if item["status"] != "ok"]
    primary_action = _primary_action(actions)
    return {
        "status": status,
        "label": label,
        "summary": summary,
        "items": items,
        "actions": actions,
        "primary_action": primary_action,
    }


def _primary_action(actions: list[dict[str, str]]) -> dict[str, str] | None:
    if not actions:
        return None
    areas = {item["area"] for item in actions}
    if "daily_bars" in areas:
        return None
    if areas.intersection({"quotes", "fundamentals", "industries"}):
        return _item(
            "prepare_data",
            "一键准备数据",
            "warn",
            "可先用一条命令刷新行情、财报和行业后重新评分并生成候选日报。",
            "python -m ths_stock_picker prepare-data --universe cache --quote-limit 3000 --fundamental-limit 100 --industry-limit 100",
        )
    return None


def _daily_bar_item(health: dict[str, object]) -> dict[str, str]:
    freshness = str(health.get("freshness_status") or "unknown")
    source_status = str(health.get("status") or "empty")
    latest = str(health.get("latest_trade_date") or "-")
    lag = health.get("weekday_lag_days")
    if freshness == "empty":
        return _item(
            "daily_bars",
            "日线",
            "block",
            "暂无可用日线，无法可靠评分、画图或回测。",
            "先运行 tdx-status 确认通达信下载源，再运行 import-tdx-history --tdx-root <通达信目录> --include-indices --replace-existing。",
        )
    if freshness == "lagging":
        return _item(
            "daily_bars",
            "日线",
            "block",
            f"股票日线最新为 {latest}，可能滞后 {lag if lag is not None else '-'} 个工作日。",
            "更新通达信日线后运行 run-daily --tdx-root <通达信目录> --tdx-include-indices。",
        )
    if source_status == "attention":
        return _item(
            "daily_bars",
            "日线",
            "warn",
            "检测到同一股票同一交易日存在多来源记录。",
            "以通达信为主数据源时，重新运行 import-tdx-history --replace-existing 统一口径。",
        )
    return _item("daily_bars", "日线", "ok", f"股票日线最新为 {latest}，来源口径可用。", "")


def _quote_item(health: dict[str, object]) -> dict[str, str]:
    freshness = str(health.get("freshness_status") or "unknown")
    priced = int(health.get("priced_symbols") or 0)
    current = int(health.get("current_priced_symbols") or 0)
    stale = int(health.get("stale_priced_symbols") or 0)
    latest = str(health.get("latest_price_date") or "-")
    if freshness == "empty":
        return _item(
            "quotes",
            "实时行情",
            "block",
            "暂无带价格行情，当前评分和候选缺少价格基础。",
            "运行 import-public-quotes --from-cache --limit 2000，或直接运行 run-daily --limit 1000。",
        )
    if freshness == "lagging":
        return _item(
            "quotes",
            "实时行情",
            "block",
            f"最近价格日期为 {latest}，{priced} 只带价格股票整体可能过期。",
            "重新运行 import-public-quotes --from-cache --limit 2000 后再 score 或 run-daily。",
        )
    if freshness == "partial":
        return _item(
            "quotes",
            "实时行情",
            "warn",
            f"带价格行情 {priced} 只，其中 {current} 只最新、{stale} 只可能过期。",
            "扩大公开行情补价范围，运行 import-public-quotes --from-cache --limit 3000；诊股时重点看单只行情时间。",
        )
    if freshness == "unknown":
        return _item(
            "quotes",
            "实时行情",
            "warn",
            "部分行情缺少可解析的观察时间。",
            "重新抓取公开行情，确保 quote_observed_at 可用于复核。",
        )
    return _item("quotes", "实时行情", "ok", f"{priced} 只带价格行情在近 1 个工作日内刷新。", "")


def _fundamental_item(health: dict[str, object]) -> dict[str, str]:
    disclosed = int(health.get("disclosed_symbols") or 0)
    cashflow = int(health.get("operating_cash_flow_symbols") or 0)
    latest_notice = str(health.get("latest_disclosed_notice_date") or "-")
    if disclosed == 0:
        return _item(
            "fundamentals",
            "财务披露",
            "warn",
            "暂无按公告日边界可用的财务披露，财务质量因子不会生效。",
            "运行 import-public-fundamentals --universe auto --limit 100 --reports 8。",
        )
    if cashflow == 0:
        return _item(
            "fundamentals",
            "财务披露",
            "warn",
            f"已披露财务覆盖 {disclosed} 只，但经营现金流字段为空。",
            "重新运行 import-public-fundamentals --universe auto --limit 100 --reports 8 补齐现金流表。",
        )
    return _item("fundamentals", "财务披露", "ok", f"已披露财务覆盖 {disclosed} 只，最新公告日 {latest_notice}。", "")


def _industry_item(health: dict[str, object]) -> dict[str, str]:
    labels = int(health.get("label_records") or 0)
    industries = int(health.get("industry_count") or 0)
    scored = int(health.get("scored_symbols") or 0)
    if labels == 0:
        return _item(
            "industries",
            "行业归属",
            "warn",
            "暂无行业归属标签，行业热度和个股行业上下文不可用。",
            "运行 import-public-industries --universe auto --limit 100。",
        )
    if scored == 0:
        return _item(
            "industries",
            "行业归属",
            "warn",
            f"已导入 {labels} 条行业标签，但当前评分批次尚未覆盖这些股票。",
            "先运行 score 或 run-daily，再打开 /industries 查看当前评分下的行业热度。",
        )
    return _item("industries", "行业归属", "ok", f"当前评分覆盖 {scored} 只、{industries} 个行业。", "")


def _item(area: str, label: str, status: str, message: str, action: str) -> dict[str, str]:
    return {"area": area, "label": label, "status": status, "message": message, "action": action}
