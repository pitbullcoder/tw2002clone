"""
Read-only rendering of game state into the short text screens players
see: sector info, the per-sector port/warps lines, and the command menu.
"""

from db import get_port, get_adjacent_sectors

from core import COMMANDS


def build_menu():
    lines = ["Available commands:"]
    seen = set()
    for cmd, (description, handler) in COMMANDS.items():
        if handler in seen:
            continue
        seen.add(handler)
        # Commands filed under a submenu (e.g. combat) aren't listed at the
        # top level -- they're reached via that submenu's own command.
        if getattr(handler, "_menu", "main") != "main":
            continue
        lines.append(f"  {cmd} - {description}")
    return "\n".join(lines)


def build_submenu(menu_name):
    """List just the commands filed under `menu_name` (e.g. 'combat').
    Mirrors build_menu's dedupe-by-handler so aliases don't double up.
    Returns a friendly note if nothing is filed there."""
    lines = [f"{menu_name.capitalize()} commands:"]
    seen = set()
    for cmd, (description, handler) in COMMANDS.items():
        if handler in seen:
            continue
        if getattr(handler, "_menu", "main") != menu_name:
            continue
        seen.add(handler)
        lines.append(f"  {cmd} - {description}")
    if len(lines) == 1:
        lines.append(f"  (no {menu_name} commands)")
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


def build_sector_info(sector_id):
    """
    The sector info screen: sector number, then port, then adjacent
    sectors, each on their own line. Shown for the `info` command and
    automatically appended whenever a player's sector actually changes.
    """
    return f"Sec{sector_id}\n{format_port_line(sector_id)}\n{format_warps_line(sector_id)}"
