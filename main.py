import asyncio
import re
import textwrap
import time
from collections import deque

from meshcore import MeshCore, EventType

from db import (
    init_db,
    log_message,
    get_or_create_player,
    reset_turns_if_needed,
    get_player_with_ship,
    get_adjacent_sectors,
    get_all_warps,
    get_port,
    move_player_to_sector,
    execute_trade,
    upgrade_ship_stat,
    SHIP_MAX_HOLDS,
    SHIP_MAX_FIGHTERS,
    SHIP_MAX_SHIELDS,
    STARDOCK_PRICES,
)

print("MeshCore bot started...")

MAX_MSG_LEN = 130  # hard limit enforced by the meshcore radio/app
PUBLIC_CHANNEL_IDX = 0  # which channel index the bot listens to for public commands

MIN_SECTOR_ID = 1
MAX_SECTOR_ID = 1000  # matches galaxy.py's NUM_SECTORS

# Messages older than this (based on the sender's own sender_timestamp,
# not arrival time) are ignored. This catches commands that queued up on
# the radio while the app was disconnected and all arrive in a burst once
# it reconnects -- without this, a stale "move" or other command would
# get acted on as if it just happened.
MAX_MESSAGE_AGE_SECONDS = 120

# Matches anything that *looks* like a number a player might type as a
# move request -- including negatives and decimals -- so we can route it
# to cmd_move for a specific validation error, rather than letting it fall
# through to the generic "Unknown command" reply.
_NUMBER_LIKE = re.compile(r"^-?\d+(\.\d+)?$")

# pubkey -> list of remaining sector ids (not including the player's
# current sector) for a multi-hop warp that's awaiting yes/no confirmation
# one hop at a time. Populated by cmd_move, consumed by cmd_confirm_warp.
PENDING_WARPS = {}

# pubkey -> dict tracking an in-progress port trade across its three
# steps (pick item -> pick quantity -> confirm). Populated by cmd_trade,
# consumed/advanced by cmd_trade_step.
PENDING_TRADES = {}

# pubkey -> dict tracking an in-progress Stardock refit visit (buying
# cargo holds, fighters, or shields). Populated by cmd_trade when
# docking at the Stardock, consumed/advanced by cmd_stardock_step.
PENDING_UPGRADES = {}

# Only one player may be "at the helm" (issuing commands) at a time.
# There's no clean way to tell "logged out" apart from "radio out of
# range" or "app crashed" -- both just look like silence -- so instead of
# trying to detect that, we hand out a single lock and time it out if its
# holder goes quiet for too long.
#
# None when nobody is active, otherwise:
#   {"pubkey":, "sender":, "last_activity": <time.time()>, "warned": bool}
ACTIVE_SESSION = None

# If the active player goes silent for this long, send one warning...
INACTIVITY_WARNING_SECONDS = 3 * 60
# ...and if they're still silent this much longer (total), free the lock.
INACTIVITY_KICK_SECONDS = 8 * 60
# How often the background task checks for inactivity.
INACTIVITY_CHECK_INTERVAL_SECONDS = 15


def _activate_session(pubkey, sender):
    """Hand the lock to pubkey, starting a fresh inactivity clock."""
    global ACTIVE_SESSION
    ACTIVE_SESSION = {
        "pubkey": pubkey,
        "sender": sender,
        "last_activity": time.time(),
        "warned": False,
    }


def _touch_session(pubkey):
    """Reset the inactivity clock -- called on every message the active
    player sends, so any reply (not just gameplay commands) counts as
    activity."""
    if ACTIVE_SESSION and ACTIVE_SESSION["pubkey"] == pubkey:
        ACTIVE_SESSION["last_activity"] = time.time()
        ACTIVE_SESSION["warned"] = False


def _release_session(pubkey):
    """Free the lock if pubkey currently holds it, and drop any
    trade/warp state they had in progress -- that state only makes sense
    mid-session, and leaving it around would let a *later* session for
    the same pubkey resume a stale trade unexpectedly."""
    global ACTIVE_SESSION
    PENDING_TRADES.pop(pubkey, None)
    PENDING_WARPS.pop(pubkey, None)
    PENDING_UPGRADES.pop(pubkey, None)
    if ACTIVE_SESSION and ACTIVE_SESSION["pubkey"] == pubkey:
        ACTIVE_SESSION = None


async def monitor_inactivity(mc):
    """
    Background loop pairing with the lock above: warn the active player
    once after INACTIVITY_WARNING_SECONDS of silence, then free the lock
    after INACTIVITY_KICK_SECONDS total. This is what recovers the game
    when a player's radio drops out of range, their app crashes, or they
    just wander off -- we can't distinguish those from a slow reply, so
    we just give it a generous timeout either way.
    """
    while True:
        await asyncio.sleep(INACTIVITY_CHECK_INTERVAL_SECONDS)

        session = ACTIVE_SESSION
        if session is None:
            continue

        idle_for = time.time() - session["last_activity"]
        pubkey = session["pubkey"]
        sender = session["sender"]

        if idle_for >= INACTIVITY_KICK_SECONDS:
            print(f"→ {sender} timed out after {idle_for:.0f}s idle, freeing lock")
            _release_session(pubkey)
            await send_reply(
                mc, pubkey, sender,
                "Logged out for inactivity so another player can sign in. "
                "Reply with anything to sign back in."
            )
        elif idle_for >= INACTIVITY_WARNING_SECONDS and not session["warned"]:
            session["warned"] = True
            minutes_left = max(1, round((INACTIVITY_KICK_SECONDS - idle_for) / 60))
            print(f"→ warning {sender}, idle {idle_for:.0f}s")
            await send_reply(
                mc, pubkey, sender,
                f"Still there? You'll be logged out in about {minutes_left} "
                "min if you don't reply."
            )


