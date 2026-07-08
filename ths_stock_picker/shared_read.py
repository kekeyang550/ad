from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path


def read_bytes_shared(path: Path, max_bytes: int | None = None) -> bytes:
    """Read a file even when another Windows process keeps it open."""
    path = Path(path)
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if _is_windows():
        return _read_bytes_shared_windows(path, max_bytes)
    with path.open("rb") as handle:
        return handle.read() if max_bytes is None else handle.read(max_bytes)


def _is_windows() -> bool:
    return hasattr(ctypes, "windll")


def _read_bytes_shared_windows(path: Path, max_bytes: int | None) -> bytes:
    kernel32 = ctypes.windll.kernel32
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    handle = create_file(
        str(path),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))

    try:
        size = path.stat().st_size
        read_size = size if max_bytes is None else min(size, max_bytes)
        chunks: list[bytes] = []
        remaining = read_size
        read_file = kernel32.ReadFile
        read_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        read_file.restype = wintypes.BOOL

        while remaining > 0:
            chunk_size = min(remaining, 1024 * 1024)
            buffer = ctypes.create_string_buffer(chunk_size)
            bytes_read = wintypes.DWORD(0)
            ok = read_file(handle, buffer, chunk_size, ctypes.byref(bytes_read), None)
            if not ok:
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), str(path))
            if bytes_read.value == 0:
                break
            chunks.append(buffer.raw[: bytes_read.value])
            remaining -= bytes_read.value
        return b"".join(chunks)
    finally:
        kernel32.CloseHandle(handle)
