from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ths_local import DEFAULT_THS_ROOT


@dataclass(frozen=True)
class THSProcessStatus:
    name: str
    running: bool
    pid: int | None = None
    path: str = ""


@dataclass(frozen=True)
class THSFileStatus:
    market: str
    path: Path
    exists: bool
    size: int
    mtime: datetime | None
    age_seconds: float | None
    status: str


@dataclass(frozen=True)
class THSMonitorSnapshot:
    root: Path
    checked_at: datetime
    processes: list[THSProcessStatus]
    files: list[THSFileStatus]
    overall_status: str
    message: str


def inspect_ths_source(root: Path = DEFAULT_THS_ROOT, now: datetime | None = None) -> THSMonitorSnapshot:
    checked_at = now or datetime.now()
    root = Path(root)
    processes = _process_statuses()
    files = [
        _stocknow_status(root / "realtime" / "shase" / "stocknow.dat", "shase", checked_at),
        _stocknow_status(root / "realtime" / "sznse" / "stocknow.dat", "sznse", checked_at),
    ]
    overall_status, message = _overall_status(processes, files)
    return THSMonitorSnapshot(
        root=root,
        checked_at=checked_at,
        processes=processes,
        files=files,
        overall_status=overall_status,
        message=message,
    )


def _stocknow_status(path: Path, market: str, now: datetime) -> THSFileStatus:
    if not path.exists():
        return THSFileStatus(market, path, False, 0, None, None, "missing")
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime)
    age_seconds = max(0.0, (now - mtime).total_seconds())
    if age_seconds <= 180:
        status = "active"
    elif age_seconds <= 3600:
        status = "stale"
    else:
        status = "old"
    return THSFileStatus(market, path, True, stat.st_size, mtime, age_seconds, status)


def _process_statuses() -> list[THSProcessStatus]:
    targets = {"hexin.exe": "hexin", "hexinhelper.exe": "hexinhelper", "xiadan.exe": "xiadan"}
    found: dict[str, THSProcessStatus] = {}
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "Get-Process | Select-Object Id,ProcessName,Path | ConvertTo-Json"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return [THSProcessStatus(name=name, running=False) for name in targets.values()]

    import json

    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = []
    if isinstance(payload, dict):
        payload = [payload]
    for item in payload:
        process_name = str(item.get("ProcessName") or "").lower()
        exe_name = f"{process_name}.exe"
        if exe_name in targets:
            found[targets[exe_name]] = THSProcessStatus(
                name=targets[exe_name],
                running=True,
                pid=int(item.get("Id")) if item.get("Id") is not None else None,
                path=str(item.get("Path") or ""),
            )
    return [found.get(name, THSProcessStatus(name=name, running=False)) for name in targets.values()]


def _overall_status(processes: list[THSProcessStatus], files: list[THSFileStatus]) -> tuple[str, str]:
    hexin_running = any(item.name == "hexin" and item.running for item in processes)
    missing = [item.market for item in files if not item.exists]
    active = [item.market for item in files if item.status == "active"]
    stale_or_old = [item.market for item in files if item.status in {"stale", "old"}]
    if missing:
        return "invalid", f"缺少关键 A 股缓存：{', '.join(missing)}"
    if not hexin_running:
        return "offline", "未检测到同花顺主进程 hexin.exe"
    if active:
        return "active", f"检测到活跃缓存：{', '.join(active)}"
    if stale_or_old:
        return "stale", f"A 股缓存存在但未在最近 3 分钟更新：{', '.join(stale_or_old)}"
    return "unknown", "同花顺状态未知"