def _prepare_lines(text, limit):
    """Split text into lines, word-wrapping any line that's too long on its own."""
    raw_lines = text.split("\n")
    lines = []
    for line in raw_lines:
        if len(line) <= limit:
            lines.append(line)
        else:
            lines.extend(textwrap.wrap(line, width=limit) or [""])
    return lines


def _pack_lines(lines, limit):
    """Greedily join lines with '\\n', keeping each resulting chunk <= limit."""
    chunks = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def chunk_message(text, limit=MAX_MSG_LEN):
    """
    Split text into one or more chunks that each fit within `limit` chars,
    preserving newlines for readability. Any single line longer than the
    limit on its own gets word-wrapped as a fallback. If more than one
    chunk is needed, each is prefixed with "(i/n) " so the recipient can
    tell a reply was split — the limit used for wrapping/packing is
    re-derived each pass so the prefix never pushes a chunk over `limit`.
    """
    chunks = _pack_lines(_prepare_lines(text, limit), limit)
    if len(chunks) <= 1:
        return chunks

    # Reserve room for the "(i/n) " prefix, then redo wrapping/packing at
    # the reduced width. Do a second pass in case digit count of n changes
    # after the first pass (e.g. 9 -> 10 chunks).
    n = len(chunks)
    prefix_width = len(f"({n}/{n}) ")
    reduced_limit = max(10, limit - prefix_width)
    chunks = _pack_lines(_prepare_lines(text, reduced_limit), reduced_limit)
    n2 = len(chunks)
    if n2 != n:
        prefix_width = len(f"({n2}/{n2}) ")
        reduced_limit = max(10, limit - prefix_width)
        chunks = _pack_lines(_prepare_lines(text, reduced_limit), reduced_limit)
        n2 = len(chunks)

    return [f"({i + 1}/{n2}) {c}" for i, c in enumerate(chunks)]


def is_stale_message(payload, max_age=MAX_MESSAGE_AGE_SECONDS):
    """
    True if payload's sender_timestamp is older than max_age seconds.
    sender_timestamp is set by the sender's radio when the message was
    originally sent, not when our app received it -- so this catches
    messages that sat queued on the radio (e.g. while the app was
    disconnected) and all arrived in a burst once it reconnected.
    Messages without a sender_timestamp are never treated as stale.
    """
    sender_timestamp = payload.get("sender_timestamp")
    if sender_timestamp is None:
        return False
    return (time.time() - sender_timestamp) > max_age



    """Per-message context passed to every command handler."""
    def __init__(self, mc, pubkey, sender, player):
        self.mc = mc
        self.pubkey = pubkey
        self.sender = sender
        self.player = player


class Ctx:
    """Per-message context passed to every command handler."""
    def __init__(self, mc, pubkey, sender, player):
        self.mc = mc
        self.pubkey = pubkey
        self.sender = sender
        self.player = player


# Map of command -> (description, async handler(ctx, args) -> str)
COMMANDS = {}


def command(name, *aliases, description=""):
    def decorator(func):
        for n in (name, *aliases):
            COMMANDS[n] = (description, func)
        return func
    return decorator


def build_menu():
    lines = ["Available commands:"]
    seen = set()
    for cmd, (description, handler) in COMMANDS.items():
        if handler in seen:
            continue
        seen.add(handler)
        lines.append(f"  {cmd} - {description}")
    return "\n".join(lines)


def format_port_line(sector_id):
    port = get_port(sector_id)
    if port is None:
        return "Port: none"
    if port["port_class"] == "STARDOCK":
        return "Port: Stardock"
    return f"Port: {port['port_class']}"


def format_warps_line(sector_id):
    adjacent = get_adjacent_sectors(sector_id)
    warps = ", ".join(str(s) for s in adjacent) if adjacent else "none"
    return f"Warps: {warps}"


# (display label, db column prefix) for each tradeable commodity, in the
# fixed order used everywhere a port's class/trades are shown -- this is
# also the order the buy/sell command ("p"/"port") lists its menu in.
COMMODITIES = [
    ("Fuel Ore", "fuel_ore"),
    ("Organics", "organics"),
    ("Equipment", "equipment"),
]


