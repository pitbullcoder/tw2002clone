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


def build_sector_info(sector_id):
    """
    The sector info screen: sector number, then port, then adjacent
    sectors, each on their own line. Shown for the `info` command and
    automatically appended whenever a player's sector actually changes.
    """
    return f"Sec{sector_id}\n{format_port_line(sector_id)}\n{format_warps_line(sector_id)}"
