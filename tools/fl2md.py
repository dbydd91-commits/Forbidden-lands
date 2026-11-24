#!/usr/bin/env python3
"""Forbidden Lands PDF -> one LLM-friendly Markdown file per book.

Not a layout copy: the goal is text an LLM can grep, read and quote reliably.

Output conventions
  - `<!-- p.N -->` = start of printed page N (the books' own "see page N").
    If a paragraph runs over the page break the anchor sits inline in it.
  - `#`..`####` = PDF bookmarks (the book's outline). `#####` = other in-page headings.
  - Blocks get typed headings, so `grep -n 'Table: \\|Box: \\|Stats: '` lists them all:
      Table: X  -> GFM table, one row per line, cells on one line
      Stats: X  -> list of `- **Key:** value` (monster / NPC stat boxes, kin info boxes)
      Box: X    -> sidebar as a `>` quote, placed at the next paragraph break
  - Dice symbols: ⚔ = success (sword), ☠ = bane (skull).
  - Small-caps game terms keep the book's CAPITALS (MOVE, NEAR, COLD).
  - Dropped: running heads/feet, page numbers, purchase watermark, decorative art.

How it works (no pymupdf4llm): each PDF text line is classified by font.
IM Fell = prose; Branding = tables / boxes / stat blocks. Branding lines are
clustered into regions; a region with a bold all-caps column-header row is a
table (columns from header x, rows from shaded cells or the key column), else a
box or stat block. Reading order: full-width elements split the page into bands;
inside a band, left column then right column.

Usage:
  pip install pymupdf
  python tools/fl2md.py IN.pdf OUT.md --title "..." [--pages A-B] [--check]
    --pages A-B : PDF pages to convert (1-based, inclusive)
    --check     : report words present in the PDF but missing from the output
    --figures F : hand transcriptions appended to (or, with `replace`, replacing) a printed
                  page; blocks start with `<!-- fig p.N -->` or `<!-- fig p.N replace -->`
"""
import argparse
import collections
import difflib
import multiprocessing
import re
import string
import sys
from dataclasses import dataclass, field

import pymupdf

# ---------------------------------------------------------------- page model
HEAD_Y, FOOT_Y = 84, 612          # running head art / page number + chapter name
MID = 241.5                       # gutter between the two text columns
DICE = {("Swordlings", "x"): "⚔", ("SkullZ", "l"): "☠"}
LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "\t": " "}
BULLETS = "✥•■◆"


@dataclass
class Span:
    text: str
    font: str
    size: float
    x0: float
    y0: float
    x1: float
    y1: float
    bold: bool
    italic: bool

    @property
    def branding(self):
        return self.font.startswith(("Branding", "Futura"))


@dataclass
class Line:
    spans: list
    x0: float = 0
    y0: float = 0
    x1: float = 0
    y1: float = 0

    def __post_init__(self):
        self.x0 = min(s.x0 for s in self.spans)
        self.y0 = min(s.y0 for s in self.spans)
        self.x1 = max(s.x1 for s in self.spans)
        self.y1 = max(s.y1 for s in self.spans)

    @property
    def text(self):
        return join_spans(self.spans)

    @property
    def size(self):
        return max(s.size for s in self.spans)

    @property
    def branding(self):
        chars = collections.Counter()
        for s in self.spans:
            chars[s.branding] += len(s.text.strip())
        return chars[True] > chars[False]

    @property
    def col(self):
        if self.x1 <= MID + 4:
            return "L"
        if self.x0 >= MID - 4:
            return "R"
        return "F"


def clean(t: str) -> str:
    for a, b in LIGATURES.items():
        t = t.replace(a, b)
    t = re.sub(r"-?\xad\s*$", "-", t)        # soft hyphen at a line end = hyphenation point
    return t.replace("\xad", "")


def join_spans(spans) -> str:
    out = ""
    prev = None
    for s in spans:
        t = s.text
        if prev and out and not out[-1].isspace() and not t[:1].isspace() and s.x0 - prev.x1 > 1.2:
            out += " "
        out += t
        prev = s
    return re.sub(r" {2,}", " ", out)


