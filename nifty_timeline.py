#!/usr/bin/env python3
"""
nifty_index_changes.py

Shows every reshuffle of an NSE index — with the date it actually took effect:
  * NIFTY 50
  * NIFTY NEXT 50
  * NIFTY MIDCAP 100

How it works — nothing is hard-coded, it's all worked out at run time. NSE
Indices Ltd announces every change in a press release at
niftyindices.com/Press_Release/ind_prs<DDMMYYYY>.pdf. This script finds which
of those exist in the period you ask for, reads them, and pulls out the
inclusion/exclusion tables for the index you picked.

Indices are reconstituted by NSE Indices Ltd (SEBI is only the regulator):
  * Twice a year   -> effective end of MARCH and SEPTEMBER, announced ~5 weeks earlier
  * Ad-hoc anytime -> for mergers, demergers and delistings
Both kinds are picked up, because both get a press release.

The first run has to check every business day in the period and takes a couple
of minutes. Everything it learns is cached under ~/.cache/nifty_index_changes/,
and a past date can never gain a new press release, so later runs are instant.

Usage
-----
    python nifty_index_changes.py            # interactive, last 5 years
    python nifty_index_changes.py --years 3  # shorter window
    python nifty_index_changes.py --refresh  # ignore the cache and re-scan

Uses only the Python standard library — no pip install needed.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, timedelta

# --- Source ----------------------------------------------------------------
PRESS_RELEASE = "https://www.niftyindices.com/Press_Release/ind_prs{stamp}{suffix}.pdf"

# Menu label -> how the index is headed inside a press release
INDICES = ["NIFTY 50", "NIFTY NEXT 50", "NIFTY MIDCAP 100"]

DEFAULT_YEARS = 5
CACHE_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "nifty_index_changes", "press_releases.json",
)
CACHE_VERSION = 3
# Bumped whenever the parser changes, to re-read the stored releases without
# throwing away the far more expensive record of which dates have one.
PARSER_VERSION = 5
# Recent days are re-checked every run, in case a release was published late.
RECHECK_DAYS = 14

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
WORKERS = 24
# Layout of the table: the timeline column on the left, and the narrowest an
# index column can get before stock names are cropped past being readable.
WHEN_WIDTH = 16
MIN_COLUMN = 24
# Page 1 carries the title, and always sits well inside this many bytes — so
# deciding "is this a reshuffle?" never pulls down a whole PDF.
PROBE_BYTES = 24 * 1024


# --- Colors (green for added, red for removed) -----------------------------
# Only emit ANSI codes when writing to a real terminal that isn't NO_COLOR'd,
# so piping to a file or another program stays clean.
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# On Windows, enable ANSI escape processing so colors render in cmd.exe too
# (Windows Terminal / PowerShell already support it). Harmless elsewhere.
if _USE_COLOR and os.name == "nt":
    os.system("")


def green(text):
    return f"\033[32m{text}\033[0m" if _USE_COLOR else text


def red(text):
    return f"\033[31m{text}\033[0m" if _USE_COLOR else text


def dim(text):
    return f"\033[2m{text}\033[0m" if _USE_COLOR else text


def _supports(char):
    """True if the terminal's encoding can actually print `char`."""
    try:
        char.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# Up/down triangles mark additions and removals; fall back to +/- on terminals
# stuck with a non-Unicode encoding (e.g. legacy Windows code pages).
UP, DOWN = ("▲", "▼") if _supports("▲▼") else ("+", "-")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _request(url, method="GET", byte_range=None, timeout=30):
    headers = {"User-Agent": UA}
    if byte_range:
        headers["Range"] = f"bytes=0-{byte_range - 1}"
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.headers, response.read() if method == "GET" else b""


def press_release_exists(url):
    """
    True if `url` is a real PDF.

    niftyindices.com answers a missing press release with 200 and the site's
    HTML shell rather than a 404, so existence is judged by content type.
    """
    try:
        headers, _ = _request(url, method="HEAD", timeout=15)
    except Exception:
        return False
    return "pdf" in (headers.get("Content-Type") or "").lower()


