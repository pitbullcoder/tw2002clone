"""
Trade Wars-style space trading game over a MeshCore/Meshtastic radio mesh.

This module is the orchestrator: it wires the radio event loop to the
command registry, owns the player-arrival path (enter_sector and the
movement commands, which is also where mine detonation is resolved), and
re-exports the names the test suite reaches for. The bulk of the game now
lives in focused modules:

    core         shared state, command registry, Ctx, parse
    messaging    chunking + send/ack transport
    display      sector/menu rendering
    pathfinding  warp-graph traversal + escape-pod placement
    combat       mine-damage math
    trading      port trade + Stardock refit/shipyard flows
    session      single-helm lock + inactivity timeout

enter_sector stays here on purpose: it defaults its RNG to this module's
`random`, which is the seam the tests monkeypatch.
"""

import asyncio
import random
import re
import time

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
    buy_ship,
    lay_mines,
    get_hostile_mine_total,
    clear_hostile_mines,
    set_ship_defenses,
    consume_probe,
    detonate_one_hostile_mine,
    SHIP_CATALOG,
    ESCAPE_POD_SHIP,
)

import session
from session import (
    _activate_session,
    _touch_session,
    _release_session,
    monitor_inactivity,
)
from core import (
    Ctx,
    COMMANDS,
    command,
    CHANNEL_COMMANDS,
    parse,
    PENDING_WARPS,
    PENDING_TRADES,
    PENDING_UPGRADES,
    _warp_confirm_options,
    _resume_navigation_suffix,
)
from messaging import send_reply, send_channel_reply, is_stale_message
from display import build_menu, build_submenu, build_sector_info, format_port_line, format_warps_line
from pathfinding import find_shortest_path, choose_escape_sector
# Re-exported so the test suite can reach it as main.sectors_within_hop_range.
from pathfinding import sectors_within_hop_range  # noqa: F401
from combat import roll_mine_damage, apply_mine_damage, _plural
from trading import cmd_trade, cmd_trade_step, cmd_stardock_step

print("MeshCore bot started...")

# Reset shared mutable state on every (re)load. The dicts physically live
# in `core` and are never replaced, so submodules holding references to
# them stay valid; clearing (not rebinding) is what lets the test suite's
# importlib.reload(main) hand each test a clean slate.
PENDING_WARPS.clear()
PENDING_TRADES.clear()
PENDING_UPGRADES.clear()
session.ACTIVE_SESSION = None

# Public surface this module deliberately exposes -- notably the handlers
# and helpers the test suite reaches for as main.<name>, including a couple
# (apply_mine_damage, choose_escape_sector, sectors_within_hop_range) that
# are defined in sibling modules and re-exported here on purpose.
__all__ = [
    "Ctx",
    "cmd_menu", "cmd_quit", "cmd_info", "cmd_status",
    "cmd_combat", "cmd_lay_mines", "cmd_probe",
    "cmd_move", "cmd_confirm_warp",
    "cmd_trade", "cmd_trade_step", "cmd_stardock_step",
    "enter_sector", "run_probe",
    "apply_mine_damage", "choose_escape_sector",
    "sectors_within_hop_range",
    "PENDING_WARPS", "PENDING_TRADES", "PENDING_UPGRADES",
    "on_message", "on_channel_message", "main",
]


PUBLIC_CHANNEL_IDX = 0  # which channel index the bot listens to for public commands


MIN_SECTOR_ID = 1


MAX_SECTOR_ID = 1000  # matches galaxy.py's NUM_SECTORS


# --- Mine warfare balance ---------------------------------------------
# Mines can't be laid in the first MINE_FREE_SECTORS sectors -- the home
# sector and its immediate neighborhood are a protected safe zone so new
# players can't be ambushed the moment they leave the Stardock.
MINE_FREE_SECTORS = 10


# Matches anything that *looks* like a number a player might type as a
# move request -- including negatives and decimals -- so we can route it
# to cmd_move for a specific validation error, rather than letting it fall
# through to the generic "Unknown command" reply.
_NUMBER_LIKE = re.compile(r"^-?\d+(\.\d+)?$")


@command("menu", "help", "?", description="list commands ('help combat' for combat)")
async def cmd_menu(ctx, args):
    sub = args.strip().lower()
    if sub:
        return build_submenu(sub)
    return build_menu()


@command("combat", description="combat & recon commands (lay mines, send probes)")
async def cmd_combat(ctx, args):
    return build_submenu("combat")


@command("quit", "logout", description="sign off so another player can take a turn")
async def cmd_quit(ctx, args):
    _release_session(ctx.pubkey)
    return "You've signed off. Reply with anything to sign back in later."


@command("info", "i", description="show info for your current sector")
async def cmd_info(ctx, args):
    return build_sector_info(ctx.player["sector_id"], ctx.player["id"])


