#!/usr/bin/env python3
"""
nifty_marketcap_52w.py

One-shot dashboard (no prompts) laying four panels side by side:

    | NIFTY 100 market cap | MIDCAP 100 market cap | NIFTY 100 52W | MIDCAP 100 52W |

Two indices, 200 stocks: NIFTY 100 is the large-cap end (it is exactly NIFTY 50
plus NIFTY NEXT 50), and NIFTY MIDCAP 100 the tier below it.

Panels 3 and 4 each hold the Top 20 nearest the 52-WEEK LOW and the Top 20
nearest the 52-WEEK HIGH for that index.

"Nearest" is the % distance between the current price and the 52-week level,
so during market hours it reflects the live price and off-market / weekends it
reflects the last close — either way it's correct.

Data sources (all fetched with the standard library — NO pip install needed):
  * Constituent lists : NSE public CSVs (so the lists auto-update)
  * Prices / mkt cap  : Yahoo Finance public JSON API (one batched pass for
                        both indices)

Usage
-----
    python nifty_marketcap_52w.py            # full 100-name market-cap columns
    python nifty_marketcap_52w.py --top 25   # trim the market-cap columns
    python nifty_marketcap_52w.py --near 10  # 10 names per 52-week section
"""

import argparse
import csv
import datetime
import gzip
import http.cookiejar
import io
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if _USE_COLOR and os.name == "nt":
    os.system("")  # enable ANSI on Windows cmd.exe


def _wrap(code):
    def paint(text):
        return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text
    return paint


green = _wrap("32")
red = _wrap("31")
bold = _wrap("1")
dim = _wrap("2")
cyan = _wrap("36")


def _supports(char):
    """True if the terminal's encoding can actually print `char`."""
    try:
        char.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# Up/down triangles mark the near-high / near-low rows; fall back to +/- on
# terminals stuck with a non-Unicode encoding (e.g. legacy Windows code pages).
UP, DOWN = ("▲", "▼") if _supports("▲▼") else ("+", "-")
RULE = "─" if _supports("─") else "-"


# ---------------------------------------------------------------------------
# A tiny shared HTTP client (cookie-aware, gzip-aware)
# ---------------------------------------------------------------------------
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_cookiejar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookiejar))


def http_get(url, extra_headers=None):
    """GET a URL, returning decoded text. Raises urllib.error.* on failure."""
    headers = {"User-Agent": _UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    resp = _opener.open(req, timeout=30)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", "replace")


# ---------------------------------------------------------------------------
# Constituents (from NSE's archive CSVs)
# ---------------------------------------------------------------------------
NSE_CSV = {
    "NIFTY 100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY MIDCAP 100":
        "https://archives.nseindia.com/content/indices/ind_niftymidcap100list.csv",
}


def fetch_constituents(index_name):
    """Return list of (nse_symbol, company_name) for the given index."""
    text = http_get(NSE_CSV[index_name])
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("Symbol") or "").strip()
        name = (row.get("Company Name") or "").strip()
        if symbol:
            rows.append((symbol, name))
    if not rows:
        raise RuntimeError(f"No symbols parsed for {index_name} — CSV format may have changed.")
    return rows


# ---------------------------------------------------------------------------
# Market data (Yahoo Finance public JSON API — batched)
# ---------------------------------------------------------------------------
def _yahoo_crumb():
    """Prime a cookie and fetch the crumb token Yahoo requires for /quote."""
    try:
        http_get("https://fc.yahoo.com")
    except urllib.error.HTTPError:
        pass  # this call is only to set a cookie; a 4xx is fine
    return http_get("https://query1.finance.yahoo.com/v1/test/getcrumb").strip()