# (display label, ships column, price per unit, ship's per-stat cap) for
# each Stardock refit option, in the fixed order shown in the refit
# menu. The cap is the same for every player's ship in v1 (there's only
# one ship type), so it's a plain constant here rather than something
# pulled per-ship.
STARDOCK_UPGRADES = [
    ("Cargo Holds", "holds_total", STARDOCK_PRICES["holds_total"], SHIP_MAX_HOLDS),
    ("Fighters", "fighters", STARDOCK_PRICES["fighters"], SHIP_MAX_FIGHTERS),
    ("Shields", "shields", STARDOCK_PRICES["shields"], SHIP_MAX_SHIELDS),
]


def build_stardock_menu(p):
    """The Stardock refit menu: current/max and price for each
    upgradeable stat, shown when a player first docks and again after
    every purchase (or skip) so they can keep buying in one visit."""
    lines = ["Stardock refits:"]
    for i, (label, col, price, limit) in enumerate(STARDOCK_UPGRADES, start=1):
        lines.append(f"  {i}) {label} {p[col]}/{limit} @ {price}cr each")
    lines.append(f"{p['credits']}cr available. Reply with a number to buy, or 'cancel'.")
    return "\n".join(lines)


def build_sector_info(sector_id):
    """
    The sector info screen: sector number, then port, then adjacent
    sectors, each on their own line. Shown for the `info` command and
    automatically appended whenever a player's sector actually changes.
    """
    return f"Sec{sector_id}\n{format_port_line(sector_id)}\n{format_warps_line(sector_id)}"


def _warp_confirm_options(sector_id):
    """
    '(p/yes/no)' if sector_id has a port to dock at, else just
    '(yes/no)' -- the 'p' option is only worth advertising on a "Warp
    to: ...?" prompt when there's actually something to dock at right
    where the player is currently standing.
    """
    return "(p/yes/no)" if get_port(sector_id) is not None else "(yes/no)"


def _resume_navigation_suffix(pubkey, sector_id):
    """
    If a multi-hop warp confirmation is still pending for pubkey,
    return text re-prompting to continue that route -- formatted the
    same as the original "Warp to: ...?" prompt, just starting from the
    player's current sector instead of the one they set out from.
    Returns "" if there's no route in progress.

    This is what lets a player dock at a port partway through a plotted
    multi-hop course (see cmd_confirm_warp's "p"/"port" handling)
    without losing the rest of the route: cmd_trade_step and
    cmd_stardock_step both call this once their visit ends, appending
    the result to their own closing message so the player is dropped
    straight back into the warp confirmation for the remaining hops.
    """
    remaining = PENDING_WARPS.get(pubkey)
    if not remaining:
        return ""
    route = " -> ".join(str(s) for s in [sector_id] + remaining)
    return f"\n\nWarp to: {route}? {_warp_confirm_options(sector_id)}"


@command("menu", "help", "?", description="list all commands")
async def cmd_menu(ctx, args):
    return build_menu()


@command("quit", "logout", description="sign off so another player can take a turn")
async def cmd_quit(ctx, args):
    _release_session(ctx.pubkey)
    return "You've signed off. Reply with anything to sign back in later."


@command("info", "i", description="show info for your current sector")
async def cmd_info(ctx, args):
    return build_sector_info(ctx.player["sector_id"])


@command("status", "st", description="show credits, sector, ship, turns")
async def cmd_status(ctx, args):
    p = ctx.player
    return (
        f"Sec{p['sector_id']} {p['credits']}cr {p['turns_remaining']}turn\n"
        f"{p['ship_type']}\n"
        f"Cargo Holds {p['holds_total']} Fighters {p['fighters']} Shields {p['shields']}\n"
        f"fuel{p['fuel_ore']} organics{p['organics']} equipment{p['equipment']}\n"
        f"{format_warps_line(p['sector_id'])}\n"
        f"{format_port_line(p['sector_id'])}"
    )


@command("p", "port", description="dock at port to trade or refit")
async def cmd_trade(ctx, args):
    """
    Start a guided port visit. At the Stardock (Sec1), this means a
    refit visit -- see build_stardock_menu/cmd_stardock_step -- since
    the Stardock deals in ship upgrades, not commodities. At any other
    port, builds a queue of trade offers in two passes, both walking
    COMMODITIES (fuel, organics, equipment) in order:

      1. SELL -- for every commodity the player is carrying that this
         port buys (direction "B"), offer to sell however many units
         they have, capped by the port's remaining storage room.
      2. BUY  -- for every commodity this port sells (direction "S"),
         offer to buy however many cargo holds are free *at that point*
         -- which grows as cargo is sold off in step 1 -- capped by port
         stock and what the player can afford.

    The queue is stored in PENDING_TRADES and walked one item at a time
    by cmd_trade_step: each offer asks for a quantity (0 skips to the
    next item), then yes/no/counter-offer same as before. 'cancel' ends
    the whole visit early. Recomputing the queue fresh on each dock means
    it always reflects current cargo and port stock, never stale state
    from a previous visit.
    """
    p = ctx.player
    port = get_port(p["sector_id"])
    if port is None:
        return "No port in current sector."
    if port["port_class"] == "STARDOCK":
        PENDING_UPGRADES[ctx.pubkey] = {"stage": "menu"}
        return build_stardock_menu(p)

    queue = []
    for label, key in COMMODITIES:
        if p[key] > 0 and port[f"{key}_dir"] == "B":
            queue.append({"action": "sell", "key": key, "label": label})
    for label, key in COMMODITIES:
        if port[f"{key}_dir"] == "S":
            queue.append({"action": "buy", "key": key, "label": label})

    if not queue:
        return "Nothing to trade with this port."

    PENDING_TRADES[ctx.pubkey] = {
        "sector_id": p["sector_id"],
        "port_id": port["id"],
        "queue": queue,
    }
    return _advance_trade_queue(ctx.pubkey, p, port)


