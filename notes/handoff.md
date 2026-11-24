# Handoff
Limit: 2,000 characters (see CLAUDE.md). Rewrite only at session end; next task only.

## Next: user review of PHB + GMG, then Legends & Adventurers and Raven's Purge
1. User reviews `sources/fl-gamemasters-guide.md` (bestiary Stats, encounters, adventure sites, artifacts).
   Fix in the tool, run `tools/regress.py`, accept intended diffs, regenerate.
2. Legends & Adventurers (pdf p.2–40): no page labels; printed page numbers come from the footer.
3. Raven's Purge: different layout, no typed bookmarks — inspect fonts first, add samples.
Known open point: GMG p.52 "Box: Elven Saying" bookmark has no Branding box (italic quote), so its
heading sits at the page top.
