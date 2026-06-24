"""
Generates the TradeWars-style galaxy: sectors, warps, and ports.

Run directly to (re)generate the galaxy:
    python galaxy.py

This wipes and rebuilds sectors/warps/ports only — it does not touch
the messages table or any player/ship data (those come later).
"""

import random

from db import init_db, get_connection

NUM_SECTORS = 1000
EXTRA_WARP_EDGES = 1000      # additional undirected edges on top of the spanning tree
PORT_DENSITY = 0.20          # ~20% of sectors get a port
HOME_SECTOR = 1              # always gets the Stardock

# Each port class is a 3-letter code for (fuel_ore, organics, equipment) direction.
# 'B' = port buys from the player (player sells), 'S' = port sells to the player (player buys).
PORT_CLASSES = ["SSS", "SSB", "SBS", "SBB", "BSS", "BSB", "BBS", "BBB"]

# (min_qty, max_qty) per commodity -- capacity range, independent of
# whether a given port buys or sells that commodity.
QUANTITY_RANGES = {
    "fuel_ore": (1000, 9000),
    "organics": (1000, 9000),
    "equipment": (500, 5000),
}

# (min_price, max_price) a port pays per unit when it's BUYING that
# commodity from a trader. Selling-port prices are derived from these via
# PRICE_DISCOUNT_WHEN_SELLING below, rather than given their own
# independent range -- that's what guarantees traders a margin, instead
# of buy/sell prices both being drawn from the same distribution and
# only averaging out to a profit by chance.
BUY_PRICE_RANGES = {
    "fuel_ore": (100, 200),
    "organics": (200, 350),
    "equipment": (300, 550),
}

# How much cheaper a port's selling price is, on average, than its buying
# price for the same commodity. 0.10 = sell prices average 10% below buy
# prices. Applied by scaling both ends of the buy range down by this
# fraction (see _sell_price_range), which keeps the same range *shape*
# while making the *average* sell price exactly (1 - discount) times the
# average buy price -- a trader buying fuel ore at one port's sell price
# and selling it at another port's buy price nets roughly this margin.
PRICE_DISCOUNT_WHEN_SELLING = {
    "fuel_ore": 0.10,
    "organics": 0.20,
    "equipment": 0.35,
}


def _sell_price_range(commodity):
    """Selling-port (min, max) price for a commodity, scaled down from
    its buying-port range by PRICE_DISCOUNT_WHEN_SELLING. Scaling both
    ends by the same factor preserves the range's relative shape and
    guarantees the average sell price comes out to exactly
    (1 - discount) times the average buy price, since averages scale
    linearly."""
    buy_min, buy_max = BUY_PRICE_RANGES[commodity]
    factor = 1 - PRICE_DISCOUNT_WHEN_SELLING[commodity]
    return (round(buy_min * factor), round(buy_max * factor))


def generate_sectors(conn):
    conn.executemany(
        "INSERT INTO sectors (id, name) VALUES (?, ?)",
        [(i, f"Sector {i}") for i in range(1, NUM_SECTORS + 1)]
    )


def generate_warps(conn):
    sector_ids = list(range(1, NUM_SECTORS + 1))
    edges = set()

    # Randomized spanning tree (Prim-style): guarantees full connectivity.
    shuffled = sector_ids[:]
    random.shuffle(shuffled)
    connected = [shuffled[0]]
    for node in shuffled[1:]:
        other = random.choice(connected)
        edges.add(tuple(sorted((node, other))))
        connected.append(node)

    # Extra random edges for realistic warp density (~4 warps/sector avg).
    attempts = 0
    while len(edges) < (NUM_SECTORS - 1) + EXTRA_WARP_EDGES and attempts < EXTRA_WARP_EDGES * 20:
        a, b = random.sample(sector_ids, 2)
        edges.add(tuple(sorted((a, b))))
        attempts += 1

    rows = []
    for a, b in edges:
        rows.append((a, b))
        rows.append((b, a))  # symmetric warps for v1

    conn.executemany(
        "INSERT OR IGNORE INTO warps (from_sector_id, to_sector_id) VALUES (?, ?)",
        rows
    )


