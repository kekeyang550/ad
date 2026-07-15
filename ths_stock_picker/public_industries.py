from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path


EASTMONEY_INDUSTRY_SOURCE = Path("eastmoney://company-survey/industry")
_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688")


@dataclass(frozen=True)
class IndustryClassification:
    symbol: str
    industry: str
    source_file: Path = EASTMONEY_INDUSTRY_SOURCE


def fetch_eastmoney_industry_one(symbol: str, timeout: float = 15.0) -> IndustryClassification | None:
    """Fetch a current Eastmoney industry label for one A-share symbol."""
    clean_symbol = symbol.strip()
    if not _is_a_share_symbol(clean_symbol):
        return None
    market = "SH" if clean_symbol.startswith(("600", "601", "603", "605", "688")) else "SZ"
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={market}{clean_symbol}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    profiles = payload.get("jbzl") if isinstance(payload, dict) else None
    profile = profiles[0] if isinstance(profiles, list) and profiles and isinstance(profiles[0], dict) else None
    if profile is None:
        return None
    industry = str(profile.get("EM2016") or profile.get("INDUSTRYCSRC1") or "").strip()
    return IndustryClassification(symbol=clean_symbol, industry=industry) if industry and industry != "-" else None


def _is_a_share_symbol(symbol: str) -> bool:
    return len(symbol) == 6 and symbol.isdigit() and symbol.startswith(_A_SHARE_PREFIXES)