# ---------------------------------------------------------------------------
# PDF text extraction (stdlib only)
#
# The press releases are text PDFs whose content streams are Flate-compressed,
# so zlib plus a scan for literal strings is enough — no PDF library needed.
# ---------------------------------------------------------------------------
def _literal_strings(stream):
    """Collect the (…) literal strings a PDF content stream draws as text."""
    out = bytearray()
    i, n = 0, len(stream)
    while i < n:
        if stream[i] != 0x28:  # not '('
            i += 1
            continue
        depth, i = 1, i + 1
        while i < n and depth:
            char = stream[i]
            if char == 0x5C:  # backslash escape: take the next byte verbatim
                if i + 1 < n:
                    out.append(stream[i + 1])
                i += 2
                continue
            if char == 0x28:
                depth += 1
            elif char == 0x29:
                depth -= 1
                if not depth:
                    i += 1
                    break
            out.append(char)
            i += 1
    return bytes(out)


def _looks_like_prose(chunk):
    """
    Guard against image and font streams that happen to contain 'Tj'.

    Their literal strings decode to binary noise, so require that most of what
    came out is printable before trusting it.
    """
    if len(chunk) < 8:
        return False
    printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(chunk) > 0.9


def pdf_text(data):
    """Best-effort plain text of a PDF, or of a truncated prefix of one."""
    parts = []
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        end = data.find(b"endstream", start)
        raw = data[start:end if end > 0 else len(data)]
        if not raw or len(raw) > 400_000:  # images etc. — never body text
            continue
        try:
            # decompressobj tolerates the truncated tail of a prefix download
            decoded = zlib.decompressobj().decompress(raw)
        except zlib.error:
            continue
        if b"Tj" not in decoded and b"TJ" not in decoded:
            continue
        chunk = _literal_strings(decoded)
        if not _looks_like_prose(chunk):
            continue
        parts.append(chunk)

    text = b"\n".join(parts).decode("latin-1")
    text = text.replace("en-US", " ").replace("en-IN", " ")  # exporter artefacts
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Press release parsing
# ---------------------------------------------------------------------------
# The standard title of an announcement that moves stocks between indices.
REPLACEMENT_TITLE = re.compile(r"Replacements?\s+in\s+indices", re.I)
# Mergers, demergers and delistings get a different release: one stock, and a
# plain list of the indices it enters or leaves. Missing these would leave gaps
# — the Tata Motors demerger and the Jio Financial spin-off are both this kind.
CORPORATE_TITLE = re.compile(
    r"\b(Inclusion|Exclusion)\s+of\s+(.+?)\s+(?:from|in|to)\s+(?:the\s+)?"
    r"(?:Nifty|NSE)?\s*indices", re.I)
INDEX_TABLE_RE = re.compile(
    r"Sr\.\s*No\.\s*Index\s*Name(.*?)(?:About\s+NSE|Note\s*:|Disclaimer|$)", re.I | re.S)
EFFECTIVE_RE = re.compile(r"effective\s+from\s+([A-Z][a-z]+\s+\d{1,2},\s*\d{4})", re.I)
ANNOUNCED_RE = re.compile(r"Mumbai,\s*([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")
# Each index gets its own labelled block. NSE alternates between numbering
# ("1) Nifty 50") and lettering ("a) Nifty 50") from one release to the next.
SECTION_RE = re.compile(
    r"(?:\d+|[a-z])\)\s*(Nifty[A-Za-z0-9 &()./\-]*?)\s+(?=The following|No changes?|No stocks?)",
    re.I,
)
EXCLUDED_RE = re.compile(r"following[^.]{0,60}?being\s+excluded\s*:", re.I)
INCLUDED_RE = re.compile(r"following[^.]{0,60}?being\s+included\s*:", re.I)
# A table ends where the next sentence or footnote begins. The footnote branch
# ("* On account of …") must stay case-SENSITIVE: it relies on an uppercase
# letter followed by a lowercase one, so that a symbol like "Ltd.* MINDTREE"
# isn't mistaken for the start of a footnote. Hence the scoped (?i:…) rather
# than a re.I flag over the whole pattern.
TABLE_END_RE = re.compile(
    r"(?i:The following|The above|Note\s*:|(?:\d+|[a-z])\)\s*Nifty|B\.\s|C\.\s)"
    r"|\*\s*[A-Z][a-z]"
)
NO_CHANGE_RE = re.compile(r"No changes?\s+(?:are|is)\s+being\s+made", re.I)
# The semi-annual reconstitution. The broad market indices — Nifty 50, Next 50
# and Midcap 100 among them — are always covered by this part of a release, so
# if one of them is absent from such a release it was left untouched — worth
# saying, rather than leaving a silent gap in the list.
BROAD_REVIEW_RE = re.compile(r"semi-?annual review of broad market indices", re.I)

