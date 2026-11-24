# Handoff
Limit: 2,000 characters (see CLAUDE.md). Rewrite only at session end; next task only.

## Next: more books (user has others to extract) / user review
- All four Forbidden Lands books are in `sources/`. The user reads them and reports problems: fix the
  request first in the tool, run `tools/regress.py`, accept intended diffs, regenerate, `tools/verify.py`.
- A new book: inspect fonts first (`get_text("dict")` font counts). Same publisher (IM Fell / Branding)
  should work as is; other fonts → make the font names in fl2md.py configurable. Add 5–7 sample page
  sets per book before changing the tool; compare ~10 pages by eye.
