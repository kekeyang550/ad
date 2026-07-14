from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import json
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import NewsItem
from .shared_read import read_bytes_shared
from .ths_local import DEFAULT_THS_ROOT


EASTMONEY_ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
PUBLIC_ANNOUNCEMENT_SOURCE_FILE = Path("public/eastmoney_announcements")


def default_ths_news_files(root: Path = DEFAULT_THS_ROOT) -> list[Path]:
    root = Path(root)
    candidates = sorted(root.glob("*/*/text/同花顺/实时解盘.xml"))
    direct = root.glob("*/text/同花顺/实时解盘.xml")
    files = sorted(set(candidates) | set(direct))
    fallback = root / "杨斌xT" / "text" / "同花顺" / "实时解盘.xml"
    if fallback.exists() and fallback not in files:
        files.append(fallback)
    return files


def load_ths_news_xml(path: Path, limit: int | None = None) -> list[NewsItem]:
    raw = read_bytes_shared(path)
    text = raw.decode("gb18030", errors="replace")
    root = ET.fromstring(text)
    rows: list[NewsItem] = []
    for data in root.findall(".//data"):
        title = _node_text(data, "title")
        news_id = _node_text(data, "id") or _fallback_news_id(title)
        properties = _parse_properties(_node_text(data, "properties"))
        timestamp = _node_text(data, "time") or properties.get("ctime", "")
        event_time = _format_timestamp(timestamp)
        summary = properties.get("summ", "")
        source = properties.get("source", "")
        importance = _to_int(properties.get("imp"))
        tags = ",".join(_classify_news(title, summary))
        if not title:
            continue
        rows.append(
            NewsItem(
                news_id=news_id,
                title=title,
                summary=summary,
                source=source,
                event_time=event_time,
                importance=importance,
                tags=tags,
                source_file=path,
            )
        )
        if limit is not None and len(rows) >= limit:
            break
    return rows


def load_default_ths_news(root: Path = DEFAULT_THS_ROOT, limit_per_file: int | None = None) -> list[NewsItem]:
    items: list[NewsItem] = []
    for path in default_ths_news_files(root):
        items.extend(load_ths_news_xml(path, limit=limit_per_file))
    dedup: dict[str, NewsItem] = {}
    for item in items:
        dedup[item.news_id] = item
    return sorted(dedup.values(), key=lambda item: item.event_time or "", reverse=True)


