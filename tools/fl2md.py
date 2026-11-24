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
                  page; blocks start with `<!-- fig p.N -->`, `<!-- fig p.N replace -->` or
                  `<!-- fig p.N drop y>300 -->` (drop page text below y=300, e.g. figure labels)
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
BULLETS = "✥•■◆…"                 # "…" starts hanging list items ("… always makes a lot of noise")


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


def subseq(a: str, b: str) -> bool:
    """a's letters appear in b in order ("Z rtorm" in "Zertorme")."""
    it = iter(b)
    return all(c in it for c in a if not c.isspace())


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
    # prose words that the PDF stores as separate lines on one baseline -> one line
    lines.sort(key=lambda l: (round(l.y0), l.x0))
    merged = []
    for l in lines:
        m = merged[-1] if merged else None
        if (m and all(sp.font.startswith("IM_FELL") for sp in m.spans + l.spans) and abs(m.y0 - l.y0) < 1.5
                and 0 <= l.x0 - m.x1 < 12 and l.size < 12 and m.size < 12
                and not (m.x1 <= MID + 4 and l.x0 >= MID - 4)):      # never across the column gutter
            merged[-1] = Line(m.spans + l.spans)
        else:
            merged.append(l)
    lines = merged
    # encounter number badge ("2" in Branding-Black) belongs to the heading on its right
    for l in lines:
        if len(l.spans) > 1 and "Black" in l.spans[0].font and l.spans[0].text.strip().isdigit():
            sp = l.spans[0]
            l.spans[0] = Span(sp.text.strip() + ": ", l.spans[1].font, l.spans[1].size,
                              sp.x0, sp.y0, sp.x1, sp.y1, False, False)
    # encounter number badge ("2" in Branding-Black) belongs to the heading on its right
    for b in [l for l in lines if l.text.strip().isdigit() and "Black" in l.spans[0].font]:
        right = [l for l in lines if l is not b and abs(l.y0 - b.y0) < 8 and 0 < l.x0 - b.x1 < 20]
        if right:
            t = min(right, key=lambda l: l.x0)
            t.spans[:0] = [Span(b.text.strip() + ": ", t.spans[0].font, t.spans[0].size, b.x0, t.y0,
                                b.x1, t.y1, False, False)]
            t.__post_init__()
            lines.remove(b)
    # partial copies of a line drawn on top of it (text over art): keep the full line
    groups = collections.defaultdict(list)
    for l in lines:
        groups[(round(l.x0), round(l.y0))].append(l)
    drop = set()
    for g in groups.values():
        g.sort(key=lambda l: len(l.text))
        for i, l in enumerate(g):
            if any(o.text.strip().startswith(l.text.strip()) for o in g[i + 1:]):
                drop.add(id(l))
    # ... also when the copy starts mid-line: same baseline, inside the full line, a substring of it
    for l in lines:
        t = l.text.strip().rstrip("-").strip()
        if id(l) in drop:
            continue
        for o in lines:
            if o is not l and id(o) not in drop and abs(o.y0 - l.y0) < 1 and o.x0 - 1 <= l.x0 \
                    and l.x1 <= o.x1 + 1 and len(o.text) > len(l.text) and subseq(t, o.text):
                drop.add(id(l))
                break
    lines = [l for l in lines if id(l) not in drop]
    # a bullet glyph is often its own PDF line: glue it to the text on its right
    for b in [l for l in lines if l.text.strip() in tuple(BULLETS)]:
        twin = [l for l in lines if l is not b and abs(l.y0 - b.y0) < 4 and -12 <= l.x0 - b.x1 < 25
                and l.text.strip()[:1] in BULLETS]
        if twin:                          # the glyph is drawn twice (once inside the text line)
            lines.remove(b)
            continue
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


HYPH_FIRST = {"half", "self", "well", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
              "eighty", "ninety", "two", "three", "four", "five", "six", "seven", "eight", "nine"}
HYPH_SECOND = {"like", "laden", "shaped", "sized", "eyed", "haired", "legged"}   # never a plain suffix


class Vocab:
    """Decides whether a line-end hyphen is a real hyphen ("two-handed")."""

    def __init__(self, cache):
        self.words, self.hyph, self.count = set(), set(), collections.Counter()
        for lines in cache.values():
            for line in lines:
                for w in WORD_RE.findall(line.text)[:-1]:   # last word may be broken
                    w = w.lower()
                    (self.hyph if "-" in w else self.words).add(w)
                    self.count[w] += 1

    def join(self, a: str, b: str) -> str:
        """a ends with '-', b is the next line."""
        m1 = re.search(r"([A-Za-z’']+)-$", a)
        m2 = re.match(r"([A-Za-z’']+)", b)
        if not (m1 and m2):
            return a + b
        w1, w2 = m1.group(1).lower(), m2.group(1).lower()
        if f"{w1}-{w2}" in self.hyph and (w1 + w2) not in self.words:
            return a + b
        if m2.group(1)[0].isupper() and not m2.group(1).isupper():
            return a + b                                    # "Half-Elf", "Demon-Tainted"
        if re.match(r"[A-Za-z]+-[a-z]", b):
            return a + b                                    # "well-" + "to-do"
        if (w1 + w2) not in self.words and (w1 in HYPH_FIRST or w2 in HYPH_SECOND):
            return a + b                                    # "half-" + "human", "energy-" + "laden"
        return a[:-1] + b


