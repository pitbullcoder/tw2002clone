"""
Unit tests for the guided port-trading flow in main.py.

Centerpiece scenario (matches the worked example from the design
discussion): a trader docks at an SSB port carrying 15 units of
Equipment, with 5 free cargo holds and 10,000cr.

    1. Port offers to sell Equipment (it buys what they're carrying) --
       player sells 5 units, freeing up cargo space.
    2. Port then offers to buy Fuel Ore, now that more holds are free --
       player buys 5 units.
    3. Port offers to buy Organics with the remaining free holds --
       player buys 5 units, filling the hold completely.
    4. The port visit ends on its own once nothing's left to offer.

A couple of supporting tests cover the related behaviors discussed
alongside it: replying with a quantity of 0 (or 'no' at the price-confirm
step) skips to the next item rather than ending the whole visit, while
'cancel' ends it immediately.

This stubs out the `db` and `meshcore` modules main.py imports from, so
it runs standalone -- no real database, radio, or network needed. Run it
with either of:

    python3 main_test.py
    python3 -m unittest main_test -v

It expects main.py to be importable from the same directory.
"""

import importlib
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Stub out `db` and `meshcore` *before* main.py is imported, since main.py
# does `from db import (...)` and `from meshcore import MeshCore, EventType`
# at module load time.
# ---------------------------------------------------------------------------

# Mutable fixture state the stub db functions read/write. Tests reset its
# *contents* in setUp() (rather than rebinding it) so the stub functions
# defined once below always see the current test's data.
STATE = {"player": {}, "port": {}, "trade_log": []}


def _stub_get_port(sector_id):
    return dict(STATE["port"])


def _stub_get_player_with_ship(pubkey):
    return dict(STATE["player"])


def _stub_get_or_create_player(pubkey, sender):
    return dict(STATE["player"]), False


def _stub_execute_trade(player_id, port_id, key, qty, total_price, player_is_buying):
    STATE["trade_log"].append((key, qty, total_price, player_is_buying))
    player = STATE["player"]
    port = STATE["port"]
    if player_is_buying:
        player[key] += qty
        player["credits"] -= total_price
        port[f"{key}_qty"] -= qty
    else:
        player[key] -= qty
        player["credits"] += total_price
        port[f"{key}_qty"] += qty


def _install_stub_modules():
    db_stub = types.ModuleType("db")
    db_stub.init_db = lambda: None
    db_stub.log_message = lambda *a, **k: None
    db_stub.get_or_create_player = _stub_get_or_create_player
    db_stub.reset_turns_if_needed = lambda *a, **k: None
    db_stub.get_player_with_ship = _stub_get_player_with_ship
    db_stub.get_adjacent_sectors = lambda sector_id: []
    db_stub.get_all_warps = lambda: {}
    db_stub.get_port = _stub_get_port
    db_stub.move_player_to_sector = lambda *a, **k: None
    db_stub.execute_trade = _stub_execute_trade
    sys.modules["db"] = db_stub

    meshcore_stub = types.ModuleType("meshcore")

    class MeshCore:
        pass

    class EventType:
        ERROR = "ERROR"

    meshcore_stub.MeshCore = MeshCore
    meshcore_stub.EventType = EventType
    sys.modules["meshcore"] = meshcore_stub


_install_stub_modules()
import main  # noqa: E402  (must come after the stubs are installed)


class FakeCtx:
    """Minimal stand-in for main.Ctx -- only the attributes cmd_trade and
    cmd_trade_step actually read."""

    def __init__(self, pubkey, player):
        self.pubkey = pubkey
        self.sender = "Tester"
        self.player = player


PUBKEY = "test-pubkey"


def fresh_player(**overrides):
    base = {
        "id": 1,
        "sector_id": 1,
        "credits": 10000,
        "turns_remaining": 50,
        "ship_type": "Merchant Cruiser",
        "holds_total": 20,
        "fighters": 0,
        "shields": 0,
        "fuel_ore": 0,
        "organics": 0,
        "equipment": 0,
    }
    base.update(overrides)
    return base


def fresh_port(port_class, **overrides):
    base = {"id": 1, "port_class": port_class}
    for key in ("fuel_ore", "organics", "equipment"):
        base[f"{key}_dir"] = None
        base[f"{key}_price"] = 0
        base[f"{key}_qty"] = 0
        base[f"{key}_max"] = 0
    base.update(overrides)
    return base


class PortTradeFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reload main.py so module-level state (PENDING_TRADES, etc.)
        # starts empty for every test, independent of test order. Its
        # one-time startup print() fires again on every reload, so it's
        # muted here to keep test output clean.
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        STATE["trade_log"] = []

    def ctx(self):
        """A fresh Ctx wired to whatever STATE['player'] currently holds --
        mirrors how on_message re-fetches the player on every turn."""
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_trade_step(self.ctx(), message)

    async def test_sell_then_buy_fills_holds_in_order(self):
        """The worked example: sell Equipment, then buy Fuel Ore, then
        buy Organics, using the cargo space freed up by each sale."""
        STATE["player"] = fresh_player(equipment=15, holds_total=20, credits=10000)
        STATE["port"] = fresh_port(
            "SSB",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="B", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        # 1. Docking offers to sell the 15 carried Equipment first.
        prompt = await self.dock()
        self.assertIn("Equipment", prompt)
        self.assertIn("Sell how many? (0-15", prompt)

        # 2. Sell 5 of them at the listed price.
        prompt = await self.say("5")
        self.assertIn("Sell 5 Equipment for 100cr (20cr/unit)?", prompt)
        prompt = await self.say("yes")
        self.assertIn("Sold 5 Equipment for +100cr (20cr/unit).", prompt)

        # 3. Selling freed 5 holds (5 already free + 5 just freed = 10),
        # so the next offer is to buy up to 10 Fuel Ore.
        self.assertIn("Buy how many? (0-10", prompt)
        prompt = await self.say("5")
        self.assertIn("Buy 5 Fuel Ore for 50cr (10cr/unit)?", prompt)
        prompt = await self.say("yes")
        self.assertIn("Bought 5 Fuel Ore for -50cr (10cr/unit).", prompt)

        # 4. 5 holds remain free, so the Organics offer is capped at 5.
        self.assertIn("Buy how many? (0-5", prompt)
        prompt = await self.say("5")
        self.assertIn("Buy 5 Organics for 75cr (15cr/unit)?", prompt)
        prompt = await self.say("yes")
        self.assertIn("Bought 5 Organics for -75cr (15cr/unit).", prompt)

        # 5. Holds are now full and nothing else is queued -- the visit
        # closes itself out without the player needing to say 'cancel'.
        self.assertIn("Nothing more to trade here.", prompt)

        final = STATE["player"]
        self.assertEqual(final["equipment"], 10)   # 15 - 5 sold
        self.assertEqual(final["fuel_ore"], 5)
        self.assertEqual(final["organics"], 5)
        self.assertEqual(final["credits"], 10000 + 100 - 50 - 75)
        self.assertEqual(
            STATE["trade_log"],
            [
                ("equipment", 5, 100, False),
                ("fuel_ore", 5, 50, True),
                ("organics", 5, 75, True),
            ],
        )
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)  # session cleaned up

    async def test_quantity_zero_skips_to_next_item(self):
        """Empty holds at an all-selling (SSS) port: replying 0 to an
        offer moves straight to the next one instead of ending the visit."""
        STATE["player"] = fresh_player(holds_total=20, credits=200)
        STATE["port"] = fresh_port(
            "SSS",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="S", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        prompt = await self.dock()
        self.assertIn("Fuel Ore", prompt)

        prompt = await self.say("0")   # skip fuel
        self.assertIn("Organics", prompt)

        prompt = await self.say("0")   # skip organics
        self.assertIn("Equipment", prompt)

        prompt = await self.say("2")
        prompt = await self.say("yes")
        self.assertIn("Bought 2 Equipment for -40cr (20cr/unit).", prompt)
        self.assertIn("Nothing more to trade here.", prompt)

        self.assertEqual(STATE["player"]["fuel_ore"], 0)
        self.assertEqual(STATE["player"]["organics"], 0)
        self.assertEqual(STATE["player"]["equipment"], 2)

    async def test_no_at_confirm_skips_rather_than_cancels(self):
        """'no' at the price-confirm step behaves like a quantity of 0 --
        it moves on to the next queued item, not the whole port visit."""
        STATE["player"] = fresh_player(equipment=4, holds_total=10, credits=1000)
        STATE["port"] = fresh_port(
            "BSS",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="B", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        await self.dock()                 # offers to sell Equipment
        await self.say("4")               # quote a sale of all 4 units
        prompt = await self.say("no")     # decline -- should move on, not cancel

        self.assertIn("Fuel Ore", prompt)
        self.assertIn(PUBKEY, main.PENDING_TRADES)  # visit is still active
        self.assertEqual(STATE["player"]["equipment"], 4)  # nothing sold

    async def test_cancel_ends_the_whole_visit(self):
        STATE["player"] = fresh_player(equipment=15, holds_total=20, credits=10000)
        STATE["port"] = fresh_port(
            "SSB",
            fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            organics_dir="S", organics_price=15, organics_qty=500, organics_max=1000,
            equipment_dir="B", equipment_price=20, equipment_qty=100, equipment_max=1000,
        )

        await self.dock()
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Trade cancelled.")
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(STATE["player"]["equipment"], 15)  # unchanged
        self.assertEqual(STATE["trade_log"], [])


if __name__ == "__main__":
    unittest.main()