def fetch_quotes(symbols):
    """
    Return {yahoo_symbol: {price, high, low, mcap, chg}} for the given symbols,
    fetched in batches from Yahoo Finance. Symbols missing data are omitted.
    """
    crumb = _yahoo_crumb()
    if not crumb:
        raise RuntimeError("Could not obtain a Yahoo Finance token.")

    out = {}
    batch = 50
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        url = (
            "https://query1.finance.yahoo.com/v7/finance/quote"
            f"?symbols={urllib.parse.quote(','.join(chunk))}"
            f"&crumb={urllib.parse.quote(crumb)}"
        )
        data = json.loads(http_get(url, {"Accept": "application/json"}))
        for q in data.get("quoteResponse", {}).get("result", []):
            price = q.get("regularMarketPrice")
            high = q.get("fiftyTwoWeekHigh")
            low = q.get("fiftyTwoWeekLow")
            mcap = q.get("marketCap")
            chg = q.get("regularMarketChangePercent")
            if price and high and low and mcap:
                # `chg` is deliberately outside the guard: a flat stock reports
                # 0.0 and a pre-open one reports nothing, and neither should
                # drop the row from the report.
                out[q["symbol"]] = {"price": float(price), "high": float(high),
                                    "low": float(low), "mcap": float(mcap),
                                    "chg": None if chg is None else float(chg)}
    return out


def build_dataset(constituents, quotes):
    """Join one index's constituents with the shared quote map -> (rows, failures)."""
    rows, failures = [], []
    for sym, name in constituents:
        q = quotes.get(f"{sym}.NS")
        if not q:
            failures.append(sym)
            continue
        rows.append({
            "symbol": sym,
            "name": name,
            "price": q["price"],
            "high": q["high"],
            "low": q["low"],
            "mcap": q["mcap"],
            "change_pct": q["chg"],
            "pct_from_low": (q["price"] - q["low"]) / q["low"] * 100.0,
            "pct_from_high": (q["high"] - q["price"]) / q["high"] * 100.0,
        })
    return rows, failures


# ---------------------------------------------------------------------------
# Panel plumbing
#
# Every rendered line carries both its plain text (for width math) and its
# ANSI-styled text (for output), because escape codes would otherwise break
# every ljust/rjust in the layout.
# ---------------------------------------------------------------------------
class Line:
    __slots__ = ("plain", "styled")

    def __init__(self, plain, styled=None):
        self.plain = plain
        self.styled = plain if styled is None else styled

    def padded(self, width):
        return self.styled + " " * (width - len(self.plain))


class Panel:
    def __init__(self, title, lines):
        self.title = title
        self.lines = lines
        self.width = max([len(title)] + [len(l.plain) for l in lines])

    def render(self):
        """Header + rule + body, as Line objects, all padded to panel width."""
        head = Line(self.title, bold(cyan(self.title)))
        rule = Line(RULE * self.width, dim(RULE * self.width))
        return [head, rule] + self.lines


BLANK = Line("")


def print_side_by_side(panels, gap=3):
    """Print panels in bands that fit the terminal, stacking when too narrow."""
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


def _print_band(band, gap):
    height = max(len(lines) for lines, _ in band)
    sep = " " * gap
    for i in range(height):
        cells = []
        for lines, width in band:
            line = lines[i] if i < len(lines) else BLANK
            cells.append(line.padded(width))
        print(sep.join(cells).rstrip())
    print()


# ---------------------------------------------------------------------------
# Cell formatters
# ---------------------------------------------------------------------------
SYM_W = 11  # widest symbols in these indices are 10 chars (ADANIENSOL, AUROPHARMA)


def crore(mcap_rupees):
    """Rupee market cap -> crores, comma grouped (1 crore = 10,000,000)."""
    return f"{mcap_rupees / 1e7:,.0f}"


def change_cell(pct):
    """(plain, styled) for a fixed-width 8-char '▲   1.2%' day-change cell."""
    if pct is None:
        return f"{'n/a':>8}", dim(f"{'n/a':>8}")
    if pct == 0:
        return f"{'0.0%':>8}", dim(f"{'0.0%':>8}")
    # The triangle carries the sign, so the number itself is shown unsigned.
    text = f"{UP if pct > 0 else DOWN} {abs(pct):>4.1f}%"
    text = f"{text:>8}"
    return text, (green if pct > 0 else red)(text)


def _rank_width(count):
    """Width of the '#' column — a 100-name index needs three digits, not two."""
    return max(2, len(str(count)))


def pct_cell(pct, paint):
    """(plain, styled) for a fixed-width 7-char percentage cell."""
    text = f"{pct:>6.1f}%"
    return text, paint(text)


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------
def _short(index_name):
    """'NIFTY MIDCAP 100' -> 'MIDCAP 100', to keep panel headings compact."""
    return index_name[len("NIFTY "):] if index_name.startswith("NIFTY MIDCAP") else index_name


