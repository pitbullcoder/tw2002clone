import sqlite3
from datetime import datetime, timezone, timedelta

DB_PATH = "meshcore_messages.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,      -- 'rx' or 'tx'
            pubkey_prefix TEXT,
            sender_name TEXT,
            text TEXT,
            timestamp TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sectors (
            id INTEGER PRIMARY KEY,       -- sector number, 1..1000
            name TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS warps (
            from_sector_id INTEGER NOT NULL REFERENCES sectors(id),
            to_sector_id INTEGER NOT NULL REFERENCES sectors(id),
            PRIMARY KEY (from_sector_id, to_sector_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_warps_from ON warps(from_sector_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_id INTEGER NOT NULL UNIQUE REFERENCES sectors(id),
            port_class TEXT NOT NULL,     -- e.g. 'BBS', 'SSB', or 'STARDOCK'
            fuel_ore_dir TEXT,            -- 'B' (port buys) or 'S' (port sells)
            organics_dir TEXT,
            equipment_dir TEXT,
            fuel_ore_qty INTEGER DEFAULT 0,
            organics_qty INTEGER DEFAULT 0,
            equipment_qty INTEGER DEFAULT 0,
            fuel_ore_max INTEGER DEFAULT 0,
            organics_max INTEGER DEFAULT 0,
            equipment_max INTEGER DEFAULT 0,
            fuel_ore_price INTEGER DEFAULT 0,
            organics_price INTEGER DEFAULT 0,
            equipment_price INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pubkey_prefix TEXT NOT NULL UNIQUE,
            name TEXT,
            sector_id INTEGER NOT NULL REFERENCES sectors(id),
            credits INTEGER NOT NULL DEFAULT 0,
            turns_remaining INTEGER NOT NULL DEFAULT 0,
            last_turn_reset TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL UNIQUE REFERENCES players(id),
            ship_type TEXT NOT NULL,
            holds_total INTEGER NOT NULL,
            fighters INTEGER NOT NULL DEFAULT 0,
            shields INTEGER NOT NULL DEFAULT 0,
            mines INTEGER NOT NULL DEFAULT 0,
            fuel_ore INTEGER NOT NULL DEFAULT 0,
            organics INTEGER NOT NULL DEFAULT 0,
            equipment INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migration for databases created before mines existed -- CREATE
    # TABLE IF NOT EXISTS above is a no-op against an already-existing
    # ships table, so this adds the column on its own for anyone running
    # against an older meshcore_messages.db.
    try:
        conn.execute("ALTER TABLE ships ADD COLUMN mines INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # already has the column

    # Mines a player has *deployed* into a sector (distinct from the
    # ships.mines column, which is the unlaid count carried aboard). One
    # row per (sector, owner); qty accumulates as the same player lays
    # more in the same spot. A player's own mines never detonate on them,
    # which is why ownership is tracked per row rather than as a single
    # per-sector count -- see clear_hostile_mines / get_hostile_mine_total.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_mines (
            sector_id INTEGER NOT NULL REFERENCES sectors(id),
            player_id INTEGER NOT NULL REFERENCES players(id),
            qty INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (sector_id, player_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sector_mines_sector ON sector_mines(sector_id)")

    conn.commit()
    conn.close()


# --- Game balance constants ---
HOME_SECTOR = 1
STARTING_CREDITS = 1000
DAILY_TURNS = 50

DEFAULT_SHIP_TYPE = "Falcon"

# The hull a destroyed player is dropped into. Looked up in SHIP_CATALOG
# like any other, but flagged "purchasable": False there so it never
# appears in the shipyard.
ESCAPE_POD_SHIP = "Escape Pod"

# --- Shipyard catalog --------------------------------------------------
# Every purchasable hull, keyed by name. To add a new ship later, just
# add another entry here -- nothing else in db.py or main.py needs to
# change structurally to support it.
#
#   classification -- flavor text shown in the shipyard menu.
#   price          -- credits to buy this hull outright. 0 for the
#                      Falcon: it's the free starter ship, never an
#                      actual purchase -- a player only ever lands back
#                      on it via a sell-back (see sell_value() below).
#   base_*         -- what a freshly acquired hull starts with.
#   max_*          -- the per-stat caps a Stardock refit can push that
#                      hull's holds/fighters/shields/mines up to. Looked
#                      up per the player's *current* ship_type (see
#                      upgrade_ship_stat and cmd_stardock_step in
#                      main.py) rather than a single fixed limit, since
#                      different hulls cap out at different points.
#                      max_mines is 0 for any hull without a mine bay --
#                      main.py's refit menu hides the Mines option
#                      entirely for those ships rather than showing a
#                      0/0 line.
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
        "price": 1,
        "base_holds": 30,
        "base_fighters": 200,
        "base_shields": 500,
        "base_mines": 0,
        "max_holds": 125,
        "max_fighters": 2000,
        "max_shields": 3500,
        "max_mines": 50,
    },
    # Not a hull anyone buys -- it's where a destroyed pilot ends up. A
    # mine field punches through a ship and the player is ejected into one
    # of these (see eject-to-pod handling in main.py): no holds, no
    # fighters, no shields, no mine bay, nothing. "purchasable": False
    # keeps it out of the Stardock shipyard menu (main.py filters on it)
    # so it can live in the catalog -- as the destruction code needs --
    # without ever showing up as something to buy. A pilot flying one can
    # still trade it back in for the free Falcon at a Stardock, which is
    # the intended way to climb back out of the pod.
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

# Fraction of a hull's catalog price refunded when it's traded in at the
# Stardock shipyard.
SHIP_RESALE_FRACTION = 0.5


def sell_value(ship_type):
    """Trade-in credit for handing back `ship_type` at the Stardock
    shipyard -- a flat fraction of its catalog price. Always 0 for the
    Falcon, since it's priced at 0cr to begin with."""
    return round(SHIP_CATALOG[ship_type]["price"] * SHIP_RESALE_FRACTION)


STARTER_SHIP = {
    "ship_type": DEFAULT_SHIP_TYPE,
    "holds_total": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_holds"],
    "fighters": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_fighters"],
    "shields": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_shields"],
    "mines": SHIP_CATALOG[DEFAULT_SHIP_TYPE]["base_mines"],
}

# Stardock refit prices, in credits per unit. Keyed by the ships column
# each upgrade applies to, so callers can go straight from a column name
# to its price without a separate lookup table. Same price regardless of
# ship type -- only the per-stat *caps* (SHIP_CATALOG[...]["max_*"])
# vary by hull.
STARDOCK_PRICES = {
    "holds_total": 500,
    "fighters": 50,
    "shields": 25,
    "mines": 1,
}


def get_all_warps():
    """
    Return the full warp graph as {sector_id: [adjacent_sector_ids]}.
    Used for pathfinding (BFS) when a requested destination isn't directly
    adjacent -- one query here is far cheaper than calling
    get_adjacent_sectors() once per node visited during the search.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT from_sector_id, to_sector_id FROM warps ORDER BY from_sector_id, to_sector_id"
    ).fetchall()
    conn.close()

    graph = {}
    for row in rows:
        graph.setdefault(row["from_sector_id"], []).append(row["to_sector_id"])
    return graph


def get_adjacent_sectors(sector_id):
    """Return a sorted list of sector ids directly reachable by warp from sector_id."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT to_sector_id FROM warps WHERE from_sector_id = ? ORDER BY to_sector_id",
        (sector_id,)
    ).fetchall()
    conn.close()
    return [row["to_sector_id"] for row in rows]


def get_port(sector_id):
    """Fetch the port in a sector, as a dict. None if the sector has no port."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM ports WHERE sector_id = ?", (sector_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


_TRADEABLE_COMMODITIES = ("fuel_ore", "organics", "equipment")


def execute_trade(player_id, port_id, commodity, qty, total_price, player_is_buying):
    """
    Apply a completed trade in a single transaction:
      - player's credits go down (buying) or up (selling) by total_price
      - player's ship cargo for `commodity` goes up (buying) or down (selling) by qty
      - the port's stock for `commodity` goes down (buying, since the
        port is selling and depleting) or up (selling, since the port is
        buying and filling) by qty
    Caller is responsible for validating quantities/credits/capacity
    beforehand -- this function does not re-check anything.
    """
    if commodity not in _TRADEABLE_COMMODITIES:
        raise ValueError(f"invalid commodity column: {commodity}")

    credit_delta = -total_price if player_is_buying else total_price
    cargo_delta = qty if player_is_buying else -qty
    port_qty_delta = -qty if player_is_buying else qty

    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits + ? WHERE id = ?",
        (credit_delta, player_id)
    )
    conn.execute(
        f"UPDATE ships SET {commodity} = {commodity} + ? WHERE player_id = ?",
        (cargo_delta, player_id)
    )
    conn.execute(
        f"UPDATE ports SET {commodity}_qty = {commodity}_qty + ? WHERE id = ?",
        (port_qty_delta, port_id)
    )
    conn.commit()
    conn.close()


_UPGRADEABLE_SHIP_STATS = ("holds_total", "fighters", "shields", "mines")


def upgrade_ship_stat(player_id, stat_column, qty, total_price):
    """
    Apply a completed Stardock refit purchase in a single transaction:
      - player's credits go down by total_price
      - the ship's `stat_column` (holds_total, fighters, or shields)
        goes up by qty
    Caller is responsible for validating quantity/credits/the ship's
    per-stat cap beforehand -- this function does not re-check anything.
    """
    if stat_column not in _UPGRADEABLE_SHIP_STATS:
        raise ValueError(f"invalid ship stat column: {stat_column}")

    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits - ? WHERE id = ?",
        (total_price, player_id)
    )
    conn.execute(
        f"UPDATE ships SET {stat_column} = {stat_column} + ? WHERE player_id = ?",
        (qty, player_id)
    )
    conn.commit()
    conn.close()


def buy_ship(player_id, ship_type, holds_total, fighters, shields, mines, credit_delta):
    """
    Apply a completed Stardock shipyard transaction in a single
    transaction -- either buying a different hull, or selling the
    current one back (the caller just passes the Falcon's base stats
    and a positive credit_delta for that case):
      - player's credits change by credit_delta (negative for a net
        purchase cost, positive for a net refund)
      - the ship's type and holds_total/fighters/shields/mines are
        replaced with the new hull's values
      - any cargo being carried is cleared -- a hull swap empties the
        hold, since the old ship's cargo doesn't transfer to the new one
    Caller is responsible for validating affordability beforehand --
    this function does not re-check anything.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE players SET credits = credits + ? WHERE id = ?",
        (credit_delta, player_id)
    )
    conn.execute(
        """UPDATE ships
           SET ship_type = ?, holds_total = ?, fighters = ?, shields = ?, mines = ?,
               fuel_ore = 0, organics = 0, equipment = 0
           WHERE player_id = ?""",
        (ship_type, holds_total, fighters, shields, mines, player_id)
    )
    conn.commit()
    conn.close()


def move_player_to_sector(player_id, sector_id):
    """Update a player's current sector. Caller is responsible for validating
    that the move is legal (e.g. that sector_id is adjacent)."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET sector_id = ? WHERE id = ?", (sector_id, player_id)
    )
    conn.commit()
    conn.close()


def lay_mines(player_id, sector_id, qty):
    """
    Deploy `qty` of a player's carried mines into `sector_id`, in one
    transaction:
      - the ship's `mines` count (unlaid, aboard) drops by qty
      - the sector_mines row for (sector_id, player_id) goes up by qty,
        created if this is the player's first mine in that sector
    Caller validates qty (> 0, <= mines aboard) and the sector rule
    (not a mine-free sector) beforehand -- this does not re-check.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET mines = mines - ? WHERE player_id = ?",
        (qty, player_id)
    )
    conn.execute(
        """INSERT INTO sector_mines (sector_id, player_id, qty)
           VALUES (?, ?, ?)
           ON CONFLICT(sector_id, player_id) DO UPDATE SET qty = qty + excluded.qty""",
        (sector_id, player_id, qty)
    )
    conn.commit()
    conn.close()