def page_lines(pg) -> list:
    """Text lines of a page minus running heads, page numbers and the watermark.
    A PDF line whose spans are far apart is split (table cells on one baseline)."""
    lines, seen = [], set()
    for b in pg.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            if l["dir"][0] < 0.99:          # rotated text (spine labels etc.)
                continue
            spans = []
            for s in l["spans"]:
                font = s["font"].split("+")[-1]
                size = s["size"]
                if font.startswith("Helvetica") and size < 7:   # purchase watermark
                    continue
                bold = bool(s["flags"] & 16) or "Bold" in font
                ital = bool(s["flags"] & 2) or "Ita" in font
                # Branding (table) text: a wide gap between letters is a column gap
                pieces, cur = [], []
                for c in s["chars"]:
                    if cur and font.startswith("Branding") and c["c"] != " " and \
                            c["bbox"][0] - max(x["bbox"][2] for x in cur if x["c"] != " " or True) > 5:
                        pieces.append(cur)
                        cur = []
                    cur.append(c)
                if cur:
                    pieces.append(cur)
                for ch in pieces:
                    t = clean("".join(c["c"] for c in ch))
                    if font in ("Swordlings", "SkullZ"):
                        t = re.sub(r"\S", lambda m: DICE.get((font, m.group()), m.group()), t)
                    if not t.strip():
                        continue
                    ink = [c for c in ch if c["c"].strip()] or ch
                    x0, x1 = min(c["bbox"][0] for c in ink), max(c["bbox"][2] for c in ink)
                    y0, y1 = s["bbox"][1], s["bbox"][3]
                    if y1 < HEAD_Y or y0 > FOOT_Y:
                        continue
                    key = (t, round(x0), round(y0))
                    if key in seen:                 # text drawn twice (fake bold)
                        continue
                    seen.add(key)
                    spans.append(Span(t, font, round(size, 1), x0, y0, x1, y1, bold, ital))
            if not spans:
                continue
            spans.sort(key=lambda s: s.x0)
            cur = [spans[0]]
            for s in spans[1:]:
                limit = 5 if s.branding and cur[-1].branding else 12
                if s.x0 - cur[-1].x1 > limit and join_spans(cur).strip() not in BULLETS:
                    lines.append(Line(cur))
                    cur = []
                cur.append(s)
            lines.append(Line(cur))
    # a bullet glyph is often its own PDF line: glue it to the text on its right
    for b in [l for l in lines if l.text.strip() in tuple(BULLETS)]:
        right = [l for l in lines if l is not b and abs(l.y0 - b.y0) < 4 and 0 <= l.x0 - b.x1 < 25]
        if right:
            t = min(right, key=lambda l: l.x0)
            t.spans[:0] = b.spans
            t.__post_init__()
            lines.remove(b)
    return lines


_DOC = None


def _init_worker(path):
    global _DOC
    _DOC = pymupdf.open(path)


def _page_job(p):
    return p, page_lines(_DOC[p])


def extract_lines(pdf: str, pages, jobs: int) -> dict:
    """page_lines for every page, once (text extraction is ~95% of the run time)."""
    if jobs > 1 and len(pages) > 8:
        with multiprocessing.get_context("fork").Pool(jobs, _init_worker, (pdf,)) as pool:
            return dict(pool.map(_page_job, pages, chunksize=4))
    doc = pymupdf.open(pdf)
    return {p: page_lines(doc[p]) for p in pages}


# ---------------------------------------------------------------- text helpers
WORD_RE = re.compile(r"[A-Za-z’']+(?:-[A-Za-z’']+)*")


class Vocab:
    """Decides whether a line-end hyphen is a real hyphen ("two-handed")."""

    def __init__(self, cache):
        self.words, self.hyph = set(), set()
        for lines in cache.values():
            for line in lines:
                for w in WORD_RE.findall(line.text)[:-1]:   # last word may be broken
                    w = w.lower()
                    (self.hyph if "-" in w else self.words).add(w)

    def join(self, a: str, b: str) -> str:
        """a ends with '-', b is the next line."""
        m1 = re.search(r"([A-Za-z’']+)-$", a)
        m2 = re.match(r"([A-Za-z’']+)", b)
        if not (m1 and m2):
            return a + b
        w1, w2 = m1.group(1).lower(), m2.group(1).lower()
        if f"{w1}-{w2}" in self.hyph and (w1 + w2) not in self.words:
            return a + b
        if w2[0].isupper() and m2.group(1)[0].isupper() and not m2.group(1).isupper():
            return a + b                                    # "Half-Elf"
        return a[:-1] + b


def join_lines(texts, vocab) -> str:
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
        elif out.endswith("-*") and t.startswith("*") and t[1:2].isalpha():
            out = vocab.join(out[:-1], t[1:])          # *Forbid-* *den Lands* -> *Forbidden Lands*
        elif out.endswith("-") and not out.endswith(" -") and t[:1].isalpha():
            out = vocab.join(out, t)
        else:
            out += " " + t
    return re.sub(r"(?<=[\w,.;:!?’”)])\* \*(?=[\w‘“(])", " ", out)


def fmt_spans(spans) -> str:
    """Inline markdown for a line: runs of italic spans become *...*.
    Small-caps terms between italic spans stay inside the italic run."""
    ital = [s.italic and not s.bold for s in spans]
    for i, s in enumerate(spans):
        if not ital[i] and not re.search(r"[a-z]", s.text) and \
                (i == 0 or ital[i - 1]) and (i + 1 == len(spans) or ital[i + 1]) and any(ital):
            ital[i] = True
    out, i = [], 0
    while i < len(spans):
        j = i
        while j < len(spans) and ital[j] == ital[i]:
            j += 1
        txt = join_spans(spans[i:j])
        if ital[i] and txt.strip():
            core = txt.strip()
            txt = txt.replace(core, f"*{core}*", 1)
        out.append((spans[i], spans[j - 1], txt))
        i = j
    res = ""
    for a, b, txt in out:
        if res and not res[-1].isspace() and not txt[:1].isspace() and a.x0 - prev_x1 > 1.2:
            res += " "
        res += txt
        prev_x1 = b.x1
    return re.sub(r" {2,}", " ", res)


