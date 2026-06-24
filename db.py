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
            fuel_ore INTEGER NOT NULL DEFAULT 0,
            organics INTEGER NOT NULL DEFAULT 0,
            equipment INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


# --- Game balance constants ---
HOME_SECTOR = 1
STARTING_CREDITS = 1000
DAILY_TURNS = 50
STARTER_SHIP = {
    "ship_type": "Merchant Cruiser",
    "holds_total": 20,
    "fighters": 10,
    "shields": 10,
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


def move_player_to_sector(player_id, sector_id):
    """Update a player's current sector. Caller is responsible for validating
    that the move is legal (e.g. that sector_id is adjacent)."""
    conn = get_connection()
    conn.execute(
        "UPDATE players SET sector_id = ? WHERE id = ?", (sector_id, player_id)
    )
    conn.commit()
    conn.close()


def get_player_with_ship(pubkey_prefix):
    """Fetch a player joined with their ship, as a dict. None if no such player."""
    conn = get_connection()
    row = conn.execute("""
        SELECT players.*,
               ships.id AS ship_id, ships.ship_type, ships.holds_total,
               ships.fighters, ships.shields,
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
        """INSERT INTO ships (player_id, ship_type, holds_total, fighters, shields, fuel_ore, organics, equipment)
           VALUES (?, ?, ?, ?, ?, 0, 0, 0)""",
        (player_id, STARTER_SHIP["ship_type"], STARTER_SHIP["holds_total"],
         STARTER_SHIP["fighters"], STARTER_SHIP["shields"])
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
