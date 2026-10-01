# Work tags

Each row in the class drawer carries a tag. It reads `Summative` or `Formative` when the class's `counted_insight` category for that assignment has that kind, otherwise the Aeries category name as-is, and nothing when the assignment has no category. Scored work in a 0%-weight category also shows `0% category (doesn't count)`. The fixture gives Science Fixture (`Fixture Beta`) `Unit Project` (Summative), `Lab Safety` (Formative), `Field Notes` (category `Participation`), `Notebook Check` (category `Lab Notebook`, 0% weight), and the Classroom card `Study Guide` with no category.

## Sub-features

- `tag-kind` shows `Summative` or `Formative` from the category kind.
- `tag-category-fallback` shows the raw category name when there is no kind.
- `tag-none` shows no tag when there is no category.
- `tag-zero-weight` adds the `doesn't count` note on scored work in a 0%-weight category.

## How to get to it (user POV)

- Unlock, choose `Fixture Beta` in the header, then choose the Science Fixture grade in Current standing.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The browser is on the lock screen.

- **Unlock.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive fill --selector "#pinInput" --value "246801"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open grades"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "h2" --text "Current standing"`. The Current standing heading is visible.
- **Switch student.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Fixture Beta"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#topBarTitle" --text "Fixture Beta"`.
- **Open the class.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open detail for Science Fixture, A, 91%, no baseline"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#glanceDrawerTitle" --text "Science Fixture"`.
- **Read the tags.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive text --selector "#glanceWorkList"`. `Field Notes` is followed by `Participation`, `Unit Project` by `Summative`, `Lab Safety` by `Formative`, and `Notebook Check` by `Lab Notebook` and `0% category (doesn't count)`. `Study Guide` has no tag line.
- **Proof.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "<evidence dir>/work-tags-drawer.png"`.
- **Mobile.** Export `AERIES_VERIFY_VIEWPORT=390x844`, close and reopen the drawer, then screenshot again. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --selector ".glance-work-tags .assn-status.zero_weight"` to scroll the Turned in rows into view before a second screenshot.

## Gotchas

- The 0%-weight note follows `inferAssignmentStatus`, which returns early for unscored work. An unscored row in a 0% category shows its category tag but no note.
- The drawer is a bottom sheet under 620px wide and does not fit every row at 390×844. Scroll before judging the Turned in group.