def join_lines(texts, vocab) -> str:
    out, held = "", []
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if t.startswith("<!-- p") and out.endswith("-"):
            held.append(t)                   # don't split a hyphenated word with a page anchor
            continue
        if held and not t.startswith("<!--"):
            out = join_lines([out, t], vocab) + " " + " ".join(held)
            held = []
            continue
        if not out:
            out = t
        elif out.endswith("-*") and t.startswith("*") and t[1:2].isalpha():
            out = vocab.join(out[:-1], t[1:])          # *Forbid-* *den Lands* -> *Forbidden Lands*
        elif out.endswith("-") and not out.endswith(" -") and t[:1].isalpha():
            out = vocab.join(out, t)
        elif out.endswith("/") and t[:1].isalpha():
            out += t                             # "gear/" + "weapon"
        else:
            out += " " + t
    if held:
        out += " " + " ".join(held)
    out = re.sub(r"(?<=[\w,.;:!?’”)])\* (<!-- p\.\S+ -->) \*(?=[\w‘“(])", r" \1 ", out)   # italics across a page
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
        if re.fullmatch(r"NPCS|PCS|GMS", w):
            words.append(w[:-1] + "s")                                     # NPCs
        elif re.fullmatch(r"[A-Z]?\d*D\d+.*|[IVX]+|NPC|PC|GM|XP|WP", w):   # D66, 2D6, roman numerals, NPC
            words.append(w)
        else:
            words.append(string.capwords(w.lower(), "-") if w.isupper() else w)
    small = {"Of", "The", "A", "An", "And", "Or", "In", "On", "To", "For", "At", "By", "With"}
    first = 1 if words and re.fullmatch(r"\d+[.:]?", words[0]) else 0     # "11. The Old Cemetery"
    return " ".join(w if i <= first or w not in small else w.lower() for i, w in enumerate(words))


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def bookmark_title(t: str) -> str:
    t = t.replace("\x85", "–").replace("\x90", "’").replace("\x92", "’")
    if t.isupper():                          # Raven's Purge: "MONSTERS AND NPCS" -> "Monsters and NPCs"
        t = title_case(t)
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
    single_col: bool = False
    skip: bool = False
    intro: object = None
    headerless: bool = False
    single_head: bool = False

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


def split_zones(lines):
    """Vertical ranges with two tables side by side: a pair of captions on one baseline,
    one on each side of the gutter. Inside them nothing is joined across the gutter."""
    caps = [l for l in lines if l.branding and is_caption([l])]
    dice = [l for l in lines if l.branding and all(sp.bold for sp in l.spans)
            and re.fullmatch(r"D\d+", l.text.strip())]            # header cells "D66" on both sides
    pair = lambda a, b: a.x1 <= MID + 4 and b.x0 >= MID - 4 and abs(a.y0 - b.y0) < 3
    # a zone ends at the next full-width caption, or at a "D66" header with no partner
    ends = [c.y0 for c in caps if c.x0 < MID - 4 and c.x1 > MID + 4]
    ends += [d.y0 for d in dice if not any(pair(d, e) or pair(e, d) for e in dice)]
    zones = []
    spans_gutter = lambda d: any(c.x0 < MID - 4 and c.x1 > MID + 4 and 0 < d.y0 - c.y0 < 30 for c in caps)
    dice_pairs = [(a, b) for a in dice for b in dice if not spans_gutter(a)]   # one caption over both
    for a, b in [(a, b) for a in caps for b in caps] + dice_pairs:                # halves = one table
        if pair(a, b):
            below = [y for y in ends if y > a.y0 + 5]
            zones.append((a.y0 - 2, min(below) - 2 if below else 1e9))
    return zones


def units_of(lines, zones=()):
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
        caps = u and all(s.bold and s.size < 8.8 and not re.search(r"[a-z]", s.text) for x in u + [l] for s in x.spans)
        if u and cross and any(a <= l.y0 <= b for a, b in zones):
            gap = 999                    # two tables side by side
        if u and abs(u[-1].y0 - l.y0) <= 2.5 and (gap < 45 or (not cross and gap < 150) or (caps and gap < 300)) and gap < 999:
            u.append(l)
        else:
            units.append([l])
    return units


def find_regions(lines):
    blines = [l for l in lines if l.branding]
    regions = []
    zones = split_zones(blines)
    for u in units_of(blines, zones):
        ux0, uy0 = min(l.x0 for l in u), min(l.y0 for l in u)
        ux1, uy1 = max(l.x1 for l in u), max(l.y1 for l in u)
        target, best = None, None
        for r in reversed(regions):
            x0, y0, x1, y1 = r.bbox
            caption_only = all(is_caption([l]) for l in r.lines)
            overlap = ux0 < x1 + 2 and ux1 > x0 - 2
            near = overlap
            if caption_only and not near:      # centred caption over a wider table
                cx = (x0 + x1) / 2
                slack = 150 if is_header(u) or (all_caps_bold(u) and max(l.size for l in u) < 8.8) else 20
                near = ux0 - slack < cx < ux1 + slack
            if near and uy0 - y1 < (40 if r.table else 22) and uy0 >= y0 - 2:
                # an overlapping region wins; else the caption whose centre is closest
                score = (0 if overlap else 1, abs((x0 + x1) / 2 - (ux0 + ux1) / 2))
                if best is None or score < best:
                    target, best = r, score
                if overlap:
                    break
        if target and target.table and label_of(u[0].spans) and len(target.lines) > 4:
            target = None                # "OUTPOST: One roll" under a table: notes for the next one
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
        body = sum(len(s.text) for s in l.spans if s.font.startswith("IM_FELL_Great_Primer"))
        if l.text.strip()[:1] not in BULLETS and (          # (a bullet item set in the body font
                body * 2 > len(l.text) or (heading_line(l) and not any(sp.branding for sp in l.spans))):
            continue                     # body text and headings are never part of a table/box
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
    elif (len(units) > 3 and len(units[0]) == 1 and all_caps_bold(units[0]) and units[0][0].size < 8.8
          and multi_cell_rows(units[1:]) >= 0.7):
        r.kind, r.single_head = "table", True      # one header cell ("D66"), unnamed value column
    elif units and sum(1 for u in units if label_of(u[0].spans)) * 2 >= len(units):
        r.kind = "stats"
    else:
        r.kind = "box"


