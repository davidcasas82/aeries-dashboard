# Work tags

Each row in the class drawer carries a tag built from the class's `counted_insight` footer weights. A percent-of-grade class reads `<Category> · N% of grade`. A summative/formative class reads `Summative · N%` or `Formative · N%`, and a category on both sides reads `<Category> · Summative N% or Formative N%`. A 0% category reads `<tag> · 0% (doesn't count)` in a dashed tag. The one category or bucket with the unique highest weight above 0 also gets a `weighs most` marker; a tie gets none. The bare category name shows only when no weight is known, and a row with no category has no tag.

The fixture gives `Fixture Beta` two classes:

- Science Fixture is percent of grade (Assessments 70 / Assignments 20 / Presentations 10 / Daily Assignments 0). `Unit Project` is `Assessments · 70% of grade` with `weighs most`, `Lab Safety` is `Assignments · 20% of grade`, `Notebook Check` is `Daily Assignments · 0% (doesn't count)`, `Field Notes` is the bare `Field Work` (not in the footer), and the Classroom card `Study Guide` has no tag.
- History Fixture is summative/formative 70/30. `Unit Essay` is `Summative · 70%` with `weighs most`, `Reading Notes` is `Formative · 30%`, and `Timeline Project` is `Projects · Summative 70% or Formative 30%`.

## Sub-features

- `tag-percent-of-grade` shows `<Category> · N% of grade`.
- `tag-bucket` shows `Summative · N%` or `Formative · N%`.
- `tag-weighs-most` marks the unique highest weight above 0.
- `tag-zero-weight` reads `· 0% (doesn't count)` in a dashed tag.
- `tag-bare` shows the category name when no weight is known.
- `tag-none` shows no tag when there is no category.

## How to get to it (user POV)

- Unlock, choose `Fixture Beta` in the header, then choose the Science Fixture or History Fixture grade in Current standing.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The browser is on the lock screen.

- **Unlock.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive fill --selector "#pinInput" --value "246801"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open grades"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "h2" --text "Current standing"`. The Current standing heading is visible.
- **Switch student.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Fixture Beta"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#topBarTitle" --text "Fixture Beta"`.
- **Open the percent-of-grade class.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open detail for Science Fixture, A, 91%, no baseline"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#glanceDrawerTitle" --text "Science Fixture"`.
- **Read the tags.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive text --selector "#glanceWorkList"`. `Unit Project` is followed by `Assessments · 70% of grade` and `weighs most`, `Lab Safety` by `Assignments · 20% of grade`, `Notebook Check` by `Daily Assignments · 0% (doesn't count)`, and `Field Notes` by `Field Work`. `Study Guide` has no tag line.
- **Proof.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "<evidence dir>/work-tags-percent-of-grade.png"`.
- **Open the summative/formative class.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Close details"`, then `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open detail for History Fixture, B, 86%, no baseline"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#glanceDrawerTitle" --text "History Fixture"`. `Unit Essay` reads `Summative · 70%` and `weighs most`, `Reading Notes` reads `Formative · 30%`, and `Timeline Project` reads `Projects · Summative 70% or Formative 30%`. Screenshot to `<evidence dir>/work-tags-summative-formative.png`.
- **Mobile.** Export `AERIES_VERIFY_VIEWPORT=390x844`, close and reopen the Science drawer, then screenshot. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --selector ".glance-work-tag.zero_weight"` to scroll the 0% row into view before a second screenshot.

## Gotchas

- The page builds these tags itself (`glanceWorkTag` in `index.html`); `glance.work_tag` must agree. `tests/test_glance.py` `WorkTagPageParityTests` runs both through node.
- The drawer is a bottom sheet under 620px wide and does not fit every row at 390×844. Scroll before judging the Turned in group.
- `AERIES_VERIFY_EVIDENCE` overrides the evidence directory when the default store path does not exist.
