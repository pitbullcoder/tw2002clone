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


class PlayersInSectorTests(unittest.TestCase):
    """get_players_in_sector against a real db: it reports each present
    pilot's fighters (what the sector-info screen advertises) but never
    their shields, in stable id order, and honors exclude_player_id."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "presence.db")
        db.init_db()
        conn = db.get_connection()
        conn.executemany("INSERT INTO sectors (id) VALUES (?)", [(1,), (5,)])
        conn.commit()
        conn.close()

    def _place(self, pubkey, name, sector_id, fighters, shields):
        player = db.create_player(pubkey, name)  # starts at the home sector
        db.move_player_to_sector(player["id"], sector_id)
        db.set_ship_defenses(player["id"], shields, fighters)
        return player

    def test_lists_present_pilots_with_fighters_and_no_shields(self):
        self._place("pk1", "Alice", 5, fighters=10, shields=111)
        self._place("pk2", "Bob", 5, fighters=20, shields=222)
        self._place("pk3", "Cleo", 1, fighters=30, shields=333)  # parked elsewhere

        here = db.get_players_in_sector(5)

        self.assertEqual(
            here,
            [{"name": "Alice", "fighters": 10}, {"name": "Bob", "fighters": 20}],
        )
        # Shields are never part of the row -- they stay hidden.
        self.assertTrue(all("shields" not in row for row in here))

    def test_exclude_player_id_drops_the_viewer(self):
        alice = self._place("pk1", "Alice", 5, fighters=10, shields=0)
        self._place("pk2", "Bob", 5, fighters=20, shields=0)

        here = db.get_players_in_sector(5, exclude_player_id=alice["id"])

        self.assertEqual(here, [{"name": "Bob", "fighters": 20}])

    def test_empty_sector_returns_nothing(self):
        self.assertEqual(db.get_players_in_sector(5), [])


class KillLogDbTests(unittest.TestCase):
    """record_kill / get_kills_since / cutoff handling against a real db."""

    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "kills.db")
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO sectors (id) VALUES (1)")
        conn.commit()
        conn.close()

    def _insert_kill(self, victim, killer, sector, kind, created_at):
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO kills (victim_name, killer_name, sector_id, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (victim, killer, sector, kind, created_at),
        )
        conn.commit()
        conn.close()

    def test_record_kill_persists_combat_and_mine_kills(self):
        db.record_kill("Bob", "Alice", 5, "ship")
        db.record_kill("Cleo", None, 9, "pod")   # None killer = mines

        rows = db.get_kills_since("2000-01-01T00:00:00+00:00")
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["victim_name"], rows[0]["killer_name"]), ("Bob", "Alice"))
        self.assertEqual(rows[0]["kind"], "ship")
        self.assertIsNone(rows[1]["killer_name"])
        self.assertEqual((rows[1]["victim_name"], rows[1]["kind"]), ("Cleo", "pod"))

    def test_get_kills_since_filters_by_cutoff_oldest_first(self):
        self._insert_kill("A", "K", 1, "ship", "2026-06-27T10:00:00+00:00")
        self._insert_kill("B", "K", 2, "ship", "2026-06-27T12:00:00+00:00")
        self._insert_kill("C", None, 3, "pod", "2026-06-27T14:00:00+00:00")

        rows = db.get_kills_since("2026-06-27T11:00:00+00:00")
        self.assertEqual([r["victim_name"] for r in rows], ["B", "C"])  # A excluded, ordered

    def test_get_kills_since_none_returns_empty(self):
        self._insert_kill("A", "K", 1, "ship", "2026-06-27T10:00:00+00:00")
        self.assertEqual(db.get_kills_since(None), [])

    def test_get_kills_since_respects_limit(self):
        for i in range(5):
            self._insert_kill(f"V{i}", "K", i, "ship", f"2026-06-27T1{i}:00:00+00:00")
        rows = db.get_kills_since("2000-01-01T00:00:00+00:00", limit=2)
        self.assertEqual([r["victim_name"] for r in rows], ["V0", "V1"])  # oldest two

    def test_new_player_cutoff_is_their_join_time(self):
        player = db.create_player("pk", "Alice")
        cutoff = db.get_kill_log_cutoff(player["id"])
        self.assertIsNotNone(cutoff)
        # A kill from long before they joined never shows.
        self._insert_kill("Old", "K", 1, "ship", "2000-01-01T00:00:00+00:00")
        self.assertEqual(db.get_kills_since(cutoff), [])

    def test_mark_kill_log_seen_advances_the_cutoff(self):
        player = db.create_player("pk", "Alice")
        before = db.get_kill_log_cutoff(player["id"])
        db.mark_kill_log_seen(player["id"])
        after = db.get_kill_log_cutoff(player["id"])
        self.assertGreaterEqual(after, before)


if __name__ == "__main__":
    unittest.main()