def multi_cell_rows(units) -> float:
    """Share of baselines that hold two or more cells."""
    rows = collections.Counter()
    for u in units:
        for l in u:
            rows[round(l.y0 / 3)] += 1
    return sum(n >= 2 for n in rows.values()) / len(rows) if rows else 0


def label_of(spans):
    """Leading bold run ending in ':' -> label text, else ''."""
    lab = ""
    for s in spans:
        if not s.bold:
            if lab.strip() and s.text.lstrip().startswith(":"):   # "KIN TALENT" + ": Adaptive"
                return lab.strip() + ":"
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

    note = ""

    @property
    def md(self):
        out = [f"| {' | '.join(self.names)} |", "|" + "---|" * len(self.names)]
        out += ["| " + " | ".join(r) + " |" for r in self.rows]
        return "\n".join(out) + (f"\n\n*Note:* {self.note}" if self.note else "")

    def join_spread(self, other) -> bool:
        """Facing-page table: same rows, more columns on the right-hand page."""
        if not isinstance(other, Table) or len(self.rows) != len(other.rows) or not self.rows:
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
            is_header(units[0]) or any(is_header(u) for u in units[1:3]) or (r.single_col and not head_units)
            or (head_units and units[0][0].y0 - head_units[-1][0].y1 < 4)
            or (r.single_head and not head_units)):
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
    names = [title_case(join_lines([p.text for p in sorted(c["parts"], key=lambda p: p.y0)], vocab))
             for c in cols]                     # "MOUN-" + "TAINS" -> Mountains
    if r.single_head and cols:                  # columns right of the only header cell are unnamed
        extra = sorted(l.x0 for u in units for l in u if l.x0 > cols[-1]["x1"] + 4)
        for x in extra:
            if cols[-1]["parts"] == [] and x - cols[-1]["x1"] < 60:
                cols[-1]["x1"] = x
            else:
                cols.append({"x0": x, "x1": x, "parts": []})
                names.append(" ")
    if not cols and r.headerless:               # rows only: columns from the cells' left edges
        for x in sorted(l.x0 for u in units for l in u):
            if cols and x - cols[-1]["x1"] < 60:      # centred values start at varying x
                cols[-1]["x1"] = x
            else:
                cols.append({"x0": x, "x1": x, "parts": []})
        names = [" "] * len(cols)
    body = [l for u in units for l in u]
    # small-print notes under the last row ("Parry without Parrying weapon: −2")
    note = []
    if body:
        bsize = collections.Counter(round(l.size) for l in body).most_common(1)[0][0]
        while body:
            last = max(body, key=lambda l: l.y0)
            starred = last.text.strip().startswith("*") and not any(
                o is not last and abs(o.y0 - last.y0) < 3 for o in body)       # "*Boars attack you…"
            if starred or (last.size < bsize - 0.5 and not any(
                    o.y0 > last.y0 - 3 and o is not last and o.size >= bsize - 0.5 for o in body)):
                note.insert(0, last)
                body.remove(last)
            else:
                break
    if not cols or not body:
        return None
    # row labels with no column header (Young / Adult / Old)
    lead = [l for u in units for l in u if l.x0 < cols[0]["x0"] - 6]
    nxt = cols[1]["x0"] if len(cols) > 1 else 1e9
    paired = [l for l in lead if any(o is not l and abs(o.y0 - l.y0) < 3 and cols[0]["x0"] - 6 <= o.x0 < nxt - 6
                                     for u in units for o in u)]
    if len(lead) >= 2 and len(paired) * 2 >= len(lead):     # first column has its own values too
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
    period = next((m for m in range(1, len(names)) if len(names) % m == 0 and names[:m] * (len(names) // m) == names
                   and names[0].strip()), 0)                            # header repeated side by side: A B | A B
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
    t = Table(names, [[cell(c) for c in r] for r in rows],
              [min(l.y0 for c in r for l in c) for r in rows])
    t.note = " ".join(fmt_spans(l.spans).strip() for l in sorted(note, key=lambda l: l.y0))
    return t


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
        sep = "" if rest.strip()[:1] in ".,:;!?" else " "            # "**NOTHING**."
        return f"**{lead.strip()}**{sep}{rest.strip()}"
    return fmt_spans(spans)


# ---------------------------------------------------------------- boxes & stats
def box_items(r: Region, vocab):
    """Paragraphs and `- **Label:** value` items of a box / stat block."""
    body = [l for l in r.lines if not any(l is c for c in r.caplines)]
    body.sort(key=lambda l: (l.col if r.col == "F" else "", round(l.y0), l.x0))
    items, cur, prev = [], None, None
    right = max(l.x1 for l in body) if body else 0
    box_left = min(l.x0 for l in body) if body else 0
    for l in body:
        lab = label_of(l.spans)
        bullet = l.text.strip()[:1] in BULLETS
        # box text is ragged-right: the previous line ended its paragraph if the next line's first
        # word would have fitted on it (and it ends a sentence); numbered steps; gaps; indents
        first = (l.text.strip().split() or [""])[0]
        cw = (prev.x1 - prev.x0) / max(len(prev.text), 1) if prev else 0
        fits = prev is not None and prev.x1 + (len(first) + 1) * cw < right - 2
        bold_lead = (l.spans[0].bold and l.spans[0].text.strip() and not all(sp.bold for sp in l.spans)
                     and re.search(r"[A-Z]{3}", l.spans[0].text))      # "HARAMA’S soup stone looks…"
        new_para = (prev is None or lab or bullet or bold_lead or l.y0 - prev.y1 > 7
                    or re.match(r"\d+\.\s", l.text.strip())
                    or (fits and re.search(r"[.!?:”’)*]$", prev.text.strip()))
                    or (l.x0 - box_left >= 8 and prev.x0 - box_left < 3 and prev.x1 >= right - 15
                        and abs((l.x0 + l.x1) - (prev.x0 + prev.x1)) > 6      # not centred text
                        and not (cur and cur.get("bullet"))))
        if lab:
            rest = re.sub(r"^\s*:\s*", "", l.text.strip()[len(lab) - 1:])   # colon may sit after a space
            cur = {"label": lab.rstrip(":").strip(), "lines": [rest] if rest else []}
            items.append(cur)
        elif bullet:
            t = l.text.strip()
            cur = {"label": "", "bullet": True, "lines": [t if t[0] == "…" else re.sub(r"^[\s" + BULLETS + "]+", "", t)]}
            items.append(cur)
        elif new_para and not (cur and cur.get("label") and not cur["lines"]):
            cur = {"label": "", "lines": [cell_text(l) if bold_lead else fmt_spans(l.spans)]}
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
        if re.match(r"STRENGTH \d", i):          # unlabelled attribute line of an NPC stat block
            i = "**Attributes:** " + re.sub(r",(?=\S)", ", ", title_case(i))
        out.append(i if i.startswith("- ") else "- " + i)
    return "\n".join(out)


# ---------------------------------------------------------------- prose
def heading_line(l: Line) -> bool:
    t = l.text.strip()
    if (not l.branding and 6.8 <= l.size < 8.8 and t != "EXAMPLE" and not t.endswith(":")
            and all("PICA" in s.font for s in l.spans) and not re.search(r"[a-z]", t)
            and len(t) >= 3 and len(t.split()) <= 6):
        return True                     # small-caps name heading (NPCs: "KORDOMAR SULAM")
    return (not l.branding and l.size >= 8.8
            and all(not s.font.startswith("IM_FELL_Great_Primer") for s in l.spans)
            and not t[:1] in BULLETS)


MAP_LABEL = re.compile(r"(?:\d{1,2}[a-z]?|[A-Z])(?: (?:\d{1,2}[a-z]?|[A-Z]))*")   # "7", "C", "7 10 9 8 4"


def art_label(l: Line, prose) -> bool:
    """Small isolated caption inside an illustration (e.g. a skill icon label), or a map key
    ("7", "12", "C") set in the body font."""
    if MAP_LABEL.fullmatch(l.text.strip()) and l.size < 12 and not any(   # (a drop cap is not a map key)
            o is not l and abs(o.y0 - l.y0) < 3 and re.search(r"[a-z]{3}", o.text) for o in prose):
        return True                      # (a page number in a contents line is not one either)
    if l.size >= 6.5 or len(l.text.split()) > 4 or l.text.strip() == "EXAMPLE":
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


class TableBlock:
    """A table plus its heading; rows from later pages may still be appended."""

    cont, base, intro = False, "", ""

    def __init__(self, heading, table, label):
        self.heading, self.table, self.pages = heading, table, [label]

    @staticmethod
    def merge(items, vocab=None):
        """Join a table continued on the next page(s) into one: same columns, only page
        anchors in between, and either no heading or a "<caption>, Cont." heading."""
        items = [str(x) if isinstance(x, Holder) else x for x in items]
        items = [x for x in items if not (isinstance(x, str) and not x.strip())]
        out, last, prev_names = [], None, None
        box_at = None                    # index in out of the last sidebar (box) block
        prev_real = None
        for x in items:
            if isinstance(x, str) and x.startswith("**[Figure — map") and x == prev_real:
                continue                 # a map spread over two pages: one note
            if not (isinstance(x, str) and re.fullmatch(r"<!-- p\.\S+ -->", x.strip())):
                prev_real = x
            if (isinstance(x, str) and x.startswith("> ") and box_at is not None
                    and not re.search(r"[.!?”’)]\*?$", out[box_at].rstrip())):
                # a sidebar running on over the page break: one box
                out[box_at] = join_lines([out[box_at].rstrip(), x[2:]], vocab)
                continue
            if isinstance(x, str) and (x.startswith("> ") or re.match(r"#+ Box: .*\n\n> ", x)):
                box_at = len(out)
            elif not (isinstance(x, str) and re.fullmatch(r"<!-- p\.\S+ -->", x.strip())):
                box_at = None
            if isinstance(x, TableBlock):
                title = lambda h: norm(re.sub(r"^#+\s*", "", h or ""))
                same = last is not None and x.heading and title(x.heading) == title(last.heading)  # caption repeated
                if ((x.cont or same) and last is not None and x.table.names == last.table.names
                        and isinstance(x.table, Table) and (same or not x.base or norm(x.base) in norm(last.heading))):
                    last.table.rows += x.table.rows
                    last.pages += x.pages
                    if x.intro:
                        last.intro = "\n\n".join(i for i in (last.intro, x.intro) if i)
                    keys = [re.match(r"(\d+)(?:–\d+)?$", r[0]) for r in last.table.rows]
                    if all(keys):        # halves met out of order: d66 rows go by their roll
                        last.table.rows = [r for _, r in sorted(zip([int(k.group(1)) for k in keys],
                                                                    last.table.rows), key=lambda z: z[0])]
                    continue
                if x.cont and not x.heading and x.table.names == prev_names:
                    x.heading = "##### Table: (continued)"   # same table, but text in between
                last, prev_names = x, x.table.names
            elif not (isinstance(x, str) and re.fullmatch(r"<!-- p\.\S+ -->", x.strip())):
                last = None
            out.append(x)
        return out

    def __str__(self):
        pages = [p for p in self.pages if p]
        rng = f"<!-- p.{pages[0]}–{pages[-1]} -->" if len(set(pages)) > 1 else ""
        head = f"{self.heading} {rng}".strip() if self.heading else rng
        return "\n\n".join(x for x in (head, self.intro, self.table.md) if x)

    def __bool__(self):
        return True


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
        self.para_start = 0

    def flush(self, keep_floats=False):
        if self.para:
            txt = join_lines(self.para, self.vocab)
            self.out.append(txt)
            self.para = []
        if not keep_floats:
            self.out.extend(self.floats)
            self.floats = []
        self.prev = None

    def block(self, md, floating=True):
        if not md:
            return
        p = self.prev
        example = p is not None and all(sp.italic for sp in p.spans if sp.text.strip())
        if p and p.x1 < self.prev_right - 10 and re.search(r"[.!?:”’)]$", p.text.strip()) and not example:
            floating = False                       # the open paragraph has visibly ended
        self.place_anchor(inline=floating)
        if floating and self.para:
            self.floats.append(md)
        else:
            self.flush()
            self.out.append(md)

    def anchor(self, a):
        if self.pending_anchor:          # previous page had no new content (e.g. joined table)
            self.place_anchor(inline=True)
        if self.para and self.prev is not None and re.search(r"[.!?”]$", self.prev.text.strip()):
            self.flush()                 # page ends on a full stop: close the paragraph, and let
                                         # waiting tables/boxes land on their own page
        elif self.para and self.floats:  # paragraph runs on: put the waiting blocks before it,
            self.out[self.para_start:self.para_start] = self.floats    # not pages later
            self.floats = []
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
            first = re.match(r"[A-Za-z’']*", txt.lstrip()).group(0)
            joined = (dc + first).lower()
            if dc.isalpha() and dc.lower() in ("a", "i") and joined not in self.vocab.words:
                txt = dc + " " + txt.lstrip()            # "A" + "roleplaying game" is two words
            else:
                txt = dc + txt.lstrip()
        lab = re.match(r"^([A-Z][A-Z’' &/-]+:)\s", l.text)
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
        ital = lambda x: all(sp.italic for sp in x.spans if sp.text.strip())
        if not new and p is not None and p.col == "F" and l.col != "F" and ital(p) != ital(l):
            new = True                                  # centred label line, then column text
        if not new and p is not None and ital(p) != ital(l) and (
                p.x1 < pright - 10 or re.search(r"[.!?”’]$", p.text.strip())):
            new = True                                  # short line, then italic flavour text (or back)
        if new and p is not None and re.search(r"[a-z]-$", p.text.strip()) and l.text[:1].islower():
            new = bool(bullet or lab or dc)            # a hyphenated word always continues
        if bullet and not re.sub(r"[\s" + BULLETS + "]", "", l.text):
            return                       # a lone bullet glyph: nothing to say
        if bullet:
            body = l.text.strip() if l.text.strip()[0] == "…" else re.sub(r"^[\s" + BULLETS + "]+", "", l.text)
            m = re.match(r"^([A-Z][A-Z’' ]+?)(?::\s*|\s+)(.*)$", body)
            gl = re.match(r"^([A-Z][A-Z’' &/-]+:)\s*(.*)$", body)
            if gl and not (m and m.group(1).strip() in ("RANK", "RANGE", "DURATION", "INGREDIENT")):
                body = f"**{gl.group(1)}** {gl.group(2)}"          # "- **CREATURES:** Olm, …"
            if m and m.group(1).strip() in ("RANK", "RANGE", "DURATION", "INGREDIENT"):
                key, val = title_case(m.group(1).strip()), m.group(2)
                n = re.match(r"(\d+):\s*(.*)$", val)          # talent "RANK 1: text"
                body = f"**{key} {n.group(1)}:** {n.group(2)}" if n else f"**{key}:** {val}"
            self.place_anchor(inline=False)
            self.flush()
            self.para_start = len(self.out)
            self.para = ["- " + body]                         # wrapped lines continue the item
            self.item_strict = bool(m and m.group(1).strip() in ("RANK", "RANGE", "DURATION", "INGREDIENT")
                                    and not re.match(r"\d+:", m.group(2)))   # spell stat line
            self.item_x = next((s.x0 for s in l.spans if re.sub(r"[\s" + BULLETS + "]", "", s.text)), l.x0)   # (glyph drawn twice)
            self.prev, self.prev_right = l, right
            return
        if lab:
            word = lab.group(1)[:-1]
            rest = txt[len(word):] if txt.startswith(word) else txt[len(lab.group(1)):]
            rest = re.sub(r"^\s*(\*?)\s*:\s*", r"\1", rest)      # "CORRIDOR *: A dark…*" (italic colon)
            txt = f"**{lab.group(1)}** " + rest.lstrip()
        if new:
            self.place_anchor(inline=False)
            # an italic example running on in a new paragraph: sidebars wait until it ends
            in_example = p is not None and ital(p) and ital(l) and not dc and not bullet
            self.flush(keep_floats=in_example) if (self.para or self.floats) else None
            if not in_example:
                self.para_start = len(self.out)  # (an example's paragraphs count as one block)
        else:
            self.place_anchor(inline=True)
        self.para.append(txt)
        self.prev, self.prev_right = l, right

    def text(self):
        self.place_anchor(inline=False)
        self.flush()
        self.out = TableBlock.merge(self.out, self.vocab)
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
        left = sorted([e for e in seg if e[0] == "L"], key=lambda e: e[1])
        right = sorted([e for e in seg if e[0] == "R"], key=lambda e: e[1])
        # two tables side by side (starting at the same height): left, then right
        for e in left:
            out.append(e)
            is_tab = lambda x: x[2][0] == "region" and x[2][1].kind == "table"
            if is_tab(e):
                mate = next((r for r in right if is_tab(r) and abs(r[1] - e[1]) < 15), None)
                if mate and all(is_tab(r) for r in right[:right.index(mate)]):
                    out += right[:right.index(mate) + 1]
                    del right[:right.index(mate) + 1]
        out += right
        if f[2] is not None:
            out.append(f)
        top = f[1]
    return out


FIG_RE = re.compile(r"^<!-- fig p\.(\d+)( replace| drop y>\d+)? -->\n", re.M)


def load_figures(path: str) -> dict:
    """Hand transcriptions: {printed_page: (mode, markdown)}; mode "" (append),
    "replace" (drop the page text) or "drop y>N" (drop text below y=N, e.g. figure labels)."""
    if not path:
        return {}
    parts = FIG_RE.split(open(path, encoding="utf-8").read())
    return {parts[i]: ((parts[i + 1] or "").strip(), parts[i + 2].strip()) for i in range(1, len(parts), 3)}


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
    prev_tnames, carry, prev_head_text = None, None, ""
    page_tables = collections.defaultdict(list)    # pno -> [[TableBlock, None, joined?]]
    for pno in pages:
        pg = doc[pno]
        lab = printed_label(pg)
        w.anchor(f"<!-- p.{lab} -->" if lab else f"<!-- pdf {pno + 1} -->")
        marks = toc.get(pno, [])
        fig = figs.pop(lab, None)
        if fig and fig[0] == "replace":          # page replaced by a hand transcription
            for m in marks:
                if not m["title"].startswith("Map:"):
                    w.heading("#" * m["lvl"] + " " + m["title"])
            w.block(fig[1])                     # waits for the paragraph running round it
            continue
        lines = cache[pno]
        if fig and fig[0].startswith("drop"):    # figure labels replaced by the transcription
            lim = float(fig[0].split(">")[1])
            lines = [l for l in lines if l.y0 <= lim]
        # big title set letter by letter ("T","H","E","VA"...) on chapter openers: decoration
        big = [l for l in lines if dropcap(l)]
        if len(big) > 2:
            lines = [l for l in lines if not dropcap(l)]
        for l in lines:
            l.absorbed = False
        shaded = [it[1] for d in pg.get_drawings() if d.get("fill") and d["fill"][0] < 0.95
                  and abs(d["fill"][0] - d["fill"][2]) < 0.1 for it in d["items"] if it[0] == "re"]
        regions = find_regions(lines)
        # a box made of rows with two or more cells on shaded bands is a table without header
        for b in regions:
            if b.kind != "box" or any(label_of(l.spans) for l in b.lines):
                continue
            body = [l for l in b.lines if not any(l is c for c in b.caplines)]
            paired = [l for l in body if any(o is not l and abs(o.y0 - l.y0) < 2 and abs(o.x0 - l.x0) > 40
                                             for o in body)]
            bands_in = sum(1 for sh in shaded if sh.x0 < b.x1 and sh.x1 > b.x0 and b.y0 - 2 < sh.y0 < b.y1)
            if len(body) >= 4 and len(paired) * 10 >= len(body) * 7 and bands_in >= 2:
                above = [c for c in regions if c is not b and c.kind == "box" and c.caption and not c.skip
                         and -40 < b.y0 - c.y1 < 6 and c.y0 < b.y0 and c.x0 < b.x1 and c.x1 > b.x0]
                if above:                        # its caption block also caught the first row
                    c = above[0]
                    b.lines = [l for l in c.lines if not any(l is x for x in c.caplines)] + b.lines
                    b.caplines, b.caption, c.skip = c.caplines, c.caption, True
                    b.lines += c.caplines
                b.kind, b.headerless = "table", True
        for b in regions:
            if b.kind == "stats" and not b.caption and len(b.lines) <= 4:
                below = [t for t in regions if t.kind == "table" and not t.caption and t.intro is None
                         and 0 <= t.y0 - b.y1 < 12 and t.x0 < b.x1 and t.x1 > b.x0]
                if below:
                    t = min(below, key=lambda t: t.y0)
                    t.intro, b.skip = b, True
        # caption + short intro text in its own block, table right below: one captioned table
        for b in regions:
            if b.caption and (b.kind == "box" or (b.kind == "stats" and len(b.lines) - len(b.caplines) <= 4)):
                below = [t for t in regions if t.kind == "table" and not t.caption and not t.skip
                         and 0 <= t.y0 - b.y1 < 30 and t.x0 < b.x1 and t.x1 > b.x0]
                if below:
                    t = min(below, key=lambda t: t.y0)
                    t.caption, t.intro, b.skip = b.caption, b, True
        prose = [l for l in lines if not l.branding and not l.absorbed]
        dropped = [l for l in prose if art_label(l, prose)]
        prose = [l for l in prose if not any(l is d for d in dropped)]
        map_keys = [l for l in dropped if MAP_LABEL.fullmatch(l.text.strip())]
        map_note = ""
        if len(map_keys) >= 4:                   # a map with numbered keys on this page
            titles = [title_case(l.text) for l in dropped if l not in map_keys and not re.search(r"[a-z]", l.text)]
            name = f": {' / '.join(titles)}" if titles else ""
            map_note = (f"**[Figure — map{name}; numbered/lettered locations match the keyed "
                        f"location descriptions; not transcribed]**")
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
        actions, dc, last_head, carry_next = [], "", "", None
        last_head_text = prev_head_text
        page_heads = {mark_key(l.text) for l in merged if heading_line(l)}
        page_caps = {norm(r.caption) for r in regions if r.caption}
        last_y = max([e[1] for e in elems] + [r.y1 for r in regions] + [0])
        for _, _, (kind, obj) in bands(elems):
            if kind == "region":
                r = obj
                if r.skip or not re.search(r"\w", " ".join(l.text for l in r.lines)):
                    continue                     # merged away, or a stray "." from the art
                if (r.kind == "box" and len(r.caplines) == 1 and r.caplines[0].size < 8.8
                        and not any(label_of(l.spans) for l in r.lines)
                        and sum(1 for b in shaded if b.x0 < r.x1 and b.x1 > r.x0 and r.y0 < b.y0 < r.y1) >= 2):
                    # one-column table (right half of a facing-page table): header + shaded rows
                    r.kind, r.single_col, r.caplines, r.caption = "table", True, [], ""
                if r.kind != "table":
                    items = box_items(r, vocab)
                    n_lab = sum(i.startswith("- **") for i in items)
                    r.kind = "stats" if n_lab and n_lab * 2 >= len(items) else "box"
                t = table_md(r, shaded, vocab) if r.kind == "table" else None
                # an uncaptioned table with the same columns as the last one continues it
                cont_like = t is not None and not r.caption and t.names == prev_tnames
                carried = None
                if carry and t is not None and not r.caption and not cont_like and not actions:
                    carried, r.caption, r.intro = carry, carry["region"].caption, carry["region"]
                carry = None
                heading = region_heading(r, marks, last_head, page_heads, cont_like, page_caps)
                if carried:                      # caption + intro at the foot of the previous page
                    carried["holder"].md = ""
                    if carried.get("mark"):
                        m = carried["mark"]
                        heading = "#" * m["lvl"] + " " + m["title"]
                if (r.kind == "stats" and not heading and not r.caption and actions
                        and actions[-1][0] == "block" and actions[-1][2] == "stats"):
                    # stat block continued in the next column
                    actions[-1] = ("block", actions[-1][1] + "\n" + stats_md(r, vocab), "stats")
                    continue
                if r.kind == "stats" and not heading and last_head_text:
                    heading = f"##### Stats: {re.sub(r'^(Stats|Box|Table): ', '', last_head_text)}"
                if r.kind == "table":
                    if t is not None:
                        prev_tnames = t.names
                    if t is None:
                        md = box_md(r, vocab)
                    else:
                        if not heading and not r.caption:
                            mate = next((x for x in page_tables.get(pno - 1, [])
                                         if not x[2] and x[0].table.join_spread(t)), None)
                            if mate:                         # facing-page columns
                                mate[0].pages.append(lab)
                                mate[2] = True
                                continue
                        block = TableBlock(heading, t, lab)
                        if r.intro is not None:
                            block.intro = "\n\n".join(box_items(r.intro, vocab))
                        base = re.sub(r",?\s*\(?cont(inued)?\.?\)?$", "", r.caption, flags=re.I)
                        if not heading:
                            block.cont = True            # may continue the table before it
                        elif r.caption and base != r.caption:
                            block.cont, block.base = True, base
                        page_tables[pno].append([block, None, False])
                        actions.append(("block", block, "table"))
                        continue
                else:
                    md = stats_md(r, vocab) if r.kind == "stats" else box_md(r, vocab)
                text = "\n\n".join(x for x in (heading, md) if x)
                if r.kind == "box" and r.caption and r.y1 >= last_y - 2:
                    # last thing on the page: maybe the caption + intro of a table on the next page
                    text = Holder(text)
                    carry_next = {"region": r, "holder": text}
                actions.append(("block", text, r.kind))
                continue
            l = obj
            if dropcap(l):
                dc = l.text.strip()
                continue
            if l.text.strip() == "EXAMPLE":
                dc = "**Example:** "
                continue
            if heading_line(l):
                htext = re.sub(r"([A-Za-z]+)- ([A-Za-z]+)",          # "THE STONE- LOOM", "DE- CEIVED"
                               lambda m: vocab.join(m.group(1) + "-", m.group(2)), l.text)
                hmd = match_heading(despace(htext, vocab), marks)
                actions.append(("heading", hmd))
                last_head = mark_key(htext)
                last_head_text = re.sub(r"^#+\s*", "", hmd)
                continue
            actions.append(("prose", l, right.get(l.col, 0), dc, left.get(l.col, 0)))
            dc = ""
        prev_head_text = last_head_text
        if carry_next:
            m = next((m for m in marks if not m["used"] and m["title"].startswith("Table:")
                      and norm(m["title"].split(":", 1)[1]) == norm(carry_next["region"].caption)), None)
            if m:
                m["used"], carry_next["mark"] = True, m
        carry = carry_next
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
        elif map_note:
            w.block(map_note)
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


def region_heading(r: Region, marks, last_head="", page_heads=(), cont_like=False, page_caps=()) -> str:
    kinds = {"table": "Table", "stats": "Stats", "box": "Box"}
    want = kinds[r.kind]
    cap = r.caption
    if cap:
        for m in marks:                  # tolerate a typo in the bookmark ("Reoccuring")
            if not m["used"] and norm(m["title"].split(":", 1)[-1]) != norm(cap) and difflib.SequenceMatcher(
                    None, norm(m["title"].split(":", 1)[-1]), norm(cap)).ratio() > 0.9:
                m["title"] = m["title"].split(":", 1)[0] + ": " + cap if ":" in m["title"] else cap
        for m in marks:
            kind = re.match(r"(Table|Stats|Box): ", m["title"])
            if kind and kind.group(1) != want and "Table" in (kind.group(1), want):
                continue                 # a Table bookmark is not for the Box with the same caption
            if not m["used"] and norm(m["title"].split(":", 1)[-1]) == norm(cap):
                m["used"] = True
                t = m["title"] if re.match(r"(Table|Stats|Box): ", m["title"]) else f"{want}: {m['title']}"
                return "#" * m["lvl"] + " " + t
    if not cap and (cont_like or r.single_col):
        return ""     # rest of the previous table (next page / facing page): no bookmark of its own
    if not cap:
        cands = [m for m in marks if not m["used"] and m["title"].startswith(want + ":")]
        # a stat block belongs to the heading just above it ("## Bloodling" -> "Stats: Bloodling");
        # never take the bookmark of another creature that has its own heading on the page
        m = next((m for m in cands if last_head and mark_key(m["title"]) == last_head), None)
        if m is None:
            m = next((m for m in cands if mark_key(m["title"]) not in page_heads
                      and norm(m["title"].split(":", 1)[1]) not in page_caps), None)
        if m:
            m["used"] = True
            return "#" * m["lvl"] + " " + m["title"]
        return ""
    return f"##### {want}: {cap}"


def mark_key(title: str) -> str:
    """"4. Talents" ~ "TALENTS"; "Stats: Typical Melder" ~ "TYPICAL MELDER"."""
    return norm(re.sub(r"^(Stats:\s*)?(\d+[.:]\s*)?", "", title))


def find_mark(text, marks):
    key = mark_key(text)                  # "13. CHAPEL" ~ bookmark "13. Chapel"
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


def despace(text: str, vocab) -> str:
    """Letter-spaced display titles: "for bidden la nds" -> "forbidden lands"."""
    toks, out, i = text.split(), [], 0
    while i < len(toks):
        for j in range(min(len(toks), i + 4), i + 1, -1):
            joined = "".join(toks[i:j])
            if vocab.count[joined.lower()] >= 3 and any(vocab.count[t.lower()] <= 2 for t in toks[i:j]):
                out.append(joined)
                i = j
                break
        else:
            out.append(toks[i])
            i += 1
    return string.capwords(" ".join(out)) if out != toks else text


def match_heading(text, marks) -> str:
    m = find_mark(text, marks)
    if m:
        m["used"] = True
        longer = len(mark_key(text)) > len(mark_key(m["title"])) or m.get("fuzzy")  # "Animal Handling (Empathy)"
        return "#" * m["lvl"] + " " + (title_case(text) if longer else m["title"])
    return "##### " + title_case(text)


def coverage(doc, pages, md, cache):
    """Words in the PDF (after the same filtering) that are missing from the output."""
    have = collections.Counter(w.lower() for w in re.findall(r"[A-Za-z]{3,}", md))
    miss = collections.Counter()
    where = collections.defaultdict(set)
    for p in pages:
        prev_hyph = False
        for l in sorted(cache[p], key=lambda l: (l.col, round(l.y0), l.x0)):
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
                    where[w_].add(printed_label(doc[p]) or f"pdf{p + 1}")
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