def _build_sell_prompt(state, p, port, item):
    """Fill `state` in for a sell offer and return its prompt text, or
    None if there's nothing actually sellable right now (port has no
    room) so the caller should move on to the next queued item."""
    key = item["key"]
    label = item["label"]
    price = port[f"{key}_price"]
    have = p[key]
    room = port[f"{key}_max"] - port[f"{key}_qty"]
    max_qty = max(0, min(have, room))

    state.update({
        "stage": "quantity",
        "action": "sell",
        "commodity": key,
        "direction": "B",
        "price": price,
        "label": label,
        "max_qty": max_qty,
    })
    if max_qty <= 0:
        return None
    return (
        f"Port buys {label} @ {price}cr each.\n"
        f"You have {have}, port has room for {room} more.\n"
        f"Sell how many? (0-{max_qty}, or 'cancel')"
    )


def _build_buy_prompt(state, p, port, item):
    """Fill `state` in for a buy offer and return its prompt text, or
    None if there's nothing actually buyable right now (no free holds,
    no stock, or can't afford even 1) so the caller should move on."""
    key = item["key"]
    label = item["label"]
    price = port[f"{key}_price"]
    holds_used = p["fuel_ore"] + p["organics"] + p["equipment"]
    free_holds = p["holds_total"] - holds_used
    stock = port[f"{key}_qty"]
    max_affordable = p["credits"] // price if price > 0 else free_holds
    max_qty = max(0, min(free_holds, stock, max_affordable))

    state.update({
        "stage": "quantity",
        "action": "buy",
        "commodity": key,
        "direction": "S",
        "price": price,
        "label": label,
        "max_qty": max_qty,
    })
    if max_qty <= 0:
        return None
    return (
        f"Port sells {label} @ {price}cr each.\n"
        f"Free holds: {free_holds}, port stock: {stock}, you can afford {max_affordable}.\n"
        f"Buy how many? (0-{max_qty}, or 'cancel')"
    )


def _advance_trade_queue(pubkey, p, port):
    """
    Pop items off the pending trade's queue, in order, until one yields a
    usable prompt (max_qty > 0) -- `state` is updated in place for
    whichever item it settles on. Returns that prompt, or a closing
    message once the queue is exhausted with nothing left worth offering.
    """
    state = PENDING_TRADES[pubkey]
    while state["queue"]:
        item = state["queue"].pop(0)
        builder = _build_sell_prompt if item["action"] == "sell" else _build_buy_prompt
        prompt = builder(state, p, port, item)
        if prompt is not None:
            return prompt
    PENDING_TRADES.pop(pubkey, None)
    return "Nothing more to trade here." + _resume_navigation_suffix(pubkey, p["sector_id"])


