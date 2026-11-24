# Forbidden Lands — source extraction project

Goal: the Forbidden Lands books as grep-friendly Markdown that an LLM can read, quote and cite
reliably. Stage 1 = faithful extraction (`sources/`); no rules rewriting yet. Private project.

## Layout

| Path | What |
|---|---|
| `sources/fl-players-handbook.md` | Player's Handbook (5th printing), printed p.4–200 |
| `tools/fl2md.py` | PDF → Markdown converter (conventions in its docstring) |
| `notes/` | `handoff.md` (next task), `log.md` (what's done) |

Planned: Gamemaster's Guide, Legends & Adventurers, Raven's Purge. Map & Stickers is image-only.

## Reading the sources (token budget)
- **Never Read a whole source file.** grep → `Read offset/limit` around the hit.
- Outline: `grep -n '^#\{1,4\} ' sources/<book>.md` (`#`–`####` = PDF bookmarks; `#####` = other in-page headings).
- Blocks: `grep -n 'Table: \|Stats: \|Box: ' sources/<book>.md`
  - `Table: X` = GFM table, one row per line. Facing-page tables are joined into one.
  - `Stats: X` = `- **Key:** value` list (monster/NPC stat boxes, kin info).
  - `Box: X` = sidebar as `>` quote, placed at the next paragraph break.
- Page anchors `<!-- p.N -->` = printed page N (matches the books' "see page N"); may sit inline when a paragraph crosses pages.
- Dice symbols: ⚔ = success (sword), ☠ = bane (skull). Small-caps game terms keep the book's CAPITALS (MOVE, NEAR).
- Cite as (PHB p.N), (GMG p.N), (L&A p.N), (RP p.N).

## Regenerate
PDFs are not committed (`.gitignore`). `pip install pymupdf`, then the commands in `notes/log.md`.

## Git
- Push commits to `main` (default branch) and to the session branch if one is assigned.

## Notes size limits
- `notes/handoff.md` ≤ 2,000 characters, `notes/log.md` ≤ 3,000 characters. Check with `wc -m` before committing.
- Start each session by reading `notes/handoff.md`.

## Discussion
- Talk with the user in Korean; keep game terms in English.
