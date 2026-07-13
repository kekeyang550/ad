from __future__ import annotations

from datetime import datetime, timedelta, timezone


_SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


def display_shanghai_time(value: object) -> str:
    text = str(value or "")
    try:
        utc_time = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return text
    return utc_time.astimezone(_SHANGHAI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
