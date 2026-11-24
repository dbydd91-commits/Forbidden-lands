# Project log
Limit: 3,000 characters (see CLAUDE.md). Merge or compress older entries; don't just append.

## Done
- Session 1: `tools/fl2md.py` written from scratch (pymupdf4llm dropped: it broke the styled tables).
  Lines classified by font (IM Fell = prose, Branding = tables/boxes/stats); regions → Table / Stats / Box;
  facing-page tables joined; stat blocks continued across columns joined; watermark, running heads, art labels dropped.
- PHB extracted: `sources/fl-players-handbook.md` (printed p.4–200; char sheets + index skipped).
  `--check`: ~124 word occurrences missing, almost all weapon-illustration labels (p.109–112, 186–193).
  p.35 example character sheet hand-transcribed (`tools/figures/phb.md`).
- Spot-checked on GMG bestiary (p.77–132) and Legends & Adventurers: stats/tables OK, 0–1 words missing.

## Commands
```
python tools/fl2md.py Forbidden_Lands_Players_Handbook_5th_printing.pdf sources/fl-players-handbook.md \
  --title "Forbidden Lands — Player's Handbook (5th printing)" --pages 8-204 --figures tools/figures/phb.md
```
Add `--check` for the missing-word report. PDFs: user's Drive folder (not committed).
