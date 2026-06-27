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
STATE = {
    "player": {},
    "port": {},
    "trade_log": [],
    "upgrade_log": [],
    "ship_log": [],
    # Per-sector fixtures for the navigation/docking tests below. Left
    # empty by the port-trade and Stardock tests above, which only ever
    # care about one port (whichever sector the player is currently
    # standing in) and don't exercise movement at all.
    "ports": {},
    "warps": {},
    # Mine fixtures: sector_id -> {owner_player_id: qty deployed there}.
    # mine_log/defense_log record calls the way trade_log/ship_log do, so
    # tests can assert on what the lay/detonation paths did.
    "sector_mines": {},
    "mine_log": [],
    "defense_log": [],
}


def _stub_get_port(sector_id):
    # Navigation tests populate "ports" per sector_id; everything else
    # uses the single "port" fixture regardless of which sector_id is
    # asked for, since those tests only ever have the player docked in
    # one place. Either way, mirror the real get_port's contract of
    # returning None (not an empty dict) when there's nothing there --
    # callers like _warp_confirm_options rely on `is not None`.
    if STATE["ports"]:
        port = STATE["ports"].get(sector_id)
        return dict(port) if port is not None else None
    return dict(STATE["port"]) if STATE["port"] else None


def _stub_get_adjacent_sectors(sector_id):
    return list(STATE["warps"].get(sector_id, []))


def _stub_get_all_warps():
    return {k: list(v) for k, v in STATE["warps"].items()}


def _stub_move_player_to_sector(player_id, sector_id):
    STATE["player"]["sector_id"] = sector_id


def _stub_get_player_with_ship(pubkey):
    return dict(STATE["player"])


def _stub_get_or_create_player(pubkey, sender):
    return dict(STATE["player"]), False


def _stub_execute_trade(player_id, port_id, key, qty, total_price, player_is_buying):
    STATE["trade_log"].append((key, qty, total_price, player_is_buying))
    player = STATE["player"]
    # Mutate the actual backing dict (not a get_port()-returned copy) so
    # the change is visible on the next get_port() call. The per-sector
    # "ports" fixture (navigation tests) is looked up by id; everything
    # else just has the one "port" fixture regardless of port_id.
    if STATE["ports"]:
        port = next(p for p in STATE["ports"].values() if p["id"] == port_id)
    else:
        port = STATE["port"]
    if player_is_buying:
        player[key] += qty
        player["credits"] -= total_price
        port[f"{key}_qty"] -= qty
    else:
        player[key] -= qty
        player["credits"] += total_price
        port[f"{key}_qty"] += qty


def _stub_upgrade_ship_stat(player_id, stat_column, qty, total_price):
    STATE["upgrade_log"].append((stat_column, qty, total_price))
    player = STATE["player"]
    player[stat_column] += qty
    player["credits"] -= total_price


def _stub_lay_mines(player_id, sector_id, qty):
    STATE["mine_log"].append((sector_id, player_id, qty))
    # Only decrement the aboard count if this is the active player (it
    # always is for the lay-command tests, which is the only path that
    # calls this).
    if STATE["player"].get("id") == player_id:
        STATE["player"]["mines"] -= qty
    sec = STATE["sector_mines"].setdefault(sector_id, {})
    sec[player_id] = sec.get(player_id, 0) + qty


def _stub_get_hostile_mine_total(sector_id, player_id):
    sec = STATE["sector_mines"].get(sector_id, {})
    return sum(q for owner, q in sec.items() if owner != player_id)


def _stub_clear_hostile_mines(sector_id, player_id):
    sec = STATE["sector_mines"].get(sector_id, {})
    for owner in list(sec):
        if owner != player_id:
            del sec[owner]


def _stub_set_ship_defenses(player_id, shields, fighters):
    STATE["defense_log"].append((player_id, shields, fighters))
    STATE["player"]["shields"] = shields
    STATE["player"]["fighters"] = fighters


# Mirrors db.SHIP_CATALOG -- kept as a separate copy here (rather than
# importing the real db module) since this whole file exists to test
# main.py without a real db module loaded at all.
SHIP_CATALOG = {
    "Falcon": {
        "classification": "Frigate",
        "price": 0,
        "base_holds": 20,
        "base_fighters": 10,
        "base_shields": 10,
        "base_mines": 0,
        "max_holds": 75,
        "max_fighters": 50,
        "max_shields": 200,
        "max_mines": 0,
    },
    "SS Endeavour": {
        "classification": "Merchant Freighter",
        "price": 20000,
        "base_holds": 50,
        "base_fighters": 0,
        "base_shields": 50,
        "base_mines": 0,
        "max_holds": 200,
        "max_fighters": 10,
        "max_shields": 400,
        "max_mines": 0,
    },
    "Bismark": {
        "classification": "Capital Ship",
        "price": 23500,
        "base_holds": 30,
        "base_fighters": 200,
        "base_shields": 500,
        "base_mines": 0,
        "max_holds": 125,
        "max_fighters": 2000,
        "max_shields": 3500,
        "max_mines": 50,
    },
    "Escape Pod": {
        "classification": "Escape Pod",
        "price": 0,
        "base_holds": 0,
        "base_fighters": 0,
        "base_shields": 0,
        "base_mines": 0,
        "max_holds": 0,
        "max_fighters": 0,
        "max_shields": 0,
        "max_mines": 0,
        "purchasable": False,
    },
}
DEFAULT_SHIP_TYPE = "Falcon"
ESCAPE_POD_SHIP = "Escape Pod"
SHIP_RESALE_FRACTION = 0.5