def title_case(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    words = []
    for w in t.split(" "):
        if re.fullmatch(r"[A-Z]?\d*D\d+.*|[IVX]+", w):   # D66, 2D6, roman numerals
            words.append(w)
        else:
            words.append(string.capwords(w.lower(), "-") if w.isupper() else w)
    small = {"Of", "The", "A", "An", "And", "Or", "In", "On", "To", "For", "At", "By", "With"}
    return " ".join(w if i == 0 or w not in small else w.lower() for i, w in enumerate(words))


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def bookmark_title(t: str) -> str:
    t = t.replace("\x85", "–").replace("\x90", "’").replace("\x92", "’")
    t = re.sub(r"^Text [Bb]ox:\s*", "Box: ", t)
    t = re.sub(r"\s{2,}", " – ", t.strip())
    return t


# ---------------------------------------------------------------- regions
@dataclass
class Region:
    lines: list = field(default_factory=list)
    kind: str = ""        # table / stats / box
    caption: str = ""
    caplines: list = field(default_factory=list)
    table: bool = False

    @property
    def bbox(self):
        return (min(l.x0 for l in self.lines), min(l.y0 for l in self.lines),
                max(l.x1 for l in self.lines), max(l.y1 for l in self.lines))

    @property
    def x0(self): return self.bbox[0]
    @property
    def y0(self): return self.bbox[1]
    @property
    def x1(self): return self.bbox[2]
    @property
    def y1(self): return self.bbox[3]

    @property
    def col(self):
        x0, _, x1, _ = self.bbox
        return "L" if x1 <= MID + 4 else "R" if x0 >= MID - 4 else "F"


def is_caption(unit) -> bool:
    return all(s.bold for l in unit for s in l.spans) and max(l.size for l in unit) >= 8.8 and len(unit) == 1


def is_title(unit) -> bool:
    """Bold all-caps line on its own, e.g. a stat block name ("SMALL DRAGON")."""
    t = " ".join(l.text for l in unit).strip()
    return (len(unit) == 1 and all(s.bold for s in unit[0].spans) and not re.search(r"[a-z]", t)
            and not t.endswith(":") and len(t) > 2 and not re.search(r"\d", t))


def all_caps_bold(unit) -> bool:
    return all(s.bold and not re.search(r"[a-z]", s.text) for l in unit for s in l.spans)


def is_header(unit) -> bool:
    spans = [s for l in unit for s in l.spans]
    return (len(unit) >= 2 and all(s.bold and s.size < 8.8 for s in spans)
            and all(not re.search(r"[a-z]", s.text) for s in spans))


def units_of(lines):
    """Group lines sharing a baseline and lying close together horizontally."""
    lines = sorted(lines, key=lambda l: (round(l.y0), l.x0))
    units = []
    for l in lines:
        u = units[-1] if units else None
        if u:
            ux0, ux1 = min(x.x0 for x in u), max(x.x1 for x in u)
            gap = max(l.x0 - ux1, ux0 - l.x1)
            cross = (ux1 <= MID + 4 and l.x0 >= MID - 4) or (l.x1 <= MID + 4 and ux0 >= MID - 4)
            # both pieces start at a text-column margin: two columns, not one table row
            margins = sorted([ux0, l.x0])
            if cross and margins[0] < 66 and 243 <= margins[1] <= 257:
                gap = 999
        caps = u and all(s.bold and not re.search(r"[a-z]", s.text) for x in u + [l] for s in x.spans)
        if u and abs(u[-1].y0 - l.y0) <= 2.5 and (gap < 45 or ((not cross or caps) and gap < 150)) and gap < 999:
            u.append(l)
        else:
            units.append([l])
    return units


def find_regions(lines):
    blines = [l for l in lines if l.branding]
    regions = []
    for u in units_of(blines):
        ux0, uy0 = min(l.x0 for l in u), min(l.y0 for l in u)
        ux1, uy1 = max(l.x1 for l in u), max(l.y1 for l in u)
        target = None
        for r in reversed(regions):
            x0, y0, x1, y1 = r.bbox
            caption_only = all(is_caption([l]) for l in r.lines)
            near = ux0 < x1 + 2 and ux1 > x0 - 2
            if caption_only and not near:      # centred caption over a wider table
                cx = (x0 + x1) / 2
                slack = 150 if is_header(u) or all_caps_bold(u) else 20
                near = ux0 - slack < cx < ux1 + slack
            if near and uy0 - y1 < (40 if r.table else 22) and uy0 >= y0 - 2:
                target = r
                break
        if target and (is_caption(u) or (is_title(u) and not target.table)) \
                and not all(is_caption([l]) for l in target.lines):
            target = None
        if target and is_header(u):
            head_only = all(is_caption([l]) for l in target.lines)
            same_head = abs(uy0 - target.lines[-1].y0) < 12 and all(l.spans[0].bold for l in target.lines[-2:])
            if not (head_only or same_head):
                target = None
        if target is None:
            target = Region()
            regions.append(target)
        target.lines.extend(u)
        target.table = target.table or is_header(u)
    # absorb non-branding lines (small caps terms, dice) lying inside a region
    for l in lines:
        if l.branding:
            continue
        cy = (l.y0 + l.y1) / 2
        for r in regions:
            x0, y0, x1, y1 = r.bbox
            if x0 - 2 <= l.x0 <= x1 + 2 and l.x1 <= x1 + 40 and y0 - 1 <= cy <= y1 + 12:
                r.lines.append(l)
                l.absorbed = True
                break
    for r in regions:
        r.lines.sort(key=lambda l: (round(l.y0), l.x0))
        classify(r)
    return regions


def classify(r: Region):
    units = units_of(r.lines)
    caps = []
    r.caplines = []
    while units and (is_caption(units[0]) or (not caps and is_title(units[0]) and not
                                               any(is_header(u) for u in units[:2]))):
        r.caplines.append(units[0][0])
        caps.append(units.pop(0)[0].text)
    r.caption = title_case(" ".join(caps)) if caps else ""
    if any(is_header(u) for u in units[:2]):
        r.kind = "table"
    elif units and sum(1 for u in units if label_of(u[0].spans)) * 2 >= len(units):
        r.kind = "stats"
    else:
        r.kind = "box"


def label_of(spans):
    """Leading bold run ending in ':' -> label text, else ''."""
    lab = ""
    for s in spans:
        if not s.bold:
            break
        lab += s.text
        if lab.rstrip().endswith(":"):
            return lab.strip()
    return ""


# ---------------------------------------------------------------- tables
LISTY_RE = re.compile(r"^[A-Z][A-Za-z’' ]+ [−–+-]?\d+$")


class ListTable:
    """Side-by-side lists under bold headings -> `- **Heading:** item; item; ...`."""

    def __init__(self, cols, names, body, col_of, vocab):
        groups = []                                     # [name, [items]]
        for k, name in enumerate(names):
            ls = sorted((l for l in body if col_of(l) == k), key=lambda l: l.y0)
            cur = [name, []]
            groups.append(cur)
            prev = None
            for l in ls:
                if all_caps_bold([l]) and re.search(r"[A-Z]{3}", l.text):
                    cur = [title_case(l.text), []]
                    groups.append(cur)
                elif prev is not None and l.y0 - prev.y0 < 12.5 and cur[1]:
                    cur[1][-1] = join_lines([cur[1][-1], l.text], vocab)   # wrapped item
                else:
                    cur[1].append(l.text.strip())
                prev = l
        merged = {}
        for name, items in groups:
            if items:
                merged.setdefault(name.strip() or "Items", []).extend(items)
        self.names = list(merged)
        self.md = "\n".join(f"- **{n}:** " + "; ".join(i) for n, i in merged.items())

    def join_spread(self, other) -> bool:
        return False


class Table:
    def __init__(self, names, rows, ys):
        self.names, self.rows, self.ys = names, rows, ys

    @property
    def md(self):
        out = [f"| {' | '.join(self.names)} |", "|" + "---|" * len(self.names)]
        out += ["| " + " | ".join(r) + " |" for r in self.rows]
        return "\n".join(out)

    def join_spread(self, other) -> bool:
        """Facing-page table: same rows, more columns on the right-hand page."""
        if len(self.rows) != len(other.rows) or not self.rows:
            return False
        if sum(abs(a - b) < 5 for a, b in zip(self.ys, other.ys)) < 0.8 * len(self.ys):
            return False
        self.names += other.names
        self.rows = [a + b for a, b in zip(self.rows, other.rows)]
        return True


def table_md(r: Region, shaded, vocab):
    units = units_of([l for l in r.lines if not any(l is c for c in r.caplines)])
    # header rows: leading bold units (multi-line headers stack)
    head_units = []
    while units and all_caps_bold(units[0]) and (
            is_header(units[0]) or any(is_header(u) for u in units[1:3])
            or (head_units and units[0][0].y0 - head_units[-1][0].y1 < 4)):
        head_units.append(units.pop(0))
    hlines = [l for u in head_units for l in u]
    cols = []                                   # merged header cells
    for l in sorted(hlines, key=lambda l: l.x0):
        c = next((c for c in cols if l.x0 < c["x1"] + 1 and l.x1 > c["x0"] - 1), None)
        if c:
            c["x0"], c["x1"] = min(c["x0"], l.x0), max(c["x1"], l.x1)
            c["parts"].append(l)
        else:
            cols.append({"x0": l.x0, "x1": l.x1, "parts": [l]})
    cols.sort(key=lambda c: c["x0"])
    names = [title_case(" ".join(p.text for p in sorted(c["parts"], key=lambda p: p.y0))) for c in cols]
    body = [l for u in units for l in u]
    if not cols or not body:
        return None
    # row labels with no column header (Young / Adult / Old)
    lead = [l for u in units for l in u if l.x0 < cols[0]["x0"] - 6]
    if len(lead) >= 2:
        cols.insert(0, {"x0": min(l.x0 for l in lead), "x1": max(l.x1 for l in lead), "parts": []})
        names.insert(0, " ")
    # column boundary = middle of the widest empty gap in body x0's between two headers
    xs = sorted(l.x0 for u in units for l in u)
    bounds = [-1e9]
    for a, b in zip(cols, cols[1:]):
        lo, hi = a["x0"] + 4, b["x0"] + 4
        pts = [lo] + [x for x in xs if lo < x < hi] + [hi]
        gap = max(zip(pts, pts[1:]), key=lambda p: p[1] - p[0])
        bounds.append((gap[0] + gap[1]) / 2)

    def col_of(l):
        k = 0
        for i, b in enumerate(bounds):
            if l.x0 >= b:
                k = i
        return k

    # columns that are independent lists with bold sub-headings (List of Talents)
    subheads = [l for l in body if all_caps_bold([l]) and re.search(r"[A-Z]{3}", l.text)]
    named = [n for n in names if n.strip()]
    period = next((m for m in range(1, len(names)) if len(names) % m == 0 and names[:m] * (len(names) // m) == names),
                  0)                            # header repeated side by side: A B | A B
    if subheads or period == 1:
        return ListTable(cols, names, body, col_of, vocab)

    # row starts: key-column lines that share a baseline with another column,
    # plus the first line inside / after every shaded band
    all_bands = [b for b in shaded if b.x0 < r.x1 and b.x1 > r.x0 and r.y0 - 2 < b.y0 < r.y1 and b.height > 7]

    def build_rows(colset):
        """Rows for the columns in colset (first one = key column)."""
        k0 = colset[0]
        part = [l for l in body if col_of(l) in colset]
        px0, px1 = min(l.x0 for l in part), max(l.x1 for l in part)
        bands = sorted({(round(b.y0), round(b.y1)) for b in all_bands if b.x0 < px1 and b.x1 > px0})

        def in_band(y):
            return next(((a, b) for a, b in bands if a - 1 <= y <= b + 1), None)

        starts = set()
        key = [l for l in part if col_of(l) == k0]
        for l in key:
            cy = (l.y0 + l.y1) / 2
            band = in_band(cy)
            if band and any(band[0] - 1 <= (o.y0 + o.y1) / 2 < cy for o in part):
                continue                    # inside a shaded row, below its first line
            if any(0 < l.y0 - o.y0 < 12.5 for o in key):
                continue                    # wrapped key cell ("Fishing Hook and / Line")
            if len(colset) == 1 or any(col_of(o) != k0 and abs(o.y0 - l.y0) <= 3 for o in part):
                starts.add(l.y0)
        for a, b in bands:
            inside = [l.y0 for l in part if a - 1 <= (l.y0 + l.y1) / 2 <= b + 1]
            after = [l.y0 for l in part if (l.y0 + l.y1) / 2 > b + 1]
            if inside:
                starts.add(min(inside))
            if after:
                starts.add(min(after))
        if not starts:
            starts = {l.y0 for l in part}
        starts = sorted(starts)
        dedup = [starts[0]]
        for s in starts[1:]:
            if s - dedup[-1] > 4:
                dedup.append(s)
        rows = [[[] for _ in colset] for _ in dedup]
        for l in sorted(part, key=lambda l: (l.y0, l.x0)):
            k = max([i for i, s in enumerate(dedup) if l.y0 >= s - 3] or [0])
            rows[k][colset.index(col_of(l))].append(l)
        return [r for r in rows if any(r)]

    def cell(ls):
        if not ls:
            return "—"
        ls = sorted(ls, key=lambda l: (round(l.y0), l.x0))
        texts = [cell_text(l) for l in ls]
        if len(texts) > 1 and all(LISTY_RE.match(t.strip()) for t in texts):
            return ", ".join(t.strip() for t in texts)
        return join_lines(texts, vocab).replace("|", "/")

    if period > 1:                              # side-by-side halves: stack them
        rows = [r for h in range(len(names) // period)
                for r in build_rows(list(range(h * period, (h + 1) * period)))]
        names = names[:period]
    else:
        rows = build_rows(list(range(len(cols))))
    return Table(names, [[cell(c) for c in r] for r in rows],
                 [min(l.y0 for c in r for l in c) for r in rows])


def cell_text(l: Line) -> str:
    """Bold run-in names inside table cells -> **Name!**."""
    spans = l.spans
    lead = ""
    i = 0
    while i < len(spans) and spans[i].bold:
        lead += spans[i].text
        i += 1
    rest = join_spans(spans[i:]) if i < len(spans) else ""
    if lead.strip() and rest.strip():
        return f"**{lead.strip()}** {rest.strip()}"
    return fmt_spans(spans)


# ---------------------------------------------------------------- boxes & stats
def box_items(r: Region, vocab):
    """Paragraphs and `- **Label:** value` items of a box / stat block."""
    body = [l for l in r.lines if not any(l is c for c in r.caplines)]
    body.sort(key=lambda l: (l.col if r.col == "F" else "", round(l.y0), l.x0))
    items, cur, prev = [], None, None
    right = max(l.x1 for l in body) if body else 0
    for l in body:
        lab = label_of(l.spans)
        bullet = l.text.strip()[:1] in BULLETS
        new_para = (prev is None or lab or bullet or l.y0 - prev.y1 > 7
                    or (prev.x1 < right - 12 and re.search(r"[.!?:”’)]$", prev.text.strip())))
        if lab:
            rest = l.text.strip()[len(lab):].strip()
            cur = {"label": lab.rstrip(":").strip(), "lines": [rest] if rest else []}
            items.append(cur)
        elif bullet:
            cur = {"label": "", "bullet": True, "lines": [l.text.strip()[1:].strip()]}
            items.append(cur)
        elif new_para and not (cur and cur.get("label") and not cur["lines"]):
            cur = {"label": "", "lines": [fmt_spans(l.spans)]}
            items.append(cur)
        else:
            cur["lines"].append(fmt_spans(l.spans))
        prev = l
    out = []
    for it in items:
        txt = join_lines(it["lines"], vocab)
        if it["label"]:
            val = txt
            if val.isupper():
                val = title_case(val)
            val = re.sub(r",(?=\S)", ", ", val)
            if it["label"].upper().startswith(("ATTRIBUTES", "SKILLS")):
                val = re.sub(r"(\d) (?=[A-Z])", r"\1, ", val)
            out.append(f"- **{title_case(it['label'])}:** {val}")
        elif it.get("bullet"):
            out.append(f"- {txt}")
        else:
            out.append(txt)
    return out


def box_md(r: Region, vocab) -> str:
    items = box_items(r, vocab)
    return "\n>\n".join("> " + i for i in items) if items else ""


def stats_md(r: Region, vocab) -> str:
    out = []
    for i in box_items(r, vocab):
        out.append(i if i.startswith("- ") else "- " + i)
    return "\n".join(out)


# ---------------------------------------------------------------- prose
def heading_line(l: Line) -> bool:
    return (not l.branding and l.size >= 8.8
            and all(not s.font.startswith("IM_FELL_Great_Primer") for s in l.spans)
            and not l.text.strip()[:1] in BULLETS)


def art_label(l: Line, prose) -> bool:
    """Small isolated caption inside an illustration (e.g. a skill icon label)."""
    if l.size >= 7.5 or len(l.text.split()) > 4 or l.text.strip() == "EXAMPLE":
        return False
    if not re.search(r"[a-z]", l.text):           # all-caps label placed over the art
        if min(abs(l.x0 - m) for m in (57, 71, 248, 262)) > 4:
            return True
        return not any(o.col == l.col and abs(o.y0 - l.y0) < 18 and re.search(r"[a-z]", o.text)
                       for o in prose)
    return not any(o is not l and o.col == l.col and abs(o.y0 - l.y0) < 18 for o in prose)


def dropcap(l: Line) -> bool:
    return l.size >= 18 and len(l.text.strip()) <= 2


class Holder:
    """Markdown block that may still change (a facing-page table gets its columns later)."""

    def __init__(self, md=""):
        self.md = md
        self.parts = None

    @staticmethod
    def join(heading, holder):
        h = Holder()
        h.parts = [heading, holder]
        return h

    def __str__(self):
        if self.parts is not None:
            return "\n\n".join(str(p) for p in self.parts if str(p))
        return self.md

    def __bool__(self):
        return bool(str(self))


class Writer:
    """Accumulates markdown; keeps a paragraph open across columns and pages."""

    def __init__(self, vocab):
        self.vocab = vocab
        self.out = []
        self.para = []            # lines of the open paragraph
        self.prev = None          # previous prose line
        self.right = {}           # column right edge per page/col
        self.floats = []          # blocks waiting for a paragraph break
        self.pending_anchor = None
        self.prev_right = 0

    def flush(self):
        if self.para:
            txt = join_lines(self.para, self.vocab)
            self.out.append(txt)
            self.para = []
        self.out.extend(self.floats)
        self.floats = []
        self.prev = None

    def block(self, md, floating=True):
        if not md:
            return
        p = self.prev
        if p and p.x1 < self.prev_right - 10 and re.search(r"[.!?:”’)]$", p.text.strip()):
            floating = False                       # the open paragraph has visibly ended
        self.place_anchor(inline=floating)
        if floating and self.para:
            self.floats.append(md)
        else:
            self.flush()
            self.out.append(md)

    def anchor(self, a):
        self.pending_anchor = a

    def place_anchor(self, inline: bool):
        a, self.pending_anchor = self.pending_anchor, None
        if not a:
            return
        if inline and self.para:
            self.para.append(a)
        else:
            self.flush()
            self.out.append(a)

    def heading(self, md):
        self.place_anchor(inline=False)
        self.flush()
        self.out.append(md)

    def prose(self, l: Line, right: float, dc: str = "", left: float = 0):
        pright = self.prev_right if self.prev is not None else right   # right edge of p's column
        txt = fmt_spans(l.spans)
        if dc:
            txt = dc + txt.lstrip()
        lab = re.match(r"^([A-Z][A-Z’' &]+:)\s", l.text)
        bullet = l.text.strip()[:1] in BULLETS
        p = self.prev
        new = (p is None or bullet or lab or dc
               or ((p.x1 < pright - 10 or (l.col == p.col and abs(p.x0 - left) < 2 and 11 < l.x0 - left < 17
                                            and not (self.para and self.para[0].startswith("- "))
                                            and not all(sp.italic for sp in l.spans if sp.text.strip())))
                   and re.search(r"[.!?:”’*]$", p.text.strip()))
               or l.y0 - p.y1 > 9 and l.col == p.col and l.y0 > p.y0
               or (self.para and self.para[0].startswith("- ") and self.item_strict
                   and not (l.col == p.col and abs(l.x0 - self.item_x) <= 2.5 and l.y0 - p.y1 < 4))
               or (self.para and self.para[0].startswith("- ") and l.col == p.col
                   and abs(l.x0 - self.item_x) > 2.5
                   and p.x1 < pright - 15 and re.search(r"[.!?:”’*]$", p.text.strip())))
        if new and p is not None and re.search(r"[a-z]-$", p.text.strip()) and l.text[:1].islower():
            new = bool(bullet or lab or dc)            # a hyphenated word always continues
        if bullet:
            body = l.text.strip()[1:].strip()
            m = re.match(r"^([A-Z][A-Z’' ]+?)(?::\s*|\s+)(.*)$", body)
            if m and m.group(1).strip() in ("RANK", "RANGE", "DURATION", "INGREDIENT"):
                key, val = title_case(m.group(1).strip()), m.group(2)
                n = re.match(r"(\d+):\s*(.*)$", val)          # talent "RANK 1: text"
                body = f"**{key} {n.group(1)}:** {n.group(2)}" if n else f"**{key}:** {val}"
            self.place_anchor(inline=False)
            self.flush()
            self.para = ["- " + body]                         # wrapped lines continue the item
            self.item_strict = bool(m and m.group(1).strip() in ("RANK", "RANGE", "DURATION", "INGREDIENT")
                                    and not re.match(r"\d+:", m.group(2)))   # spell stat line
            self.item_x = next((s.x0 for s in l.spans if s.text.strip() and s.text.strip() not in BULLETS), l.x0)
            self.prev, self.prev_right = l, right
            return
        if lab:
            txt = f"**{lab.group(1)}** " + txt[len(lab.group(1)):].lstrip()
        if new:
            self.place_anchor(inline=False)
            self.flush() if (self.para or self.floats) else None
        else:
            self.place_anchor(inline=True)
        self.para.append(txt)
        self.prev, self.prev_right = l, right

    def text(self):
        self.place_anchor(inline=False)
        self.flush()
        md = "\n\n".join(str(x) for x in self.out if str(x))
        md = re.sub(r"\n{3,}", "\n\n", md)
        md = re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", md)
        md = re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", md)
        return md


# ---------------------------------------------------------------- driver
def printed_label(pg) -> str:
    lab = pg.get_label()
    if lab.isdigit():
        return lab
    w = pg.rect.width
    nums = [t[4] for t in pg.get_text("words") if t[4].isdigit() and len(t[4]) <= 3
            and t[1] > FOOT_Y and abs((t[0] + t[2]) / 2 - w / 2) < 30]
    return nums[0] if nums else ""


def bands(elements):
    """Order elements: full-width items split the page into bands; L then R inside."""
    full = sorted([e for e in elements if e[0] == "F"], key=lambda e: e[1])
    out, top = [], -1
    for f in full + [("F", 10_000, None)]:
        seg = [e for e in elements if e[0] != "F" and top <= e[1] < f[1]]
        out += sorted([e for e in seg if e[0] == "L"], key=lambda e: e[1])
        out += sorted([e for e in seg if e[0] == "R"], key=lambda e: e[1])
        if f[2] is not None:
            out.append(f)
        top = f[1]
    return out


FIG_RE = re.compile(r"^<!-- fig p\.(\d+)( replace)? -->\n", re.M)


def load_figures(path: str) -> dict:
    """Hand transcriptions: {printed_page: (replace_page?, markdown)}."""
    if not path:
        return {}
    parts = FIG_RE.split(open(path, encoding="utf-8").read())
    return {parts[i]: (bool(parts[i + 1]), parts[i + 2].strip()) for i in range(1, len(parts), 3)}


def convert(pdf, out, title, first, last, check=False, figures="", jobs=4):
    figs = load_figures(figures)
    doc = pymupdf.open(pdf)
    pages = list(range(first - 1, last))
    cache = extract_lines(pdf, pages, jobs)
    vocab = Vocab(cache)
    toc = collections.defaultdict(list)
    for lvl, t, p in doc.get_toc():
        if p - 1 in pages:
            toc[p - 1].append({"lvl": min(lvl, 4), "title": bookmark_title(t), "used": False})
    w = Writer(vocab)
    prev_names = None
    page_tables = collections.defaultdict(list)    # pno -> [[Table, Holder, joined?]]
    for pno in pages:
        pg = doc[pno]
        lab = printed_label(pg)
        w.anchor(f"<!-- p.{lab} -->" if lab else f"<!-- pdf {pno + 1} -->")
        marks = toc.get(pno, [])
        fig = figs.pop(lab, None)
        if fig and fig[0]:                       # page replaced by a hand transcription
            for m in marks:
                if not m["title"].startswith("Map:"):
                    w.heading("#" * m["lvl"] + " " + m["title"])
            w.block(fig[1], floating=False)
            continue
        lines = cache[pno]
        for l in lines:
            l.absorbed = False
        regions = find_regions(lines)
        shaded = [it[1] for d in pg.get_drawings() if d.get("fill") and d["fill"][0] < 0.95
                  and abs(d["fill"][0] - d["fill"][2]) < 0.1 for it in d["items"] if it[0] == "re"]
        prose = [l for l in lines if not l.branding and not l.absorbed]
        prose = [l for l in prose if not art_label(l, prose)]
        right = {c: max([l.x1 for l in prose if l.col == c] or [0]) for c in "LR"}
        left = {c: min([l.x0 for l in prose if l.col == c] or [0]) for c in "LR"}
        elems = [(r.col, r.y0, ("region", r)) for r in regions]
        # merge consecutive heading lines
        merged = []
        for l in sorted(prose, key=lambda l: (l.col, round(l.y0), l.x0)):
            if merged and heading_line(l) and heading_line(merged[-1]) and l.col == merged[-1].col \
                    and -1 <= l.y0 - merged[-1].y1 < 0.8 * l.size and abs(l.size - merged[-1].size) < 1:
                merged[-1] = Line(merged[-1].spans + l.spans)
                continue
            merged.append(l)
        elems += [(l.col, l.y0, ("line", l)) for l in merged]
        actions, dc = [], ""
        for _, _, (kind, obj) in bands(elems):
            if kind == "region":
                r = obj
                if r.kind != "table":
                    items = box_items(r, vocab)
                    n_lab = sum(i.startswith("- **") for i in items)
                    r.kind = "stats" if n_lab and n_lab * 2 >= len(items) else "box"
                heading = region_heading(r, marks)
                if (r.kind == "stats" and not heading and not r.caption and actions
                        and actions[-1][0] == "block" and actions[-1][2] == "stats"):
                    # stat block continued in the next column
                    actions[-1] = ("block", actions[-1][1] + "\n" + stats_md(r, vocab), "stats")
                    continue
                if r.kind == "table":
                    t = table_md(r, shaded, vocab)
                    if t is None:
                        md = box_md(r, vocab)
                    else:
                        if not heading and not r.caption:
                            mate = next((x for x in page_tables.get(pno - 1, [])
                                         if not x[2] and x[0].join_spread(t)), None)
                            if mate:
                                mate[1].md, mate[2] = mate[0].md, True
                                continue
                        holder = Holder(t.md)
                        page_tables[pno].append([t, holder, False])
                        if not heading and t.names == prev_names:
                            heading = "##### Table: (continued)"
                        prev_names = list(t.names)
                        actions.append(("block", Holder.join(heading, holder), "table"))
                        continue
                else:
                    md = stats_md(r, vocab) if r.kind == "stats" else box_md(r, vocab)
                actions.append(("block", "\n\n".join(x for x in (heading, md) if x), r.kind))
                continue
            l = obj
            if dropcap(l):
                dc = l.text.strip()
                continue
            if l.text.strip() == "EXAMPLE":
                dc = "**Example:** "
                continue
            if heading_line(l):
                actions.append(("heading", match_heading(l.text, marks)))
                continue
            actions.append(("prose", l, right.get(l.col, 0), dc, left.get(l.col, 0)))
            dc = ""
        # bookmarks whose text wasn't found on the page go to the page top
        for m in marks:
            if not m["used"] and not m["title"].startswith("Map:"):
                w.heading("#" * m["lvl"] + " " + m["title"])
        for act in actions:
            if act[0] == "block":
                w.block(act[1])
            elif act[0] == "heading":
                w.heading(act[1])
            else:
                w.prose(*act[1:])
        if fig:
            w.block(fig[1])
    if figs:
        print(f"WARNING: figure pages not found: {sorted(figs)}", file=sys.stderr)
    md = w.text()
    front = (f"---\ntitle: {title}\nsource: {pdf.split('/')[-1]}\npdf_pages: {first}-{last} of {doc.page_count}\n---\n\n"
             "<!-- Conventions: see CLAUDE.md and the tools/fl2md.py docstring. -->\n\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write(front + md + "\n")
    print(f"{out}: {len(pages)} pages, {len(md):,} chars", file=sys.stderr)
    if check:
        coverage(doc, pages, md, cache)


def region_heading(r: Region, marks) -> str:
    kinds = {"table": "Table", "stats": "Stats", "box": "Box"}
    want = kinds[r.kind]
    cap = r.caption
    if cap:
        for m in marks:
            if not m["used"] and norm(m["title"].split(":", 1)[-1]) == norm(cap):
                m["used"] = True
                t = m["title"] if re.match(r"(Table|Stats|Box): ", m["title"]) else f"{want}: {m['title']}"
                return "#" * m["lvl"] + " " + t
    if not cap:
        for m in marks:
            if not m["used"] and m["title"].startswith(want + ":"):
                m["used"] = True
                return "#" * m["lvl"] + " " + m["title"]
        return ""
    return f"##### {want}: {cap}"


def mark_key(title: str) -> str:
    return norm(re.sub(r"^\d+\.\s*", "", title))     # "4. Talents" ~ "TALENTS"


def find_mark(text, marks):
    key = norm(text)
    if not key:
        return None
    for exact in (True, False):
        for m in marks:
            mk = mark_key(m["title"])
            if m["used"] or not mk:
                continue
            if mk == key or (not exact and len(mk) >= 4 and (key.startswith(mk) or mk.startswith(key))):
                return m
    for m in marks:                       # typo in a bookmark ("Cleans Spirit")
        if not m["used"] and len(key) >= 6 and difflib.SequenceMatcher(None, mark_key(m["title"]), key).ratio() > 0.9:
            m["fuzzy"] = True
            return m
    return None


def match_heading(text, marks) -> str:
    m = find_mark(text, marks)
    if m:
        m["used"] = True
        longer = len(norm(text)) > len(mark_key(m["title"])) or m.get("fuzzy")  # "Animal Handling (Empathy)"
        return "#" * m["lvl"] + " " + (title_case(text) if longer else m["title"])
    return "##### " + title_case(text)


def coverage(doc, pages, md, cache):
    """Words in the PDF (after the same filtering) that are missing from the output."""
    have = collections.Counter(w.lower() for w in re.findall(r"[A-Za-z]{3,}", md))
    miss = collections.Counter()
    where = collections.defaultdict(set)
    for p in pages:
        prev_hyph = False
        for l in cache[p]:
            t = l.text.strip()
            ws = re.findall(r"[A-Za-z]{3,}|-$", t)
            if prev_hyph and ws:
                ws = ws[1:]
            prev_hyph = t.endswith("-")
            if prev_hyph:
                ws = ws[:-2]
            for w_ in ws:
                w_ = w_.lower()
                if have[w_] <= 0:
                    miss[w_] += 1
                    where[w_].add(doc[p].get_label() or f"pdf{p + 1}")
                have[w_] -= 1
    tot = sum(miss.values())
    print(f"coverage: {tot} word occurrences missing", file=sys.stderr)
    for w_, n in miss.most_common(40):
        print(f"  {w_} x{n}: p.{', '.join(sorted(where[w_]))[:60]}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("out")
    ap.add_argument("--title", required=True)
    ap.add_argument("--pages", default="")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--jobs", type=int, default=4, help="worker processes for text extraction")
    ap.add_argument("--figures", default="", help="hand transcriptions, see tools/figures/")
    a = ap.parse_args()
    n = pymupdf.open(a.pdf).page_count
    first, last = (int(x) for x in a.pages.split("-")) if a.pages else (1, n)
    convert(a.pdf, a.out, a.title, first, last, a.check, a.figures, a.jobs)


if __name__ == "__main__":
    main()
