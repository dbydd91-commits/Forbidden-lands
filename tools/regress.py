#!/usr/bin/env python3
"""Quick regression check for tools/fl2md.py on a fixed set of sample pages.

Each sample (tests/samples.txt) is converted on its own and compared with
tests/golden/<name>.md. Run it after every converter change instead of whole books;
when a diff is an intended improvement, review it and accept it.

Usage:
  python tools/regress.py --pdf-dir DIR            # convert + diff against golden
  python tools/regress.py --pdf-dir DIR --accept   # overwrite golden with current output
  python tools/regress.py --pdf-dir DIR -k phb-age # only samples whose name contains this
"""
import argparse
import difflib
import io
import os
import sys
import contextlib

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fl2md  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS = {
    "phb": ("Forbidden_Lands_Players_Handbook_5th_printing.pdf", "tools/figures/phb.md"),
    "gmg": ("Forbidden_Lands_Gamemasters_Guide_5th_printing.r.pdf", "tools/figures/gmg.md"),
    "la": ("Forbidden_Lands_Legends_and_Adventurers_5th_printing.pdf", ""),
    "rp": ("Forbidden_Lands_Raven_s_Purge.pdf", ""),
}


def samples():
    for line in open(os.path.join(ROOT, "tests/samples.txt"), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            name, book, rng = line.split()[:3]
            a, _, b = rng.partition("-")
            yield name, book, a, b or a


def pdf_page(doc, label: str) -> int:
    """Printed page label -> 1-based PDF page (page labels, else the printed footer number)."""
    if label.startswith("pdf"):                 # page without a printed number: "pdf1"
        return int(label[3:])
    hit = doc.get_page_numbers(label)
    if hit:
        return hit[0] + 1
    return next(i + 1 for i in range(doc.page_count) if fl2md.printed_label(doc[i]) == label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--accept", action="store_true")
    ap.add_argument("-k", default="")
    a = ap.parse_args()
    gold_dir = os.path.join(ROOT, "tests/golden")
    os.makedirs(gold_dir, exist_ok=True)
    changed = 0
    for name, book, p0, p1 in samples():
        if a.k not in name:
            continue
        pdf_name, figs = BOOKS[book]
        pdf = os.path.join(a.pdf_dir, pdf_name)
        doc = pymupdf.open(pdf)
        first, last = pdf_page(doc, p0), pdf_page(doc, p1)
        figs = os.path.join(ROOT, figs) if figs and os.path.exists(os.path.join(ROOT, figs)) else ""
        tmp = os.path.join(gold_dir, f".{name}.tmp")
        with contextlib.redirect_stderr(io.StringIO()):
            fl2md.convert(pdf, tmp, name, first, last, figures=figs, jobs=1)
        new = open(tmp, encoding="utf-8").read().replace(pdf_name, "PDF")
        os.remove(tmp)
        gold = os.path.join(gold_dir, f"{name}.md")
        old = open(gold, encoding="utf-8").read() if os.path.exists(gold) else ""
        if new == old:
            print(f"  ok       {name}")
            continue
        changed += 1
        if a.accept or not old:
            open(gold, "w", encoding="utf-8").write(new)
            print(f"  {'accepted' if old else 'created '} {name}")
        else:
            print(f"  CHANGED  {name}")
            diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "golden", "now", n=0, lineterm=""))[2:]
            for i, d in enumerate(diff[:80]):
                nxt = diff[i + 1] if i + 1 < len(diff) else ""
                if d.startswith("-") and nxt.startswith("+") and len(d) > 200:
                    # long changed line: show only the part that differs
                    sm = difflib.SequenceMatcher(None, d[1:], nxt[1:])
                    for op, a0, a1, b0, b1 in sm.get_opcodes():
                        if op != "equal":
                            print(f"     ~ …{d[1:][max(0, a0 - 50):a1 + 30]}…")
                            print(f"     ~ →…{nxt[1:][max(0, b0 - 50):b1 + 30]}…")
                    continue
                if d.startswith("+") and i and diff[i - 1].startswith("-") and len(diff[i - 1]) > 200:
                    continue
                print("     " + d[:200])
    print(f"{changed} sample(s) differ" + (" (accepted)" if a.accept else ""))


if __name__ == "__main__":
    main()