async def cmd_trade_step(ctx, message):
    """
    Advance a pending port visit by one step. Stages, in order per queued
    item:
      "quantity" -- reply with a whole number of units. 0 skips this
                    item and moves on to the next queued offer (or closes
                    out the visit if none remain). A non-zero amount,
                    capped at state["max_qty"], moves to "confirm" with
                    the listed total price.
      "confirm"  -- "yes" accepts the listed price; a whole-number reply
                    is a negotiated cr/unit offer -- a buying port
                    accepts up to 104% of its listed price, a selling
                    port accepts down to 96%, otherwise it holds firm and
                    the player can try again. Accepting re-validates
                    against current state (in case the port or player's
                    cargo changed since the quote), executes the trade,
                    then advances to the next queued item. "no" skips
                    this item (same as a quantity of 0) rather than
                    ending the whole visit.
    'cancel' is accepted at any stage and ends the whole visit early.
    """
    p = ctx.player
    pubkey = ctx.pubkey
    text = message.strip()
    lower = text.lower()

    state = PENDING_TRADES.get(pubkey)
    if not state:
        PENDING_TRADES.pop(pubkey, None)
        return "No trade in progress."

    if lower == "cancel":
        PENDING_TRADES.pop(pubkey, None)
        return "Trade cancelled." + _resume_navigation_suffix(pubkey, p["sector_id"])

    port = get_port(state["sector_id"])
    if port is None or port["id"] != state["port_id"]:
        PENDING_TRADES.pop(pubkey, None)
        return "Port no longer available. Trade cancelled." + _resume_navigation_suffix(pubkey, p["sector_id"])

    if state["stage"] == "quantity":
        if not re.match(r"^\d+$", text):
            return "Enter a whole number of units (0 to skip), or 'cancel'."
        qty = int(text)
        if qty > state["max_qty"]:
            return f"Max is {state['max_qty']}. Enter a smaller quantity, or 'cancel'."

        if qty == 0:
            return _advance_trade_queue(pubkey, p, port)

        state["qty"] = qty
        state["total_price"] = qty * state["price"]
        state["stage"] = "confirm"
        verb = "Sell" if state["action"] == "sell" else "Buy"
        return (
            f"{verb} {qty} {state['label']} for {state['total_price']}cr "
            f"({state['price']}cr/unit)? yes/no, or offer a cr/unit price"
        )

    if state["stage"] == "confirm":
        if lower in ("n", "no"):
            return _advance_trade_queue(pubkey, p, port)

        key = state["commodity"]
        direction = state["direction"]
        qty = state["qty"]
        label = state["label"]
        listed_price = state["price"]
        player_is_buying = state["action"] == "buy"

        if lower in ("y", "yes"):
            price_per_unit = listed_price
        elif re.match(r"^\d+$", text):
            offer = int(text)
            if offer <= 0:
                return "Price must be a positive whole number, or 'yes'/'no'."

            # Same simple rule everywhere: a buying port (direction 'B')
            # accepts an asking price up to 4% above what it offered; a
            # selling port (direction 'S') accepts a bid down to 96% of
            # what it asked. Outside that band, the port holds firm and
            # the player can try again, accept the listed price, or skip.
            if direction == "B":
                accepted = offer <= listed_price * 1.04
            else:
                accepted = offer >= listed_price * 0.96

            if not accepted:
                return (
                    f"Port won't budge to {offer}cr/unit for {label}. "
                    f"Try another price, 'yes' for {listed_price}cr/unit, or 'no' to skip."
                )
            price_per_unit = offer
        else:
            return "Reply 'yes' to confirm, 'no' to skip, or enter a cr/unit price to negotiate."

        total_price = qty * price_per_unit

        # Re-validate against current state right before committing --
        # the port or the player's cargo/credits may have changed since
        # the quote was given a step ago.
        if player_is_buying:
            holds_used = p["fuel_ore"] + p["organics"] + p["equipment"]
            free_holds = p["holds_total"] - holds_used
            stock = port[f"{key}_qty"]
            ok = qty <= free_holds and qty <= stock and total_price <= p["credits"]
        else:
            have = p[key]
            room = port[f"{key}_max"] - port[f"{key}_qty"]
            ok = qty <= have and qty <= room

        if not ok:
            nxt = _advance_trade_queue(pubkey, p, port)
            return f"Conditions changed since this quote -- skipping.\n\n{nxt}"

        execute_trade(p["id"], port["id"], key, qty, total_price, player_is_buying)
        verb = "Bought" if player_is_buying else "Sold"
        sign = "-" if player_is_buying else "+"
        result_line = f"{verb} {qty} {label} for {sign}{total_price}cr ({price_per_unit}cr/unit)."

        # Re-fetch so the next queued item (if any) reflects this trade's
        # effect on cargo space and credits -- ctx.player is otherwise
        # stale for the rest of this call.
        p = get_player_with_ship(pubkey)
        nxt = _advance_trade_queue(pubkey, p, port)
        return f"{result_line}\n\n{nxt}"

    # Shouldn't be reachable, but don't leave a broken trade stuck in state.
    PENDING_TRADES.pop(pubkey, None)
    return "Something went wrong with this trade. Cancelled." + _resume_navigation_suffix(pubkey, p["sector_id"])