@command("status", "st", description="show credits, sector, ship, turns")
async def cmd_status(ctx, args):
    p = ctx.player
    defenses = f"Cargo Holds {p['holds_total']} Fighters {p['fighters']} Shields {p['shields']}"
    if p["mines"] > 0:
        defenses += f" Mines {p['mines']}"
    if p["probes"] > 0:
        defenses += f" Probes {p['probes']}"
    return (
        f"Sec{p['sector_id']} {p['credits']}cr {p['turns_remaining']}turn\n"
        f"{p['ship_type']}\n"
        f"{defenses}\n"
        f"fuel{p['fuel_ore']} organics{p['organics']} equipment{p['equipment']}\n"
        f"{format_warps_line(p['sector_id'])}\n"
        f"{format_port_line(p['sector_id'])}"
    )


@command("lay", "mine", description="lay mines in this sector: 'lay <n>'", menu="combat")
async def cmd_lay_mines(ctx, args):
    """
    Deploy mines from the ship into the current sector, where they wait
    for the next pilot who isn't their owner (the owner can re-enter
    safely -- see enter_sector). Banned in the Sec1..MINE_FREE_SECTORS
    safe zone. Only ships with a mine bay ever carry mines to begin with,
    so a Falcon (mines always 0) is turned away by the "none aboard"
    check without needing a separate hull test.
    """
    p = ctx.player
    sector_id = p["sector_id"]

    if sector_id <= MINE_FREE_SECTORS:
        return (
            f"Can't lay mines in Sec{sector_id} -- the Sec1-{MINE_FREE_SECTORS} "
            "safe zone is off limits."
        )

    aboard = p["mines"]
    if aboard <= 0:
        return "No mines aboard. Buy some at a Stardock (needs a ship with a mine bay)."

    arg = args.strip()
    if not arg:
        return f"Lay how many mines? You have {aboard} aboard. Try 'lay <n>'."
    if not re.match(r"^\d+$", arg):
        return f"Enter a whole number of mines to lay. You have {aboard} aboard."

    qty = int(arg)
    if qty == 0:
        return "Lay how many? Enter a number from 1 up."
    if qty > aboard:
        return f"You only have {aboard} mines aboard."

    lay_mines(p["id"], sector_id, qty)
    left = aboard - qty
    return (
        f"Laid {_plural(qty, 'mine')} in Sec{sector_id}; {left} still aboard. "
        "They'll detonate on the next pilot through who isn't you."
    )


@command("probe", description="send a recon probe to a sector: 'probe <n>'", menu="combat")
async def cmd_probe(ctx, args):
    """
    Launch a recon probe toward a target sector. The probe follows the
    same shortest-path route a piloted warp would (see cmd_move), but the
    player stays put -- it's remote scouting. It reports each sector it
    passes through, just as the player would see on arrival, and is
    consumed on launch whether or not it makes it.

    A probe is fragile: a single hostile mine in any sector it enters
    destroys it on the spot (that one mine is spent; the rest of the
    field stays put for real ships). Probes are bought at a Stardock.
    """
    p = ctx.player

    if p["probes"] <= 0:
        return "No probes aboard. Buy some at a Stardock (100cr each)."

    arg = args.strip()
    if not arg:
        return f"Send a probe where? You have {p['probes']}. Try 'probe <sector>'."
    if not re.match(r"^\d+$", arg):
        return f"'{arg}' isn't a sector number. Try 'probe <sector>'."

    target = int(arg)
    if target < MIN_SECTOR_ID or target > MAX_SECTOR_ID:
        return f"Sec{target} is out of range. Sectors range from {MIN_SECTOR_ID} to {MAX_SECTOR_ID}."
    if target == p["sector_id"]:
        return "The probe's already in your sector -- send it somewhere else."

    graph = get_all_warps()
    path = find_shortest_path(graph, p["sector_id"], target)
    if path is None:
        return f"No route found to Sec{target}."

    consume_probe(p["id"])
    return run_probe(p, path)


def run_probe(p, path):
    """
    Fly a launched probe along `path` (which starts at the player's
    current sector) and build its travelogue. Each sector it reaches is
    reported with the same info screen the player would see there. The
    first sector holding a hostile mine destroys the probe -- one mine is
    spent (detonate_one_hostile_mine), the report ends there, and the
    rest of the route goes unscouted. Returns the full report string.
    """
    hops = path[1:]  # the player's current sector isn't re-reported
    left = p["probes"] - 1  # one was just consumed launching this probe
    lines = [f"Probe away to Sec{path[-1]} ({len(hops)} hops); {left} left aboard."]
    for sector_id in hops:
        if get_hostile_mine_total(sector_id, p["id"]) > 0:
            detonate_one_hostile_mine(sector_id, p["id"])
            lines.append(f"Sec{sector_id}: a mine detonates -- PROBE DESTROYED here.")
            break
        lines.append(build_sector_info(sector_id, p["id"]))
    else:
        lines.append(f"Probe reached Sec{path[-1]} and signs off.")
    return "\n".join(lines)


