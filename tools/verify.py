#!/usr/bin/env python3
"""Page-level verification of a converted book against its PDF.

For every printed page it compares the PDF text (same filtering as fl2md: no running heads,
page numbers or watermark) with the output segment between that page's anchor and the next:

  words   share of the page's words (4+ letters) found in its own segment, or within one page
  numbers numbers, dice codes and ranges (D6+3, 2D6, 11–16, −2, +1) missing from the page's
          segment and its neighbours: these are the values rules depend on

Usage: python tools/verify.py IN.pdf OUT.md --pages A-B [--show N]
Prints the pages with the lowest word share and every page with missing numbers.
"""
import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fl2md  # noqa: E402
import pymupdf  # noqa: E402

WORD = re.compile(r"[A-Za-z]{4,}")
NUM = re.compile(r"[−+-]?\d*D\d+(?:[+−-]\d+)?|\d+[–-]\d+|[−+]\d+|\d+")


def tokens(text):
    text = text.replace("\xad", "")
    words = collections.Counter(w.lower() for w in WORD.findall(text))
    nums = collections.Counter(n.replace("-", "–") if re.fullmatch(r"\d+-\d+", n) else n.replace("-", "−")
                               for n in NUM.findall(text))
    return words, nums


def segments(md):
    """{printed page label: text} split at page anchors (inline anchors included), and
    {page: segment holding it} for pages inside a joined table (`<!-- p.A–B -->`)."""
    host = {}
    md = re.sub(r"<!-- p\.(\d+)–(\d+) -->", lambda m: f"@@RANGE {m.group(1)} {m.group(2)}@@", md)
    parts = re.split(r"<!-- (p\.\S+|pdf \d+) -->", md)
    seg = {}
    for i in range(1, len(parts), 2):
        key = parts[i].replace("p.", "").replace("pdf ", "pdf")
        seg[key] = seg.get(key, "") + parts[i + 1]
        for a, b in re.findall(r"@@RANGE (\d+) (\d+)@@", parts[i + 1]):
            for n in range(int(a), int(b) + 1):
                host.setdefault(str(n), []).append(key)
    return seg, host


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("md")
    ap.add_argument("--pages", required=True)
    ap.add_argument("--show", type=int, default=15)
    a = ap.parse_args()
    doc = pymupdf.open(a.pdf)
    first, last = (int(x) for x in a.pages.split("-"))
    pages = list(range(first - 1, last))
    cache = fl2md.extract_lines(a.pdf, pages, 4)
    seg, host = segments(open(a.md, encoding="utf-8").read())
    labels = [(fl2md.printed_label(doc[p]) or f"pdf{p + 1}") for p in pages]
    seg_tok = {k: tokens(v) for k, v in seg.items()}
    rows = []
    for i, p in enumerate(pages):
        lab = labels[i]
        text = "\n".join(l.text.strip() for l in sorted(cache[p], key=lambda l: (l.col, l.y0)))
        text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)      # line-end hyphenation, as the output has it
        w, n = tokens(text)
        if not w and not n:
            continue
        near = [labels[j] for j in range(max(0, i - 1), min(len(labels), i + 2))] + host.get(lab, [])
        own_w = seg_tok.get(lab, (collections.Counter(), None))[0]
        near_w = sum((seg_tok.get(k, (collections.Counter(), None))[0] for k in near), collections.Counter())
        near_n = sum((seg_tok.get(k, (None, collections.Counter()))[1] for k in near), collections.Counter())
        tot = sum(w.values())
        own = sum(min(c, own_w[t]) for t, c in w.items())
        nb = sum(min(c, near_w[t]) for t, c in w.items())
        miss_n = [t for t, c in n.items() if near_n[t] < c]
        rows.append((lab, tot, own / tot if tot else 1, nb / tot if tot else 1, miss_n))
    print(f"{len(rows)} pages; own-segment word share < 90%: "
          f"{sum(r[2] < .9 for r in rows)}, within ±1 page < 95%: {sum(r[3] < .95 for r in rows)}")
    print("\nlowest word share (own / within ±1 page):")
    for r in sorted(rows, key=lambda r: r[3])[:a.show]:
        print(f"  p.{r[0]:>5}  {r[1]:4d} words  own {r[2]:4.0%}  near {r[3]:4.0%}")
    print("\npages with numbers missing (not on the page or its neighbours):")
    for r in rows:
        if r[4]:
            print(f"  p.{r[0]:>5}  {' '.join(r[4][:12])}")


if __name__ == "__main__":
    main()
