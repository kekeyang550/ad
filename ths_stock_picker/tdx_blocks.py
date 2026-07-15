from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .tdx_local import DEFAULT_TDX_ROOT


BLOCK_HEADER_SIZE = 384
BLOCK_RECORD_SIZE = 2813
BLOCK_HEADER_PREFIX = b"Registry ver:1.0"
BLOCK_FILE_SPECS = {
    "concept": ("概念", "block_gn.dat"),
    "style": ("风格", "block_fg.dat"),
}


@dataclass(frozen=True)
class ThemeMembership:
    symbol: str
    category: str
    theme: str
    source_file: Path


def discover_tdx_block_files(
    tdx_root: Path = DEFAULT_TDX_ROOT,
    kinds: list[str] | None = None,
) -> list[tuple[str, str, Path]]:
    selected_kinds = kinds or list(BLOCK_FILE_SPECS)
    files: list[tuple[str, str, Path]] = []
    cache_dir = Path(tdx_root) / "T0002" / "hq_cache"
    for kind in selected_kinds:
        spec = BLOCK_FILE_SPECS.get(kind)
        if spec is None:
            continue
        category, filename = spec
        path = cache_dir / filename
        if path.is_file():
            files.append((kind, category, path))
    return files


def load_tdx_theme_memberships(
    tdx_root: Path = DEFAULT_TDX_ROOT,
    kinds: list[str] | None = None,
) -> tuple[list[ThemeMembership], list[Path]]:
    memberships: list[ThemeMembership] = []
    files: list[Path] = []
    for _, category, path in discover_tdx_block_files(tdx_root=tdx_root, kinds=kinds):
        memberships.extend(load_tdx_block_file(path, category))
        files.append(path)
    return memberships, files


def load_tdx_block_file(path: Path, category: str) -> list[ThemeMembership]:
    path = Path(path)
    data = path.read_bytes()
    if not data.startswith(BLOCK_HEADER_PREFIX) or len(data) < BLOCK_HEADER_SIZE:
        return []

    memberships: list[ThemeMembership] = []
    for offset in range(BLOCK_HEADER_SIZE, len(data) - BLOCK_RECORD_SIZE + 1, BLOCK_RECORD_SIZE):
        memberships.extend(_parse_block_record(data[offset : offset + BLOCK_RECORD_SIZE], category, path))
    return memberships


def _parse_block_record(record: bytes, category: str, source_file: Path) -> list[ThemeMembership]:
    name_start = 2
    name_end = record.find(b"\0", name_start)
    if name_end <= name_start or name_end + 5 > len(record):
        return []
    try:
        theme = record[name_start:name_end].decode("gbk").strip()
    except UnicodeDecodeError:
        return []
    if not theme:
        return []

    count_offset = name_end + 1
    member_count = int.from_bytes(record[count_offset : count_offset + 2], "little")
    code_offset = count_offset + 4
    code_end = code_offset + member_count * 7
    if member_count < 1 or code_end > len(record):
        return []

    symbols: list[str] = []
    for offset in range(code_offset, code_end, 7):
        raw = record[offset : offset + 7]
        if len(raw) != 7 or raw[-1] != 0 or not raw[:6].isdigit():
            return []
        symbols.append(raw[:6].decode("ascii"))
    return [
        ThemeMembership(symbol=symbol, category=category, theme=theme, source_file=source_file)
        for symbol in symbols
    ]
