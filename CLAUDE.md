# Forbidden Lands — source extraction project

Goal: the Forbidden Lands books as grep-friendly Markdown that an LLM can read, quote and cite
reliably. Stage 1 = faithful extraction (`sources/`); no rules rewriting yet. Private project.

## Layout

| Path | What |
|---|---|
| `sources/fl-players-handbook.md` | Player's Handbook (5th printing), printed p.4–200, ~90k tokens |
| `sources/fl-gamemasters-guide.md` | Gamemaster's Guide (5th printing), printed p.4–261, ~125k tokens |
| `tools/fl2md.py` | PDF → Markdown converter (conventions in its docstring) |
| `tools/regress.py`, `tests/` | Regression check: sample pages (`tests/samples.txt`) vs `tests/golden/` |
| `notes/` | `handoff.md` (next task), `log.md` (what's done) |

Planned: Legends & Adventurers, Raven's Purge. Map & Stickers is image-only.
Maps (GMG p.46–47, 198–199, 218–219, 234–235) are replaced by `**[Figure — …]**` notes (`tools/figures/`).

## Reading the sources (token budget)
- **Never Read a whole source file.** grep → `Read offset/limit` around the hit.
- Outline: `grep -n '^#\{1,4\} ' sources/<book>.md` (`#`–`####` = PDF bookmarks; `#####` = other in-page headings).
- Blocks: `grep -n 'Table: \|Stats: \|Box: ' sources/<book>.md`
  - `Table: X` = GFM table, one row per line. Facing-page tables and tables continued on the next
    page(s) are joined into one; the heading then ends with the page range, e.g. `<!-- p.12–15 -->`.
    `Table: (continued)` = continuation that could not be joined (text in between).
  - `Stats: X` = `- **Key:** value` list (monster/NPC stat boxes, kin info).
  - `Box: X` = sidebar as `>` quote, placed at the next paragraph break.
- Page anchors `<!-- p.N -->` = printed page N (matches the books' "see page N"); may sit inline when a paragraph crosses pages.
- Dice symbols: ⚔ = success (sword), ☠ = bane (skull). Small-caps game terms keep the book's CAPITALS (MOVE, NEAR).
- Cite as (PHB p.N), (GMG p.N), (L&A p.N), (RP p.N).

## Regenerate
PDFs are not committed (`.gitignore`). `pip install pymupdf`, then the commands in `notes/log.md`.

## Changing the converter
- After every change run `python tools/regress.py --pdf-dir DIR` (~10 s), not whole books.
  Review each CHANGED diff; `--accept` only intended changes. Add a sample when a new page type is fixed.
- Regenerate whole books (~15 s each) only once the samples look right, then diff against the committed `sources/`.
- Fix what the user asked first; list other problems found and ask before widening the scope.

## Git
- Push commits to `main` (default branch) and to the session branch if one is assigned.

## Notes size limits
- `notes/handoff.md` ≤ 2,000 characters, `notes/log.md` ≤ 3,000 characters. Check with `wc -m` before committing.
- Start each session by reading `notes/handoff.md`.

## Discussion
- Talk with the user in Korean; keep game terms in English.
