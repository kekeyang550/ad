from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .factor_engine import evaluate_factors

_FACTOR_QUALITY_CACHE: dict[str, dict[str, dict[str, object]]] = {}


@dataclass(frozen=True)
class AIDecision:
    symbol: str
    name: str
    board: str
    decision: str
    confidence: float
    summary: str
    strengths: list[str]
    risks: list[str]
    next_actions: list[str]
    evidence: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def analyze_symbol(repo: Any, symbol: str) -> AIDecision | None:
    row = repo.score_explanation(symbol)
    if row is None:
        return None
    note = repo.stock_note(symbol)
    bars = list(reversed(repo.recent_daily_bars(symbol, limit=30)))
    factor_signals = evaluate_factors(bars)
    news = repo.related_news_for_symbol(symbol, name=row["name"], limit=5)
    if not news:
        news = _context_news(repo, row, limit=3)
    components = json.loads(row["components_json"])
    rules = json.loads(row["triggered_rules_json"])
    trend = _trend_metrics(bars)
    news_signal = _news_signal(news)
    decision = _decision_label(row, components, rules, trend, note, news_signal)
    strengths = _strengths(row, components, rules, trend, note, news, factor_signals)
    risks = _risks(row, components, rules, trend, note, news, factor_signals)
    next_actions = _next_actions(decision, row, trend, risks, note)
    confidence = _confidence(row, components, trend, note, news)
    summary = _summary(row, decision, confidence, strengths, risks, trend, news)

    return AIDecision(
        symbol=row["symbol"],
        name=row["name"] or "",
        board=row["board"] or "",
        decision=decision,
        confidence=confidence,
        summary=summary,
        strengths=strengths,
        risks=risks,
        next_actions=next_actions,
        evidence={
            "score_date": row["score_date"],
            "profile_name": row["profile_name"],
            "total_score": row["total_score"],
            "latest_price": row["latest_price"],
            "pct_change": row["pct_change"],
            "amount": row["amount"],
            "turnover_rate": row["turnover_rate"],
            "market_cap": row["market_cap"],
            "components": components,
            "rules": rules,
            "trend": trend,
            "factor_signals": _factor_evidence(repo, factor_signals),
            "news": [_news_evidence(item) for item in news],
            "news_signal": news_signal,
            "note": dict(note) if note is not None else None,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def rank_candidates(repo: Any, limit: int = 20, min_score: float = 1.0) -> list[AIDecision]:
    rows = repo.latest_candidates(limit=max(limit * 3, limit), min_score=min_score)
    decisions: list[AIDecision] = []
    for row in rows:
        decision = analyze_symbol(repo, row["symbol"])
        if decision is not None:
            decisions.append(decision)
    return sorted(decisions, key=_rank_key, reverse=True)[:limit]


def decisions_to_rows(decisions: list[AIDecision]) -> list[dict[str, object]]:
    return [
        {
            "symbol": item.symbol,
            "name": item.name,
            "decision": item.decision,
            "confidence": item.confidence,
            "summary": item.summary,
            "thesis_json": item.to_json(),
        }
        for item in decisions
    ]


def _rank_key(item: AIDecision) -> tuple[float, float, float]:
    score = float(item.evidence.get("total_score") or 0.0)
    amount = float(item.evidence.get("amount") or 0.0) / 100_000_000
    decision_bonus = {
        "重点观察": 30.0,
        "观察": 18.0,
        "等待回踩": 10.0,
        "谨慎复盘": 0.0,
        "回避": -40.0,
    }.get(item.decision, 0.0)
    return (decision_bonus + score, item.confidence, amount)


def _decision_label(
    row: Any,
    components: dict[str, float],
    rules: list[str],
    trend: dict[str, float | None],
    note: Any,
    news_signal: dict[str, float],
) -> str:
    score = float(row["total_score"] or 0.0)
    pct = float(row["pct_change"] or 0.0)
    turnover = float(row["turnover_rate"] or 0.0)
    status = note["status"] if note is not None else ""
    risk_score = _risk_score(components, rules, trend)

    if status == "avoid" or news_signal["risk"] >= 2 or risk_score >= 35 or score < 15:
        return "回避"
    if pct > 7 or turnover > 15 or trend.get("pct_5d", 0) and float(trend["pct_5d"] or 0) > 12:
        return "等待回踩"
    if score >= 85 and risk_score <= 15 and news_signal["risk"] == 0:
        return "重点观察"
    if score >= 55 and risk_score <= 25:
        if news_signal["positive"] >= 1 and news_signal["risk"] == 0 and score >= 70:
            return "重点观察"
        return "观察"
    return "谨慎复盘"


def _strengths(
    row: Any,
    components: dict[str, float],
    rules: list[str],
    trend: dict[str, float | None],
    note: Any,
    news: list[Any],
    factor_signals: list[Any],
) -> list[str]:
    items: list[str] = []
    score = float(row["total_score"] or 0.0)
    amount = float(row["amount"] or 0.0)
    if score >= 80:
        items.append(f"综合评分较高：{score:.2f}")
    if amount >= 1_000_000_000:
        items.append(f"成交额活跃：{amount / 100_000_000:.2f} 亿")
    elif amount >= 100_000_000:
        items.append(f"成交额达到可观察区间：{amount / 100_000_000:.2f} 亿")
    if components.get("trend_ma20", 0) > 0:
        items.append("日线结构在 MA20 上方，趋势条件较好")
    if components.get("trend_ma5", 0) > 0:
        items.append("收盘价位于 MA5 上方，短线强度尚可")
    if components.get("turnover_quality", 0) > 0:
        items.append("换手率处在相对健康区间")
    if "close_near_intraday_high" in rules:
        items.append("盘中收在相对高位，承接表现较强")
    if note is not None and note["tags"]:
        items.append(f"本地标签：{note['tags']}")
    for signal in factor_signals:
        if signal.direction == "positive":
            items.append(f"公式型因子：{signal.name}，{signal.reason}")
            break
    for item in news:
        tags = item["tags"] or ""
        if any(token in tags for token in ("业绩预告", "并购投资", "AI算力", "新能源", "消费")):
            items.append(f"消息催化：{item['title']}")
            break
    if not items:
        items.append("暂无特别突出的正向证据")
    return items[:5]


def _risks(
    row: Any,
    components: dict[str, float],
    rules: list[str],
    trend: dict[str, float | None],
    note: Any,
    news: list[Any],
    factor_signals: list[Any],
) -> list[str]:
    items: list[str] = []
    pct = float(row["pct_change"] or 0.0)
    turnover = float(row["turnover_rate"] or 0.0)
    if pct > 7:
        items.append("涨幅接近高位，追高风险较大")
    if turnover > 15:
        items.append("换手率偏热，短线分歧可能加大")
    if components.get("name_risk", 0) < 0:
        items.append("名称触发 ST/PT/退市类风险")
    if components.get("trend_ma20", 0) < 0:
        items.append("价格低于 MA20，中期趋势仍需修复")
    if components.get("volatility", 0) < 0:
        items.append("近 20 日波动偏高")
    if "near_limit_up_chase_risk" in rules:
        items.append("规则触发接近涨停追高风险")
    if note is not None and note["status"] == "avoid":
        items.append("本地观察状态已标记为回避")
    for item in news:
        tags = item["tags"] or ""
        if any(token in tags for token in ("退市风险", "政策监管")):
            items.append(f"风险消息：{item['title']}")
            break
    for signal in factor_signals:
        if signal.direction == "risk":
            items.append(f"公式型风险：{signal.name}，{signal.reason}")
            break
    if not items:
        items.append("当前主要风险不突出，但仍需结合大盘和行业环境复核")
    return items[:5]


def _next_actions(decision: str, row: Any, trend: dict[str, float | None], risks: list[str], note: Any) -> list[str]:
    price = row["latest_price"]
    ma5 = trend.get("ma5")
    ma20 = trend.get("ma20")
    actions: list[str] = []
    if decision == "重点观察":
        actions.append("加入重点观察池，下一步复核行业催化、公告和财务质量")
        if ma5:
            actions.append(f"关注是否能在 MA5 附近企稳，MA5 约 {ma5:.2f}")
    elif decision == "观察":
        actions.append("保留在观察池，等待评分连续性和量价结构进一步确认")
        if ma20:
            actions.append(f"若回踩不破 MA20 附近 {ma20:.2f}，再复盘强弱")
    elif decision == "等待回踩":
        actions.append("不追高，等待缩量回踩或盘中分歧后的承接确认")
        if price and ma5:
            actions.append(f"现价 {float(price):.2f}，优先观察 MA5 附近 {ma5:.2f} 的支撑")
    elif decision == "回避":
        actions.append("暂不进入候选池，除非风险项消失并重新获得正向评分")
    else:
        actions.append("进入复盘清单，补充基本面、行业位置和近期公告信息")
    if note is not None and note["note"]:
        actions.append(f"结合本地备注复核：{note['note'][:60]}")
    return actions[:4]


def _confidence(row: Any, components: dict[str, float], trend: dict[str, float | None], note: Any, news: list[Any]) -> float:
    confidence = 45.0
    if row["latest_price"] is not None and row["pct_change"] is not None:
        confidence += 15
    if row["amount"] is not None and row["turnover_rate"] is not None:
        confidence += 12
    if len(components) >= 6:
        confidence += 10
    if trend.get("bar_count", 0) and float(trend["bar_count"] or 0) >= 20:
        confidence += 12
    if note is not None and (note["tags"] or note["note"]):
        confidence += 6
    if news:
        confidence += 5
    return min(95.0, confidence)


def _summary(
    row: Any,
    decision: str,
    confidence: float,
    strengths: list[str],
    risks: list[str],
    trend: dict[str, float | None],
    news: list[Any],
) -> str:
    score = float(row["total_score"] or 0.0)
    name = row["name"] or row["symbol"]
    pct = row["pct_change"]
    pct_text = "-" if pct is None else f"{float(pct):.2f}%"
    trend_text = ""
    if trend.get("ma5") is not None and trend.get("ma20") is not None:
        trend_text = f"，MA5 {float(trend['ma5']):.2f} / MA20 {float(trend['ma20']):.2f}"
    news_text = f"相关新闻 {len(news)} 条。" if news else "暂无匹配相关新闻。"
    return (
        f"{name} 当前 AI 结论为“{decision}”，置信度 {confidence:.0f}。"
        f"综合评分 {score:.2f}，涨跌幅 {pct_text}{trend_text}。"
        f"{news_text}核心正向证据：{strengths[0]}；主要风险：{risks[0]}。"
    )


def _risk_score(components: dict[str, float], rules: list[str], trend: dict[str, float | None]) -> float:
    risk = 0.0
    for value in components.values():
        if value < 0:
            risk += abs(float(value))
    if "near_limit_up_chase_risk" in rules:
        risk += 12
    if trend.get("volatility_20d") and float(trend["volatility_20d"] or 0) > 6:
        risk += 8
    return risk


def _news_signal(news: list[Any]) -> dict[str, float]:
    positive = 0.0
    risk = 0.0
    for item in news:
        tags = item["tags"] or ""
        if any(token in tags for token in ("业绩预告", "并购投资", "AI算力", "新能源", "消费")):
            positive += 1
        if any(token in tags for token in ("退市风险", "政策监管")):
            risk += 1
    return {"positive": positive, "risk": risk}


def _news_evidence(item: Any) -> dict[str, object]:
    return {
        "news_id": item["news_id"],
        "title": item["title"],
        "summary": item["summary"],
        "source": item["source"],
        "event_time": item["event_time"],
        "tags": item["tags"],
        "importance": item["importance"],
    }


def _factor_evidence(repo: Any, signals: list[Any]) -> list[dict[str, object]]:
    quality = _factor_quality_map(repo)
    rows = []
    for item in signals:
        row = asdict(item)
        quality_row = quality.get(item.factor_id, {})
        row["effectiveness_score"] = quality_row.get("effectiveness_score")
        row["effectiveness_verdict"] = quality_row.get("verdict")
        row["effectiveness_samples"] = quality_row.get("total_samples")
        rows.append(row)
    return rows


def _factor_quality_map(repo: Any) -> dict[str, dict[str, object]]:
    db_path = str(getattr(repo, "db_path", "default"))
    if db_path not in _FACTOR_QUALITY_CACHE:
        try:
            rows = repo.factor_backtest_matrix(horizons=[5], limit_symbols=300)
        except Exception:
            rows = []
        _FACTOR_QUALITY_CACHE[db_path] = {str(row["factor_id"]): row for row in rows}
    return _FACTOR_QUALITY_CACHE[db_path]


def _context_news(repo: Any, row: Any, limit: int = 3) -> list[Any]:
    name = row["name"] or ""
    board = row["board"] or ""
    tags: list[str] = []
    if any(token in name for token in ("芯", "半导体", "光电", "电子", "科技")) or board == "科创板":
        tags.append("AI算力")
    if any(token in name for token in ("消费", "食品", "旅游", "传媒", "菜百", "金龙鱼")):
        tags.append("消费")
    if any(token in name for token in ("锂", "电池", "光伏", "能源")):
        tags.append("新能源")
    rows: list[Any] = []
    seen: set[str] = set()
    for tag in tags:
        for item in repo.latest_news(limit=limit, tag=tag):
            if item["news_id"] in seen:
                continue
            seen.add(item["news_id"])
            rows.append(item)
            if len(rows) >= limit:
                return rows
    return rows


def _trend_metrics(rows: list[Any]) -> dict[str, float | None]:
    closes = [float(row["close"]) for row in rows if row["close"] is not None]
    if not closes:
        return {"bar_count": 0, "ma5": None, "ma20": None, "pct_5d": None, "pct_20d": None, "volatility_20d": None}
    ma5 = sum(closes[-5:]) / min(5, len(closes))
    ma20 = sum(closes[-20:]) / min(20, len(closes))
    pct_5d = (closes[-1] / closes[-5] - 1) * 100 if len(closes) >= 5 and closes[-5] else None
    pct_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 and closes[-20] else None
    volatility = None
    if len(closes) >= 20:
        returns = [closes[index] / closes[index - 1] - 1 for index in range(len(closes) - 19, len(closes)) if closes[index - 1]]
        if returns:
            avg = sum(returns) / len(returns)
            volatility = math.sqrt(sum((item - avg) ** 2 for item in returns) / len(returns)) * 100
    return {
        "bar_count": len(closes),
        "ma5": ma5,
        "ma20": ma20,
        "pct_5d": pct_5d,
        "pct_20d": pct_20d,
        "volatility_20d": volatility,
    }
