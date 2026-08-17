#!/usr/bin/env python3
"""
nifty_midcap_grid.py

The NIFTY midcap universe on one screen, with the overlap taken out.

The four midcap indices are not four sets of stocks — they are one set of 150
printed four times over: MIDCAP 50 sits inside MIDCAP 100, which sits inside
MIDCAP 150, and the 25-stock derivatives index (NIFTY MIDCAP SELECT, the
MIDCPNIFTY contract) is drawn from the 150. Their union is exactly the 150
names. Printing the four constituent lists whole would spend 325 rows saying
what 150 rows can.

So each column shows only what its index *adds* to the one below it:

    | SELECT 25 (F&O) |  CORE 50  |  BAND 51-100  |  BAND 101-150 |

Columns 2, 3 and 4 are disjoint: a stock appears in exactly one of them, and
their market caps add up to the whole of MIDCAP 150, which is what makes the
share-of-universe line under each total mean something. Column 1 is the odd one
out — it is a liquidity/derivatives selection rather than a size tier, so it
overlaps the other three and is marked with a *.

    --view groups sorts by membership rather than by size: one column per
                  pattern, deepest first, so column 1 is every stock that sits
                  in all four indices at once, then the rest of the 50, then
                  the 51-100 band, and so on. Each column carries its own
                  count, market cap and share of the universe.

    --view ticks  prints the other way round: one row per stock, with a tick
                  under each index it belongs to. Same 150 names, no repetition
                  at all, and the tick pattern is the finding — '..##' is a
                  stock that made the 100 but not the 50. It reads deepest
                  first: the fully ticked stocks lead, then each block below
                  gives up one more index, so the ticks thin out as you go
                  down. --sort mcap orders it flat instead.

Caveat worth keeping in mind when reading either view: these columns are ranked
by *full* market cap, while NSE selects and weights by *free float*. That gap is
exactly why LICI can top the MIDCAP 150 column on size yet sit outside MIDCAP
100 — the government holds most of it, so its tradable float is midcap-sized
even though the company is not.

Data sources (standard library only — NO pip install needed):
  * Constituent lists : NSE public CSVs (so the lists auto-update)
  * Prices / mkt cap  : Yahoo Finance public JSON API (one batched pass)

The HTTP client, the quote fetcher and the panel layout are borrowed from
nifty_marketcap_52w.py rather than copied, so the two screens can't drift.

Usage
-----
    python nifty_midcap_grid.py                # the four bands, top 25 of each
    python nifty_midcap_grid.py --top 0        # every name in every band
    python nifty_midcap_grid.py --view groups  # grouped by index membership
    python nifty_midcap_grid.py --view ticks   # one row per stock + membership
    python nifty_midcap_grid.py --view ticks --sort mcap   # flat, biggest first
"""

import argparse
import csv
import datetime
import io
import math
import shutil
import sys

import nifty_marketcap_52w as base

# The market-cap screen owns the fetching, colouring and layout primitives.
green, red, bold, dim, cyan = base.green, base.red, base.bold, base.dim, base.cyan
RULE = base.RULE
Line, Panel = base.Line, base.Panel

ARCHIVE = "https://archives.nseindia.com/content/indices/"

# Keyed narrowest to widest, which is also how they nest.
INDEX_FILES = [
    ("SELECT", "ind_niftymidcapselect_list.csv"),
    ("50", "ind_niftymidcap50list.csv"),
    ("100", "ind_niftymidcap100list.csv"),
    ("150", "ind_niftymidcap150list.csv"),
]

SYM_W = 11    # the longest midcap symbols are 10 characters (e.g. ADANIENSOL)
CHG_W = 8     # matches base.change_cell's fixed-width cell; the market-cap
              # column is measured from the numbers a view actually prints
TICK_W = 4    # one membership column in the ticks view
GAP = 2       # between columns
MAX_CHUNKS = 3  # most tick columns to split the 150 names across
MIN_CHUNK = 20  # and the fewest rows worth giving a column of its own

IN, OUT = ("●", "·") if base._supports("●·") else ("#", ".")


