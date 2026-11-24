# Project log
Limit: 3,000 characters (see CLAUDE.md). Merge or compress older entries; don't just append.

## Done
- Converter `tools/fl2md.py` (own layout engine; pymupdf4llm dropped). Lines by font (IM Fell = prose,
  Branding = tables/boxes/stats) → regions → Table / Stats / Box; 4 workers, ~15 s per book.
- Regression: `tools/regress.py` + `tests/samples.txt` (32 page sets) vs `tests/golden/`.
- PHB (printed p.4–200), GMG (p.4–261), L&A (whole) extracted. Maps/figures hand-transcribed in
  `tools/figures/` (PHB p.35 sheet; GMG kin map, village maps, calendar wheel).
- Table handling built up over 3 user reviews: facing-page and next-page continuations joined (page range
  in heading); side-by-side tables split at the gutter (paired captions or paired "D66" headers, not when
  one caption spans both halves); repeated-header halves stacked; header-less and one-header-cell tables;
  caption/intro/modifier lines attached to the table; list-like tables as grouped lists; footnotes.
- Text: overlay copies over art removed (letter subsequence), paragraph rules (indent, list items, spell
  stat lines, italic flavour), hyphenation via vocabulary, small-caps NPC headings, encounter badges,
  letter-spaced titles, bookmark typos (fuzzy), Stats bookmarks to the heading above.
- Review 4 (verification): `tools/verify.py` per-page word share + numbers. Found and fixed: tables
  waiting for a paragraph end drifted pages (PHB p.101–103 weapons, p.147, GMG p.197/217); hyphenated
  table headers; sidebars continued over a page; box paragraphs by word-fit test; numbered steps in boxes.
- Review 5 (visual sampling, ~12 table/stat pages): table values all matched. Fixed: "*" table
  footnotes, "gear/ weapon", kept hyphens (half-, two-, -like), NPC/XP acronyms, italic-colon labels,
  bold labels in bullets (CREATURES:, REQUIREMENT:), sidebars no longer split an italic example.
  Round 2: "…" list items, NPC **Attributes:**, bold-led stat rows, colon outside a bold
  label (KIN TALENT: Adaptive), italic→roman paragraph break at a sentence end.
- Known leftovers: `--check` misses ~200/150/40 words (PHB/GMG/L&A): art labels, map labels and dropped
  repeated table headers. GMG p.52 Box: Elven Saying heading sits at the page top.

## Commands
```
python tools/fl2md.py Forbidden_Lands_Players_Handbook_5th_printing.pdf sources/fl-players-handbook.md \
  --title "Forbidden Lands — Player's Handbook (5th printing)" --pages 8-204 --figures tools/figures/phb.md
python tools/fl2md.py Forbidden_Lands_Gamemasters_Guide_5th_printing.r.pdf sources/fl-gamemasters-guide.md \
  --title "Forbidden Lands — Gamemaster's Guide (5th printing)" --pages 8-265 --figures tools/figures/gmg.md
python tools/fl2md.py Forbidden_Lands_Legends_and_Adventurers_5th_printing.pdf sources/fl-legends-and-adventurers.md \
  --title "Forbidden Lands — Legends & Adventurers (5th printing)" --pages 1-40
```
Add `--check` for the missing-word report. PDFs: user's Drive folder (not committed).
