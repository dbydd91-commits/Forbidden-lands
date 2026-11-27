# Handoff
Limit: 2,000 characters (see CLAUDE.md). Rewrite only at session end; next task only.

## Status
All four books extracted and QA'd (verify: ≥95% words within ±1 page except art/map pages; ~50 pages
compared by eye). Use them for rules questions (see CLAUDE.md "Answering rules questions").

## Next: split tables (QA + converter fix)
- Defect: a table continued in the other column/page without a caption is emitted as a separate
  caption-less table, often under a later heading, with no `Table: (continued)` marker.
- Hand-fixed in `sources/` only (PHB p.148–149 Leading the Way Mishaps, p.154–155 Making Camp
  Mishaps; p.156 Sea Travel Mishaps moved to Sea Travel). Regenerating PHB reverts these until fixed.
- To do: fix fl2md.py joins, add a sample (phb 147-149, 154-156), regress, regenerate, diff vs the
  hand fix. Then QA all books for caption-less tables (awk: table not within 4 lines of a
  `Table:` heading); suspects: GMG Hollows event table (sources line ~4106), L&A Home Region / Talent table halves.

## Then: user review / more books
- The user reports problems while using the sources: fix the request first in the tool, run
  `tools/regress.py`, accept intended diffs, regenerate, `tools/verify.py`.
- Small leftovers: RP p.83 stray "B"; GMG p.52 Box heading at page top; synthetic Stats names follow
  the nearest heading.
- A new book: inspect fonts first (`get_text("dict")` font counts). Same publisher (IM Fell / Branding)
  should work as is; other fonts → make the font names in fl2md.py configurable. Add 5–7 sample page
  sets per book before changing the tool; compare ~10 pages by eye.

## Deferred: maps
Hex map (separate PDF) and site maps are not data. Rules need only terrain per hex, river borders,
impassable High Mountains, site symbols (type only; GM assigns sites, GMG p.167) and hex distances.
If wanted later: a hex → terrain / river edges / symbol / place-name table.