def market_cap_panel(index_name, data, top):
    ranked = sorted(data, key=lambda x: x["mcap"], reverse=True)[:top]
    rank_w = _rank_width(len(ranked))
    header = f"{'#':>{rank_w}} {'SYMBOL':<{SYM_W}}{'MCAP Cr':>11} {'CHG':>8}"
    lines = [Line(header, bold(header))]
    for rank, d in enumerate(ranked, 1):
        left = f"{rank:>{rank_w}} {d['symbol'][:SYM_W]:<{SYM_W}}{crore(d['mcap']):>11} "
        chg_plain, chg_styled = change_cell(d["change_pct"])
        lines.append(Line(left + chg_plain, left + chg_styled))
    return Panel(f"{index_name} {RULE} MARKET CAP", lines)


def _hilo_section(title, paint, rows, key, level_key, level_header, gap_header):
    rank_w = _rank_width(len(rows))
    header = (f"{'#':>{rank_w}} {'SYMBOL':<{SYM_W}}{'PRICE':>9}"
              f"{level_header:>9}{gap_header:>7}")
    lines = [Line(title, bold(paint(title))), Line(header, bold(header))]
    for rank, d in enumerate(rows, 1):
        left = (f"{rank:>{rank_w}} {d['symbol'][:SYM_W]:<{SYM_W}}{d['price']:>9,.0f}"
                f"{d[level_key]:>9,.0f}")
        gap_plain, gap_styled = pct_cell(d[key], paint)
        lines.append(Line(left + gap_plain, left + gap_styled))
    return lines


def fifty_two_week_panel(index_name, data, near):
    near_low = sorted(data, key=lambda x: x["pct_from_low"])[:near]
    near_high = sorted(data, key=lambda x: x["pct_from_high"])[:near]

    lines = _hilo_section(f"{DOWN} NEAREST 52-WEEK LOW", red, near_low,
                          "pct_from_low", "low", "52W LOW", "ABOVE")
    lines.append(BLANK)
    lines += _hilo_section(f"{UP} NEAREST 52-WEEK HIGH", green, near_high,
                           "pct_from_high", "high", "52W HIGH", "GAP")
    note = "(negative gap = above prior high)"
    lines += [BLANK, Line(note, dim(note))]
    return Panel(f"{index_name} {RULE} 52-WEEK", lines)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="NIFTY 100 / NIFTY MIDCAP 100 one-screen snapshot.")
    ap.add_argument("--top", type=int, default=100, metavar="N",
                    help="rows in each market-cap column (default: all 100)")
    ap.add_argument("--near", type=int, default=20, metavar="N",
                    help="rows in each 52-week section (default: 20)")
    args = ap.parse_args()

    indices = list(NSE_CSV)

    print(dim("Fetching NSE constituents ..."), file=sys.stderr)
    try:
        constituents = {name: fetch_constituents(name) for name in indices}
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not fetch constituents: {exc}")

    # One quote pass covers both indices, so the whole screen is a single
    # consistent point in time.
    all_symbols = [f"{sym}.NS" for name in indices for sym, _ in constituents[name]]
    print(dim(f"Pulling live market data for {len(all_symbols)} stocks ..."), file=sys.stderr)
    try:
        quotes = fetch_quotes(all_symbols)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not fetch market data: {exc}")

    datasets, failures = {}, {}
    for name in indices:
        datasets[name], failures[name] = build_dataset(constituents[name], quotes)
    if not any(datasets.values()):
        sys.exit("No market data could be retrieved. Check your internet connection.")

    stamp = datetime.datetime.now().strftime("%a %d %b %Y  %H:%M:%S")
    print()
    print(bold(f"  NIFTY 100 & NIFTY MIDCAP 100 {RULE} LIVE SNAPSHOT") + dim(f"   {stamp}"))
    print()

    # Market-cap columns first, then the 52-week columns, so the whole of one
    # kind of panel sits together however the terminal wraps the band.
    print_side_by_side(
        [market_cap_panel(_short(name), datasets[name], args.top) for name in indices]
        + [fifty_two_week_panel(_short(name), datasets[name], args.near) for name in indices]
    )

    for name in indices:
        got, total = len(datasets[name]), len(constituents[name])
        note = f"  {name}: {got}/{total} priced."
        if failures[name]:
            note += f" No data for: {', '.join(failures[name])}."
        print(dim(note))


if __name__ == "__main__":
    main()
