# Open standing detail

Current standing lists every class with its mark, percent, and seven-day trend. Choosing a grade opens a drawer with that class's work.

## Sub-features

- `standing-board` shows a grade button for each class after unlock.
- `standing-drawer` opens the work list for the chosen class.
- `standing-close` closes the drawer and leaves the board in place.

## How to get to it (user POV)

- Unlock, then choose a grade button in Current standing. The button name includes the course, mark, percent, and trend.
- Choose `Close details` or press `Escape` to dismiss the drawer.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The browser is on the lock screen.

- **Unlock.** Enter the fixture PIN and open the board. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive fill --selector "#pinInput" --value "246801"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open grades"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "h2" --text "Current standing"`. The Current standing heading is visible.
- **Capture the board.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/standing-board.png"`. The image shows the grade button for `Algebra Fixture`.
- **Open the class.** Choose the Algebra Fixture grade. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open detail for Algebra Fixture, A, 94%, no baseline"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#glanceDrawerTitle" --text "Algebra Fixture"`. The drawer title is `Algebra Fixture`.
- **Confirm the work.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive visible --text "Lab Worksheet"`. The drawer lists `Lab Worksheet`.
- **Capture the drawer.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive text --selector "#glanceDrawerTitle"` and write stdout to `/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/standing-drawer-title.txt`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/standing-drawer.png"`. The title file reads `Algebra Fixture` and the image shows `Lab Worksheet` in the drawer.
- **Close.** Choose `Close details`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Close details"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "h2" --text "Current standing"`. The board heading remains and the drawer title is no longer showing as an open dialog (`#glanceDrawer` has `aria-hidden="true"`).

## Gotchas

- The accessible name includes the trend text. This fixture has no grade history, so the name ends in `no baseline`. A payload with a 7-day delta changes that name.
- `Lab Worksheet` is also listed under Due soon. The drawer title `Algebra Fixture` is the proof that the grade button opened the class, not only that the assignment is on the board.
- On a viewport narrower than 620px the drawer slides up from the bottom. This harness forces 1400×1000.