def generate_ports(conn):
    sector_ids = list(range(1, NUM_SECTORS + 1))
    sector_ids.remove(HOME_SECTOR)

    num_ports = int(NUM_SECTORS * PORT_DENSITY)
    port_sectors = random.sample(sector_ids, num_ports)

    rows = []

    # Stardock at the home sector — special, no commodity trading in v1.
    rows.append((
        HOME_SECTOR, "STARDOCK", None, None, None,
        0, 0, 0, 0, 0, 0, 0, 0, 0
    ))

    for sector_id in port_sectors:
        port_class = random.choice(PORT_CLASSES)
        fuel_dir, organics_dir, equip_dir = port_class[0], port_class[1], port_class[2]

        commodity_data = {}
        for commodity, direction in (
            ("fuel_ore", fuel_dir),
            ("organics", organics_dir),
            ("equipment", equip_dir),
        ):
            min_qty, max_qty = QUANTITY_RANGES[commodity]
            capacity = random.randint(min_qty, max_qty)
            min_price, max_price = (
                _sell_price_range(commodity) if direction == "S" else BUY_PRICE_RANGES[commodity]
            )
            price = random.randint(min_price, max_price)
            # Selling ports ('S') start fully stocked (qty = capacity) --
            # they have product on hand ready to sell to traders. Buying
            # ports ('B') start empty (qty = 0) -- they have demand for
            # product they don't have yet, which is what gives traders
            # something to sell *to* them. Capacity (the max column) is
            # the same random range either way; only the starting qty
            # differs by direction.
            starting_qty = capacity if direction == "S" else 0
            commodity_data[commodity] = (starting_qty, capacity, price)

        rows.append((
            sector_id, port_class, fuel_dir, organics_dir, equip_dir,
            commodity_data["fuel_ore"][0], commodity_data["organics"][0], commodity_data["equipment"][0],
            commodity_data["fuel_ore"][1], commodity_data["organics"][1], commodity_data["equipment"][1],
            commodity_data["fuel_ore"][2], commodity_data["organics"][2], commodity_data["equipment"][2],
        ))

    conn.executemany(
        """INSERT INTO ports (
            sector_id, port_class, fuel_ore_dir, organics_dir, equipment_dir,
            fuel_ore_qty, organics_qty, equipment_qty,
            fuel_ore_max, organics_max, equipment_max,
            fuel_ore_price, organics_price, equipment_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows
    )


def generate_galaxy(seed=None, skip_confirmation=False):
    """
    Wipe and rebuild sectors/warps/ports. Player and ship rows are left
    alone (their data isn't touched), but since players.sector_id and,
    transitively, ships reference sectors(id), the delete-then-reinsert
    sequence below is momentarily inconsistent: every existing sector row
    is gone for an instant before it's recreated. Foreign keys are
    disabled on this connection only, for just that window, since by the
    time this commits every sector id 1..NUM_SECTORS exists again and
    those references are valid. (NUM_SECTORS is a fixed constant, so the
    set of valid ids doesn't change across runs.)

    Set skip_confirmation=True to bypass the interactive prompt below
    (e.g. for scripted/test use); the CLI entry point always prompts.
    """
    if not skip_confirmation:
        print("WARNING: this will permanently reset the galaxy -- every")
        print("sector's warps and ports will be wiped and regenerated from")
        print("scratch. Existing players keep their credits, cargo, and")
        print("ship, but the universe around them (the entire port/warp")
        print("layout) will be completely different afterward.")
        answer = input("Type 'yes' to continue, anything else to cancel: ").strip().lower()
        if answer != "yes":
            print("Cancelled. Galaxy was not regenerated.")
            return

    if seed is not None:
        random.seed(seed)

    init_db()
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")

    conn.execute("DELETE FROM ports")
    conn.execute("DELETE FROM warps")
    conn.execute("DELETE FROM sectors")

    generate_sectors(conn)
    generate_warps(conn)
    generate_ports(conn)

    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    num_warps = conn.execute("SELECT COUNT(*) FROM warps").fetchone()[0]
    num_ports = conn.execute("SELECT COUNT(*) FROM ports").fetchone()[0]
    conn.close()

    print(f"Galaxy generated: {NUM_SECTORS} sectors, {num_warps} directed warps, {num_ports} ports")


if __name__ == "__main__":
    generate_galaxy()