def fetch_eastmoney_announcements(
    symbols: list[str],
    per_symbol: int = 5,
    timeout: float = 10.0,
    opener: Callable[..., object] | None = None,
) -> list[NewsItem]:
    selected_symbols = sorted({symbol.strip() for symbol in symbols if symbol.strip().isdigit() and len(symbol.strip()) == 6})
    if not selected_symbols:
        return []
    selected_per_symbol = max(1, min(per_symbol, 50))
    open_url = opener or urlopen
    items: list[NewsItem] = []
    for symbol in selected_symbols:
        params = {
            "sr": "-1",
            "page_size": str(selected_per_symbol),
            "page_index": "1",
            "ann_type": "A",
            "client_source": "web",
            "stock_list": symbol,
            "f_node": "0",
            "s_node": "0",
        }
        request = Request(
            f"{EASTMONEY_ANNOUNCEMENT_URL}?{urlencode(params)}",
            headers={
                "User-Agent": "Mozilla/5.0 ths-stock-picker/0.1",
                "Referer": "https://data.eastmoney.com/notices/",
            },
        )
        with open_url(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
        items.extend(_eastmoney_announcements_from_payload(payload, symbol))
    dedup: dict[str, NewsItem] = {}
    for item in items:
        dedup[item.news_id] = item
    return sorted(dedup.values(), key=lambda item: item.event_time or "", reverse=True)


def _node_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


def _eastmoney_announcements_from_payload(payload: str, requested_symbol: str) -> list[NewsItem]:
    data = _json_or_jsonp(payload)
    rows = ((data.get("data") or {}).get("list") or []) if isinstance(data, dict) else []
    items: list[NewsItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title_ch") or row.get("title") or "").strip()
        if not title:
            continue
        art_code = str(row.get("art_code") or "").strip()
        codes = row.get("codes") if isinstance(row.get("codes"), list) else []
        code_text = _eastmoney_codes_summary(codes, requested_symbol)
        column_text = _eastmoney_columns_summary(row.get("columns"))
        event_time = _clean_eastmoney_time(row.get("display_time") or row.get("notice_date") or row.get("sort_date"))
        summary_parts = [part for part in [code_text, column_text, art_code] if part]
        summary = "；".join(summary_parts)
        tags = _classify_news(title, summary)
        if "公告" not in tags:
            tags.append("公告")
        items.append(
            NewsItem(
                news_id=f"eastmoney:{art_code or requested_symbol + ':' + title}",
                title=title,
                summary=summary,
                source="东方财富公告",
                event_time=event_time,
                importance=None,
                tags=",".join(tags),
                source_file=PUBLIC_ANNOUNCEMENT_SOURCE_FILE / requested_symbol,
            )
        )
    return items


def _json_or_jsonp(payload: str) -> object:
    text = payload.strip()
    if not text:
        return {}
    if not text.startswith("{"):
        match = re.search(r"\((\{.*\})\)\s*$", text, re.S)
        if match:
            text = match.group(1)
    return json.loads(text)


def _eastmoney_codes_summary(codes: object, fallback_symbol: str) -> str:
    if not isinstance(codes, list):
        return fallback_symbol
    parts = []
    for item in codes:
        if not isinstance(item, dict):
            continue
        stock_code = str(item.get("stock_code") or "").strip()
        short_name = str(item.get("short_name") or "").strip()
        if stock_code or short_name:
            parts.append(" ".join(part for part in [stock_code, short_name] if part))
    return "，".join(parts) or fallback_symbol


def _eastmoney_columns_summary(columns: object) -> str:
    if not isinstance(columns, list):
        return ""
    names = []
    for item in columns:
        if isinstance(item, dict):
            column_name = str(item.get("column_name") or "").strip()
            if column_name:
                names.append(column_name)
    return "公告栏目：" + "，".join(dict.fromkeys(names)) if names else ""


def _clean_eastmoney_time(value: object) -> str | None:
    text = str(value or "").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}:\d{2}))?", text)
    if not match:
        return None
    if match.group(2):
        return f"{match.group(1)} {match.group(2)}"
    return match.group(1)


def _parse_properties(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    current_key: str | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            current_key = key.strip()
            values[current_key] = value.strip()
        elif current_key:
            values[current_key] = (values[current_key] + "\n" + line.strip()).strip()
    return values


def _format_timestamp(value: str) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    # Some THS cache timestamps appear shifted far into the future; keep a normalized
    # display value but preserve source ordering through the original field in news_id.
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except ValueError:
        return None


def _fallback_news_id(title: str) -> str:
    return str(abs(hash(title)))


def _classify_news(title: str, summary: str) -> list[str]:
    text = f"{title} {summary}"
    rules = [
        ("退市风险", r"退市|强制退市|重大违法|ST|立案调查|行政处罚"),
        ("业绩预告", r"预计|预告|净利润|同比增长|同比下降|扭亏|亏损"),
        ("并购投资", r"收购|并购|对外投资|项目投资|股权|重组|购买资产"),
        ("政策监管", r"监管|证监会|交易所|政策|管理局|违法|立案|处罚"),
        ("AI算力", r"AI|算力|服务器|芯片|半导体|机器人|存储"),
        ("消费", r"消费|补贴|旅游|食品|饮料"),
        ("新能源", r"新能源|光伏|储能|电池|锂|充电"),
        ("公告", r"公告|公司称|表示"),
    ]
    tags = [label for label, pattern in rules if re.search(pattern, text, re.IGNORECASE)]
    return tags or ["资讯"]