# ---------------------------------------------------------------------------
# Constituents
# ---------------------------------------------------------------------------
def fetch_constituents(csv_name):
    """Return [(nse_symbol, company_name)] for one index's NSE archive CSV."""
    text = base.http_get(ARCHIVE + csv_name)
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("Symbol") or "").strip()
        if symbol:
            rows.append((symbol, (row.get("Company Name") or "").strip()))
    if not rows:
        raise RuntimeError(f"No symbols parsed from {csv_name} — format may have changed.")
    return rows


def build_rows(constituents, quotes):
    """Join one list with the shared quote map -> (rows, symbols_with_no_data)."""
    rows, missing = [], []
    for symbol, name in constituents:
        quote = quotes.get(f"{symbol}.NS")
        if not quote:
            missing.append(symbol)
            continue
        rows.append({"symbol": symbol, "name": name,
                     "mcap": quote["mcap"], "chg": quote["chg"]})
    return rows, missing


def split_into_bands(priced):
    """
    The four columns of the default view, with the nesting divided out.

    Returns [(title, rows, overlaps)] where `overlaps` marks the column that is
    not a size tier — SELECT is picked for liquidity, so it cuts across the
    other three instead of stacking under them.
    """
    in_50 = {row["symbol"] for row in priced["50"]}
    in_100 = {row["symbol"] for row in priced["100"]}
    return [
        ("SELECT 25 (F&O)", priced["SELECT"], True),
        ("CORE 50", priced["50"], False),
        ("BAND 51-100", [r for r in priced["100"] if r["symbol"] not in in_50], False),
        ("BAND 101-150", [r for r in priced["150"] if r["symbol"] not in in_100], False),
    ]


TIER_TITLES = ("CORE 50", "BAND 51-100", "BAND 101-150")


def _tier(member):
    """Which size band a stock's membership puts it in: 0 = the 50, 2 = the tail."""
    if "50" in member:
        return 0
    return 1 if "100" in member else 2


def split_into_groups(universe):
    """
    The 150 names bucketed by the exact set of indices they belong to.

    Sorting the keys puts the deepest membership first — a stock in all four
    indices leads, then the rest of that size band, then the band below it —
    which is the order that makes a column of ticks worth reading.
    """
    buckets = {}
    for row in universe:
        # `False` sorts first, so the F&O group heads each tier.
        buckets.setdefault((_tier(row["member"]), "SELECT" not in row["member"]),
                           []).append(row)
    groups = []
    for (tier, without_select), rows in sorted(buckets.items()):
        title = TIER_TITLES[tier] + ("" if without_select else " + F&O")
        groups.append((title, rows))
    return groups


# ---------------------------------------------------------------------------
# Shared cell work
# ---------------------------------------------------------------------------
def _ordered(rows):
    """Biggest first — the only ordering used inside a column."""
    return sorted(rows, key=lambda r: -r["mcap"])


def _weighted_change(rows):
    """A group's move, approximated by weighting each stock by market cap.

    NSE weights by *free-float* market cap, so this lands close to the printed
    index move without matching it exactly.
    """
    priced = [r for r in rows if r["chg"] is not None]
    total = sum(r["mcap"] for r in priced)
    if not total:
        return None
    return sum(r["mcap"] * r["chg"] for r in priced) / total


def _short_change(pct):
    """(plain, styled) for a 7-char day-change cell — the arrow does the sign.

    Two characters narrower than base.change_cell, keeping one space clear of
    the market-cap column beside it.
    """
    if pct is None or pct == 0:
        return f"{'n/a' if pct is None else '0.0%':>7}", \
            dim(f"{'n/a' if pct is None else '0.0%':>7}")
    text = f"{base.UP if pct > 0 else base.DOWN}{abs(pct):>4.1f}%"
    return f" {text}", (green if pct > 0 else red)(f" {text}")


