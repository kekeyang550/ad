from __future__ import annotations

from dataclasses import dataclass
from typing import Any


FACTOR_ENGINE_VERSION = 7


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    name: str
    category: str
    description: str
    source: str
    future_function_risk: str = "low"


@dataclass(frozen=True)
class FactorSignal:
    factor_id: str
    name: str
    category: str
    strength: float
    direction: str
    reason: str


FACTOR_DEFINITIONS = [
    FactorDefinition(
        factor_id="ma_multi_breakout",
        name="均线共振突破",
        category="趋势",
        description="收盘价同时站上 MA5/MA10/MA20，且当日最低价曾低于至少一条均线，类似一阳穿多线思路。",
        source="公式思路库：股旁网/通达信公式常见均线突破类",
    ),
    FactorDefinition(
        factor_id="volume_breakout",
        name="温和放量突破",
        category="量价",
        description="收盘价创近 20 日新高附近，同时成交量为近 5 日均量的 1.2-3 倍，避免极端爆量追高。",
        source="公式思路库：成交量、量比、平台突破类",
    ),
    FactorDefinition(
        factor_id="ma20_pullback",
        name="MA20 缩量回踩",
        category="回踩",
        description="中期趋势仍在 MA20 上方，价格回踩 MA20 附近且成交量低于近 5 日均量。",
        source="公式思路库：趋势回踩、均线支撑类",
    ),
    FactorDefinition(
        factor_id="macd_zero_axis_cross",
        name="MACD 零轴上金叉",
        category="动量",
        description="DIF 在零轴上方向上穿越 DEA，强调上升趋势中的动量再次增强。",
        source="经典技术分析：MACD 趋势确认与零轴上金叉思路。",
    ),
    FactorDefinition(
        factor_id="rsi14_recovery",
        name="RSI14 回升确认",
        category="动量",
        description="RSI14 从弱势区上穿 45，且收盘仍在 MA20 上方，识别趋势内修复。",
        source="经典技术分析：RSI 强弱区间与趋势过滤思路。",
    ),
    FactorDefinition(
        factor_id="kdj_low_cross",
        name="KDJ 低位金叉",
        category="动量",
        description="K 线上穿 D 线且仍位于低位，收盘不显著跌破 MA20，识别趋势中的短线修复。",
        source="经典技术分析：KDJ 随机指标低位金叉与趋势过滤思路。",
    ),
    FactorDefinition(
        factor_id="limit_up_pullback",
        name="涨停后缩量回踩",
        category="回踩",
        description="近 8 日出现约 10% 的单日强势上涨后，价格缩量回到短期均线附近且仍守住 MA20。",
        source="经典技术分析：强势涨停后回踩均线、缩量确认思路。",
    ),
    FactorDefinition(
        factor_id="platform_breakout",
        name="平台整理突破",
        category="突破",
        description="近 20 日价格波动收敛，收盘突破整理区间高点且量能不低于近 5 日均量。",
        source="经典技术分析：箱体整理与平台突破思路。",
    ),
    FactorDefinition(
        factor_id="disclosed_profitability_quality",
        name="已披露盈利质量",
        category="基本面",
        description="只在公告日后的下一个交易日起使用已披露报表：加权 ROE 不低于 8%，且营业收入和归母净利润为正。",
        source="财务质量基础规则：以公告日期作为信息可见性边界。",
    ),
    FactorDefinition(
        factor_id="disclosed_growth_quality",
        name="已披露增长质量",
        category="基本面",
        description="只在公告日后的下一个交易日起使用已披露的营收同比和归母净利同比，要求两项均不为负。",
        source="财务质量基础规则：使用源数据的同比字段，以公告日期作为信息可见性边界。",
    ),
    FactorDefinition(
        factor_id="disclosed_cashflow_quality",
        name="已披露现金流质量",
        category="基本面",
        description="只在公告日后的下一个交易日起使用已披露报表：经营活动现金流为正，且不低于归母净利润的 80%。",
        source="财务质量基础规则：经营现金流与归母净利润同口径比较，并以公告日期作为信息可见性边界。",
    ),
    FactorDefinition(
        factor_id="overheat_chase_risk",
        name="追高过热风险",
        category="风险",
        description="近 5 日涨幅过大或当日长上影放量，提示不宜盲目追高。",
        source="公式思路库：涨停后风险、长上影、放量滞涨类",
    ),
    FactorDefinition(
        factor_id="rps_60_strength",
        name="RPS60 相对强势",
        category="相对强弱",
        description="近 60 个交易日涨幅在可比股票池中排名靠前，反映中期相对强度。",
        source="RPS 相对强弱思路：阶段涨幅横截面排名，常用于趋势选股。",
    ),
    FactorDefinition(
        factor_id="rps_120_strength",
        name="RPS120 长期强势",
        category="相对强弱",
        description="近 120 个交易日涨幅在可比股票池中排名靠前，反映更长周期趋势强度。",
        source="RPS 相对强弱思路：阶段涨幅横截面排名，常用于趋势选股。",
    ),
    FactorDefinition(
        factor_id="rps_60_weakness",
        name="RPS60 相对弱势",
        category="风险",
        description="近 60 个交易日涨幅在可比股票池中排名靠后，提示相对弱势风险。",
        source="RPS 相对强弱思路：阶段涨幅横截面排名，常用于趋势过滤。",
    ),
]


