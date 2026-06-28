"""
The mine-damage math: rolling detonation damage and cascading it through
shields -> fighters -> hull. Pure arithmetic with no db access, so the
destruction rules are easy to test in isolation.
"""

import math
import random


# Each detonating mine deals a random 1..MINE_MAX_DAMAGE points (the spec
# is "up to 10 points" per mine). For flat damage instead, make
# roll_mine_damage return num_mines * MINE_MAX_DAMAGE.
MINE_MAX_DAMAGE = 10


# How incoming mine damage is soaked up, in order. Shields go first at
# 1 damage per shield; then fighters, each one soaking DAMAGE_PER_FIGHTER
# points; then, if damage still remains with both gone, the ship is lost.
SHIELD_DAMAGE_PER_POINT = 1


DAMAGE_PER_FIGHTER = 2


def roll_mine_damage(num_mines, rng=None):
    """Total damage from `num_mines` detonating mines -- each deals a
    random 1..MINE_MAX_DAMAGE points, summed."""
    r = rng if rng is not None else random
    return sum(r.randint(1, MINE_MAX_DAMAGE) for _ in range(num_mines))


def apply_mine_damage(shields, fighters, total_damage):
    """
    Cascade `total_damage` through a ship's defenses and report the
    outcome, without touching the db -- pure arithmetic, so it's the
    easy part to unit-test.

    Shields absorb first, 1 point each. Whatever's left hits fighters,
    each soaking DAMAGE_PER_FIGHTER points (a partial hit still claims a
    whole fighter). If damage still remains once both are gone, the ship
    is destroyed.

    Returns (shields_after, fighters_after, shields_lost, fighters_lost,
    destroyed).
    """
    shields_lost = min(shields, total_damage // SHIELD_DAMAGE_PER_POINT)
    shields_after = shields - shields_lost
    remaining = total_damage - shields_lost * SHIELD_DAMAGE_PER_POINT

    # Round up: 3 leftover damage at 2-per-fighter still costs 2 fighters.
    fighters_needed = (remaining + DAMAGE_PER_FIGHTER - 1) // DAMAGE_PER_FIGHTER
    fighters_lost = min(fighters, fighters_needed)
    fighters_after = fighters - fighters_lost
    remaining -= fighters_lost * DAMAGE_PER_FIGHTER

    destroyed = remaining > 0
    return shields_after, fighters_after, shields_lost, fighters_lost, destroyed


def _plural(n, noun):
    """'1 mine' / '2 mines' -- naive +s pluralization, fine for the
    handful of nouns the mine messages use."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# Ship-to-ship attack ratios. Fighters trade against the defender's
# fighters first: the attacker loses ATTACK_FIGHTER_RATIO of a fighter for
# each defender fighter destroyed (0.75 -> 1000 attacking fighters wipe
# 1000 defenders and 250 attackers survive). Any fighters left over then
# strip shields, each surviving fighter taking down ATTACK_SHIELD_RATIO
# shields before it's spent (10 -> 20 fighters clear 200 shields).
ATTACK_FIGHTER_RATIO = 0.75
ATTACK_SHIELD_RATIO = 10


def resolve_attack(attacker_fighters, defender_fighters, defender_shields):
    """
    Resolve one fighter attack, purely. Returns

        (attacker_fighters_left, defender_fighters_left,
         defender_shields_left, destroyed)

    Fighters clash first (0.75 attacker fighters spent per defender
    fighter killed); only once the defender's fighters are gone do leftover
    attackers hit shields (10 shields stripped per attacker fighter spent).
    The defender is destroyed only if both their fighters AND shields reach
    zero AND the attacker still has at least one fighter standing -- exactly
    spending your last fighter to clear the final shield leaves the
    defender alive at 0/0, mirroring how mine damage works.
    """
    atk = attacker_fighters
    df = defender_fighters
    ds = defender_shields

    # Phase 1: fighters vs fighters.
    clear_fighters_cost = math.ceil(ATTACK_FIGHTER_RATIO * df)
    if atk >= clear_fighters_cost:
        atk -= clear_fighters_cost
        df = 0
    else:
        df -= math.floor(atk / ATTACK_FIGHTER_RATIO)
        atk = 0

    # Phase 2: leftover fighters vs shields (only once fighters are clear).
    if atk > 0 and df == 0:
        clear_shields_cost = math.ceil(ds / ATTACK_SHIELD_RATIO)
        if atk >= clear_shields_cost:
            atk -= clear_shields_cost
            ds = 0
        else:
            ds -= atk * ATTACK_SHIELD_RATIO
            atk = 0

    destroyed = (df == 0 and ds == 0 and atk >= 1)
    return atk, df, ds, destroyed