async def cmd_stardock_step(ctx, message):
    """
    Advance a pending Stardock refit visit. Unlike the port trade queue
    (which walks every tradeable commodity exactly once), the Stardock
    menu is open-ended: after each completed or skipped purchase the
    player is shown the menu again and can keep buying -- limited only
    by their ship's per-stat caps (SHIP_MAX_HOLDS/FIGHTERS/SHIELDS) and
    their credits -- until they reply 'cancel'.

    Stages, in order:
      "menu"     -- reply with the menu number of an upgrade to buy, or
                    'cancel' to leave. Rejects a stat that's already at
                    its cap, or one the player can't afford even 1 unit
                    of, before moving to "quantity".
      "quantity" -- reply with a whole number of units. 0 returns to the
                    menu without buying. A non-zero amount, capped at
                    state["max_qty"], moves to "confirm" with the listed
                    total price.
      "confirm"  -- "yes" commits the purchase and returns to the menu.
                    Anything else (e.g. "no") returns to the menu without
                    buying.
    'cancel' is accepted at any stage and ends the visit.
    """
    pubkey = ctx.pubkey
    text = message.strip()
    lower = text.lower()

    state = PENDING_UPGRADES.get(pubkey)
    if not state:
        PENDING_UPGRADES.pop(pubkey, None)
        return "No Stardock visit in progress."

    if lower == "cancel":
        PENDING_UPGRADES.pop(pubkey, None)
        return "Left the Stardock." + _resume_navigation_suffix(pubkey, ctx.player["sector_id"])

    # Always re-fetch -- a purchase made a step ago in this same visit
    # changes both credits and the stat just bought.
    p = get_player_with_ship(pubkey)

    if state["stage"] == "menu":
        if not re.match(r"^\d+$", text):
            return "Reply with a number to buy, or 'cancel'.\n\n" + build_stardock_menu(p)
        choice = int(text)
        if choice < 1 or choice > len(STARDOCK_UPGRADES):
            return "Not a valid option.\n\n" + build_stardock_menu(p)

        label, col, price, limit = STARDOCK_UPGRADES[choice - 1]
        current = p[col]
        room = limit - current
        if room <= 0:
            return f"Already at max {label} ({limit}).\n\n" + build_stardock_menu(p)

        max_qty = max(0, min(room, p["credits"] // price))
        if max_qty <= 0:
            return f"Can't afford even 1 {label} ({price}cr each).\n\n" + build_stardock_menu(p)

        state.update({
            "stage": "quantity",
            "label": label,
            "column": col,
            "price": price,
            "limit": limit,
            "max_qty": max_qty,
        })
        return (
            f"{label}: {current}/{limit}, {price}cr each.\n"
            f"Buy how many? (0-{max_qty}, or 'cancel')"
        )

    if state["stage"] == "quantity":
        if not re.match(r"^\d+$", text):
            return "Enter a whole number of units (0 to go back), or 'cancel'."
        qty = int(text)
        if qty > state["max_qty"]:
            return f"Max is {state['max_qty']}. Enter a smaller quantity, or 'cancel'."
        if qty == 0:
            state["stage"] = "menu"
            return build_stardock_menu(p)

        state["qty"] = qty
        state["total_price"] = qty * state["price"]
        state["stage"] = "confirm"
        return (
            f"Buy {qty} {state['label']} for {state['total_price']}cr "
            f"({state['price']}cr/unit)? yes/no"
        )

    if state["stage"] == "confirm":
        if lower not in ("y", "yes"):
            state["stage"] = "menu"
            return build_stardock_menu(p)

        col = state["column"]
        qty = state["qty"]
        total_price = state["total_price"]
        label = state["label"]
        limit = state["limit"]

        # Re-validate against current state right before committing --
        # credits or the stat's current value may have changed since the
        # quote a step ago (only this player can act during their own
        # visit, but this keeps the same safety margin as cmd_trade_step).
        if total_price > p["credits"] or p[col] + qty > limit:
            state["stage"] = "menu"
            return "Conditions changed -- purchase skipped.\n\n" + build_stardock_menu(p)

        upgrade_ship_stat(p["id"], col, qty, total_price)
        p = get_player_with_ship(pubkey)
        state["stage"] = "menu"
        return (
            f"Installed {qty} {label} for -{total_price}cr.\n\n"
            f"{build_stardock_menu(p)}"
        )

    # Shouldn't be reachable, but don't leave a broken visit stuck in state.
    PENDING_UPGRADES.pop(pubkey, None)
    return "Something went wrong with this visit. Cancelled." + _resume_navigation_suffix(pubkey, ctx.player["sector_id"])


def find_shortest_path(graph, start, goal):
    """
    BFS shortest path through the warp graph. Returns a list of sector ids
    from start to goal inclusive (e.g. [12, 47, 803]), or None if goal is
    unreachable from start. BFS guarantees the fewest warps, not physical
    distance, which matches how warps work in this game.
    """
    if start == goal:
        return [start]

    visited = {start}
    queue = deque([[start]])

    while queue:
        path = queue.popleft()
        node = path[-1]
        for neighbor in graph.get(node, []):
            if neighbor in visited:
                continue
            if neighbor == goal:
                return path + [neighbor]
            visited.add(neighbor)
            queue.append(path + [neighbor])

    return None


async def cmd_move(ctx, args):
    """
    Handle a number-like message as a move request.
      - Non-integers (e.g. "4.5") are rejected.
      - Sectors outside [MIN_SECTOR_ID, MAX_SECTOR_ID] are rejected.
      - Adjacent sectors are moved to directly (single warp, no confirmation
        needed).
      - Non-adjacent (but valid) sectors are routed via the shortest path
        through the warp network (BFS). The player is NOT moved yet --
        instead the route is plotted and the player is asked to confirm
        the first hop. See cmd_confirm_warp for the rest of the flow.
    """
    p = ctx.player

    try:
        target = int(args)
    except ValueError:
        return f"'{args}' isn't a whole number. Enter a sector number, e.g. 42."

    if target < MIN_SECTOR_ID or target > MAX_SECTOR_ID:
        return f"Sec{target} is out of range. Sectors range from {MIN_SECTOR_ID} to {MAX_SECTOR_ID}."

    if target == p["sector_id"]:
        return f"You're already in Sec{target}."

    adjacent = get_adjacent_sectors(p["sector_id"])
    if target in adjacent:
        move_player_to_sector(p["id"], target)
        return f"Moved to Sec{target}.\n{build_sector_info(target)}"

    graph = get_all_warps()
    path = find_shortest_path(graph, p["sector_id"], target)
    if path is None:
        return f"No route found to Sec{target}."

    remaining = path[1:]  # hops after the player's current sector
    PENDING_WARPS[ctx.pubkey] = remaining
    hops = len(remaining)
    route = " -> ".join(str(s) for s in path)
    return f"Plotted a {hops}-warp course to Sec{target}.\nWarp to: {route}? {_warp_confirm_options(p['sector_id'])}"


async def cmd_confirm_warp(ctx, message):
    """
    Handle a reply while a multi-hop warp is awaiting confirmation.
    "yes" advances one hop and asks again if more remain, or reports
    arrival if that was the last one. "no"/"cancel" cancels the rest of
    the plotted course and leaves the player where they are.

    The port command ('p'/'port') is also accepted here: it lets the
    player dock at the sector they've just warped into -- regular
    trading, or a Stardock refit if it's Sec1 -- without losing the
    rest of the route. PENDING_WARPS is left untouched while that visit
    runs (cmd_trade starts its own PENDING_TRADES/PENDING_UPGRADES,
    which on_message checks ahead of PENDING_WARPS, so follow-up
    messages go to the visit, not back here). Once the visit ends,
    cmd_trade_step/cmd_stardock_step append this same yes/no prompt via
    _resume_navigation_suffix so the player is dropped straight back
    into the route. If docking didn't actually start a visit (no port,
    or nothing to trade), the prompt is re-shown immediately instead.
    """
    p = ctx.player
    pubkey = ctx.pubkey
    text = message.strip().lower()

    remaining = PENDING_WARPS.get(pubkey)
    if not remaining:
        PENDING_WARPS.pop(pubkey, None)
        return "No warp in progress."

    verb, args = parse(text)
    if verb in COMMANDS and COMMANDS[verb][1] is cmd_trade:
        response = await cmd_trade(ctx, args)
        if pubkey not in PENDING_TRADES and pubkey not in PENDING_UPGRADES:
            # Nothing to dock for -- no port here, or nothing tradeable
            # -- so no visit actually started to resume the prompt
            # later. Re-show it now instead of leaving the player stuck.
            response += _resume_navigation_suffix(pubkey, p["sector_id"])
        return response

    if text in ("y", "yes"):
        next_sector = remaining.pop(0)
        move_player_to_sector(p["id"], next_sector)
        if remaining:
            route = " -> ".join(str(s) for s in [next_sector] + remaining)
            return (
                f"Warped to Sec{next_sector}.\n{build_sector_info(next_sector)}\n"
                f"Warp to: {route}? {_warp_confirm_options(next_sector)}"
            )
        PENDING_WARPS.pop(pubkey, None)
        return f"Arrived at Sec{next_sector}.\n{build_sector_info(next_sector)}"

    if text in ("n", "no", "cancel"):
        PENDING_WARPS.pop(pubkey, None)
        return f"Navigation cancelled. You remain in Sec{p['sector_id']}."

    if get_port(p["sector_id"]) is not None:
        return "Reply 'yes' to continue warping, 'no' to cancel, or 'p' to dock here."
    return "Reply 'yes' to continue warping or 'no' to cancel."


def parse(text):
    parts = text.strip().split(maxsplit=1)
    verb = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    return verb, args


async def send_reply(mc, pubkey, sender, text):
    """
    Send each chunk and wait for the recipient's radio to actually
    acknowledge it (send_msg_with_retry blocks until ACK or gives up after
    its own retries) before sending the next chunk. This replaces a fixed
    time delay with a real delivery confirmation, which is only possible
    for direct messages -- channel broadcasts have no per-recipient ACK.
    """
    for chunk in chunk_message(text):
        result = await mc.commands.send_msg_with_retry(pubkey, chunk)
        if result is None:
            print(f"  Error sending reply (no ack received): {chunk}")
            return
        print(f"  Reply sent + acked: {chunk}")
        log_message("tx", pubkey, sender, chunk)


async def send_channel_reply(mc, channel_idx, text):
    """Broadcast a reply to everyone on the given channel (not a private DM)."""
    chunks = chunk_message(text)
    for i, chunk in enumerate(chunks):
        result = await mc.commands.send_chan_msg(channel_idx, chunk)
        if result.type == EventType.ERROR:
            print(f"  Error sending channel reply: {result.payload}")
            return
        print(f"  Channel reply sent OK: {chunk}")
        log_message("tx", f"chan{channel_idx}", "channel", chunk)
        if len(chunks) > 1 and i < len(chunks) - 1:
            await asyncio.sleep(0.1)


# Map of command -> async handler(args) -> str, for messages on the public channel.
# Unlike COMMANDS (private DMs), there's no player/Ctx here and unrecognized
# verbs are silently ignored rather than replying with a menu — channel
# chatter that isn't a bot command shouldn't get an automatic reply.
CHANNEL_COMMANDS = {}


def channel_command(name, *aliases):
    def decorator(func):
        for n in (name, *aliases):
            CHANNEL_COMMANDS[n] = func
        return func
    return decorator


async def on_channel_message(mc, event):
    payload = getattr(event, "payload", {})
    channel_idx = payload.get("channel_idx", -1)
    text = payload.get("text", "")

    if is_stale_message(payload):
        age = time.time() - payload["sender_timestamp"]
        print(f"CHAN[{channel_idx}] ignoring stale message (age {age:.0f}s): {text}")
        return

    # Some apps prefix the sender's nickname, e.g. "alice: weather 43215".
    # Channel messages carry no pubkey, so this is best-effort only.
    if ":" in text:
        _, _, after_colon = text.partition(":")
        content = after_colon.strip()
    else:
        content = text.strip()

    print(f"CHAN[{channel_idx}] RX: {text}")
    log_message("rx", f"chan{channel_idx}", "channel", text)

    verb, args = parse(content)
    handler = CHANNEL_COMMANDS.get(verb)
    if handler is None:
        return  # not a recognized public-channel command; stay quiet

    response = await handler(args)
    await send_channel_reply(mc, channel_idx, response)


async def on_message(mc, event):
    payload = getattr(event, "payload", {})
    pubkey = payload.get("pubkey_prefix", "UNKNOWN")
    message = payload.get("text", "")

    if is_stale_message(payload):
        age = time.time() - payload["sender_timestamp"]
        print(f"RX from {pubkey} ignoring stale message (age {age:.0f}s): {message}")
        return

    contact = mc.get_contact_by_key_prefix(pubkey)
    sender = contact["adv_name"] if contact else pubkey[:8]

    print(f"RX from {sender}: {message}")
    log_message("rx", pubkey, sender, message)

    player, is_new = get_or_create_player(pubkey, sender)

    if is_new:
        print(f"→ new player onboarded: {sender}")
        welcome = (
            f"Welcome {sender}! Sec{player['sector_id']} "
            f"{player['credits']}cr {player['turns_remaining']}trn. "
            f"Reply 'menu' for commands."
        )
        await send_reply(mc, pubkey, sender, welcome)
        return

    reset_turns_if_needed(player["id"])
    player = get_player_with_ship(pubkey)  # re-fetch in case turns were just reset

    # Lockout: someone else is at the helm -- turn this sender away
    # without touching any game state. New players still get onboarded
    # above regardless of the lock; it's gameplay commands that wait.
    if ACTIVE_SESSION is not None and ACTIVE_SESSION["pubkey"] != pubkey:
        other = ACTIVE_SESSION["sender"]
        print(f"→ {sender} turned away, {other} is active")
        await send_reply(
            mc, pubkey, sender,
            f"{other} is currently at the helm. Try again in a few minutes."
        )
        return

    if ACTIVE_SESSION is None:
        if player["turns_remaining"] <= 0:
            print(f"→ {sender} has no turns left, not activating")
            await send_reply(
                mc, pubkey, sender,
                "You're out of turns for now. Check back after they reset."
            )
            return
        _activate_session(pubkey, sender)
        print(f"→ {sender} is now active")
    else:
        _touch_session(pubkey)

    ctx = Ctx(mc, pubkey, sender, player)

    if pubkey in PENDING_TRADES:
        response = await cmd_trade_step(ctx, message)
    elif pubkey in PENDING_UPGRADES:
        response = await cmd_stardock_step(ctx, message)
    elif pubkey in PENDING_WARPS:
        response = await cmd_confirm_warp(ctx, message)
    else:
        stripped = message.strip()
        if _NUMBER_LIKE.match(stripped):
            response = await cmd_move(ctx, stripped)
        else:
            verb, args = parse(message)
            if verb in COMMANDS:
                _, handler = COMMANDS[verb]
                response = await handler(ctx, args)
            else:
                print(f"→ unrecognized command from {sender}")
                response = "Unknown command. Reply 'menu' for list."

    # Re-fetch so a move/trade that just spent the player's last turn is
    # reflected here, then free the lock if they're out so the next
    # player isn't stuck waiting on the inactivity timeout.
    player = get_player_with_ship(pubkey)
    if (
        player["turns_remaining"] <= 0
        and ACTIVE_SESSION is not None
        and ACTIVE_SESSION["pubkey"] == pubkey
    ):
        _release_session(pubkey)
        response += "\n\nYou're out of turns. Logged out to let someone else play."

    print(f"→ replying to {sender}: {response}")
    await send_reply(mc, pubkey, sender, response)


async def main():
    init_db()

    mc = await MeshCore.create_serial("/dev/ttyACM0", 115200)
    print("Connected OK")

    result = await mc.commands.get_contacts()
    if result.type == EventType.ERROR:
        print(f"Error getting contacts: {result.payload}")

    await mc.start_auto_message_fetching()

    mc.subscribe(
        EventType.CONTACT_MSG_RECV,
        lambda event: asyncio.create_task(on_message(mc, event))
    )

    mc.subscribe(
        EventType.CHANNEL_MSG_RECV,
        lambda event: asyncio.create_task(on_channel_message(mc, event)),
        attribute_filters={"channel_idx": PUBLIC_CHANNEL_IDX}
    )

    asyncio.create_task(monitor_inactivity(mc))

    print("Bot is running...")
    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())