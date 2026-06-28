"""
Tests for db-level turn handling: the 3am-Eastern reset boundary, the
sign-in refill, and per-move turn spending. Like galaxy_test.py (and
unlike main_test.py, which stubs db out), these run against the real db
module backed by a throwaway SQLite file. Run directly:

    python db_test.py
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import db


class TurnResetBoundaryTests(unittest.TestCase):
    """_last_reset_boundary: the most recent 3am-Eastern instant, in UTC.
    Eastern is UTC-4 in summer (EDT) and UTC-5 in winter (EST), so 3am
    Eastern is 07:00 UTC in summer and 08:00 UTC in winter."""

    def test_after_3am_eastern_uses_todays_boundary(self):
        # 2026-06-27 10:00 UTC = 06:00 EDT, past 3am -> today's 3am EDT = 07:00 UTC.
        now = datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            db._last_reset_boundary(now),
            datetime(2026, 6, 27, 7, 0, tzinfo=timezone.utc),
        )

    def test_before_3am_eastern_uses_yesterdays_boundary(self):
        # 2026-06-27 05:00 UTC = 01:00 EDT, before 3am -> yesterday's 3am EDT.
        now = datetime(2026, 6, 27, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(
            db._last_reset_boundary(now),
            datetime(2026, 6, 26, 7, 0, tzinfo=timezone.utc),
        )

    def test_winter_boundary_tracks_est(self):
        # 2026-01-15 10:00 UTC = 05:00 EST, past 3am -> today's 3am EST = 08:00 UTC.
        now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            db._last_reset_boundary(now),
            datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
        )


class TurnDbTests(unittest.TestCase):
    """reset_turns_if_needed and spend_turn against a real db."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "turns.db")
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO sectors (id) VALUES (1)")
        conn.commit()
        conn.close()
        self.player = db.create_player("pk", "Alice")

    def _set(self, last_reset_dt, turns):
        conn = db.get_connection()
        conn.execute(
            "UPDATE players SET last_turn_reset = ?, turns_remaining = ? WHERE id = ?",
            (last_reset_dt.isoformat(), turns, self.player["id"]),
        )
        conn.commit()
        conn.close()

    def _turns(self):
        conn = db.get_connection()
        value = conn.execute(
            "SELECT turns_remaining FROM players WHERE id = ?", (self.player["id"],)
        ).fetchone()["turns_remaining"]
        conn.close()
        return value

    def test_daily_turn_limit_is_100(self):
        self.assertEqual(db.DAILY_TURNS, 100)

    def test_spend_turn_decrements_and_never_goes_negative(self):
        self._set(datetime.now(timezone.utc), turns=2)
        db.spend_turn(self.player["id"])
        self.assertEqual(self._turns(), 1)
        db.spend_turn(self.player["id"])
        db.spend_turn(self.player["id"])  # already at 0 -> no-op
        self.assertEqual(self._turns(), 0)

    def test_reset_refills_when_overdue(self):
        self._set(datetime.now(timezone.utc) - timedelta(days=2), turns=7)
        db.reset_turns_if_needed(self.player["id"])
        self.assertEqual(self._turns(), db.DAILY_TURNS)

    def test_no_reset_when_already_topped_up_today(self):
        self._set(datetime.now(timezone.utc), turns=7)
        db.reset_turns_if_needed(self.player["id"])
        self.assertEqual(self._turns(), 7)  # unchanged


if __name__ == "__main__":
    unittest.main()