# A company name ends in one of these before its NSE symbol starts. Anchoring
# on them keeps footnotes and stray sentences out of the parsed row, and stops
# names that open with capitals (ICICI, REC, SRF …) being read as the symbol.
NAME_END = re.compile(
    r"\b(?:Ltd\.?|Limited|India|Bank|Corporation|Industries|Company|Services|Enterprises)"
    r"\*?\s+([A-Z][A-Z0-9&\-]{1,14})\*?(?=\s|$)"
)
TRAILING_SYMBOL = re.compile(r"\s([A-Z][A-Z0-9&\-]{1,14})\*?\s*$")


def _key(name):
    """'Nifty Next 50' / 'NIFTY NEXT 50' -> 'niftynext50' so headings match."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _split_name_and_symbol(cell):
    """'Shriram Finance Ltd. SHRIRAMFIN <prose…>' -> 'Shriram Finance Ltd. (SHRIRAMFIN)'."""
    match = NAME_END.search(cell) or TRAILING_SYMBOL.search(cell)
    if not match:
        return cell.strip() or None
    name = cell[: match.start(1)].strip().rstrip("*").strip()
    return f"{name} ({match.group(1)})" if name else match.group(1)


def _walk_serials(segment):
    """
    Split a numbered table into its cells by following the serial numbers.

    Walking 1, 2, 3… in order — rather than splitting on any digit — keeps
    company names that contain numbers ("3M India Ltd.") in one piece.
    """
    cells, position, serial = [], 0, 1
    while True:
        start = re.compile(rf"(?<![\d.]){serial}\s+(?=[A-Za-z0-9])").search(segment, position)
        if not start:
            break
        nxt = re.compile(rf"(?<![\d.]){serial + 1}\s+(?=[A-Za-z0-9])").search(segment, start.end())
        cells.append(segment[start.end(): nxt.start() if nxt else len(segment)].strip())
        position, serial = start.end(), serial + 1
    return cells


def _parse_table(segment):
    """Read an 'Sr. No. / Company Name / Symbol' table into ["Name (SYMBOL)", …]."""
    segment = re.sub(r"Sr\.\s*No\.\s*Company Name\s*Symbol", " ", segment, flags=re.I)
    end = TABLE_END_RE.search(segment)
    if end:
        segment = segment[: end.start()]
    return [row for row in (_split_name_and_symbol(c) for c in _walk_serials(segment)) if row]


def _as_iso(match):
    if not match:
        return None
    cleaned = re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", match.group(1)))
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_corporate_action(text, url):
    """
    Parse the one-stock kind of release: "Exclusion of X from Nifty indices".

    The body names the stock once and then simply lists the indices it enters
    or leaves, so the shape is nothing like a replacement table.
    """
    title = CORPORATE_TITLE.search(text)
    table = INDEX_TABLE_RE.search(text)
    if not title or not table:
        return None

    # These releases recap the earlier announcement first ("…had announced
    # inclusion … effective from July 20") before giving the date that matters,
    # so read the date from after the decision sentence where there is one.
    tail = text[title.end():]
    decided = re.search(r"has\s+decided\s+to", tail, re.I)
    effective = _as_iso(EFFECTIVE_RE.search(tail[decided.end():] if decided else tail))
    if not effective:
        return None

    named = re.sub(r"\s+", " ", title.group(2)).strip(" ,.")
    named = re.sub(r"\s*\((?:formerly|erstwhile)[^)]*\)", "", named, flags=re.I).strip()
    # One release can move several stocks ("Exclusion of X Ltd. and Y Ltd."), so
    # split on the separators that follow a company suffix. Splitting on every
    # "and" would tear "Vedanta Iron and Steel Ltd." in half.
    companies = [c.strip(" ,.") for c in
                 re.split(r"(?<=Ltd\.)\s*(?:,|and)\s+|(?<=Ltd)\s*(?:,|and)\s+"
                          r"|(?<=Limited)\s*(?:,|and)\s+", named) if c.strip(" ,.")]

    labels = []
    for company in companies:
        # Tie the symbol to THIS company by name, and require it to follow
        # closely — otherwise a release listing four demerged entities staples
        # the first symbol it finds onto whichever name came first.
        anchor = re.escape(" ".join(company.split()[:2]))
        symbol = re.search(anchor + r"[^()]{0,60}?\(([A-Z][A-Z0-9&\-]{1,14})\)", text)
        labels.append(f"{company} ({symbol.group(1)})" if symbol else company)

    joining = title.group(1).lower() == "inclusion"
    sections = {}
    for name in _walk_serials(table.group(1)):
        if name.lower().startswith("nifty"):
            sections[_key(name)] = {
                "added": labels if joining else [],
                "removed": [] if joining else labels,
            }
    if not sections:
        return None
    return {
        "effective": effective,
        "announced": _as_iso(ANNOUNCED_RE.search(text)),
        "periodic": False,
        "broad_review": False,
        "url": url,
        "sections": sections,
    }


def parse_press_release(text, url):
    """
    Turn one press release into {index_key: {added, removed}} plus its dates.

    An index the release explicitly leaves alone is recorded with empty lists,
    so a quiet review reads as "no change" instead of vanishing.
    """
    effective = _as_iso(EFFECTIVE_RE.search(text))
    if not effective:
        return None

    marks = [(m.start(), m.end(), m.group(1).strip()) for m in SECTION_RE.finditer(text)]
    sections = {}
    for i, (_, body_start, name) in enumerate(marks):
        body = text[body_start: marks[i + 1][0] if i + 1 < len(marks) else len(text)]
        key = _key(name)
        if key in sections:  # an index can reappear under a later heading
            continue
        excluded = EXCLUDED_RE.search(body)
        included = INCLUDED_RE.search(body)
        if not excluded and not included:
            if NO_CHANGE_RE.search(body[:200]):
                sections[key] = {"added": [], "removed": []}
            continue
        sections[key] = {
            "added": _parse_table(body[included.end():]) if included else [],
            "removed": _parse_table(body[excluded.end():]) if excluded else [],
        }

    broad_review = bool(BROAD_REVIEW_RE.search(text))
    if not sections and not broad_review:
        return None
    return {
        "effective": effective,
        "announced": _as_iso(ANNOUNCED_RE.search(text)),
        "periodic": broad_review or bool(re.search(r"periodic review", text, re.I)),
        "broad_review": broad_review,
        "url": url,
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Finding the press releases
# ---------------------------------------------------------------------------
def business_days(since, until):
    days, day = [], since
    while day <= until:
        if day.weekday() < 5:  # NSE Indices publishes on business days
            days.append(day)
        day += timedelta(days=1)
    return days


def _probe_day(day):
    """The URLs of every press release published on `day`."""
    stamp = day.strftime("%d%m%Y")
    base = PRESS_RELEASE.format(stamp=stamp, suffix="")
    if not press_release_exists(base):
        return []
    urls = [base]
    # Busy days add ind_prs<date>_1.pdf, _2.pdf … Stop at the first gap.
    for n in range(1, 6):
        extra = PRESS_RELEASE.format(stamp=stamp, suffix=f"_{n}")
        if not press_release_exists(extra):
            break
        urls.append(extra)
    return urls


def _is_reshuffle(url):
    """Judged from page 1 alone, fetched as a range request."""
    try:
        _, prefix = _request(url, byte_range=PROBE_BYTES)
    except Exception:
        return url, False
    if not prefix.startswith(b"%PDF"):
        return url, False
    title = pdf_text(prefix)
    return url, bool(REPLACEMENT_TITLE.search(title) or CORPORATE_TITLE.search(title))


def _fetch_event(url):
    try:
        _, data = _request(url, timeout=90)
    except Exception:
        return url, None
    if not data.startswith(b"%PDF"):
        return url, None
    text = pdf_text(data)
    return url, parse_press_release(text, url) or parse_corporate_action(text, url)


def _map(function, items, label):
    """Run `function` over `items` in parallel, showing progress."""
    results = []
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        for i, result in enumerate(pool.map(function, items), 1):
            results.append(result)
            if sys.stdout.isatty() and (i % 25 == 0 or i == len(items)):
                sys.stdout.write(f"\r  {label}: {i}/{len(items)}   ")
                sys.stdout.flush()
    if items and sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 60 + "\r")
    return results


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as handle:
            cache = json.load(handle)
        if cache.get("version") == CACHE_VERSION:
            if cache.get("parser") != PARSER_VERSION:
                cache["events"] = {}  # keep the day scan, re-read the releases
                cache["parser"] = PARSER_VERSION
            return cache
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "parser": PARSER_VERSION, "days": {}, "events": {}}


def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(cache, handle)
    except OSError:
        pass  # a read-only home directory just means no speed-up next time


def collect_events(since, until, refresh=False):
    """Every reshuffle announced between `since` and `until`, newest first."""
    empty = {"version": CACHE_VERSION, "parser": PARSER_VERSION, "days": {}, "events": {}}
    cache = empty if refresh else load_cache()
    recent = (date.today() - timedelta(days=RECHECK_DAYS)).isoformat()

    days = business_days(since, until)
    unknown = [d for d in days if d.isoformat() not in cache["days"] or d.isoformat() >= recent]
    if unknown:
        print(dim(f"  checking {len(unknown)} publication dates at niftyindices.com…"))
        for day, urls in zip(unknown, _map(_probe_day, unknown, "dates checked")):
            cache["days"][day.isoformat()] = urls
        save_cache(cache)

    published = [u for d in days for u in cache["days"].get(d.isoformat(), [])]
    unread = [u for u in published if u not in cache["events"]]
    if unread:
        reshuffles = [u for u, hit in _map(_is_reshuffle, unread, "releases scanned") if hit]
        for url in unread:
            cache["events"][url] = None  # remembered as "not a reshuffle"
        for url, event in _map(_fetch_event, reshuffles, "reshuffles read"):
            cache["events"][url] = event
        save_cache(cache)

    events = [e for u, e in cache["events"].items() if e and u in published]
    events.sort(key=lambda e: (e["effective"], e["url"]), reverse=True)
    return events


def index_events(events, index_name, since):
    """The events touching `index_name` that took effect on or after `since`."""
    key = _key(index_name)
    out = []
    for event in events:
        if datetime.strptime(event["effective"], "%Y-%m-%d").date() < since:
            continue
        section = event["sections"].get(key)
        if section is None:
            # A reconstitution that doesn't list this index left it unchanged.
            if not event.get("broad_review"):
                continue
            section = {"added": [], "removed": []}
        out.append({**event, **section})
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _shorten(stock, width):
    """'Bharat Electronics Ltd. (BEL)' -> fits `width`, symbol kept intact."""
    stock = re.sub(r"\s+(?:Ltd\.?|Limited)\s*(?=\()", " ", stock)
    if len(stock) <= width:
        return stock
    symbol = re.search(r"\s*\([A-Z0-9&\-]+\)$", stock)
    if symbol and len(symbol.group(0)) < width - 2:
        keep = width - len(symbol.group(0)) - 1
        return stock[:keep].rstrip() + "…" + symbol.group(0)
    return stock[: width - 1].rstrip() + "…"


def _event_lines(event, width):
    """The (text, colour) lines that make up one index's cell for one date."""
    # Colour is applied after padding, so the text here must stay plain —
    # otherwise the escape codes get counted as visible width.
    if event is None:
        return [("—", dim)]
    if not event["added"] and not event["removed"]:
        return [("no change to this index", dim)]
    lines = []
    for stock in event["added"]:
        lines.append((f"{UP} {_shorten(stock, width - 2)}", green))
    for stock in event["removed"]:
        lines.append((f"{DOWN} {_shorten(stock, width - 2)}", red))
    return lines


