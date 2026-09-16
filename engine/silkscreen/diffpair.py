"""Coupled differential-pair routing: two traces, one path, one constant gap.

A differential pair is not two nets that happen to be routed early. Its two
traces run side by side at a fixed spacing for their whole length, because
the pair's impedance and its noise rejection both come from that coupling;
the only uncoupled copper is the short breakout at each end, where the pads'
pitch is not the trace pitch.

The design is KiCad's interactive diff-pair router
(KiCad/kicad-source-mirror ``pcbnew/router/pns_diff_pair.cpp`` at 91b6796,
GPL, read for the design only -- none of its code is here):

- ``DP_GATEWAY``: a pair of anchor points exactly ``width + gap`` apart near
  each end, joined to the pads by short *entry lines*;
- ``DIFF_PAIR::BuildInitial``: between the two gateways both traces take the
  same 45-degree shape, so the gap is constant by construction;
- ``DIFF_PAIR::CoupledLength``: a candidate is judged by how much of it is
  coupled and rejected if the gap check fails.

KiCad draws the coupled section under a cursor; here it is searched. The
search is an A* over the centreline of the pair, treated as one fat trace:
a lattice node is usable only when every obstacle is at least half the pair's
total width plus clearance away, so both offset traces fit wherever the
centreline goes. Turns are limited to 45 degrees per node (a right angle is
two chamfers), which keeps the mitred offset corners within the corridor.
The two traces are then the centreline offset exactly by
``+-(width + gap) / 2`` -- not snapped to any grid -- and every segment is
checked against every obstacle before anything is returned.

What this does not do, stated rather than hidden: it routes a pair on one
copper layer only (no pair vias), it routes a pair whose nets each have
exactly two terminals, and it does not tune length -- the skew the corners
leave is measured and reported. The gap is geometry, not impedance: no field
solver or fabricator stackup exists here, so a 90 ohm target is a label.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .routing import (
    DEFAULT_EDGE_CLEARANCE_NM,
    DEFAULT_ROUTE_CLEARANCE_NM,
    DEFAULT_ROUTE_GRID_NM,
    RoutePad,
    Track,
    Via,
)
from .units import mm

__all__ = [
    "DIFF_PAIR_GAP_NM",
    "DIFF_PAIR_WIDTH_NM",
    "CoupledRoute",
    "PairFailure",
    "route_pair",
]

#: Trace width and edge-to-edge gap of a coupled pair. The same numbers are
#: written into the project's diff-pair net class
#: (:func:`silkscreen.schematic.emit_kicad_pro`), so KiCad's own router and
#: this one agree on what the pair should look like.
DIFF_PAIR_WIDTH_NM: int = mm(0.2)
DIFF_PAIR_GAP_NM: int = mm(0.25)

#: How far a gateway may sit from the middle of its two pads.
GATEWAY_RADIUS_NM: int = mm(3)

#: Search effort, in node expansions (the router's unit: the same pair routes
#: the same way twice, whatever the machine's load).
DEFAULT_PAIR_EXPANSIONS = 300_000

#: A mitred corner of a 45-degree turn sits 1/cos(22.5 deg) further from the
#: centreline than a straight run does.
_MITER = 1 / math.cos(math.radians(22.5))

_ORTHO, _DIAG = 12, 17
_TURN_COST = 4
#: Uncoupled lead length costs this many times coupled length: KiCad picks
#: the candidate with the highest coupled ratio, and so does this search.
_LEAD_WEIGHT = 4
#: Headings, counter-clockwise from +X.
_DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


@dataclass(frozen=True)
class CoupledRoute:
    positive: str
    negative: str
    tracks: list[Track]
    #: Length of the coupled section, the same for both traces up to skew.
    coupled_nm: int
    #: Total copper per net, leads included.
    length_nm: tuple[int, int]
    gap_nm: int
    width_nm: int
    #: The coupled section alone, both nets: every segment here runs at the
    #: pitch from the other net's copper. ``tracks`` adds the pad leads.
    coupled_tracks: tuple[Track, ...] = ()

    @property
    def skew_nm(self) -> int:
        return abs(self.length_nm[0] - self.length_nm[1])

    @property
    def coupled_ratio(self) -> float:
        """KiCad's measure: coupled length over the shorter trace's length."""
        shortest = min(self.length_nm)
        return self.coupled_nm / shortest if shortest else 0.0

    def as_dict(self) -> dict:
        return {
            "nets": [self.positive, self.negative],
            "gap_mm": self.gap_nm / 1e6,
            "width_mm": self.width_nm / 1e6,
            "coupled_mm": round(self.coupled_nm / 1e6, 3),
            "length_mm": [round(v / 1e6, 3) for v in self.length_nm],
            "skew_mm": round(self.skew_nm / 1e6, 3),
            "coupled_ratio": round(self.coupled_ratio, 3),
        }


@dataclass(frozen=True)
class PairFailure:
    positive: str
    negative: str
    reason: str


# ---------------------------------------------------------------- geometry


@dataclass(frozen=True)
class _Seg:
    ax: float
    ay: float
    bx: float
    by: float
    half: float
    net: str


@dataclass
class _Obstacles:
    boxes: list[RoutePad] = field(default_factory=list)
    segs: list[_Seg] = field(default_factory=list)
    discs: list[tuple[float, float, float, str]] = field(default_factory=list)

    def gap(self, seg: _Seg, own: str) -> float:
        """Edge-to-edge distance from ``seg`` to everything not on ``own``."""
        best = math.inf
        for pad in self.boxes:
            if pad.net == own:
                continue
            best = min(best, _seg_box(seg, pad) - seg.half)
        for other in self.segs:
            if other.net == own:
                continue
            best = min(best, _seg_seg(seg, other) - seg.half - other.half)
        for x, y, r, net in self.discs:
            if net == own:
                continue
            best = min(best, _pt_seg(x, y, seg) - seg.half - r)
        return best


def _pt_seg(px: float, py: float, s: _Seg) -> float:
    vx, vy = s.bx - s.ax, s.by - s.ay
    span = vx * vx + vy * vy
    along = ((px - s.ax) * vx + (py - s.ay) * vy) / span if span else 0.0
    u = max(0.0, min(1.0, along))
    return math.hypot(px - (s.ax + u * vx), py - (s.ay + u * vy))


def _cross(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _seg_seg(s: _Seg, t: _Seg) -> float:
    d1 = _cross(s.ax, s.ay, s.bx, s.by, t.ax, t.ay)
    d2 = _cross(s.ax, s.ay, s.bx, s.by, t.bx, t.by)
    d3 = _cross(t.ax, t.ay, t.bx, t.by, s.ax, s.ay)
    d4 = _cross(t.ax, t.ay, t.bx, t.by, s.bx, s.by)
    if d1 * d2 < 0 and d3 * d4 < 0:
        return 0.0
    return min(
        _pt_seg(t.ax, t.ay, s), _pt_seg(t.bx, t.by, s),
        _pt_seg(s.ax, s.ay, t), _pt_seg(s.bx, s.by, t),
    )


def _seg_box(s: _Seg, pad: RoutePad) -> float:
    """Distance from a segment's centreline to a pad rectangle's edge."""
    hw, hh = pad.w_nm / 2, pad.h_nm / 2
    x0, x1 = pad.x_nm - hw, pad.x_nm + hw
    y0, y1 = pad.y_nm - hh, pad.y_nm + hh

    def inside(x, y):
        return x0 <= x <= x1 and y0 <= y <= y1

    if inside(s.ax, s.ay) or inside(s.bx, s.by):
        return 0.0
    edges = (
        _Seg(x0, y0, x1, y0, 0, ""), _Seg(x1, y0, x1, y1, 0, ""),
        _Seg(x1, y1, x0, y1, 0, ""), _Seg(x0, y1, x0, y0, 0, ""),
    )
    return min(_seg_seg(s, e) for e in edges)


def _box_point(px: float, py: float, pad: RoutePad) -> float:
    dx = max(abs(px - pad.x_nm) - pad.w_nm / 2, 0.0)
    dy = max(abs(py - pad.y_nm) - pad.h_nm / 2, 0.0)
    return math.hypot(dx, dy)


def _normal(d: int) -> tuple[float, float]:
    """Unit left normal of heading ``d``."""
    dx, dy = _DIRS[d]
    n = math.hypot(dx, dy)
    return -dy / n, dx / n


def _offset_polyline(
    points: list[tuple[int, int]], dirs: list[int], dist: float
) -> list[tuple[int, int]]:
    """``points`` (corner vertices) moved ``dist`` to the left of travel.

    ``dirs[k]`` is the heading of the segment from ``points[k]`` to
    ``points[k+1]``. An inner vertex goes to the intersection of its two
    offset lines: ``c + dist * (n1 + n2) / (1 + n1.n2)``, the mitre.
    """
    out = []
    for k, (x, y) in enumerate(points):
        n1 = _normal(dirs[max(k - 1, 0)])
        n2 = _normal(dirs[min(k, len(dirs) - 1)])
        scale = dist / (1 + n1[0] * n2[0] + n1[1] * n2[1])
        out.append((round(x + scale * (n1[0] + n2[0])),
                    round(y + scale * (n1[1] + n2[1]))))
    return out


def _leads(ax: int, ay: int, bx: int, by: int) -> list[list[tuple[int, int]]]:
    """KiCad's ``DIRECTION_45::BuildInitialTrace`` shapes from a to b:
    straight or diagonal alone when it fits, else diagonal-then-straight or
    straight-then-diagonal."""
    dx, dy = bx - ax, by - ay
    if dx == 0 or dy == 0 or abs(dx) == abs(dy):
        return [[(ax, ay), (bx, by)]]
    m = min(abs(dx), abs(dy))
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    diag_first = (ax + sx * m, ay + sy * m)
    straight_first = (bx - sx * m, by - sy * m)
    return [[(ax, ay), diag_first, (bx, by)], [(ax, ay), straight_first, (bx, by)]]


def _length(points: list[tuple[int, int]]) -> float:
    pairs = zip(points, points[1:], strict=False)
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in pairs)


# ---------------------------------------------------------------- search


def route_pair(
    positive: tuple[RoutePad, RoutePad],
    negative: tuple[RoutePad, RoutePad],
    *,
    pads: list[RoutePad],
    tracks: list[Track] = (),
    vias: list[Via] = (),
    min_x_nm: int,
    min_y_nm: int,
    max_x_nm: int,
    max_y_nm: int,
    width_nm: int = DIFF_PAIR_WIDTH_NM,
    gap_nm: int = DIFF_PAIR_GAP_NM,
    clearance_nm: int = DEFAULT_ROUTE_CLEARANCE_NM,
    edge_clearance_nm: int = DEFAULT_EDGE_CLEARANCE_NM,
    grid_nm: int = DEFAULT_ROUTE_GRID_NM,
    max_expansions: int = DEFAULT_PAIR_EXPANSIONS,
) -> CoupledRoute | PairFailure:
    """Route one coupled pair from the ``[0]`` pads to the ``[1]`` pads.

    ``positive``/``negative`` are each net's two terminals, the first of each
    at the same end. ``pads``, ``tracks`` and ``vias`` are everything already
    on the board; the four terminals may be among ``pads`` (they are skipped
    by identity of position and net).
    """
    p_net, n_net = positive[0].net, negative[0].net
    terminals = (*positive, *negative)

    def fail(reason: str) -> PairFailure:
        return PairFailure(p_net, n_net, reason)

    if any(t.through_hole for t in terminals):
        return fail("a pair terminal is a through-hole pad; pair vias are not modelled")
    layer = positive[0].layer
    if any(t.layer is not layer for t in terminals):
        return fail(
            "the pair's terminals are on different layers; pair vias are not modelled"
        )

    term_keys = {(t.x_nm, t.y_nm, t.net) for t in terminals}
    obstacles = _Obstacles()
    for pad in pads:
        if (pad.x_nm, pad.y_nm, pad.net) in term_keys:
            continue
        if pad.through_hole or pad.layer is layer:
            obstacles.boxes.append(pad)
    for t in tracks:
        if t.layer is layer:
            obstacles.segs.append(_Seg(t.start_x_nm, t.start_y_nm, t.end_x_nm,
                                       t.end_y_nm, t.width_nm / 2, t.net))
    for v in vias:
        obstacles.discs.append((v.x_nm, v.y_nm, v.diameter_nm / 2, v.net))
    # The two ends of the pair are obstacles to each other's traces.
    for t in terminals:
        obstacles.boxes.append(t)

    pitch = width_nm + gap_nm
    half_pitch = pitch / 2
    corridor = half_pitch * _MITER + width_nm / 2 + clearance_nm
    inset = edge_clearance_nm + half_pitch * _MITER + width_nm / 2
    need = clearance_nm  # edge-to-edge, every final segment

    nx = (max_x_nm - min_x_nm) // grid_nm + 1
    ny = (max_y_nm - min_y_nm) // grid_nm + 1

    def at(i: int, j: int) -> tuple[int, int]:
        return min_x_nm + i * grid_nm, min_y_nm + j * grid_nm

    blocked: set[tuple[int, int]] = set()

    def mark(x0, y0, x1, y1, dist) -> None:
        i0 = max(0, math.floor((x0 - corridor - min_x_nm) / grid_nm))
        i1 = min(nx - 1, math.ceil((x1 + corridor - min_x_nm) / grid_nm))
        j0 = max(0, math.floor((y0 - corridor - min_y_nm) / grid_nm))
        j1 = min(ny - 1, math.ceil((y1 + corridor - min_y_nm) / grid_nm))
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                if (i, j) not in blocked and dist(*at(i, j)) < corridor:
                    blocked.add((i, j))

    for pad in obstacles.boxes:
        mark(pad.x_nm - pad.w_nm / 2, pad.y_nm - pad.h_nm / 2,
             pad.x_nm + pad.w_nm / 2, pad.y_nm + pad.h_nm / 2,
             lambda x, y, p=pad: _box_point(x, y, p))
    for s in obstacles.segs:
        mark(min(s.ax, s.bx), min(s.ay, s.by), max(s.ax, s.bx), max(s.ay, s.by),
             lambda x, y, s=s: _pt_seg(x, y, s) - s.half)
    for x, y, r, _ in obstacles.discs:
        mark(x, y, x, y, lambda px, py, x=x, y=y, r=r: math.hypot(px - x, py - y) - r)

    def free(i: int, j: int) -> bool:
        if not (0 <= i < nx and 0 <= j < ny) or (i, j) in blocked:
            return False
        x, y = at(i, j)
        return (min_x_nm + inset <= x <= max_x_nm - inset
                and min_y_nm + inset <= y <= max_y_nm - inset)

    def lead(pad: RoutePad, end: tuple[int, int], own: str):
        """The shortest clear lead from ``pad`` to ``end``, or None."""
        best = None
        for shape in _leads(pad.x_nm, pad.y_nm, *end):
            segs = _segments(shape, width_nm / 2, own)
            clear = all(obstacles.gap(s, own) >= need for s in segs)
            if clear and (best is None or _length(shape) < _length(best)):
                best = shape
        return best

    def gateways(pp: RoutePad, npad: RoutePad, arriving: bool):
        """(node, heading, side) -> (cost, p_lead, n_lead) around one pad pair.

        ``side`` is +1 when P runs on the left of travel. At the start the
        heading leaves the gateway; at the end it arrives there.
        """
        mx, my = (pp.x_nm + npad.x_nm) / 2, (pp.y_nm + npad.y_nm) / 2
        r = GATEWAY_RADIUS_NM // grid_nm
        ci, cj = round((mx - min_x_nm) / grid_nm), round((my - min_y_nm) / grid_nm)
        found = {}
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                if not free(i, j):
                    continue
                x, y = at(i, j)
                if math.hypot(x - mx, y - my) > GATEWAY_RADIUS_NM:
                    continue
                for d in range(8):
                    lx, ly = _normal(d)
                    for side in (1, -1):
                        ox, oy = side * half_pitch * lx, side * half_pitch * ly
                        p_end = (round(x + ox), round(y + oy))
                        n_end = (round(x - ox), round(y - oy))
                        # A lead must come from behind the gateway (start)
                        # or continue ahead of it (end), never double back
                        # across the coupled section.
                        hx, hy = _DIRS[d]
                        sign = 1 if arriving else -1
                        if (sign * ((mx - x) * hx + (my - y) * hy)) < 0:
                            continue
                        pl = lead(pp, p_end, p_net)
                        if pl is None:
                            continue
                        nl = lead(npad, n_end, n_net)
                        if nl is None:
                            continue
                        ps = _segments(pl, width_nm / 2, p_net)
                        ns = _segments(nl, width_nm / 2, n_net)
                        if any(_seg_seg(a, b) - width_nm < need
                               for a in ps for b in ns):
                            continue
                        lead_nm = (_length(pl) + _length(nl)) / 2
                        cost = _LEAD_WEIGHT * lead_nm / grid_nm * _ORTHO
                        found[(i, j, d, side)] = (cost, pl, nl)
        return found

    starts = gateways(positive[0], negative[0], arriving=False)
    if not starts:
        return fail("no clear breakout from the pair's first pads")
    ends = gateways(positive[1], negative[1], arriving=True)
    if not ends:
        return fail("no clear breakout from the pair's second pads")

    ex = sum(k[0] for k in ends) / len(ends)
    ey = sum(k[1] for k in ends) / len(ends)
    slack = GATEWAY_RADIUS_NM / grid_nm

    def h(i: int, j: int) -> float:
        dx, dy = abs(i - ex), abs(j - ey)
        octile = _ORTHO * max(dx, dy) + (_DIAG - _ORTHO) * min(dx, dy)
        return max(0.0, octile - slack * _DIAG)

    rejected: set[tuple] = set()
    last = ""
    for _attempt in range(12):
        path = _search(starts, ends, free, h, rejected, max_expansions)
        if isinstance(path, str):
            return fail(path if not last else f"{path}; last candidate: {last}")
        start_key, end_key, nodes, dirs = path
        p_all, n_all, coupled, p_mid, n_mid = _build(
            nodes, dirs, start_key[3], starts[start_key], ends[end_key], at, half_pitch
        )
        problem = _check(p_all, n_all, obstacles, p_net, n_net, width_nm, need)
        if problem is None:
            tracks_out = [
                Track(a[0], a[1], b[0], b[1], layer, net, width_nm)
                for pts, net in ((p_all, p_net), (n_all, n_net))
                for a, b in zip(pts, pts[1:], strict=False) if a != b
            ]
            mids = tuple(
                Track(a[0], a[1], b[0], b[1], layer, net, width_nm)
                for pts, net in ((p_mid, p_net), (n_mid, n_net))
                for a, b in zip(pts, pts[1:], strict=False) if a != b
            )
            return CoupledRoute(
                p_net, n_net, tracks_out, round(coupled),
                (round(_length(p_all)), round(_length(n_all))), gap_nm, width_nm,
                mids,
            )
        # A gateway pair whose finished copper fails is not offered again.
        last = problem
        rejected.add((start_key, end_key))
    return fail("every coupled candidate failed its clearance check")


def _search(starts, ends, free, h, rejected, budget):
    """A* over (i, j, heading, side); a node turns at most 45 degrees.

    Returns ``(start_key, end_key, nodes, dirs)`` or a reason.
    """
    frontier: list = []
    best: dict = {}
    parent: dict = {}
    for key, (cost, _, _) in starts.items():
        if cost < best.get(key, math.inf):
            best[key] = cost
            parent[key] = None
            heapq.heappush(frontier, (cost + h(key[0], key[1]), cost, key))
    expansions = 0
    while frontier:
        _, g, state = heapq.heappop(frontier)
        if g > best.get(state, math.inf):
            continue
        if len(state) == 5:  # (i, j, d, side, "end"): a finished route
            return _unwind(parent, state)
        expansions += 1
        if expansions > budget:
            return f"coupled search ran out of budget ({budget} expansions)"
        i, j, d, side = state
        if state in ends:
            origin = _origin(parent, state)
            if (origin, state) not in rejected:
                done = (*state, "end")
                total = g + ends[state][0]
                if total < best.get(done, math.inf):
                    best[done] = total
                    parent[done] = state
                    heapq.heappush(frontier, (total, total, done))
        for nd in (d, (d + 1) % 8, (d - 1) % 8):
            dx, dy = _DIRS[nd]
            ni, nj = i + dx, j + dy
            if not free(ni, nj):
                continue
            if dx and dy and not (free(i + dx, j) and free(i, j + dy)):
                continue
            ng = g + (_DIAG if dx and dy else _ORTHO) + (_TURN_COST if nd != d else 0)
            nstate = (ni, nj, nd, side)
            if ng < best.get(nstate, math.inf):
                best[nstate] = ng
                parent[nstate] = state
                heapq.heappush(frontier, (ng + h(ni, nj), ng, nstate))
    return "no coupled path: the corridor for both traces is blocked"


def _origin(parent, state):
    while parent[state] is not None:
        state = parent[state]
    return state


def _unwind(parent, done):
    chain = []
    state = parent[done]
    while state is not None:
        chain.append(state)
        state = parent[state]
    chain.reverse()
    nodes = [(s[0], s[1]) for s in chain]
    # dirs[k] is the heading of the move into nodes[k + 1]; a route that
    # never leaves its gateway keeps the gateway's heading.
    dirs = [s[2] for s in chain[1:]] or [chain[0][2]]
    return chain[0], done[:4], nodes, dirs


def _build(nodes, dirs, side, start, end, at, half_pitch):
    """Both traces: lead in, the offset centreline, lead out."""
    _, p_in, n_in = start
    _, p_out, n_out = end
    # Centreline vertices: the first node, every node where heading changes,
    # the last node.
    verts = [at(*nodes[0])]
    vdirs: list[int] = []
    for k in range(1, len(nodes)):
        d = dirs[k - 1]
        if vdirs and vdirs[-1] == d:
            verts[-1] = at(*nodes[k])
        else:
            vdirs.append(d)
            verts.append(at(*nodes[k]))
    if len(verts) > 1:
        p_mid = _offset_polyline(verts, vdirs, side * half_pitch)
        n_mid = _offset_polyline(verts, vdirs, -side * half_pitch)
    else:
        p_mid, n_mid = [p_in[-1]], [n_in[-1]]
    coupled = (_length(p_mid) + _length(n_mid)) / 2

    def join(lead_in, mid, lead_out):
        out = list(lead_in)
        for pt in [*mid, *reversed(lead_out)]:
            if pt != out[-1]:
                out.append(pt)
        return out

    return (join(p_in, p_mid, p_out), join(n_in, n_mid, n_out), coupled,
            p_mid, n_mid)


def _segments(points, half, net):
    return [_Seg(a[0], a[1], b[0], b[1], half, net)
            for a, b in zip(points, points[1:], strict=False) if a != b]


def _check(p_all, n_all, obstacles, p_net, n_net, width_nm, need):
    """The first clearance problem in the finished copper, or None."""
    ps = _segments(p_all, width_nm / 2, p_net)
    ns = _segments(n_all, width_nm / 2, n_net)
    for segs, own in ((ps, p_net), (ns, n_net)):
        for seg in segs:
            if obstacles.gap(seg, own) < need - 1:
                return f"{own} would pass within the clearance of other copper"
    for a in ps:
        for b in ns:
            if _seg_seg(a, b) - width_nm < need - 1:
                return "the two traces would come closer than the clearance"
    return None
