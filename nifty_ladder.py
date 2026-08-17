#!/usr/bin/env python3
"""
nifty_ladder.py

Who climbed into the NIFTY 50, and who dropped out of it — side by side.

nifty_timeline.py reads NSE's press releases date by date: what changed, and
when. This reads the same releases the other way round, following individual
stocks as they move between the three broad indices, and draws each one's path:

    CLIMBED IN (green)              DROPPED OUT (red)
    MIDCAP 100 ▶ NIFTY 50           NIFTY 50 ◀ MIDCAP 100
    MIDCAP 100 ▶ NEXT 50 ▶ 50       NIFTY 50 ◀ NEXT 50 ◀ MIDCAP 100
                                    NIFTY 50 ◀ NEXT 50   (fell one rung only)

Both panels share the same axis — MIDCAP 100 on the left, NIFTY 50 on the
right — so only the arrows and the colour say which way a stock travelled.

NSE never labels a move as a promotion or a demotion. A release only ever says
a stock left one index and joined another, so the direction has to be inferred
from the pairing of those two records — which is why the exit from one index
and the entry into the next always carry the same effective date.

Everything comes from nifty_timeline.py: the press-release scan, the PDF
parsing and the cache under ~/.cache/nifty_index_changes/ are all shared, so
whichever script you run first pays for the scan and the other is instant.

Usage
-----
    python nifty_ladder.py             # last 5 years
    python nifty_ladder.py --years 10  # further back — finds several more
    python nifty_ladder.py --refresh   # ignore the cache and re-scan

Uses only the Python standard library — no pip install needed.
"""

import argparse
import re
import shutil
import sys
import urllib.error
from datetime import date, datetime, timedelta

import nifty_timeline as timeline

# The timeline script owns the fetching, parsing and colouring primitives; this
# one is a second reading of the same data, so it borrows them rather than
# keeping a second copy that could drift.
DEFAULT_YEARS = timeline.DEFAULT_YEARS
green, red, dim = timeline.green, timeline.red, timeline.dim

MIDCAP_KEY, NEXT_KEY, FIFTY_KEY = "niftymidcap100", "niftynext50", "nifty50"
SYMBOL_RE = re.compile(r"\(([A-Z][A-Z0-9&\-]{1,14})\)$")


# ---------------------------------------------------------------------------
# Working out who moved, and which way
# ---------------------------------------------------------------------------
def _symbol(label):
    """'Trent Ltd. (TRENT)' -> 'TRENT'. Names without one stand for themselves."""
    match = SYMBOL_RE.search(label)
    return match.group(1) if match else label


def _moves(events):
    """{symbol: [(effective, index_key, 'added'|'removed'), …]} across the three."""
    moves = {}
    for event in events:
        effective = datetime.strptime(event["effective"], "%Y-%m-%d").date()
        for key in (MIDCAP_KEY, NEXT_KEY, FIFTY_KEY):
            section = event["sections"].get(key)
            if not section:
                continue
            for direction in ("added", "removed"):
                for label in section[direction]:
                    moves.setdefault(_symbol(label), []).append(
                        (effective, key, direction))
    return moves


def _dates(history, key, direction, **bounds):
    """The dates a stock joined or left one index, oldest first."""
    lo, hi = bounds.get("after"), bounds.get("before")
    return sorted(d for d, k, w in history
                  if k == key and w == direction
                  and (lo is None or d >= lo) and (hi is None or d < hi))


def climbers(moves, since):
    """
    Stocks that reached the NIFTY 50 from the midcap index, split by route.

    Returns (direct, two_step): straight up from MIDCAP 100, and via NEXT 50.
    """
    direct, two_step = [], []
    for symbol, history in moves.items():
        joined = _dates(history, FIFTY_KEY, "added")
        if not joined or joined[-1] < since:
            continue
        arrival = joined[-1]  # the latest entry, so a re-entry reads as its own climb
        # A stock still sitting in the midcap index cannot have joined the 50,
        # so the last exit before the arrival is the rung it climbed from.
        left_midcap = _dates(history, MIDCAP_KEY, "removed", before=arrival + timedelta(1))
        if not left_midcap:
            # Never a midcap constituent in this window: it either climbed
            # before the window opened, or arrived by listing or demerger.
            continue
        # A stop in the NEXT 50 only counts if it happened on the way up —
        # after leaving the midcap index and before joining the 50.
        stopped = _dates(history, NEXT_KEY, "added", after=left_midcap[-1], before=arrival)
        row = {"symbol": symbol, "stop": stopped[-1] if stopped else None, "end": arrival}
        (two_step if stopped else direct).append(row)

    return _newest_first(direct, "end"), _newest_first(two_step, "end")