def _when_lines(event):
    """The timeline cell: effective date, what kind of change, when announced."""
    effective = datetime.strptime(event["effective"], "%Y-%m-%d").date()
    lines = [(f"{effective:%d %b %Y}", None),
             ("periodic review" if event["periodic"] else "ad-hoc change", dim)]
    if event["announced"]:
        announced = datetime.strptime(event["announced"], "%Y-%m-%d").date()
        lines.append((f"ann. {announced:%d %b %Y}", dim))
    return lines


def _pad(cell, width):
    """Colour a cell only after padding, so escape codes don't count as width."""
    text, paint = cell
    text = text[:width]
    return (paint(text) if paint else text) + " " * max(0, width - len(text))


def _natural_width(events, dates, cap):
    """The widest line this index actually prints, never more than `cap`."""
    widest = 0
    for effective in dates:
        event = next((e for e in events if e["effective"] == effective), None)
        for text, _ in _event_lines(event, cap):
            widest = max(widest, len(text))
    return widest


def _column_widths(by_index, names, dates, available):
    """Give each index the room it needs, sharing out only what's short."""
    wanted = [max(len(name), _natural_width(by_index[name], dates, available))
              for name in names]
    if sum(wanted) <= available:
        return wanted
    # Too wide to show every index in full: hand out the space smallest-first,
    # so a column that asks for little keeps all of its content and only the
    # greedy ones get trimmed — rather than shrinking every column alike.
    widths, left_over = [0] * len(wanted), available
    order = sorted(range(len(wanted)), key=lambda i: wanted[i])
    for taken, i in enumerate(order):
        widths[i] = min(wanted[i], left_over // (len(order) - taken))
        left_over -= widths[i]
    return widths


def show_table(by_index, title):
    """Timeline down the left, one column per index."""
    names = list(by_index)
    # Rows line up by effective date, so the indices stay in step even when a
    # change touched only one of them.
    dates = sorted({e["effective"] for events in by_index.values() for e in events}, reverse=True)
    if not dates:
        print("No reshuffle found in this period.")
        return

    terminal = shutil.get_terminal_size((100, 24)).columns
    # Columns are measured against their contents, so a handful of short stock
    # names never sits in a column padded out to a full share of the window.
    widths = _column_widths(by_index, names, dates,
                            terminal - WHEN_WIDTH - 3 * len(names))
    all_widths = [WHEN_WIDTH] + widths

    print(f"NSE index reshuffles — {title}\n")
    header = " │ ".join([f"{'WHEN':<{WHEN_WIDTH}}"]
                        + [f"{name:<{width}}" for name, width in zip(names, widths)])
    print(header.rstrip())
    # Every column is fenced off by "─┼─", except the last, which has no
    # padding space to its right.
    rule = "┼".join(["─" * (WHEN_WIDTH + 1)]
                    + ["─" * (width + 2) for width in widths[:-1]]
                    + ["─" * (widths[-1] + 1)])
    print(rule)

    for position, effective in enumerate(dates):
        picked = [next((e for e in by_index[name] if e["effective"] == effective), None)
                  for name in names]
        columns = [_when_lines(next(e for e in picked if e))]
        columns += [_event_lines(event, width) for event, width in zip(picked, widths)]

        if position:
            print(dim(rule))
        for i in range(max(len(column) for column in columns)):
            cells = [column[i] if i < len(column) else ("", None) for column in columns]
            row = " │ ".join(_pad(cell, width) for cell, width in zip(cells, all_widths))
            print(row.rstrip())

    print(dim("\nDates are NSE's effective dates, read from its press releases."))


def show_stacked(by_index, title):
    """Fallback for narrow terminals: one index after the other."""
    for name, events in by_index.items():
        print(f"{name} — {title}\n")
        for event in events:
            when = _when_lines(event)
            print(f"{when[0][0]}   {dim(', '.join(text for text, _ in when[1:]))}")
            for text, paint in _event_lines(event, 60):
                print(f"    {paint(text) if paint else text}")
            print()
    print(dim("Dates are NSE's effective dates, read from its press releases."))


def main():
    parser = argparse.ArgumentParser(
        description="Every reshuffle of NIFTY 50, NIFTY NEXT 50 and NIFTY MIDCAP 100, with "
                    "the date it took effect, read from NSE Indices' press releases at run time.")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                        help=f"how far back to look (default: {DEFAULT_YEARS})")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the cache and scan again from scratch")
    args = parser.parse_args()

    since = date.today() - timedelta(days=365 * args.years)
    title = f"last {args.years} years"

    try:
        # Announcements run ~5 weeks ahead of the effective date, so the scan
        # starts before the window to catch the release for its first change.
        events = collect_events(since - timedelta(days=70), date.today(), args.refresh)
        print()
        by_index = {name: index_events(events, name, since) for name in INDICES}
        # Side by side only while every index still gets a readable column;
        # below that the stacked view says more than a row of stubs.
        room = WHEN_WIDTH + (MIN_COLUMN + 3) * len(INDICES)
        if shutil.get_terminal_size((100, 24)).columns >= room:
            show_table(by_index, title)
        else:
            show_stacked(by_index, title)
    except KeyboardInterrupt:
        print("\nBye!")
    except urllib.error.URLError as exc:
        print(f"\nNetwork problem while talking to NSE: {exc.reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
