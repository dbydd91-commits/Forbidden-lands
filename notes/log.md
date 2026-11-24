# Project log
Limit: 3,000 characters (see CLAUDE.md). Merge or compress older entries; don't just append.

## Done
- Session 1: `tools/fl2md.py` written from scratch (pymupdf4llm dropped: it broke the styled tables).
  Lines classified by font (IM Fell = prose, Branding = tables/boxes/stats); regions → Table / Stats / Box;
  facing-page tables joined; stat blocks continued across columns joined; watermark, running heads, art labels dropped.
- PHB extracted: `sources/fl-players-handbook.md` (printed p.4–200; char sheets + index skipped).
  `--check`: ~124 word occurrences missing, almost all weapon-illustration labels (p.109–112, 186–193).
  p.35 example character sheet hand-transcribed (`tools/figures/phb.md`).
- Review 1 (user): Age table (header-less row-label column, merged header words), Artifact Dice (glyphs drawn
  twice), List of Talents (→ grouped list), List of Functions (side-by-side halves stacked); paragraph breaks
  via first-line indent; list items / spell stat lines; fuzzy bookmark match.
- Speed: text extracted once per page, 4 worker processes (PHB 2 min → 13 s, identical output).
  `tools/regress.py` + 18 sample page sets (`tests/samples.txt`, golden in `tests/golden/`), ~10 s.
- GMG extracted (printed p.4–261, index skipped): 101 tables, 68 Stats, all 66 Stats bookmarks placed;
  `--check` ~84 missing, mostly map labels. Fixes: map pages → figure notes; text drawn twice over art
  (partial copies); encounter number badges; small-caps NPC headings; Stats bookmark goes to the heading
  above; side-by-side table halves; table footnotes → `*Note:*`. Also fixed: PHB crit tables had a bogus
  empty first column since review 1 (caught by the samples).
- Review 2: tables continued over pages joined (heading gets page range); overlay copies matched as
  letter subsequences; calendar wheel figure (`drop y>N` figure mode); letter-by-letter chapter titles
  dropped; bookmark typos for Box captions; hyphenated words kept whole around page anchors.
- Review 3 (same lens on PHB, then GMG): one-column facing-page halves; header-less 2-column tables
  (PHB p.176 Defense/Attack Rating: values were detached); side-by-side tables split at the gutter;
  caption + intro block (or "AGE: …" modifier lines) attached to the table below, also across pages;
  uncaptioned continuations no longer steal the next table's bookmark; drop-cap "A"/"I" as a word.

## Commands
```
python tools/fl2md.py Forbidden_Lands_Players_Handbook_5th_printing.pdf sources/fl-players-handbook.md \
  --title "Forbidden Lands — Player's Handbook (5th printing)" --pages 8-204 --figures tools/figures/phb.md
python tools/fl2md.py Forbidden_Lands_Gamemasters_Guide_5th_printing.r.pdf sources/fl-gamemasters-guide.md \
  --title "Forbidden Lands — Gamemaster's Guide (5th printing)" --pages 8-265 --figures tools/figures/gmg.md
```
Add `--check` for the missing-word report. PDFs: user's Drive folder (not committed).