class Layout:
    """
    How wide a stock column is drawn.

    Columns give up detail in a fixed order — the rank number first, then the
    day change shortens, then it goes altogether — so that a view can keep all
    of its columns in a single band rather than wrapping onto a second one.
    `rank_w` of 0 drops the rank; `chg` is 'full', 'short' or None.
    """

    __slots__ = ("rank_w", "mcap_w", "chg")

    def __init__(self, rank_w, mcap_w, chg):
        self.rank_w, self.mcap_w, self.chg = rank_w, mcap_w, chg

    @property
    def chg_w(self):
        return {"full": CHG_W, "short": 7, None: 0}[self.chg]

    def width(self, ticks=0):
        return ((self.rank_w + 1 if self.rank_w else 0) + SYM_W + self.mcap_w
                + self.chg_w + ticks * TICK_W)

    def change(self, pct):
        if not self.chg:
            return "", ""
        return (base.change_cell if self.chg == "full" else _short_change)(pct)

    def steps_down(self, keep_rank=False):
        """The same layout, one notch narrower — or None at the narrowest."""
        if self.rank_w and not keep_rank:
            return Layout(0, self.mcap_w, self.chg)
        if self.chg == "full":
            return Layout(self.rank_w, self.mcap_w, "short")
        if self.chg == "short":
            return Layout(self.rank_w, self.mcap_w, None)
        return None


def fit_columns(layout, count, ticks=0, keep_rank=False):
    """Narrow `layout` until `count` columns share one band, if that is possible."""
    term = _terminal_width()
    while count * layout.width(ticks) + (count - 1) * GAP > term:
        narrower = layout.steps_down(keep_rank)
        if narrower is None:
            return layout
        layout = narrower
    return layout


def _mcap_width(values):
    """Just wide enough for the biggest number these columns will print."""
    return max(len(base.crore(value)) for value in values) + 1


def _stock_line(rank, row, layout, suffix=("", "")):
    """'  1 LICI          523,077 ▲  0.3%' — plus whatever the view appends."""
    left = f"{rank:>{layout.rank_w}} " if layout.rank_w else ""
    left += (f"{row['symbol'][:SYM_W]:<{SYM_W}}"
             f"{base.crore(row['mcap']):>{layout.mcap_w}}")
    plain, styled = layout.change(row["chg"])
    return Line(left + plain + suffix[0], left + styled + suffix[1])


def _header(layout, tail=""):
    text = f"{'#':>{layout.rank_w}} " if layout.rank_w else ""
    text += f"{'SYMBOL':<{SYM_W}}{'MCAP Cr':>{layout.mcap_w}}"
    if layout.chg:
        text += f"{'CHG':>{layout.chg_w}}"
    return Line(text + tail, bold(text + tail))


def _wrapped(parts, width, joiner="  "):
    """Fit `parts` onto as few lines of `width` as they need."""
    lines, current = [], ""
    for part in parts:
        candidate = joiner.join(filter(None, (current, part)))
        if current and len(candidate) > width:
            lines.append(current)
            current = part
        else:
            current = candidate
    return lines + [current] if current else lines


def _terminal_width():
    return shutil.get_terminal_size((120, 40)).columns


# ---------------------------------------------------------------------------
# View 1: the four disjoint bands
# ---------------------------------------------------------------------------
def band_panel(title, rows, overlaps, universe_mcap, top, layout):
    """One band: heading, ranked names, then the band's whole-group totals."""
    shown = _ordered(rows)
    if top:
        shown = shown[:top]

    lines = [_header(layout)]
    for rank, row in enumerate(shown, 1):
        lines.append(_stock_line(rank, row, layout))

    # The totals always cover the whole band, however far the column was cut,
    # so the four columns stay comparable even at --top 25.
    width = layout.width()
    lines.append(Line(RULE * width, dim(RULE * width)))

    mcap = sum(r["mcap"] for r in rows)
    indent = f"{'':>{layout.rank_w + 1}}" if layout.rank_w else ""
    total = (f"{indent}{'ALL ' + str(len(rows)):<{SYM_W}}"
             f"{base.crore(mcap):>{layout.mcap_w}}")
    plain, styled = layout.change(_weighted_change(rows))
    lines.append(Line(total + plain, bold(total) + styled))

    share = f"{mcap / universe_mcap * 100:.1f}% of MIDCAP 150" if universe_mcap else ""
    if overlaps:
        share += " *"
    share = indent + share
    lines.append(Line(share, dim(share)))

    return Panel(title, lines)


