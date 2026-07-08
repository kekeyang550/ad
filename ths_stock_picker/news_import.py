from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from .models import NewsItem
from .shared_read import read_bytes_shared
from .ths_local import DEFAULT_THS_ROOT


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


def _node_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


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
        ("并购投资", r"收购|并购|投资|项目|股权|重组"),
        ("政策监管", r"监管|证监会|交易所|政策|会议|管理局"),
        ("AI算力", r"AI|算力|服务器|芯片|半导体|机器人|存储"),
        ("消费", r"消费|补贴|旅游|食品|饮料"),
        ("新能源", r"新能源|光伏|储能|电池|锂|充电"),
        ("公告", r"公告|公司称|表示"),
    ]
    tags = [label for label, pattern in rules if re.search(pattern, text, re.IGNORECASE)]
    return tags or ["资讯"]
