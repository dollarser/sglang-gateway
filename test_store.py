#!/usr/bin/env python3
"""Offline boundary tests for store.parse_expires."""

import unittest
from datetime import datetime
from unittest.mock import patch

import store


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 31, 23, 0, 0, tzinfo=tz)


class TestParseExpires(unittest.TestCase):
    def test_empty_and_never_specs_mean_no_expiry(self):
        for spec in (None, "", "never", " NEVER ", "none", "0"):
            with self.subTest(spec=spec):
                self.assertIsNone(store.parse_expires(spec))

    def test_day_and_hour_suffixes_are_case_insensitive_at_boundary(self):
        with patch.object(store, "datetime", _FixedDateTime):
            self.assertEqual(store.parse_expires("1d"), "2026-02-01 23:00:00")
            self.assertEqual(store.parse_expires("2H"), "2026-02-01 01:00:00")

    def test_calendar_date_is_normalized_to_timestamp_format(self):
        self.assertEqual(
            store.parse_expires("2026-12-31"),
            datetime(2026, 12, 31).strftime(store.TS_FMT),
        )

    def test_malformed_specs_raise_value_error(self):
        for spec in ("1.5d", "tomorrow", "2026-02-29"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    store.parse_expires(spec)


if __name__ == "__main__":
    unittest.main()
