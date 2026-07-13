from __future__ import annotations

import unittest

from ths_stock_picker.time_utils import display_shanghai_time


class TimeUtilsTests(unittest.TestCase):
    def test_display_shanghai_time_converts_utc_system_timestamps(self) -> None:
        self.assertEqual(display_shanghai_time("2026-07-13 08:12:49"), "2026-07-13 16:12:49")
        self.assertEqual(display_shanghai_time("not-a-timestamp"), "not-a-timestamp")
        self.assertEqual(display_shanghai_time(None), "")