def band_view(bands, universe_mcap, top):
    tallest = max(len(rows) for _, rows, _ in bands)
    shown = min(top, tallest) if top else tallest
    # Wide enough for the ALL row's total as well as the stocks above it.
    mcap_w = _mcap_width([row["mcap"] for _, rows, _ in bands for row in rows]
                         + [sum(r["mcap"] for r in rows) for _, rows, _ in bands])
    layout = fit_columns(Layout(max(2, len(str(shown))), mcap_w, "full"), len(bands))
    show_chg = bool(layout.chg)

    panels = [band_panel(title, rows, overlaps, universe_mcap, top, layout)
              for title, rows, overlaps in bands]
    base.print_side_by_side(panels, gap=GAP)

    notes = []
    if top:
        notes.append(f"First {top} of each column by market cap {RULE} "
                     f"--top 0 for every name.")
    notes.append("Columns 2-4 are disjoint: each stock appears once, and they add "
                 "up to MIDCAP 150.")
    notes.append("* SELECT 25 is a liquidity/derivatives pick, not a size tier, so "
                 "it overlaps the other three.")
    notes.append("ALL row = the whole column: count, market cap"
                 + (", market-cap-weighted day move." if show_chg else "."))
    if not show_chg:
        notes.append("Day-change column hidden — widen the terminal to bring it back.")
    return notes


# ---------------------------------------------------------------------------
# View 2: one column per membership pattern
#
# The bands answer "how big"; these answer "how deeply indexed" — the first
# column is every stock that sits in all four lists at once, and each column
# after it drops one rung. Because the pattern is fixed inside a column, the
# ticks only need printing once, in the sub-heading.
# ---------------------------------------------------------------------------
def group_subheading(rows, universe_mcap, layout):
    """
    What the pattern is and what it is worth, wrapped to the column.

    The sub-heading wraps to fit the column rather than setting its width — at
    six columns across, the stock rows are what the screen has room for.
    """
    mcap = sum(r["mcap"] for r in rows)
    ticks = " ".join(IN if key in rows[0]["member"] else OUT
                     for key in ("SELECT", "50", "100", "150"))
    money = [f"{base.crore(mcap)} Cr"]
    if universe_mcap:
        money.append(f"{mcap / universe_mcap * 100:.1f}%")
    if layout.chg:
        money.append(layout.change(_weighted_change(rows))[0].strip())
    return ([f"{ticks}  {len(rows)} name{'' if len(rows) == 1 else 's'}"]
            + _wrapped(money, layout.width()))


def group_panel(title, rows, subheading, top, layout):
    """One membership pattern: what it is, what it's worth, and who's in it."""
    shown = _ordered(rows)
    if top:
        shown = shown[:top]
    lines = [Line(text, dim(text)) for text in subheading]
    lines.append(_header(layout))
    for rank, row in enumerate(shown, 1):
        lines.append(_stock_line(rank, row, layout))
    return Panel(title, lines)