def enter_sector(ctx, sector_id, lead, rng=None):
    """
    Move the player into `sector_id` and resolve any mines waiting there,
    returning (message, destroyed).

    `lead` is the arrival verb shown before the sector number ("Moved
    to" / "Warped to" / "Arrived at"), so this one function backs every
    way a player can land somewhere.

    If the sector holds mines owned by anyone else, they all detonate at
    once (the player's own mines there, if any, don't). Damage cascades
    shields -> fighters -> hull. A survivor keeps flying with reduced
    defenses; a casualty is ejected into an Escape Pod (cargo and current
    hull lost, credits kept) and drifts ESCAPE_POD_MIN_HOPS..MAX_HOPS
    away. When `destroyed` is True the player is already in that faraway
    sector and any plotted route they were following should be dropped --
    the pod went somewhere the route never planned for.

    The pod's own landing is deliberately NOT re-checked for mines: a
    wreck shouldn't chain-detonate its way across the map.
    """
    r = rng if rng is not None else random
    pubkey = ctx.pubkey

    move_player_to_sector(ctx.player["id"], sector_id)
    p = get_player_with_ship(pubkey)  # fresh defenses to test the hit against

    hostile = get_hostile_mine_total(sector_id, p["id"])
    if hostile <= 0:
        return f"{lead} Sec{sector_id}.\n{build_sector_info(sector_id, p['id'])}", False

    # The mines go off and are spent, kill or not.
    clear_hostile_mines(sector_id, p["id"])
    total_damage = roll_mine_damage(hostile, r)
    shields_after, fighters_after, shields_lost, fighters_lost, destroyed = apply_mine_damage(
        p["shields"], p["fighters"], total_damage
    )

    if not destroyed:
        set_ship_defenses(p["id"], shields_after, fighters_after)
        report = (
            f"{lead} Sec{sector_id} -- {_plural(hostile, 'mine')} detonate for "
            f"{total_damage} damage! Lost {shields_lost} shields, {fighters_lost} fighters; "
            f"now {shields_after} shields, {fighters_after} fighters.\n"
            f"{build_sector_info(sector_id, p['id'])}"
        )
        return report, False

    # Destroyed: eject into a pod and drift somewhere far off. Re-equip
    # via buy_ship (it swaps the hull, zeroes its stats, and clears cargo)
    # at no credit cost, then relocate. p["ship_type"] is still the old
    # hull's name here, which is what the message wants to report lost.
    graph = get_all_warps()
    escape_sector = choose_escape_sector(graph, sector_id, r)
    pod = SHIP_CATALOG[ESCAPE_POD_SHIP]
    buy_ship(
        p["id"], ESCAPE_POD_SHIP,
        pod["base_holds"], pod["base_fighters"], pod["base_shields"], pod["base_mines"],
        credit_delta=0,
    )
    if escape_sector is not None:
        move_player_to_sector(p["id"], escape_sector)
    landed = escape_sector if escape_sector is not None else sector_id
    message = (
        f"{_plural(hostile, 'mine')} detonate for {total_damage} damage -- your "
        f"{p['ship_type']} is DESTROYED! You eject in an Escape Pod and drift to "
        f"Sec{landed} (cargo lost, credits intact).\n{build_sector_info(landed, p['id'])}"
    )
    return message, True


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
        message, _destroyed = enter_sector(ctx, target, "Moved to")
        return message

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
        last_hop = not remaining
        message, destroyed = enter_sector(
            ctx, next_sector, "Arrived at" if last_hop else "Warped to"
        )
        if destroyed:
            # Blown out of the plotted course into a pod somewhere else --
            # the rest of the route no longer connects to where we are.
            PENDING_WARPS.pop(pubkey, None)
            return message
        if remaining:
            route = " -> ".join(str(s) for s in [next_sector] + remaining)
            return f"{message}\nWarp to: {route}? {_warp_confirm_options(next_sector)}"
        PENDING_WARPS.pop(pubkey, None)
        return message

    if text in ("n", "no", "cancel"):
        PENDING_WARPS.pop(pubkey, None)
        return f"Navigation cancelled. You remain in Sec{p['sector_id']}."

    if get_port(p["sector_id"]) is not None:
        return "Reply 'yes' to continue warping, 'no' to cancel, or 'p' to dock here."
    return "Reply 'yes' to continue warping or 'no' to cancel."


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
    if session.ACTIVE_SESSION is not None and session.ACTIVE_SESSION["pubkey"] != pubkey:
        other = session.ACTIVE_SESSION["sender"]
        print(f"→ {sender} turned away, {other} is active")
        await send_reply(
            mc, pubkey, sender,
            f"{other} is currently at the helm. Try again in a few minutes."
        )
        return

    if session.ACTIVE_SESSION is None:
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
        and session.ACTIVE_SESSION is not None
        and session.ACTIVE_SESSION["pubkey"] == pubkey
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
