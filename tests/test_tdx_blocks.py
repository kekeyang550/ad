from __future__ import annotations

import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path

from ths_stock_picker.cli import main
from ths_stock_picker.storage import Repository
from ths_stock_picker.tdx_blocks import BLOCK_HEADER_SIZE, BLOCK_RECORD_SIZE, discover_tdx_block_files, load_tdx_theme_memberships


def _record(name: str, symbols: list[str]) -> bytes:
    payload = b"\0\0" + name.encode("gbk") + b"\0"
    payload += len(symbols).to_bytes(2, "little") + b"\x02\0"
    payload += b"".join(symbol.encode("ascii") + b"\0" for symbol in symbols)
    return payload.ljust(BLOCK_RECORD_SIZE, b"\0")


def _write_block(path: Path, records: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"Registry ver:1.0 (1999-9-28)".ljust(BLOCK_HEADER_SIZE, b"\0")
    path.write_bytes(header + b"".join(records))


class TDXBlocksTests(unittest.TestCase):
    def test_loads_valid_concept_and_style_memberships(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tdx"
            cache_dir = root / "T0002" / "hq_cache"
            _write_block(cache_dir / "block_gn.dat", [_record("人工智能", ["000001", "600000"])])
            _write_block(cache_dir / "block_fg.dat", [_record("专精特新", ["000001"])])

            memberships, files = load_tdx_theme_memberships(root)

        self.assertEqual(len(files), 2)
        self.assertEqual(
            {(row.symbol, row.category, row.theme) for row in memberships},
            {("000001", "概念", "人工智能"), ("600000", "概念", "人工智能"), ("000001", "风格", "专精特新")},
        )

    def test_skips_records_with_invalid_member_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tdx"
            path = root / "T0002" / "hq_cache" / "block_gn.dat"
            invalid = bytearray(_record("无效", ["000001"]))
            code_offset = 2 + len("无效".encode("gbk")) + 1 + 4
            invalid[code_offset : code_offset + 6] = b"ABCDEF"
            _write_block(path, [bytes(invalid)])

            memberships, _ = load_tdx_theme_memberships(root)

        self.assertEqual(memberships, [])

    def test_discovers_only_requested_block_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tdx"
            cache_dir = root / "T0002" / "hq_cache"
            _write_block(cache_dir / "block_gn.dat", [_record("人工智能", ["000001"])])
            _write_block(cache_dir / "block_fg.dat", [_record("专精特新", ["000001"])])

            files = discover_tdx_block_files(root, kinds=["concept"])

        self.assertEqual([(kind, category, path.name) for kind, category, path in files], [("concept", "概念", "block_gn.dat")])

    def test_cli_imports_theme_memberships_into_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tdx"
            db = Path(temp_dir) / "picker.db"
            _write_block(root / "T0002" / "hq_cache" / "block_gn.dat", [_record("人工智能", ["000001", "600000"])])

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(db), "import-tdx-blocks", "--tdx-root", str(root)]), 0)
            repo = Repository(db)
            try:
                repo.init_schema()
                counts = repo.table_counts()
            finally:
                repo.close()

        self.assertEqual(counts["stock_themes"], 2)
        self.assertIn("Theme memberships: 2", output.getvalue())