def group_view(groups, universe_mcap, top):
    tallest = max(len(rows) for _, rows in groups)
    shown = min(top, tallest) if top else tallest
    mcap_w = _mcap_width([row["mcap"] for _, rows in groups for row in rows])
    # Deepest-first is the point of this view, so the columns can't be reordered
    # to even out their heights — which makes wrapping them onto a second band
    # expensive, as one 49-row column would sit beside three one-name ones.
    # Better to spend the width: narrow the columns until all six fit across.
    layout = fit_columns(Layout(max(2, len(str(shown))), mcap_w, "full"), len(groups))

    # A sub-heading that wraps onto three lines in one column and two in the
    # next would leave the SYMBOL rows at different heights across the screen,
    # so they are all padded to the tallest.
    subheadings = [group_subheading(rows, universe_mcap, layout) for _, rows in groups]
    depth = max(len(sub) for sub in subheadings)
    panels = [group_panel(title, rows, sub + [""] * (depth - len(sub)),
                          top, layout)
              for (title, rows), sub in zip(groups, subheadings)]
    per_band = max(1, (_terminal_width() + GAP) // (layout.width() + GAP))
    per_band = math.ceil(len(panels) / math.ceil(len(panels) / per_band))
    for start in range(0, len(panels), per_band):
        base.print_side_by_side(panels[start:start + per_band], gap=GAP)

    notes = [f"Columns are exclusive membership patterns, deepest first: "
             f"{IN} = in that index, in S / 50 / 100 / 150 order.",
             "Every one of the 150 names appears in exactly one column.",
             "+ F&O = also in NIFTY MIDCAP SELECT 25, so column 1 is the stocks "
             "that sit in all four indices."]
    if top:
        notes.append(f"First {top} of each column by market cap {RULE} "
                     f"--top 0 for every name.")
    given_up = []
    if not layout.rank_w:
        given_up.append("rank numbers")
    if layout.chg == "short":
        given_up.append("a narrower day-change column")
    elif layout.chg is None:
        given_up.append("no day-change column")
    if given_up:
        notes.append(f"Columns squeezed to fit {per_band} across: "
                     f"{', '.join(given_up)} {RULE} widen the terminal for the rest.")
    return notes


# ---------------------------------------------------------------------------
# View 3: one row per stock, ticked against each index
# ---------------------------------------------------------------------------
TICK_HEADS = ("S", "50", "100", "150")


def _tick_cells(row):
    """(plain, styled) for the four membership columns of one stock."""
    plain, styled = "", ""
    for key in ("SELECT", "50", "100", "150"):
        mark = IN if key in row["member"] else OUT
        cell = f"{mark:>{TICK_W}}"
        plain += cell
        styled += cell if mark == IN else dim(cell)
    return plain, styled


def _tick_blocks(universe, order):
    """The rows in reading order, as blocks that earn a dividing rule between them."""
    if order != "depth":
        return [_ordered(universe)]
    # Deepest first: the fully ticked stocks lead, and each block below them
    # gives up one more index, so the ticks thin out down the column.
    return [_ordered(rows) for _, rows in split_into_groups(universe)]


def tick_view(universe, top, order):
    """The 150 names once each, split across as many columns as the screen fits."""
    blocks = _tick_blocks(universe, order)
    if top:  # the cut is on the whole screen, not on each block
        kept, blocks = 0, list(blocks)
        for index, block in enumerate(blocks):
            blocks[index] = block[:max(0, top - kept)]
            kept += len(blocks[index])
        blocks = [block for block in blocks if block]
    shown = sum(len(block) for block in blocks)

    # The rank is this view's spine — it is what tells you where in the 150 a
    # stock sits — so only the day change narrows here, and the columns give
    # way before it does.
    ticks = len(TICK_HEADS)
    layout = fit_columns(Layout(max(2, len(str(shown))),
                                _mcap_width([r["mcap"] for b in blocks for r in b]),
                                "full"),
                         MAX_CHUNKS, ticks, keep_rank=True)
    chunks = max(1, min(MAX_CHUNKS,
                        (_terminal_width() + GAP) // (layout.width(ticks) + GAP),
                        # A short list reads better in one column than as three
                        # stubs, so only split once there is enough to split.
                        math.ceil(shown / MIN_CHUNK)))

    # Rendered once, then dealt into columns — a rule between blocks travels
    # with the rows it separates, wherever the split happens to fall.
    entries, rank = [], 0
    for index, block in enumerate(blocks):
        if index:
            rule = RULE * layout.width(ticks)
            entries.append((None, Line(rule, dim(rule))))
        for row in block:
            rank += 1
            entries.append((rank, _stock_line(rank, row, layout, _tick_cells(row))))

    tail = "".join(f"{head:>{TICK_W}}" for head in TICK_HEADS)
    per_chunk = math.ceil(len(entries) / chunks)
    panels = []
    for start in range(0, len(entries), per_chunk):
        chunk = entries[start:start + per_chunk]
        # A rule that lands at the very top or bottom of a column separates
        # nothing, so it is dropped rather than left dangling.
        while chunk and chunk[0][0] is None:
            chunk.pop(0)
        while chunk and chunk[-1][0] is None:
            chunk.pop()
        ranks = [rank for rank, _ in chunk if rank]
        lines = [_header(layout, tail)] + [line for _, line in chunk]
        panels.append(Panel(f"#{ranks[0]}-{ranks[-1]}" if ranks else "", lines))
    base.print_side_by_side(panels, gap=GAP)

    ordering = {"depth": "Ordered by index depth: every index first, then one "
                         "fewer down each block.",
                "mcap": "Ranked by market cap."}[order]
    notes = [f"{IN} = in that index, {OUT} = not. S = MIDCAP SELECT 25 (F&O). "
             + ordering]
    if top:
        notes.append(f"First {top} of {len(universe)} {RULE} --top 0 for every name.")
    # The pattern worth hunting for: picked for the derivatives index, yet
    # outside the 100 — a free-float story rather than a size one.
    odd = [r["symbol"] for block in blocks for r in block
           if "SELECT" in r["member"] and "100" not in r["member"]]
    if odd:
        notes.append(f"In SELECT but outside MIDCAP 100: {', '.join(odd)} "
                     f"{RULE} NSE ranks on free float, this screen on full market cap.")
    if not layout.chg:
        notes.append("Day-change column hidden — widen the terminal to bring it back.")
    return notes


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="The NIFTY midcap universe with the index overlap divided out.")
    ap.add_argument("--view", choices=("bands", "groups", "ticks"), default="bands",
                    help="four disjoint size bands; one column per membership "
                         "pattern, the stocks in all four indices first; or one "
                         "row per stock with membership ticks (default: bands)")
    ap.add_argument("--top", type=int, default=None, metavar="N",
                    help="rows per column, 0 for every name "
                         "(default: 25 for bands, all for groups and ticks)")
    ap.add_argument("--sort", choices=("mcap", "depth"), default=None,
                    help="ticks view only: order the rows by market cap, or by how "
                         "many indices a stock is in — fully ticked first, thinning "
                         "out down the screen (default: depth). The other views "
                         "group by membership already and rank by market cap inside "
                         "that")
    args = ap.parse_args()
    top = (25 if args.view == "bands" else 0) if args.top is None else args.top
    order = args.sort or ("depth" if args.view == "ticks" else "mcap")

    print(dim("Fetching NSE constituents ..."), file=sys.stderr)
    try:
        lists = {key: fetch_constituents(csv_name) for key, csv_name in INDEX_FILES}
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not fetch constituents: {exc}")

    # The four lists overlap almost entirely, so quoting their union — 150
    # names — prices every column from a single point in time.
    symbols = sorted({sym for rows in lists.values() for sym, _ in rows})
    print(dim(f"Pulling live market data for {len(symbols)} stocks ..."), file=sys.stderr)
    try:
        quotes = base.fetch_quotes([f"{sym}.NS" for sym in symbols])
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not fetch market data: {exc}")

    priced, missing = {}, {}
    for key, _ in INDEX_FILES:
        priced[key], missing[key] = build_rows(lists[key], quotes)
    if not priced["150"]:
        sys.exit("No market data could be retrieved. Check your internet connection.")

    # MIDCAP 150 is the whole universe — the other three are drawn from it — so
    # every membership question and every share-of-total is answered against it.
    # The union is taken anyway, so a stock NSE puts in SELECT but not the 150
    # would still show up rather than quietly vanish.
    members = {key: {row["symbol"] for row in rows} for key, rows in priced.items()}
    universe = {}
    for key, _ in INDEX_FILES:
        for row in priced[key]:
            universe.setdefault(row["symbol"], dict(row))
    for symbol, row in universe.items():
        row["member"] = {key for key, syms in members.items() if symbol in syms}
    universe = list(universe.values())
    universe_mcap = sum(row["mcap"] for row in priced["150"])

    stamp = datetime.datetime.now().strftime("%a %d %b %Y  %H:%M:%S")
    print()
    print(bold(f"  NIFTY MIDCAP 150 UNIVERSE {RULE} LIVE SNAPSHOT") + dim(f"   {stamp}"))
    print()

    if args.view == "bands":
        notes = band_view(split_into_bands(priced), universe_mcap, top)
    elif args.view == "groups":
        notes = group_view(split_into_groups(universe), universe_mcap, top)
    else:
        notes = tick_view(universe, top, order)

    for note in notes:
        print(dim("  " + note))
    for key, _ in INDEX_FILES:
        if missing[key]:
            print(dim(f"  MIDCAP {key}: no data for {', '.join(missing[key])}."))


if __name__ == "__main__":
    main()
