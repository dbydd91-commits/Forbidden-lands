# Project log
Limit: 3,000 characters (see CLAUDE.md). Merge or compress older entries; don't just append.

## Done
- Converter `tools/fl2md.py` (own layout engine). Lines by font (IM Fell = prose, Branding =
  tables/boxes/stats) → regions → Table / Stats / Box; 4 workers, ~5–15 s per book.
- Checks: `tools/regress.py` + `tests/samples.txt` (38 page sets, golden in `tests/golden/`);
  `tools/verify.py` (per-page word share, missing numbers). ~50 pages also compared by eye.
- Extracted: PHB, GMG, L&A, Raven's Purge. Hand figures in `tools/figures/` (PHB p.35 sheet; GMG kin
  map, village maps, calendar). RP maps get automatic figure notes.
- Table handling (reviews 1–5): facing-page / next-page joins with page range; side-by-side tables
  split at the gutter (paired captions or "D66" headers; not halves under one caption); stacked
  halves; header-less and one-header-cell tables; caption/intro/modifier lines attached; grouped
  lists; footnotes (`*Note:*`, "*" lines); hyphenated headers.
- Text: overlay copies removed; paragraph rules (indent, word-fit in boxes, lists, spell lines,
  italic flavour, page ends); hyphenation (vocabulary + safe compounds); small-caps headings; labels
  (bold, italic colon, colon outside bold); doubled bullet glyphs; acronyms (NPCs, XP).
- RP specifics: all-caps bookmarks title-cased; map keys dropped + figure note; synthetic
  `Stats:` headings (also added in PHB/GMG where no bookmark named the block).
- Hand fix (no converter change yet): PHB split mishap tables joined, Sea Travel table moved.
- Known leftovers: `--check` misses art/map labels and repeated table headers; GMG p.52 Box: Elven
  Saying heading sits at the page top.

## Commands
```
python tools/fl2md.py Forbidden_Lands_Players_Handbook_5th_printing.pdf sources/fl-players-handbook.md \
  --title "Forbidden Lands — Player's Handbook (5th printing)" --pages 8-204 --figures tools/figures/phb.md
python tools/fl2md.py Forbidden_Lands_Gamemasters_Guide_5th_printing.r.pdf sources/fl-gamemasters-guide.md \
  --title "Forbidden Lands — Gamemaster's Guide (5th printing)" --pages 8-265 --figures tools/figures/gmg.md
python tools/fl2md.py Forbidden_Lands_Legends_and_Adventurers_5th_printing.pdf sources/fl-legends-and-adventurers.md \
  --title "Forbidden Lands — Legends & Adventurers (5th printing)" --pages 1-40
python tools/fl2md.py Forbidden_Lands_Raven_s_Purge.pdf sources/fl-ravens-purge.md \
  --title "Forbidden Lands — Raven's Purge" --pages 5-225
```
