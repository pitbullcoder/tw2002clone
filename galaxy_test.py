"""
Tests for galaxy generation -- specifically the complementary adjacent
port pairs. Unlike main_test.py (which stubs the db module out entirely),
galaxy generation writes real rows, so these run against a throwaway
SQLite file under a temp dir. Run directly:

    python galaxy_test.py
"""

import math
import os
import random
import tempfile
import unittest

import db
import galaxy


class ChooseAdjacentPairsTests(unittest.TestCase):
    """The pure edge-matching helper: vertex-disjoint, adjacent, and
    respecting the exclude set, without any db involvement."""

    def setUp(self):
        random.seed(0)  # _choose_adjacent_pairs shuffles; pin it for stability

    def test_pairs_are_disjoint_and_adjacent(self):
        adjacency = {1: {2, 3}, 2: {1, 3}, 3: {1, 2, 4}, 4: {3, 5}, 5: {4}}
        pairs = galaxy._choose_adjacent_pairs(adjacency, num_pairs=2)

        self.assertLessEqual(len(pairs), 2)
        seen = set()
        for a, b in pairs:
            self.assertNotIn(a, seen)          # no sector reused across pairs
            self.assertNotIn(b, seen)
            seen.update((a, b))
            self.assertIn(b, adjacency[a])     # genuinely warp-adjacent

    def test_excluded_sectors_are_never_used(self):
        # Every edge here touches sector 2, so excluding it leaves nothing.
        adjacency = {1: {2}, 2: {1, 3}, 3: {2}}
        self.assertEqual(galaxy._choose_adjacent_pairs(adjacency, 5, exclude={2}), [])

    def test_returns_fewer_when_the_graph_runs_out_of_edges(self):
        adjacency = {1: {2}, 2: {1}}  # a single edge
        self.assertEqual(len(galaxy._choose_adjacent_pairs(adjacency, num_pairs=5)), 1)


class GalaxyPortPairingTests(unittest.TestCase):
    """End-to-end: generate a real galaxy and check the port layout."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        import io

        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "galaxy_test.db")
        with contextlib.redirect_stdout(io.StringIO()):  # mute the generation summary
            galaxy.generate_galaxy(seed=12345, skip_confirmation=True)

        conn = db.get_connection()
        cls.ports = {
            row["sector_id"]: row["port_class"]
            for row in conn.execute(
                "SELECT sector_id, port_class FROM ports WHERE port_class != 'STARDOCK'"
            )
        }
        cls.total_port_rows = conn.execute("SELECT COUNT(*) FROM ports").fetchone()[0]
        cls.adjacency = {}
        for from_id, to_id in conn.execute("SELECT from_sector_id, to_sector_id FROM warps"):
            cls.adjacency.setdefault(from_id, set()).add(to_id)
        conn.close()

        cls.allowed_pairs = {frozenset(pair) for pair in galaxy.PORT_PAIRS}

    def _paired_ports(self):
        """Sectors whose port has an adjacent sector hosting its complement."""
        paired = set()
        for sector, port_class in self.ports.items():
            for neighbor in self.adjacency.get(sector, ()):
                if neighbor in self.ports and \
                        frozenset({port_class, self.ports[neighbor]}) in self.allowed_pairs:
                    paired.update((sector, neighbor))
        return paired

    def test_total_port_count_is_unchanged(self):
        # num_ports commodity ports + the single Stardock.
        expected = int(galaxy.NUM_SECTORS * galaxy.PORT_DENSITY) + 1
        self.assertEqual(self.total_port_rows, expected)

    def test_at_least_five_percent_of_ports_are_in_adjacent_pairs(self):
        required = math.ceil(galaxy.PORT_PAIR_FRACTION * len(self.ports))
        self.assertGreaterEqual(len(self._paired_ports()), required)

    def test_paired_ports_are_adjacent_and_complementary(self):
        for sector in self._paired_ports():
            self.assertTrue(
                any(
                    neighbor in self.ports
                    and frozenset({self.ports[sector], self.ports[neighbor]}) in self.allowed_pairs
                    for neighbor in self.adjacency.get(sector, ())
                ),
                f"Sec{sector} flagged as paired but has no adjacent complement",
            )

    def test_every_port_class_is_valid(self):
        for port_class in self.ports.values():
            self.assertIn(port_class, galaxy.PORT_CLASSES)

    def test_home_sector_is_stardock_not_a_commodity_port(self):
        self.assertNotIn(galaxy.HOME_SECTOR, self.ports)


if __name__ == "__main__":
    unittest.main()
