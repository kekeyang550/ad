from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_COMPONENT_WEIGHTS: dict[str, float] = {
    "name_risk": 1.0,
    "invalid_quote": 1.0,
    "price_band": 1.0,
    "intraday_momentum": 1.0,
    "liquidity": 1.0,
    "intraday_position": 1.0,
    "gap_risk": 1.0,
    "market_cap_tier": 1.0,
    "turnover_quality": 1.0,
    "board_risk": 1.0,
    "board_quality": 1.0,
    "trend_ma5": 1.0,
    "momentum_5d": 1.0,
    "trend_ma20": 1.0,
    "momentum_20d": 1.0,
    "volatility": 1.0,
}


@dataclass(frozen=True)
class ScoringProfile:
    name: str = "default"
    component_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_COMPONENT_WEIGHTS))
    disabled_components: frozenset[str] = frozenset()

    def apply(self, components: dict[str, float]) -> dict[str, float]:
        adjusted: dict[str, float] = {}
        for name, value in components.items():
            if name in self.disabled_components:
                continue
            weight = self.component_weights.get(name, 1.0)
            adjusted[name] = float(value) * float(weight)
        return adjusted


def default_scoring_profile() -> ScoringProfile:
    return ScoringProfile()


def load_scoring_profile(path: Path | None) -> ScoringProfile:
    if path is None:
        return default_scoring_profile()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("scoring profile must be a JSON object")

    weights_payload = payload.get("component_weights", {})
    if not isinstance(weights_payload, dict):
        raise ValueError("component_weights must be a JSON object")
    weights: dict[str, float] = dict(DEFAULT_COMPONENT_WEIGHTS)
    for key, value in weights_payload.items():
        weights[str(key)] = float(value)

    disabled_payload = payload.get("disabled_components", [])
    if not isinstance(disabled_payload, list):
        raise ValueError("disabled_components must be a JSON array")
    disabled = frozenset(str(item) for item in disabled_payload)

    name = str(payload.get("name") or Path(path).stem)
    return ScoringProfile(name=name, component_weights=weights, disabled_components=disabled)


def write_default_scoring_profile(path: Path) -> None:
    payload = {
        "name": "default",
        "component_weights": DEFAULT_COMPONENT_WEIGHTS,
        "disabled_components": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