def evaluate_factors(bars: list[Any]) -> list[FactorSignal]:
    clean = [_bar_dict(row) for row in bars if _value(row, "close") is not None]
    clean.sort(key=lambda item: item["trade_date"])
    if len(clean) < 20:
        return []

    closes = [float(item["close"]) for item in clean]
    highs = [float(item["high"] if item["high"] is not None else item["close"]) for item in clean]
    lows = [float(item["low"] if item["low"] is not None else item["close"]) for item in clean]
    volumes = [float(item["volume"] or 0.0) for item in clean]
    latest = clean[-1]
    close = closes[-1]
    low = lows[-1]
    high = highs[-1]
    volume = volumes[-1]
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    avg_vol5 = _ma(volumes, 5)
    prev_20_high = max(highs[-20:-1]) if len(highs) >= 20 else max(highs[:-1])
    signals: list[FactorSignal] = []

    if ma5 and ma10 and ma20 and close > ma5 and close > ma10 and close > ma20 and low < max(ma5, ma10, ma20):
        signals.append(
            FactorSignal(
                "ma_multi_breakout",
                "均线共振突破",
                "趋势",
                _clamp((close / ma20 - 1) * 100 + 70, 50, 95),
                "positive",
                f"{latest['trade_date']} 收盘站上 MA5/MA10/MA20，盘中回踩后收复均线。",
            )
        )

    volume_ratio = volume / avg_vol5 if avg_vol5 else 0.0
    if prev_20_high and close >= prev_20_high * 0.995 and 1.2 <= volume_ratio <= 3.0:
        signals.append(
            FactorSignal(
                "volume_breakout",
                "温和放量突破",
                "量价",
                _clamp(60 + (volume_ratio - 1.2) * 15, 55, 90),
                "positive",
                f"{latest['trade_date']} 接近/突破 20 日高点，量能约为 5 日均量 {volume_ratio:.2f} 倍。",
            )
        )

    if ma20 and avg_vol5 and close >= ma20 and abs(close / ma20 - 1) <= 0.025 and volume < avg_vol5:
        signals.append(
            FactorSignal(
                "ma20_pullback",
                "MA20 缩量回踩",
                "回踩",
                _clamp(75 - abs(close / ma20 - 1) * 600, 55, 85),
                "positive",
                f"{latest['trade_date']} 价格贴近 MA20 且成交量低于 5 日均量。",
            )
        )

    if len(closes) >= 35:
        dif, dea = _macd(closes)
        if dif[-2] <= dea[-2] and dif[-1] > dea[-1] and dif[-1] > 0:
            signals.append(
                FactorSignal(
                    "macd_zero_axis_cross",
                    "MACD 零轴上金叉",
                    "动量",
                    _clamp(62 + (dif[-1] - dea[-1]) / close * 2400, 55, 92),
                    "positive",
                    f"{latest['trade_date']} DIF 在零轴上向上穿越 DEA，趋势动量重新增强。",
                )
            )

    rsi14 = _rsi(closes, 14)
    if rsi14 is not None and len(rsi14) >= 2 and ma20 and close >= ma20:
        previous_rsi = rsi14[-2]
        latest_rsi = rsi14[-1]
        if previous_rsi < 45 <= latest_rsi <= 65:
            signals.append(
                FactorSignal(
                    "rsi14_recovery",
                    "RSI14 回升确认",
                    "动量",
                    _clamp(58 + (latest_rsi - previous_rsi) * 2.5, 52, 88),
                    "positive",
                    f"{latest['trade_date']} RSI14 从 {previous_rsi:.1f} 回升至 {latest_rsi:.1f}，且收盘仍在 MA20 上方。",
                )
            )

    kdj = _kdj(highs, lows, closes)
    if kdj is not None and len(kdj) >= 2 and ma20 and close >= ma20 * 0.98:
        previous_k, previous_d, _ = kdj[-2]
        latest_k, latest_d, latest_j = kdj[-1]
        if previous_k <= previous_d and latest_k > latest_d and latest_k <= 50 and latest_j <= 75:
            signals.append(
                FactorSignal(
                    "kdj_low_cross",
                    "KDJ 低位金叉",
                    "动量",
                    _clamp(58 + (latest_k - latest_d) * 1.2 + (50 - latest_k) * 0.25, 52, 85),
                    "positive",
                    f"{latest['trade_date']} KDJ 在低位金叉（K {latest_k:.1f}，D {latest_d:.1f}），价格仍接近 MA20。",
                )
            )

    if ma10 and ma20 and avg_vol5 and close >= ma20 and volume < avg_vol5:
        recent_limit_index = next(
            (
                index
                for index in range(len(closes) - 2, max(len(closes) - 10, 1) - 1, -1)
                if closes[index - 1] > 0 and closes[index] / closes[index - 1] - 1 >= 0.095
            ),
            None,
        )
        if recent_limit_index is not None and abs(close / ma10 - 1) <= 0.035:
            days_since_limit = len(closes) - 1 - recent_limit_index
            signals.append(
                FactorSignal(
                    "limit_up_pullback",
                    "涨停后缩量回踩",
                    "回踩",
                    _clamp(72 - days_since_limit * 2 + (1 - volume_ratio) * 12, 55, 88),
                    "positive",
                    f"{latest['trade_date']} 距约 10% 强势上涨 {days_since_limit} 日，缩量回到 MA10 附近且仍守住 MA20。",
                )
            )

    if len(highs) >= 21 and avg_vol5 and avg_vol5 > 0:
        platform_high = max(highs[-21:-1])
        platform_low = min(lows[-21:-1])
        platform_width = (platform_high / platform_low - 1) * 100 if platform_low > 0 else 0.0
        if platform_width <= 12 and close > platform_high and 1.0 <= volume_ratio <= 3.5:
            signals.append(
                FactorSignal(
                    "platform_breakout",
                    "平台整理突破",
                    "突破",
                    _clamp(60 + (close / platform_high - 1) * 900 + volume_ratio * 4, 55, 90),
                    "positive",
                    f"{latest['trade_date']} 突破近 20 日 {platform_width:.1f}% 整理区间，量能约为 5 日均量 {volume_ratio:.2f} 倍。",
                )
            )

    pct_5d = (close / closes[-5] - 1) * 100 if closes[-5] else 0.0
    upper_shadow = (high - close) / close * 100 if close else 0.0
    if pct_5d > 12 or (upper_shadow > 3 and volume_ratio > 1.5):
        signals.append(
            FactorSignal(
                "overheat_chase_risk",
                "追高过热风险",
                "风险",
                _clamp(max(pct_5d * 4, upper_shadow * 15), 50, 95),
                "risk",
                f"{latest['trade_date']} 5 日涨幅 {pct_5d:.2f}%，上影 {upper_shadow:.2f}%，追高需谨慎。",
            )
        )
    return signals


