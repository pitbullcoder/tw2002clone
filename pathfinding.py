"""
Pure warp-graph traversal: shortest paths between sectors and the
hop-distance queries the escape pod uses to pick a landing spot. No db or
game-state access -- just graphs in, sectors out -- which is what makes it
straightforward to unit-test.
"""

import random
from collections import deque


# A destroyed pilot ejects to a random sector this many warp-hops from
# where they were blown up -- far enough to be a real setback, not the
# next sector over.
ESCAPE_POD_MIN_HOPS = 4


ESCAPE_POD_MAX_HOPS = 6


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


def _bfs_distances(graph, start, max_hops=None):
    """Shortest-path hop counts from `start` to every reachable sector,
    as {sector_id: hops} (start itself maps to 0). If max_hops is given,
    BFS stops expanding past that depth -- the returned dict then only
    covers sectors within max_hops, which is all the escape-pod search
    needs and saves walking the whole galaxy."""
    dist = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if max_hops is not None and dist[node] >= max_hops:
            continue
        for neighbor in graph.get(node, []):
            if neighbor not in dist:
                dist[neighbor] = dist[node] + 1
                queue.append(neighbor)
    return dist


def sectors_within_hop_range(graph, start, min_hops, max_hops):
    """All sector ids whose shortest-path distance from `start` falls in
    [min_hops, max_hops] inclusive."""
    dist = _bfs_distances(graph, start, max_hops=max_hops)
    return [s for s, d in dist.items() if min_hops <= d <= max_hops]


def choose_escape_sector(graph, start, rng=None):
    """
    Pick where a destroyed pilot's escape pod drifts to: a random sector
    ESCAPE_POD_MIN_HOPS..ESCAPE_POD_MAX_HOPS warps from `start`.

    Falls back to the farthest reachable sector(s) if nothing sits in
    that band (a corner of a sparse map might have no sector exactly 4-6
    hops out), and returns None only if `start` has no warps at all --
    nowhere to eject to, so the caller leaves the pilot put.
    """
    r = rng if rng is not None else random
    candidates = sectors_within_hop_range(graph, start, ESCAPE_POD_MIN_HOPS, ESCAPE_POD_MAX_HOPS)
    if not candidates:
        dist = _bfs_distances(graph, start)
        reachable = [(s, d) for s, d in dist.items() if d > 0]
        if not reachable:
            return None
        farthest = max(d for _, d in reachable)
        candidates = [s for s, d in reachable if d == farthest]
    return r.choice(candidates)