def fallers(moves, since):
    """
    Stocks that left the NIFTY 50, split by how far they fell.

    Returns (direct, two_step, stalled): straight down to MIDCAP 100, down via
    the NEXT 50, and those still sitting on the NEXT 50 rung.
    """
    direct, two_step, stalled = [], [], []
    for symbol, history in moves.items():
        left = _dates(history, FIFTY_KEY, "removed")
        if not left or left[-1] < since:
            continue
        departure = left[-1]
        to_next = _dates(history, NEXT_KEY, "added", after=departure)
        to_midcap = _dates(history, MIDCAP_KEY, "added", after=departure)
        row = {"symbol": symbol,
               "stop": to_next[0] if to_next else None,
               "end": to_midcap[-1] if to_midcap else None}
        if to_next and to_midcap and to_midcap[-1] > to_next[0]:
            two_step.append(row)
        elif to_midcap:
            direct.append({**row, "stop": None})
        elif to_next:
            stalled.append({**row, "end": None})
        # Anything else left the 50 without joining either: a merger, a
        # delisting, or a demerged entity being tidied up. Not a fall.

    return (_newest_first(direct, "end"), _newest_first(two_step, "end"),
            _newest_first(stalled, "stop"))


def _newest_first(rows, key):
    return sorted(rows, key=lambda r: r[key], reverse=True)


# ---------------------------------------------------------------------------
# Drawing
#
# Both panels sit on the same three-column grid: MIDCAP 100, NIFTY NEXT 50 and
# NIFTY 50. A cell is either an arrival stamp ("▶30/sep/22"), the origin dot,
# or bare track passing through a rung the stock skipped.
# ---------------------------------------------------------------------------
NAME_W = 11    # symbols only — the longest in these indices is 10 characters
CELL = 16      # one index column: a 10-character stamp plus six of track, so
               # the arrow into the next column is always plainly drawn
STAMP_W = 10
# The last column differs by panel — a stamp going up, a bare dot coming down —
# and each is padded to clear the NIFTY50 heading above it.
UP_END, DN_END = STAMP_W + 2, 9
GAP = 3        # between the two panels

if timeline._supports("●▶◀━"):
    DOT, RIGHT, LEFT, TRACK = "●", "▶", "◀", "━"
else:
    DOT, RIGHT, LEFT, TRACK = "o", ">", "<", "-"


class Row:
    """A rendered line: plain text for width maths, styled text for output."""

    __slots__ = ("plain", "styled")

    def __init__(self, plain, styled=None):
        self.plain = plain
        self.styled = plain if styled is None else styled

    def padded(self, width):
        return self.styled + " " * max(0, width - len(self.plain))


BLANK = Row("")


def _short(when):
    """30/sep/22 — small enough to fit both panels on one screen."""
    return f"{when:%d/%b/%y}".lower()


def _span(start, end):
    """
    A compact gap between two dates, in years: '.4yr', '1.2yr', '8.3yr'.

    The leading zero is dropped so the column never needs more than six
    characters — the room that buys is what keeps both panels on one screen.
    """
    return f"{(end - start).days / 365.25:.1f}".lstrip("0") + "yr"


def _row(symbol, cells, took, paint, end_w):
    """One stock's line: its symbol, its drawn path, and how long it took."""
    art = "".join(cells[:-1]) + f"{cells[-1]:<{end_w}}"
    return Row(f"{symbol:<{NAME_W}}{art}{took}",
               f"{symbol:<{NAME_W}}{paint(art)}{took}")


def _header(end_w):
    text = (f"{'STOCK':<{NAME_W}}{'MID100':<{CELL}}{'NEXT50':<{CELL}}"
            f"{'NIFTY50':<{end_w}}TOOK")
    return Row(text)


def _blocks(*groups):
    """Stack the groups, a blank line between them, skipping empty ones."""
    out = []
    for group in groups:
        if not group:
            continue
        if out:
            out.append(BLANK)
        out.extend(group)
    return out