def evaluate_disclosed_fundamental(fundamental: Any | None, signal_date: str) -> list[FactorSignal]:
    if fundamental is None:
        return []
    report_date = str(_value(fundamental, "report_date") or "")
    notice_date = str(_value(fundamental, "notice_date") or "")
    roe = _as_float(_value(fundamental, "roe"))
    revenue = _as_float(_value(fundamental, "revenue"))
    net_profit = _as_float(_value(fundamental, "net_profit"))
    revenue_yoy = _as_float(_value(fundamental, "revenue_yoy"))
    net_profit_yoy = _as_float(_value(fundamental, "net_profit_yoy"))
    operating_cash_flow = _as_float(_value(fundamental, "operating_cash_flow"))
    # Date-only disclosures are treated as available from the following trading day.
    if not report_date or not notice_date or notice_date >= signal_date:
        return []
    signals: list[FactorSignal] = []
    if roe is not None and revenue is not None and net_profit is not None and roe >= 8 and revenue > 0 and net_profit > 0:
        signals.append(
            FactorSignal(
                "disclosed_profitability_quality",
                "已披露盈利质量",
                "基本面",
                _clamp(55 + (min(roe, 25) - 8) * 2, 55, 89),
                "positive",
                f"{signal_date} 可使用 {notice_date} 已披露的 {report_date} 报表：加权 ROE {roe:.2f}%，营收和归母净利润均为正。",
            )
        )
    if revenue_yoy is not None and net_profit_yoy is not None and revenue_yoy >= 0 and net_profit_yoy >= 0:
        signals.append(
            FactorSignal(
                "disclosed_growth_quality",
                "已披露增长质量",
                "基本面",
                _clamp(55 + min((revenue_yoy + net_profit_yoy) / 2, 35), 55, 90),
                "positive",
                f"{signal_date} 可使用 {notice_date} 已披露的 {report_date} 报表：营收同比 {revenue_yoy:.2f}%，归母净利同比 {net_profit_yoy:.2f}%。",
            )
        )
    if operating_cash_flow is not None and net_profit is not None and net_profit > 0:
        cash_conversion = operating_cash_flow / net_profit
        if operating_cash_flow > 0 and cash_conversion >= 0.8:
            signals.append(
                FactorSignal(
                    "disclosed_cashflow_quality",
                    "已披露现金流质量",
                    "基本面",
                    _clamp(55 + min(cash_conversion, 2.5) * 12, 55, 85),
                    "positive",
                    f"{signal_date} 可使用 {notice_date} 已披露的 {report_date} 报表：经营现金流为归母净利的 {cash_conversion:.2f} 倍。",
                )
            )
    return signals


