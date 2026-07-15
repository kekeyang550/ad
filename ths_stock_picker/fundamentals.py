from __future__ import annotations

import csv
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path


HEADER_ALIASES = {
    "symbol": ("symbol", "code", "代码", "证券代码", "股票代码"),
    "report_date": ("report_date", "report_period", "报告期", "报告日期", "财报期", "截止日期"),
    "notice_date": ("notice_date", "公告日期", "披露日期", "最新公告日"),
    "revenue": ("revenue", "营业收入", "营业总收入", "营收"),
    "revenue_yoy": ("revenue_yoy", "营业收入同比", "营收同比", "营业收入同比增长率"),
    "net_profit": ("net_profit", "净利润", "归母净利润", "归属于母公司股东的净利润"),
    "net_profit_yoy": ("net_profit_yoy", "净利润同比", "归母净利润同比", "归母净利润同比增长率"),
    "roe": ("roe", "净资产收益率", "roe(加权)", "加权roe"),
    "operating_cash_flow": ("operating_cash_flow", "经营现金流", "经营活动产生的现金流量净额"),
    "pe_ttm": ("pe_ttm", "市盈率ttm", "市盈率(动)", "petTm"),
    "pb": ("pb", "市净率"),
}


@dataclass(frozen=True)
class FundamentalRecord:
    symbol: str
    report_date: str
    notice_date: str | None
    revenue: float | None
    net_profit: float | None
    roe: float | None
    operating_cash_flow: float | None
    pe_ttm: float | None
    pb: float | None
    source_file: Path
    revenue_yoy: float | None = None
    net_profit_yoy: float | None = None


def load_fundamentals_csv(path: Path, default_symbol: str | None = None) -> list[FundamentalRecord]:
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        mapping = _map_headers(reader.fieldnames)
        records: list[FundamentalRecord] = []
        for row in reader:
            symbol = _clean_symbol(_get(row, mapping, "symbol") or default_symbol or "")
            report_date = _clean_date(_get(row, mapping, "report_date") or "")
            if not symbol or not report_date:
                continue
            records.append(
                FundamentalRecord(
                    symbol=symbol,
                    report_date=report_date,
                    notice_date=_clean_date(_get(row, mapping, "notice_date") or "") or None,
                    revenue=_to_float(_get(row, mapping, "revenue")),
                    revenue_yoy=_to_float(_get(row, mapping, "revenue_yoy")),
                    net_profit=_to_float(_get(row, mapping, "net_profit")),
                    net_profit_yoy=_to_float(_get(row, mapping, "net_profit_yoy")),
                    roe=_to_float(_get(row, mapping, "roe")),
                    operating_cash_flow=_to_float(_get(row, mapping, "operating_cash_flow")),
                    pe_ttm=_to_float(_get(row, mapping, "pe_ttm")),
                    pb=_to_float(_get(row, mapping, "pb")),
                    source_file=path,
                )
            )
    return records


def fetch_eastmoney_fundamentals_one(symbol: str, reports: int = 8, timeout: float = 15.0) -> list[FundamentalRecord]:
    clean_symbol = _clean_symbol(symbol)
    if not clean_symbol:
        return []
    rows = _fetch_eastmoney_rows(
        "RPT_LICO_FN_CPD",
        "SECURITY_CODE,REPORTDATE,NOTICE_DATE,TOTAL_OPERATE_INCOME,YSTZ,PARENT_NETPROFIT,SJLTZ,WEIGHTAVG_ROE",
        clean_symbol,
        reports,
        "NOTICE_DATE",
        timeout,
    )
    operating_cash_flow_by_report = _fetch_eastmoney_operating_cash_flow(clean_symbol, reports, timeout)
    records: list[FundamentalRecord] = []
    source_file = Path(f"eastmoney://RPT_LICO_FN_CPD/{clean_symbol}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        report_date = _clean_date(str(row.get("REPORTDATE") or ""))
        if not report_date:
            continue
        records.append(
            FundamentalRecord(
                symbol=_clean_symbol(str(row.get("SECURITY_CODE") or clean_symbol)),
                report_date=report_date,
                notice_date=_clean_date(str(row.get("NOTICE_DATE") or "")) or None,
                revenue=_to_float(str(row.get("TOTAL_OPERATE_INCOME") or "")),
                revenue_yoy=_to_float(str(row.get("YSTZ") or "")),
                net_profit=_to_float(str(row.get("PARENT_NETPROFIT") or "")),
                net_profit_yoy=_to_float(str(row.get("SJLTZ") or "")),
                roe=_to_float(str(row.get("WEIGHTAVG_ROE") or "")),
                operating_cash_flow=operating_cash_flow_by_report.get(report_date),
                pe_ttm=None,
                pb=None,
                source_file=source_file,
            )
        )
    return records


def _fetch_eastmoney_operating_cash_flow(symbol: str, reports: int, timeout: float) -> dict[str, float]:
    """Supplement values only; the main report remains the disclosure-date authority."""
    try:
        rows = _fetch_eastmoney_rows(
            "RPT_DMSK_FN_CASHFLOW",
            "SECURITY_CODE,REPORT_DATE,NOTICE_DATE,NETCASH_OPERATE",
            symbol,
            reports,
            "REPORT_DATE",
            timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        # The core report remains usable if this supplemental statement is unavailable.
        return {}
    values: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        report_date = _clean_date(str(row.get("REPORT_DATE") or ""))
        operating_cash_flow = _to_float(str(row.get("NETCASH_OPERATE") or ""))
        if report_date and operating_cash_flow is not None and report_date not in values:
            values[report_date] = operating_cash_flow
    return values


def _fetch_eastmoney_rows(
    report_name: str,
    columns: str,
    symbol: str,
    reports: int,
    sort_column: str,
    timeout: float,
) -> list[dict[str, object]]:
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": f'(SECURITY_CODE="{symbol}")',
        "pageNumber": "1",
        "pageSize": str(max(1, min(reports, 50))),
        "sortTypes": "-1",
        "sortColumns": sort_column,
        "source": "WEB",
        "client": "WEB",
    }
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("data") if isinstance(result, dict) else None
    return rows if isinstance(rows, list) else []


def _map_headers(headers: list[str]) -> dict[str, str]:
    normalized = {_normalize(header): header for header in headers}
    mapping: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            matched = normalized.get(_normalize(alias))
            if matched:
                mapping[canonical] = matched
                break
    return mapping


def _get(row: dict[str, str], mapping: dict[str, str], canonical: str) -> str | None:
    header = mapping.get(canonical)
    return row.get(header) if header is not None else None


def _normalize(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "").replace("（", "(").replace("）", ")")


def _clean_symbol(value: str) -> str:
    digits = "".join(character for character in value.strip() if character.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _clean_date(value: str) -> str:
    normalized = value.strip().split(" ", 1)[0].replace("/", "-").replace(".", "-")
    normalized = normalized.replace("年", "-").replace("月", "-").replace("日", "")
    if len(normalized) == 8 and normalized.isdigit():
        try:
            return date(int(normalized[:4]), int(normalized[4:6]), int(normalized[6:8])).isoformat()
        except ValueError:
            return ""
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError:
        return ""


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    normalized = value.strip().replace(",", "").replace("%", "")
    if normalized in {"", "--", "None", "nan", "N/A"}:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None