def _panel(title, paint, body):
    """A titled, ruled panel — or a placeholder when nothing qualified."""
    if not body:
        body = [Row("none in this period", dim("none in this period"))]
    header = _header(UP_END if paint is green else DN_END)
    width = max(len(row.plain) for row in [header] + body)
    return [Row(title, paint(title)), header, Row(TRACK * width)] + body


def climb_panel(direct, two_step):
    """Left panel: the way up, drawn left to right."""
    def draw(row):
        # The origin dot sits under MIDCAP 100; a direct climber has unbroken
        # track through the NEXT 50 column, which is what shows the skipped rung.
        middle = (RIGHT + _short(row["stop"]) + TRACK * (CELL - STAMP_W)
                  if row["stop"] else TRACK * CELL)
        took = _span(row["stop"], row["end"]) if row["stop"] else "—"
        return _row(row["symbol"], [DOT + TRACK * (CELL - 1), middle,
                                    RIGHT + _short(row["end"])], took, green, UP_END)

    return _panel(f"{RIGHT} CLIMBED IN", green,
                  _blocks([draw(r) for r in direct], [draw(r) for r in two_step]))


def fall_panel(direct, two_step, stalled, today):
    """Right panel: the way down, drawn right to left from the same axis."""
    def draw(row):
        # Mirror of the climb: the dot is now on the right, under NIFTY 50, and
        # every arrowhead points back down the ladder.
        first = (LEFT + _short(row["end"]) + TRACK * (CELL - STAMP_W)
                 if row["end"] else " " * CELL)
        if row["stop"]:
            middle = LEFT + _short(row["stop"]) + TRACK * (CELL - STAMP_W)
        else:
            middle = TRACK * CELL
        if row["stop"] and row["end"]:
            took = _span(row["stop"], row["end"])
        elif row["end"]:
            took = "—"
        elif row["stop"] > today:
            took = "n/a"           # announced, but the date hasn't come round yet
        else:
            took = "+" + _span(row["stop"], today)  # still on that rung
        return _row(row["symbol"], [first, middle, DOT], took, red, DN_END)

    return _panel(f"{LEFT} DROPPED OUT", red,
                  _blocks([draw(r) for r in direct], [draw(r) for r in two_step],
                          [draw(r) for r in stalled]))


def show(left, right, title):
    """Panels side by side, or stacked when the terminal is too narrow."""
    print(f"NIFTY 50 — who came up, who went down   ({title})\n")
    left_width = max(len(row.plain) for row in left)
    right_width = max(len(row.plain) for row in right)

    if shutil.get_terminal_size((100, 24)).columns < left_width + GAP + right_width:
        for panel in (left, right):
            for row in panel:
                print(row.styled.rstrip())
            print()
        return

    for i in range(max(len(left), len(right))):
        one = left[i] if i < len(left) else BLANK
        two = right[i] if i < len(right) else BLANK
        print((one.padded(left_width) + " " * GAP + two.styled).rstrip())


def main():
    parser = argparse.ArgumentParser(
        description="Which stocks climbed from NIFTY MIDCAP 100 into the NIFTY 50, and "
                    "which ones dropped back out of it. Read from NSE Indices' press "
                    "releases, sharing nifty_timeline.py's scan and cache.")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                        help=f"how far back to look (default: {DEFAULT_YEARS})")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the cache and scan again from scratch")
    args = parser.parse_args()

    today = date.today()
    since = today - timedelta(days=365 * args.years)
    title = f"last {args.years} years"

    try:
        # A journey needs both of its steps, so the scan reaches back past the
        # window — otherwise the first rung of an early mover is invisible.
        events = timeline.collect_events(since - timedelta(days=70), today, args.refresh)
        print()
        moves = _moves(events)
        show(climb_panel(*climbers(moves, since)),
             fall_panel(*fallers(moves, since), today), title)
        print(dim(f"\nOnly journeys finishing inside the {title} are shown; widen it "
                  f"with --years."))
        print(dim("A trailing '+' means the stock is still on that rung today."))
    except KeyboardInterrupt:
        print("\nBye!")
    except urllib.error.URLError as exc:
        print(f"\nNetwork problem while talking to NSE: {exc.reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