def get_hostile_mine_total(sector_id, player_id):
    """Total mines in `sector_id` deployed by players OTHER than
    `player_id` -- i.e. how many would detonate if `player_id` entered.
    A player's own mines are excluded, since they never detonate on
    their owner. 0 if the sector is clear (for that player)."""
    conn = get_connection()
    row = conn.execute(
        """SELECT COALESCE(SUM(qty), 0) AS total
           FROM sector_mines
           WHERE sector_id = ? AND player_id != ?""",
        (sector_id, player_id)
    ).fetchone()
    conn.close()
    return row["total"]


def clear_hostile_mines(sector_id, player_id):
    """Remove every mine in `sector_id` NOT owned by `player_id` -- the
    detonated ones are spent and gone. The entering player's own mines
    (if any) are left in place. Pairs with get_hostile_mine_total: read
    the count, then clear, when resolving an entry."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM sector_mines WHERE sector_id = ? AND player_id != ?",
        (sector_id, player_id)
    )
    conn.commit()
    conn.close()


def set_ship_defenses(player_id, shields, fighters):
    """Set a ship's shields and fighters to absolute values -- used to
    write back what's left after a mine hit the player survived. (A hit
    they don't survive goes through buy_ship instead, swapping the whole
    hull for an Escape Pod.)"""
    conn = get_connection()
    conn.execute(
        "UPDATE ships SET shields = ?, fighters = ? WHERE player_id = ?",
        (shields, fighters, player_id)
    )
    conn.commit()
    conn.close()


def get_player_with_ship(pubkey_prefix):
    """Fetch a player joined with their ship, as a dict. None if no such player."""
    conn = get_connection()
    row = conn.execute("""
        SELECT players.*,
               ships.id AS ship_id, ships.ship_type, ships.holds_total,
               ships.fighters, ships.shields, ships.mines,
               ships.fuel_ore, ships.organics, ships.equipment
        FROM players
        JOIN ships ON ships.player_id = players.id
        WHERE players.pubkey_prefix = ?
    """, (pubkey_prefix,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_player(pubkey_prefix, name):
    """Create a new player + starter ship. Returns the player dict (with ship)."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()

    cur = conn.execute(
        """INSERT INTO players (pubkey_prefix, name, sector_id, credits, turns_remaining, last_turn_reset, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (pubkey_prefix, name, HOME_SECTOR, STARTING_CREDITS, DAILY_TURNS, now, now)
    )
    player_id = cur.lastrowid

    conn.execute(
        """INSERT INTO ships (player_id, ship_type, holds_total, fighters, shields, mines, fuel_ore, organics, equipment)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)""",
        (player_id, STARTER_SHIP["ship_type"], STARTER_SHIP["holds_total"],
         STARTER_SHIP["fighters"], STARTER_SHIP["shields"], STARTER_SHIP["mines"])
    )

    conn.commit()
    conn.close()
    return get_player_with_ship(pubkey_prefix)


def get_or_create_player(pubkey_prefix, name):
    """Returns (player_dict, is_new). Creates player + ship on first contact."""
    player = get_player_with_ship(pubkey_prefix)
    if player is not None:
        return player, False
    return create_player(pubkey_prefix, name), True


def reset_turns_if_needed(player_id):
    """Resets turns_remaining to DAILY_TURNS if more than 24h have passed since last reset."""
    conn = get_connection()
    row = conn.execute(
        "SELECT last_turn_reset FROM players WHERE id = ?", (player_id,)
    ).fetchone()

    last_reset = datetime.fromisoformat(row["last_turn_reset"])
    now = datetime.now(timezone.utc)

    if now - last_reset > timedelta(hours=24):
        conn.execute(
            "UPDATE players SET turns_remaining = ?, last_turn_reset = ? WHERE id = ?",
            (DAILY_TURNS, now.isoformat(), player_id)
        )
        conn.commit()

    conn.close()


def log_message(direction, pubkey_prefix, sender_name, text):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (direction, pubkey_prefix, sender_name, text, timestamp) VALUES (?, ?, ?, ?, ?)",
        (direction, pubkey_prefix, sender_name, text, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()