def factor_definitions() -> list[FactorDefinition]:
    return list(FACTOR_DEFINITIONS)


def _bar_dict(row: Any) -> dict[str, Any]:
    return {
        "trade_date": _value(row, "trade_date"),
        "open": _value(row, "open"),
        "high": _value(row, "high"),
        "low": _value(row, "low"),
        "close": _value(row, "close"),
        "volume": _value(row, "volume"),
    }


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[key]


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _ema(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (window + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(value * alpha + output[-1] * (1 - alpha))
    return output


def _macd(values: list[float]) -> tuple[list[float], list[float]]:
    fast = _ema(values, 12)
    slow = _ema(values, 26)
    dif = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    return dif, _ema(dif, 9)


def _rsi(values: list[float], window: int) -> list[float] | None:
    if len(values) < window + 1:
        return None
    gains = [max(values[index] - values[index - 1], 0.0) for index in range(1, len(values))]
    losses = [max(values[index - 1] - values[index], 0.0) for index in range(1, len(values))]
    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    result = []
    for index in range(window, len(gains) + 1):
        if index > window:
            avg_gain = (avg_gain * (window - 1) + gains[index - 1]) / window
            avg_loss = (avg_loss * (window - 1) + losses[index - 1]) / window
        if avg_loss == 0:
            result.append(100.0 if avg_gain > 0 else 50.0)
        else:
            relative_strength = avg_gain / avg_loss
            result.append(100 - 100 / (1 + relative_strength))
    return result


def _kdj(
    highs: list[float], lows: list[float], closes: list[float], window: int = 9
) -> list[tuple[float, float, float]] | None:
    if len(highs) != len(lows) or len(highs) != len(closes) or len(closes) < window:
        return None
    k = 50.0
    d = 50.0
    result = []
    for index in range(window - 1, len(closes)):
        period_high = max(highs[index - window + 1 : index + 1])
        period_low = min(lows[index - window + 1 : index + 1])
        rsv = (closes[index] - period_low) / (period_high - period_low) * 100 if period_high > period_low else 50.0
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        result.append((k, d, 3 * k - 2 * d))
    return result


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
