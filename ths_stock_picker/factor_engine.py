from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
        factor_id="overheat_chase_risk",
        name="追高过热风险",
        category="风险",
        description="近 5 日涨幅过大或当日长上影放量，提示不宜盲目追高。",
        source="公式思路库：涨停后风险、长上影、放量滞涨类",
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


def _ma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