def _stub_sell_value(ship_type):
    return round(SHIP_CATALOG[ship_type]["price"] * SHIP_RESALE_FRACTION)


def _stub_buy_ship(player_id, ship_type, holds_total, fighters, shields, mines, credit_delta):
    STATE["ship_log"].append((ship_type, holds_total, fighters, shields, mines, credit_delta))
    player = STATE["player"]
    player["ship_type"] = ship_type
    player["holds_total"] = holds_total
    player["fighters"] = fighters
    player["shields"] = shields
    player["mines"] = mines
    player["fuel_ore"] = 0
    player["organics"] = 0
    player["equipment"] = 0
    player["credits"] += credit_delta


def _install_stub_modules():
    db_stub = types.ModuleType("db")
    db_stub.init_db = lambda: None
    db_stub.log_message = lambda *a, **k: None
    db_stub.get_or_create_player = _stub_get_or_create_player
    db_stub.reset_turns_if_needed = lambda *a, **k: None
    db_stub.get_player_with_ship = _stub_get_player_with_ship
    db_stub.get_adjacent_sectors = _stub_get_adjacent_sectors
    db_stub.get_all_warps = _stub_get_all_warps
    db_stub.get_port = _stub_get_port
    db_stub.move_player_to_sector = _stub_move_player_to_sector
    db_stub.execute_trade = _stub_execute_trade
    db_stub.upgrade_ship_stat = _stub_upgrade_ship_stat
    db_stub.buy_ship = _stub_buy_ship
    db_stub.lay_mines = _stub_lay_mines
    db_stub.get_hostile_mine_total = _stub_get_hostile_mine_total
    db_stub.clear_hostile_mines = _stub_clear_hostile_mines
    db_stub.set_ship_defenses = _stub_set_ship_defenses
    db_stub.sell_value = _stub_sell_value
    db_stub.SHIP_CATALOG = SHIP_CATALOG
    db_stub.DEFAULT_SHIP_TYPE = DEFAULT_SHIP_TYPE
    db_stub.ESCAPE_POD_SHIP = ESCAPE_POD_SHIP
    db_stub.STARDOCK_PRICES = {"holds_total": 500, "fighters": 50, "shields": 25, "mines": 1000}
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
        "ship_type": "Falcon",
        "holds_total": 20,
        "fighters": 0,
        "shields": 0,
        "mines": 0,
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
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

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


class StardockRefitFlowTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers the Stardock refit flow added to cmd_trade/cmd_stardock_step:
    docking at a STARDOCK port opens an open-ended menu (buy cargo holds,
    fighters, or shields) rather than the commodity-trade queue, and the
    menu reappears after every purchase/skip so multiple stats can be
    upgraded in one visit.
    """

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def test_dock_shows_refit_menu_with_current_max_and_price(self):
        STATE["player"] = fresh_player(credits=5000, holds_total=20, fighters=10, shields=10)
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.dock()

        self.assertIn("Stardock refits:", prompt)
        self.assertIn("Cargo Holds 20/75 @ 500cr each", prompt)
        self.assertIn("Fighters 10/50 @ 50cr each", prompt)
        self.assertIn("Shields 10/200 @ 25cr each", prompt)
        self.assertIn("5000cr available", prompt)
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)

    async def test_buy_fighters_full_flow_then_returns_to_menu(self):
        STATE["player"] = fresh_player(credits=5000, fighters=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()

        # Choose option 2 (Fighters). Room to cap is 40 (50-10) and
        # affordability is 100 (5000/50cr) -- room is the binding limit.
        prompt = await self.say("2")
        self.assertIn("Fighters: 10/50, 50cr each.", prompt)
        self.assertIn("Buy how many? (0-40, or 'cancel')", prompt)

        prompt = await self.say("10")
        self.assertIn("Buy 10 Fighters for 500cr (50cr/unit)? yes/no", prompt)

        prompt = await self.say("yes")
        self.assertIn("Installed 10 Fighters for -500cr.", prompt)
        # Back at the menu, reflecting the purchase, ready for another.
        self.assertIn("Stardock refits:", prompt)
        self.assertIn("Fighters 20/50 @ 50cr each", prompt)

        final = STATE["player"]
        self.assertEqual(final["fighters"], 20)
        self.assertEqual(final["credits"], 5000 - 500)
        self.assertEqual(STATE["upgrade_log"], [("fighters", 10, 500)])
        # Visit stays open after a purchase, so more can be bought.
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_quantity_capped_by_affordability_not_just_ship_cap(self):
        """100cr only buys 2 fighters at 50cr each, even though there's
        plenty of room left under the 50-fighter cap."""
        STATE["player"] = fresh_player(credits=100, fighters=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("2")  # Fighters

        self.assertIn("Buy how many? (0-2, or 'cancel')", prompt)

    async def test_quantity_above_max_is_rejected(self):
        STATE["player"] = fresh_player(credits=5000, shields=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("3")  # Shields: room=190, afford=200 -> max 190
        prompt = await self.say("9999")

        self.assertIn("Max is 190. Enter a smaller quantity, or 'cancel'.", prompt)
        self.assertEqual(STATE["player"]["shields"], 10)  # nothing bought yet

    async def test_quantity_zero_returns_to_menu_without_buying(self):
        STATE["player"] = fresh_player(credits=5000, shields=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("3")  # Shields
        prompt = await self.say("0")

        self.assertIn("Stardock refits:", prompt)
        self.assertEqual(STATE["player"]["shields"], 10)
        self.assertEqual(STATE["upgrade_log"], [])
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)  # visit still open

    async def test_no_at_confirm_returns_to_menu_without_buying(self):
        STATE["player"] = fresh_player(credits=5000, holds_total=20)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("1")    # Cargo Holds
        await self.say("5")    # quote 5 holds
        prompt = await self.say("no")

        self.assertIn("Stardock refits:", prompt)
        self.assertEqual(STATE["player"]["holds_total"], 20)  # unchanged
        self.assertEqual(STATE["upgrade_log"], [])
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)

    async def test_cancel_ends_the_visit_from_the_menu(self):
        STATE["player"] = fresh_player(credits=5000)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Left the Stardock.")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)

    async def test_cancel_ends_the_visit_mid_purchase(self):
        """'cancel' works at the quantity/confirm stages too, not just
        from the top-level menu."""
        STATE["player"] = fresh_player(credits=5000, fighters=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("2")     # Fighters
        await self.say("5")     # quote 5
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Left the Stardock.")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(STATE["player"]["fighters"], 10)  # unchanged
        self.assertEqual(STATE["upgrade_log"], [])

    async def test_stat_already_at_cap_is_rejected(self):
        STATE["player"] = fresh_player(credits=100000, holds_total=75)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("1")  # Cargo Holds, already at the 75 cap

        self.assertIn("Already at max Cargo Holds (75).", prompt)
        self.assertIn("Stardock refits:", prompt)  # re-shows the menu
        # Still on the menu stage -- the rejected pick never advanced state.
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_cant_afford_even_one_unit_is_rejected(self):
        STATE["player"] = fresh_player(credits=10, fighters=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("2")  # Fighters @ 50cr each, only 10cr on hand

        self.assertIn("Can't afford even 1 Fighters (50cr each).", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_invalid_menu_choice_is_rejected(self):
        STATE["player"] = fresh_player(credits=5000)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()

        prompt = await self.say("9")  # out of range -- only 3 options exist
        self.assertIn("Not a valid option.", prompt)

        prompt = await self.say("not-a-number")
        self.assertIn("Reply with a number, or 'cancel'.", prompt)

    async def test_multiple_purchases_in_one_visit(self):
        """Buying holds, then fighters, in the same visit -- the menu
        loop should let a player upgrade more than one stat per dock."""
        STATE["player"] = fresh_player(credits=5000, holds_total=20, fighters=10)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        await self.say("1")     # Cargo Holds
        await self.say("2")     # buy 2 holds @ 500cr = 1000cr
        await self.say("yes")
        await self.say("2")     # Fighters
        await self.say("4")     # buy 4 fighters @ 50cr = 200cr
        prompt = await self.say("yes")

        final = STATE["player"]
        self.assertEqual(final["holds_total"], 22)
        self.assertEqual(final["fighters"], 14)
        self.assertEqual(final["credits"], 5000 - 1000 - 200)
        self.assertEqual(
            STATE["upgrade_log"],
            [("holds_total", 2, 1000), ("fighters", 4, 200)],
        )
        self.assertIn("Stardock refits:", prompt)
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)  # visit still open


class NavigationDockingFlowTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers docking partway through a multi-hop plotted course (the
    cmd_confirm_warp 'p'/'port' branch, and the _resume_navigation_suffix
    helper it relies on). Scenario mirrors the worked example from the
    design discussion: starting in Sec1, with Sec2 and Sec3 adjacent but
    Sec4 two hops away via Sec3, a player routes to Sec4, gets routed
    through Sec3 first, and docks there before continuing on.

    Warp graph used throughout: 1<->2, 1<->3, 3<->4 (so Sec4 is only
    reachable via Sec3, making the BFS route deterministic).
    """

    WARPS = {1: [2, 3], 2: [1], 3: [1, 4], 4: [3]}

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = dict(self.WARPS)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def plot_route_to_4(self):
        """Sec1 -> Sec4 isn't a direct warp, so this always plots the
        2-hop Sec1->Sec3->Sec4 route and leaves it awaiting confirmation."""
        return await main.cmd_move(self.ctx(), "4")

    async def test_docking_at_intermediate_sector_then_resuming_route(self):
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {
            3: fresh_port(
                "SSS",
                fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            )
        }

        # 1. Plot the route -- Sec4 isn't adjacent to Sec1, so this goes
        # through the BFS/confirmation path rather than moving directly.
        prompt = await self.plot_route_to_4()
        self.assertIn("Plotted a 2-warp course to Sec4.", prompt)
        self.assertIn("Warp to: 1 -> 3 -> 4? (yes/no)", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [3, 4])

        # 2. Confirm the first hop -- arrives at Sec3, which has a port,
        # and is asked to confirm the next (final) hop to Sec4.
        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertEqual(STATE["player"]["sector_id"], 3)
        self.assertIn("Warped to Sec3.", prompt)
        self.assertIn("Port: SSS", prompt)
        self.assertIn("Warps: 1, 4", prompt)
        self.assertIn("Warp to: 3 -> 4? (p/yes/no)", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

        # 3. Instead of yes/no, dock at Sec3's port -- the route to Sec4
        # must stay queued while this happens.
        prompt = await main.cmd_confirm_warp(self.ctx(), "p")
        self.assertIn("Fuel Ore", prompt)
        self.assertIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])  # untouched

        # 4. Buy some fuel ore -- the only item in this port's queue, so
        # the visit closes itself out, and the dropped-back-into route
        # confirmation should be appended automatically.
        await main.cmd_trade_step(self.ctx(), "5")
        prompt = await main.cmd_trade_step(self.ctx(), "yes")
        self.assertIn("Bought 5 Fuel Ore for -50cr (10cr/unit).", prompt)
        self.assertIn("Nothing more to trade here.", prompt)
        self.assertIn("Warp to: 3 -> 4? (p/yes/no)", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])  # route still open

        # 5. Pick the route back up -- arrives at the original Sec4
        # destination and the route is finally cleared.
        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertEqual(STATE["player"]["sector_id"], 4)
        self.assertIn("Arrived at Sec4.", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_WARPS)

    async def test_cancelling_the_dock_mid_route_resumes_navigation_prompt(self):
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {
            3: fresh_port(
                "SSS",
                fuel_ore_dir="S", fuel_ore_price=10, fuel_ore_qty=500, fuel_ore_max=1000,
            )
        }

        await self.plot_route_to_4()
        await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3
        await main.cmd_confirm_warp(self.ctx(), "p")    # dock

        prompt = await main.cmd_trade_step(self.ctx(), "cancel")

        self.assertEqual(prompt, "Trade cancelled.\n\nWarp to: 3 -> 4? (p/yes/no)")
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

    async def test_docking_with_nothing_to_trade_reshows_prompt_immediately(self):
        """If the port at the stopover has nothing to offer, no trade
        visit actually starts -- the route prompt should still come
        back right away rather than leaving the player stuck."""
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {3: fresh_port("SSS")}  # no commodity directions set

        await self.plot_route_to_4()
        await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3

        prompt = await main.cmd_confirm_warp(self.ctx(), "port")

        self.assertIn("Nothing to trade with this port.", prompt)
        self.assertIn("Warp to: 3 -> 4? (p/yes/no)", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_TRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

    async def test_docking_at_a_stardock_stopover_then_resuming_route(self):
        """Same idea, but the stopover is a Stardock -- refit purchases
        should be able to happen mid-route too, with the same resume
        behavior once the visit is left via 'cancel'."""
        STATE["player"] = fresh_player(sector_id=1, credits=5000, fighters=10)
        STATE["ports"] = {3: fresh_port("STARDOCK")}

        await self.plot_route_to_4()
        await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3

        prompt = await main.cmd_confirm_warp(self.ctx(), "p")
        self.assertIn("Stardock refits:", prompt)
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])  # untouched

        await main.cmd_stardock_step(self.ctx(), "2")   # Fighters
        await main.cmd_stardock_step(self.ctx(), "5")   # qty
        prompt = await main.cmd_stardock_step(self.ctx(), "yes")
        self.assertIn("Installed 5 Fighters for -250cr.", prompt)
        self.assertIn("Stardock refits:", prompt)  # visit stays open
        self.assertIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

        prompt = await main.cmd_stardock_step(self.ctx(), "cancel")
        self.assertEqual(prompt, "Left the Stardock.\n\nWarp to: 3 -> 4? (p/yes/no)")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])

        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertEqual(STATE["player"]["sector_id"], 4)
        self.assertIn("Arrived at Sec4.", prompt)
        self.assertNotIn(PUBKEY, main.PENDING_WARPS)

    async def test_no_port_at_stopover_omits_p_from_the_prompt(self):
        """The 'p' option should only show up when there's actually a
        port to dock at -- with none at either Sec1 or Sec3, every
        prompt along the route stays plain (yes/no)."""
        STATE["player"] = fresh_player(sector_id=1, credits=10000)
        STATE["ports"] = {}  # no port anywhere on the route

        prompt = await self.plot_route_to_4()
        self.assertIn("Warp to: 1 -> 3 -> 4? (yes/no)", prompt)
        self.assertNotIn("(p/yes/no)", prompt)

        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")  # arrive at Sec3
        self.assertIn("Warp to: 3 -> 4? (yes/no)", prompt)
        self.assertNotIn("(p/yes/no)", prompt)

        # 'p' is still a no-op here, not a route-breaking error -- it's
        # just that cmd_trade has nothing to dock at.
        prompt = await main.cmd_confirm_warp(self.ctx(), "p")
        self.assertIn("No port in current sector.", prompt)
        self.assertIn("Warp to: 3 -> 4? (yes/no)", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [4])


class ShipyardFlowTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers the shipyard sub-menu added to the Stardock visit
    (cmd_stardock_step's "shipyard_menu"/"shipyard_confirm" stages):
    buying a different hull (with the current one automatically traded
    in, since a player can only ever have one ship) or selling the
    current hull back to the free default Falcon.
    """

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def enter_shipyard(self):
        await self.dock()
        return await self.say("4")  # Shipyard is always one past the refits

    async def test_shipyard_entry_shows_catalog_with_current_ship_tagged(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.enter_shipyard()

        self.assertIn("Shipyard:", prompt)
        self.assertIn(
            "1) Falcon (Frigate): 75 holds / 50 fighters / 200 shields -- (current ship)",
            prompt,
        )
        self.assertIn(
            "2) SS Endeavour (Merchant Freighter): 200 holds / 10 fighters / 400 shields -- 20000cr",
            prompt,
        )
        # Bismark has a mine bay -- its line should include mine
        # capacity, unlike the other two hulls which have none.
        self.assertIn(
            "3) Bismark (Capital Ship): 125 holds / 2000 fighters / 3500 shields / 50 mines -- 23500cr",
            prompt,
        )
        # Flying the free default ship -- nothing to trade in, so no sell line.
        self.assertNotIn("Sell your", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")

    async def test_buying_the_bismark_full_flow(self):
        STATE["player"] = fresh_player(credits=30000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("3")  # Bismark
        self.assertIn(
            "Trade in your Falcon (0cr) for a Bismark (23500cr)? Net cost: 23500cr. yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn("Welcome aboard the Bismark! -23500cr (net).", prompt)
        # Back at the top-level menu, now with a Mines refit option that
        # wasn't there for the Falcon, and Shipyard bumped to #5.
        self.assertIn("Cargo Holds 30/125 @ 500cr each", prompt)
        self.assertIn("Fighters 200/2000 @ 50cr each", prompt)
        self.assertIn("Shields 500/3500 @ 25cr each", prompt)
        self.assertIn("Mines 0/50 @ 1000cr each", prompt)
        self.assertIn("5) Shipyard", prompt)

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "Bismark")
        self.assertEqual(final["holds_total"], 30)
        self.assertEqual(final["fighters"], 200)
        self.assertEqual(final["shields"], 500)
        self.assertEqual(final["mines"], 0)
        self.assertEqual(final["credits"], 30000 - 23500)
        self.assertEqual(STATE["ship_log"], [("Bismark", 30, 200, 500, 0, -23500)])

    async def test_buying_a_new_ship_full_flow(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("2")  # SS Endeavour
        self.assertIn(
            "Trade in your Falcon (0cr) for a SS Endeavour (20000cr)? Net cost: 20000cr. yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn("Welcome aboard the SS Endeavour! -20000cr (net).", prompt)
        self.assertIn("Stardock refits:", prompt)  # back at the top-level menu
        self.assertIn("Cargo Holds 50/200 @ 500cr each", prompt)  # new ship's caps

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "SS Endeavour")
        self.assertEqual(final["holds_total"], 50)
        self.assertEqual(final["fighters"], 0)
        self.assertEqual(final["shields"], 50)
        self.assertEqual(final["credits"], 25000 - 20000)
        self.assertEqual(STATE["ship_log"], [("SS Endeavour", 50, 0, 50, 0, -20000)])
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")  # visit stays open

    async def test_selling_current_ship_returns_to_falcon(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="SS Endeavour",
                                        holds_total=50, fighters=0, shields=50)
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("s")
        self.assertIn(
            "Sell your SS Endeavour and return to the Falcon for 10000cr? yes/no",
            prompt,
        )

        prompt = await self.say("yes")
        self.assertIn("Sold your old ship. Welcome back to the Falcon. +10000cr.", prompt)

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "Falcon")
        self.assertEqual(final["holds_total"], 20)
        self.assertEqual(final["fighters"], 10)
        self.assertEqual(final["shields"], 10)
        self.assertEqual(final["credits"], 5000 + 10000)
        self.assertEqual(STATE["ship_log"], [("Falcon", 20, 10, 10, 0, 10000)])

    async def test_ship_swap_clears_cargo(self):
        """Cargo doesn't transfer between hulls -- swapping (buy or
        sell) empties whatever was in the hold."""
        STATE["player"] = fresh_player(
            credits=25000, ship_type="Falcon",
            fuel_ore=5, organics=3, equipment=2,
        )
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        await self.say("2")     # SS Endeavour
        await self.say("yes")

        final = STATE["player"]
        self.assertEqual(final["fuel_ore"], 0)
        self.assertEqual(final["organics"], 0)
        self.assertEqual(final["equipment"], 0)

    async def test_cannot_sell_while_flying_the_falcon(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("sell")

        self.assertIn("You're already flying the Falcon -- nothing to trade in.", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")
        self.assertEqual(STATE["player"]["ship_type"], "Falcon")  # unchanged

    async def test_cannot_buy_a_ship_already_owned(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="SS Endeavour")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("2")  # SS Endeavour, which they already fly

        self.assertIn("You already own the SS Endeavour.", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")

    async def test_cannot_afford_ship_even_after_trade_in(self):
        STATE["player"] = fresh_player(credits=100, ship_type="Falcon")  # trade-in is 0cr
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("2")  # SS Endeavour @ 20000cr net

        self.assertIn(
            "Can't afford the SS Endeavour -- net cost 20000cr (20000cr less a 0cr trade-in), "
            "you have 100cr.",
            prompt,
        )
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")
        self.assertEqual(STATE["ship_log"], [])

    async def test_declining_purchase_confirm_returns_to_shipyard_menu_unchanged(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        await self.say("2")
        prompt = await self.say("no")

        self.assertIn("Shipyard:", prompt)
        self.assertEqual(STATE["player"]["ship_type"], "Falcon")
        self.assertEqual(STATE["ship_log"], [])
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "shipyard_menu")

    async def test_zero_returns_to_the_main_stardock_menu(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("0")

        self.assertIn("Stardock refits:", prompt)
        self.assertEqual(main.PENDING_UPGRADES[PUBKEY]["stage"], "menu")

    async def test_cancel_from_shipyard_ends_the_whole_visit(self):
        STATE["player"] = fresh_player(credits=25000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        await self.enter_shipyard()
        prompt = await self.say("cancel")

        self.assertEqual(prompt, "Left the Stardock.")
        self.assertNotIn(PUBKEY, main.PENDING_UPGRADES)


class MinesRefitTests(unittest.IsolatedAsyncioTestCase):
    """
    Covers the Mines refit stat and its per-ship visibility: only a hull
    with a mine bay (max_mines > 0, i.e. the Bismark) offers it in the
    Stardock menu. Falcon/SS Endeavour have none, so the menu -- and the
    Shipyard option's numbering -- should never mention it for them.
    """

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        STATE["trade_log"] = []
        STATE["upgrade_log"] = []
        STATE["ship_log"] = []
        STATE["port"] = {}
        STATE["ports"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def dock(self):
        return await main.cmd_trade(self.ctx(), "")

    async def say(self, message):
        return await main.cmd_stardock_step(self.ctx(), message)

    async def test_mines_hidden_for_ships_without_a_mine_bay(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="Falcon")
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.dock()

        self.assertNotIn("Mines", prompt)
        self.assertIn("3) Shields", prompt)
        self.assertIn("4) Shipyard", prompt)  # right after the 3 refits, no gap for Mines

    async def test_mines_appears_for_the_bismark_and_shifts_shipyard(self):
        STATE["player"] = fresh_player(credits=5000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=0)
        STATE["port"] = fresh_port("STARDOCK")

        prompt = await self.dock()

        self.assertIn("4) Mines 0/50 @ 1000cr each", prompt)
        self.assertIn("5) Shipyard", prompt)

    async def test_buy_mines_full_flow(self):
        STATE["player"] = fresh_player(credits=10000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=0)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("4")  # Mines
        self.assertIn("Mines: 0/50, 1000cr each.", prompt)
        self.assertIn("Buy how many? (0-10, or 'cancel')", prompt)  # 10000cr / 1000cr each

        prompt = await self.say("5")
        self.assertIn("Buy 5 Mines for 5000cr (1000cr/unit)? yes/no", prompt)

        prompt = await self.say("yes")
        self.assertIn("Installed 5 Mines for -5000cr.", prompt)
        self.assertIn("Mines 5/50 @ 1000cr each", prompt)

        final = STATE["player"]
        self.assertEqual(final["mines"], 5)
        self.assertEqual(final["credits"], 10000 - 5000)
        self.assertEqual(STATE["upgrade_log"], [("mines", 5, 5000)])

    async def test_mines_capped_at_ship_max(self):
        STATE["player"] = fresh_player(credits=1000000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=48)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("4")  # Mines: only 2 units of room left under the 50 cap

        self.assertIn("Buy how many? (0-2, or 'cancel')", prompt)

    async def test_mines_already_at_cap_is_rejected(self):
        STATE["player"] = fresh_player(credits=1000000, ship_type="Bismark",
                                        holds_total=30, fighters=200, shields=500, mines=50)
        STATE["port"] = fresh_port("STARDOCK")

        await self.dock()
        prompt = await self.say("4")  # Mines, already at the 50 cap

        self.assertIn("Already at max Mines (50).", prompt)


class FakeRandom:
    """Deterministic stand-in for the `random` module that main uses.
    `randints` is a queue popped by randint() (falling back to the high
    end once exhausted); choice() returns the element at `choice_index`.
    Install per-test with `main.random = FakeRandom(...)` after setUp's
    reload has restored the real module."""

    def __init__(self, randints=None, choice_index=0):
        self.randints = list(randints or [])
        self.choice_index = choice_index

    def randint(self, a, b):
        return self.randints.pop(0) if self.randints else b

    def choice(self, seq):
        seq = list(seq)
        return seq[self.choice_index % len(seq)]


def chain_warps(n):
    """A simple line graph 1-2-3-...-n, so hop distance equals the
    difference in sector numbers -- handy for asserting escape-pod
    distance ranges precisely."""
    warps = {}
    for i in range(1, n + 1):
        nbrs = []
        if i > 1:
            nbrs.append(i - 1)
        if i < n:
            nbrs.append(i + 1)
        warps[i] = nbrs
    return warps


class MineDamageMathTests(unittest.TestCase):
    """Pure unit tests for apply_mine_damage -- the shields -> fighters ->
    hull cascade, with no db or async in the way."""

    def test_shields_absorb_one_for_one(self):
        # 9 damage, 20 shields: shields take it all, fighters untouched.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(20, 5, 9)
        self.assertEqual((s_after, f_after), (11, 5))
        self.assertEqual((s_lost, f_lost), (9, 0))
        self.assertFalse(destroyed)

    def test_overflow_spills_into_fighters_at_two_per(self):
        # 5 shields gone, 5 damage left -> 3 fighters (ceil(5/2)) lost.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(5, 10, 10)
        self.assertEqual(s_after, 0)
        self.assertEqual(s_lost, 5)
        self.assertEqual(f_lost, 3)        # ceil(5 / 2)
        self.assertEqual(f_after, 7)
        self.assertFalse(destroyed)

    def test_exact_absorption_survives_at_zero_zero(self):
        # 2 shields + 2 fighters (worth 4) = 6 capacity vs 5 damage: the
        # ship is stripped to 0/0 but NOT destroyed -- damage ran out
        # before the defenses did.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(2, 2, 5)
        self.assertEqual((s_after, f_after), (0, 0))
        self.assertFalse(destroyed)

    def test_destroyed_when_damage_outlasts_both(self):
        # 2 shields + 1 fighter (worth 2) = 4 capacity vs 5 damage: 1
        # point punches through with nothing left -> destroyed.
        s_after, f_after, s_lost, f_lost, destroyed = main.apply_mine_damage(2, 1, 5)
        self.assertEqual((s_after, f_after), (0, 0))
        self.assertTrue(destroyed)

    def test_no_defenses_any_damage_destroys(self):
        _, _, _, _, destroyed = main.apply_mine_damage(0, 0, 1)
        self.assertTrue(destroyed)

    def test_zero_damage_is_harmless(self):
        s_after, f_after, _, _, destroyed = main.apply_mine_damage(7, 3, 0)
        self.assertEqual((s_after, f_after), (7, 3))
        self.assertFalse(destroyed)


class EscapeSectorTests(unittest.TestCase):
    """choose_escape_sector / sectors_within_hop_range against a known
    line graph where hop distance is exact."""

    def test_picks_a_sector_in_the_4_to_6_hop_band(self):
        graph = chain_warps(40)
        for _ in range(20):
            dest = main.choose_escape_sector(graph, 20)  # real randomness
            self.assertIn(dest, {14, 15, 16, 24, 25, 26})

    def test_falls_back_to_farthest_when_band_is_empty(self):
        # A 3-sector line: from the end, nothing is 4-6 hops out, so it
        # falls back to the farthest reachable sector.
        graph = chain_warps(3)
        self.assertEqual(main.choose_escape_sector(graph, 1), 3)

    def test_returns_none_when_nowhere_to_go(self):
        self.assertIsNone(main.choose_escape_sector({5: []}, 5))


class LayMinesCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        STATE["mine_log"] = []
        STATE["defense_log"] = []
        STATE["sector_mines"] = {}
        STATE["warps"] = {}

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def lay(self, args):
        return await main.cmd_lay_mines(self.ctx(), args)

    async def test_lay_mines_deploys_and_decrements_aboard(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=10)
        prompt = await self.lay("4")
        self.assertIn("Laid 4 mines in Sec42; 6 still aboard.", prompt)
        self.assertEqual(STATE["player"]["mines"], 6)
        self.assertEqual(STATE["mine_log"], [(42, 1, 4)])
        self.assertEqual(STATE["sector_mines"][42], {1: 4})

    async def test_laying_again_accumulates_in_the_same_sector(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=10)
        await self.lay("4")
        STATE["player"]["mines"] = 6  # mirror the decrement for the 2nd ctx
        await self.lay("2")
        self.assertEqual(STATE["sector_mines"][42], {1: 6})

    async def test_safe_zone_sectors_reject_laying(self):
        STATE["player"] = fresh_player(sector_id=10, ship_type="Bismark", mines=10)
        prompt = await self.lay("1")
        self.assertIn("safe zone", prompt)
        self.assertEqual(STATE["mine_log"], [])         # nothing deployed
        self.assertEqual(STATE["player"]["mines"], 10)  # nothing spent

    async def test_first_sector_outside_safe_zone_is_allowed(self):
        STATE["player"] = fresh_player(sector_id=11, ship_type="Bismark", mines=5)
        prompt = await self.lay("1")
        self.assertIn("Laid 1 mine in Sec11", prompt)

    async def test_no_mines_aboard_is_rejected(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Falcon", mines=0)
        prompt = await self.lay("3")
        self.assertIn("No mines aboard", prompt)
        self.assertEqual(STATE["mine_log"], [])

    async def test_missing_count_prompts_for_one(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=5)
        prompt = await self.lay("")
        self.assertIn("Lay how many mines?", prompt)
        self.assertEqual(STATE["mine_log"], [])

    async def test_more_than_aboard_is_rejected(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=3)
        prompt = await self.lay("4")
        self.assertIn("only have 3 mines aboard", prompt)
        self.assertEqual(STATE["mine_log"], [])

    async def test_non_numeric_and_zero_are_rejected(self):
        STATE["player"] = fresh_player(sector_id=42, ship_type="Bismark", mines=5)
        self.assertIn("whole number", await self.lay("lots"))
        self.assertIn("from 1 up", await self.lay("0"))
        self.assertEqual(STATE["mine_log"], [])


class MineDetonationTests(unittest.IsolatedAsyncioTestCase):
    """Entering a sector that holds someone else's mines: damage, survival,
    own-mine safety, and the destruction -> escape-pod path."""

    def setUp(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(main)
        STATE["ship_log"] = []
        STATE["mine_log"] = []
        STATE["defense_log"] = []
        STATE["sector_mines"] = {}
        STATE["ports"] = {}
        STATE["port"] = {}
        STATE["warps"] = chain_warps(30)

    def ctx(self):
        return FakeCtx(PUBKEY, dict(STATE["player"]))

    async def test_surviving_a_hit_records_reduced_defenses(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=20, fighters=10)
        STATE["sector_mines"] = {13: {2: 2}}   # player 2 laid 2 mines
        main.random = FakeRandom([5, 4])        # 9 total damage

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("2 mines detonate for 9 damage", prompt)
        self.assertIn("Lost 9 shields, 0 fighters", prompt)
        self.assertIn("now 11 shields, 10 fighters", prompt)
        self.assertEqual(STATE["defense_log"], [(1, 11, 10)])
        self.assertEqual(STATE["player"]["sector_id"], 13)
        self.assertEqual(STATE["sector_mines"].get(13), {})  # detonated, cleared

    async def test_own_mines_do_not_detonate(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=20, fighters=10)
        STATE["sector_mines"] = {13: {1: 5}}    # the entering player's own mines

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("Moved to Sec13.", prompt)
        self.assertNotIn("detonate", prompt)
        self.assertEqual(STATE["defense_log"], [])
        self.assertEqual(STATE["sector_mines"][13], {1: 5})  # left in place

    async def test_destruction_ejects_into_an_escape_pod_far_away(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=5, fighters=2,
                                       fuel_ore=10, organics=5, equipment=3)
        STATE["sector_mines"] = {13: {2: 10}}
        main.random = FakeRandom([10] * 10)     # 100 damage -- lethal

        prompt = await main.cmd_move(self.ctx(), "13")

        self.assertIn("Bismark is DESTROYED", prompt)
        self.assertIn("Escape Pod", prompt)

        final = STATE["player"]
        self.assertEqual(final["ship_type"], "Escape Pod")
        self.assertEqual((final["shields"], final["fighters"], final["holds_total"]), (0, 0, 0))
        self.assertEqual((final["fuel_ore"], final["organics"], final["equipment"]), (0, 0, 0))
        # Landed somewhere 4-6 hops from the blast (Sec13) on the line graph.
        landed = final["sector_id"]
        self.assertIn(landed, set(main.sectors_within_hop_range(STATE["warps"], 13, 4, 6)))
        self.assertEqual(len(STATE["ship_log"]), 1)
        self.assertEqual(STATE["ship_log"][0][0], "Escape Pod")

    async def test_death_mid_route_cancels_the_rest_of_the_course(self):
        STATE["player"] = fresh_player(id=1, sector_id=12, ship_type="Bismark",
                                       shields=0, fighters=0)
        STATE["sector_mines"] = {13: {2: 3}}
        main.random = FakeRandom([10, 10, 10])

        # Plot 12 -> 13 -> 14 -> 15; the first hop lands on the mines.
        prompt = await main.cmd_move(self.ctx(), "15")
        self.assertIn("Warp to: 12 -> 13 -> 14 -> 15?", prompt)
        self.assertEqual(main.PENDING_WARPS[PUBKEY], [13, 14, 15])

        prompt = await main.cmd_confirm_warp(self.ctx(), "yes")
        self.assertIn("DESTROYED", prompt)
        self.assertNotIn("Warp to:", prompt)              # route abandoned
        self.assertNotIn(PUBKEY, main.PENDING_WARPS)

    async def test_escape_pod_is_not_offered_for_sale_in_the_shipyard(self):
        STATE["player"] = fresh_player(id=1, sector_id=1, ship_type="Escape Pod")
        STATE["port"] = fresh_port("STARDOCK")

        main.PENDING_UPGRADES[PUBKEY] = {"stage": "shipyard_menu"}
        prompt = await main.cmd_stardock_step(self.ctx(), "")  # re-show shipyard menu

        self.assertIn("1) Falcon", prompt)
        self.assertIn("2) SS Endeavour", prompt)
        self.assertIn("3) Bismark", prompt)
        self.assertNotIn("4)", prompt)                     # pod not a buy option
        self.assertIn("Sell your Escape Pod", prompt)      # but can be traded back in


if __name__ == "__main__":
    unittest.main()