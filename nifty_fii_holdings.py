#!/usr/bin/env python3
"""
fii_holdings.py - which Nifty 100 stocks FIIs bought and sold last quarter.

    python3 fii_holdings.py            # both lists side by side, fitted to the window
    python3 fii_holdings.py --top 25   # 25 names per side
    python3 fii_holdings.py --all      # every stock, however long that runs

Bought and sold print as two panels on one screen. If the terminal is too
narrow to sit them next to each other they stack instead, so nothing is ever
cut off.

WHAT THE NUMBERS MEAN
---------------------
There is no daily stock-level FII feed in India. The quarterly shareholding
pattern is the only disclosure stating, per company, how much foreign
portfolio investors actually own. Companies file it within 21 days of quarter
end, so the quarter-on-quarter change IS what FIIs bought and sold over those
~90 days - the real thing, just reported quarterly rather than daily.

Figures are percent of shares outstanding, so changes are in percentage
POINTS, not rupees. +0.80pp means FIIs ended the quarter owning 0.8% more of
the company than they started with.

Being share-count based, price moves do NOT distort it. Anything changing the
share count DOES: a QIP, a big buyback or fresh issuance moves everyone's
percentage without a single share being traded.

Source: screener.in, which republishes the exchange filings.
Results cache for 7 days, so re-runs are instant. New filings land about 21
days after each quarter ends.

Requires: requests
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("This script needs `requests`.  Install it with:  pip install requests")

NIFTY100_CSV = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"
SCREENER = "https://www.screener.in/company/{}/"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fii_cache.json")
CACHE_MAX_AGE_DAYS = 7

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_DELAY = 1.2          # screener.in starts 429-ing if pushed harder
BACKOFF = (5, 15, 30)     # escalating waits when it does
QUARTER_ORDER = {"Mar": 1, "Jun": 2, "Sep": 3, "Dec": 4}


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch_nifty100() -> list[tuple[str, str]]:
    r = requests.get(NIFTY100_CSV, headers=HEADERS, timeout=30)
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        sym = (row.get("Symbol") or "").strip().upper()
        if sym:
            out.append((sym, (row.get("Company Name") or "").strip()))
    return out


def parse_shareholding(html_text: str):
    """Pull (quarter labels, FII percentages) from a screener company page."""
    start = html_text.find('id="quarterly-shp"')
    if start < 0:
        return None
    end = html_text.find("</table>", start)
    block = html_text[start:end if end > 0 else start + 20000]

    head_end = block.find("</thead>")
    quarters = [q.strip() for q in
                re.findall(r"<th[^>]*>([^<]+)</th>", block[:head_end]) if q.strip()]

    for row in block[head_end:].split("<tr"):
        label_match = re.search(r'<td class="text">(.*?)</td>', row, re.S)
        if not label_match:
            continue
        label = re.sub(r"<[^>]+>", " ", label_match.group(1))
        label = " ".join(label.split()).replace("&nbsp;", " ").strip(" +")
        if not label.upper().startswith("FII"):
            continue
        pcts = [float(v) for v in re.findall(r"<td>\s*([\d.]+)%\s*</td>", row)]
        if pcts:
            n = min(len(quarters), len(pcts))
            return quarters[-n:], pcts[-n:]
    return None


def sort_key(label: str) -> tuple[int, int]:
    parts = label.split()
    if len(parts) == 2 and parts[0] in QUARTER_ORDER:
        try:
            return (int(parts[1]), QUARTER_ORDER[parts[0]])
        except ValueError:
            pass
    return (0, 0)


# --------------------------------------------------------------------------
# Cache - expires on its own so new filings are never masked by stale data
# --------------------------------------------------------------------------

def load_cache() -> dict:
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        return {}
    stamp = cache.pop("_fetched_at", None)
    if not stamp:
        return {}
    try:
        age = (datetime.now() - datetime.fromisoformat(stamp)).days
    except ValueError:
        return {}
    if age >= CACHE_MAX_AGE_DAYS:
        print(f"  cache {age} days old, refetching", file=sys.stderr)
        return {}
    print(f"  using cached data ({age}d old)", file=sys.stderr)
    return cache


def save_cache(cache: dict) -> None:
    try:
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(dict(cache, _fetched_at=datetime.now().isoformat()), fh)
    except OSError:
        pass


def collect(symbols):
    cache = load_cache()
    rows, failed = [], []
    session = requests.Session()
    session.headers.update(HEADERS)

    for i, (sym, name) in enumerate(symbols, 1):
        if sym in cache:
            parsed = cache[sym]
        else:
            parsed = None
            # Retry through rate limiting rather than making the user tune a
            # delay flag - screener.in 429s unpredictably under a steady rate.
            for attempt in range(len(BACKOFF) + 1):
                try:
                    r = session.get(SCREENER.format(sym), timeout=30)
                except requests.RequestException as e:
                    failed.append(f"{sym} ({type(e).__name__})")
                    break
                if r.status_code == 200:
                    got = parse_shareholding(r.text)
                    parsed = {"quarters": got[0], "pcts": got[1]} if got else None
                    cache[sym] = parsed
                    break
                if r.status_code == 429 and attempt < len(BACKOFF):
                    wait = BACKOFF[attempt]
                    print(f"\r  [{i}/{len(symbols)}] {sym:<12} rate limited, "
                          f"waiting {wait}s ", end="", file=sys.stderr)
                    time.sleep(wait)
                    continue
                failed.append(f"{sym} (HTTP {r.status_code})")
                break
            time.sleep(BASE_DELAY)

        print(f"\r  [{i}/{len(symbols)}] {sym:<24}", end="", file=sys.stderr)

        if not parsed or len(parsed.get("pcts", [])) < 2:
            if not any(f.startswith(sym + " ") for f in failed):
                failed.append(f"{sym} (no FII data)")
            continue

        pairs = sorted(zip(parsed["quarters"], parsed["pcts"]),
                       key=lambda x: sort_key(x[0]))
        qs = [q for q, _ in pairs]
        ps = [p for _, p in pairs]
        rows.append({
            "symbol": sym, "name": name,
            "latest_q": qs[-1], "prev_q": qs[-2],
            "latest": ps[-1], "prev": ps[-2],
            "delta": round(ps[-1] - ps[-2], 2),
            "yr_delta": round(ps[-1] - ps[-5], 2) if len(ps) >= 5 else None,
            "history": ps[-8:],
        })

    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)
    save_cache(cache)
    return rows, failed


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

GREEN, RED, DIM, BOLD, RESET = ("\033[32m", "\033[31m",
                                "\033[2m", "\033[1m", "\033[0m")

COLOR = False   # decided once in main(); escape codes are noise in a redirect

SYM_W = 11      # widest Nifty 100 symbols are 10 characters
GAP = 3         # blank columns between the two panels


def supports_color() -> bool:
    """Colour only for a real terminal, so `> report.txt` stays clean."""
    return (sys.stdout.isatty()
            and os.environ.get("TERM") != "dumb"
            and "NO_COLOR" not in os.environ)


def style(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if COLOR else text


def arrow(v) -> str:
    if v is None:
        return "-"
    if v > 0:
        return f"▲ {v:+.2f}"
    if v < 0:
        return f"▼ {v:+.2f}"
    return f"= {v:+.2f}"


def paint(text: str, v) -> str:
    if v is None or v == 0:
        return text
    return style(text, GREEN if v > 0 else RED)


def moves(vals: list[float]) -> tuple[str, str]:
    """(plain, styled) arrows, one per quarter-on-quarter move, oldest first.

    Direction rather than level: a stake that only ever fell reads as a run of
    red whatever the absolute percentages, and nothing is scaled per stock, so
    two rows can be compared directly.
    """
    plain, styled = [], []
    for a, b in zip(vals, vals[1:]):
        # Rounded to the 2dp the table prints, so a move too small to show as a
        # number does not show as an arrow either.
        d = round(b - a, 2)
        ch = "▲" if d > 0 else "▼" if d < 0 else "="
        plain.append(ch)
        styled.append(paint(ch, d))
    return "".join(plain), "".join(styled)


# --------------------------------------------------------------------------
# Side-by-side layout
#
# Every rendered line carries both its plain text (for width maths) and its
# styled text (for output), because escape codes would otherwise break the
# ljust/rjust the columns depend on.
# --------------------------------------------------------------------------

class Line:
    __slots__ = ("plain", "styled")

    def __init__(self, plain: str, styled: str | None = None):
        self.plain = plain
        self.styled = plain if styled is None else styled

    def padded(self, width: int) -> str:
        return self.styled + " " * (width - len(self.plain))


class Panel:
    def __init__(self, title: str, lines: list[Line], tint=None):
        self.title = title
        self.lines = lines
        self.tint = tint
        self.width = max([len(title)] + [len(l.plain) for l in lines])

    def render(self) -> list[Line]:
        """Title + rule + body, as Lines, all measured against panel width."""
        head = Line(self.title, style(paint(self.title, self.tint), BOLD))
        rule = "-" * self.width
        return [head, Line(rule, style(rule, DIM))] + self.lines


BLANK = Line("")


def print_side_by_side(panels: list[Panel], gap: int = GAP) -> None:
    """Sit the panels next to each other, stacking if the window is narrow."""
    term_width = shutil.get_terminal_size((120, 40)).columns
    blocks = [(p.render(), p.width) for p in panels]

    band, band_width = [], 0
    for block in blocks:
        needed = block[1] + (gap if band else 0)
        if band and band_width + needed > term_width:
            _print_band(band, gap)
            band, band_width = [], 0
            needed = block[1]
        band.append(block)
        band_width += needed
    if band:
        _print_band(band, gap)


def _print_band(band, gap: int) -> None:
    height = max(len(lines) for lines, _ in band)
    sep = " " * gap
    for i in range(height):
        cells = [(lines[i] if i < len(lines) else BLANK).padded(width)
                 for lines, width in band]
        print("  " + sep.join(cells).rstrip())
    print()


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------

def panel(title: str, tint: int, rows: list[dict], limit: int,
          name_w: int) -> Panel:
    """One ranked list. name_w == 0 drops the company column on narrow windows."""
    header = f"{'#':>2} {'SYMBOL':<{SYM_W}}"
    if name_w:
        header += f"{'COMPANY':<{name_w}}"
    header += f"{'PREV%':>7}{'NOW%':>7}{'CHANGE':>9}{'1Y':>9}  {'8Q MOVES':<8}"
    lines = [Line(header, style(header, BOLD))]

    if not rows:
        lines.append(Line("   (none)", style("   (none)", DIM)))
        return Panel(title, lines, tint)

    for rank, r in enumerate(rows[:limit], 1):
        left = f"{rank:>2} {r['symbol'][:SYM_W]:<{SYM_W}}"
        if name_w:
            left += f"{r['name'][:name_w - 1]:<{name_w}}"
        left += f"{r['prev']:>7.2f}{r['latest']:>7.2f}"
        chg, yr = f"{arrow(r['delta']):>9}", f"{arrow(r['yr_delta']):>9}"
        mv_plain, mv_styled = moves(r["history"])
        lines.append(Line(left + chg + yr + "  " + mv_plain,
                          left + paint(chg, r["delta"])
                          + paint(yr, r["yr_delta"]) + "  " + mv_styled))

    hidden = len(rows) - min(limit, len(rows))
    if hidden:
        more = f"   ... {hidden} more (--all to see them)"
        lines.append(Line(more, style(more, DIM)))
    return Panel(title, lines, tint)


def fit_rows(term_lines: int) -> int:
    """Rows per panel that leave the banner, footer and notes on screen too."""
    return max(5, term_lines - 24)


def fit_name_width(term_width: int) -> int:
    """Company names only if both panels still fit next to each other."""
    bare = 2 + 1 + SYM_W + 7 + 7 + 9 + 9 + 2 + 8
    spare = (term_width - GAP - 2 - 2 * bare) // 2
    return min(22, spare) if spare >= 12 else 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="FII buying and selling across the Nifty 100, "
                    "bought and sold shown side by side.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--top", type=int, metavar="N",
                       help="names per side (default: as many as the window fits)")
    group.add_argument("--all", action="store_true",
                       help="every stock, ignoring the window height")
    args = ap.parse_args()

    global COLOR
    COLOR = supports_color()

    print("Fetching Nifty 100 list...", file=sys.stderr)
    try:
        symbols = fetch_nifty100()
    except requests.RequestException as e:
        sys.exit(f"Could not fetch the index list: {e}")

    print(f"Reading shareholding for {len(symbols)} stocks "
          f"(first run takes a few minutes)...", file=sys.stderr)
    rows, failed = collect(symbols)
    if not rows:
        sys.exit("No shareholding data could be parsed.")

    rows.sort(key=lambda r: r["delta"], reverse=True)
    bought = [r for r in rows if r["delta"] > 0]
    sold = [r for r in rows if r["delta"] < 0][::-1]

    latest_q = max((r["latest_q"] for r in rows), key=sort_key)
    prev_q = max((r["prev_q"] for r in rows), key=sort_key)

    term_width, term_lines = shutil.get_terminal_size((120, 40))
    width = min(term_width - 2, 118)

    print("\n" + "=" * width)
    print(f"  FII ACTIVITY IN NIFTY 100   {prev_q} -> {latest_q}   "
          f"({len(rows)} stocks)")
    print("=" * width)
    print("  Change is in percentage POINTS of shares outstanding, not rupees.\n")

    if args.all:
        limit = len(rows)
    elif args.top:
        limit = args.top
    else:
        limit = fit_rows(term_lines)
    name_w = fit_name_width(term_width)

    print_side_by_side([
        panel(f"▲ FIIs BOUGHT  ({len(bought)} stocks, biggest rise first)",
              1, bought, limit, name_w),
        panel(f"▼ FIIs SOLD  ({len(sold)} stocks, biggest fall first)",
              -1, sold, limit, name_w),
    ])

    avg = sum(r["delta"] for r in rows) / len(rows)
    print(f"  Breadth: FIIs raised stake in "
          f"{paint(str(len(bought)), 1)}, cut in "
          f"{paint(str(len(sold)), -1)}, unchanged in "
          f"{len(rows) - len(bought) - len(sold)}.")
    print(f"  Average change across the index: {paint(f'{avg:+.2f}pp', avg)}")

    if failed:
        print(f"\n  Not covered ({len(failed)}): {', '.join(failed[:10])}"
              f"{' ...' if len(failed) > 10 else ''}")

    print("\n" + "-" * width)
    print("  READ WITH CARE")
    print("  - QIPs, buybacks and fresh issuance shift these percentages with")
    print("    nobody trading. Verify any unusually large move before trusting it.")
    print("  - Much FII stake in large caps is passive (MSCI/FTSE/ETF), so a")
    print("    rise can be an index weight change rather than a decision.")
    print("  - One quarter is one data point. 8Q MOVES gives one arrow per")
    print("    quarter-on-quarter move, oldest on the left, because a run of")
    print("    six red arrows says far more than any single delta.")
    print("-" * width)


if __name__ == "__main__":
    main()
