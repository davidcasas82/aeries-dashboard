# Switch student

The header picker and the number keys change which student's grades are on screen. The top bar title follows the selected student.

## Sub-features

- `switch-picker` selects the other student from the header.
- `switch-keys` selects a student with `1` or `2` when focus is outside a field.

## How to get to it (user POV)

- Unlock, then choose the other name in the header control labeled `Switch student`.
- Press `1` or `2` while focus is outside a text field.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The board is unlocked on Current standing for the first student (`Fixture Alpha`, course `Algebra Fixture`).

- **Open the second student from the header.** Choose `Fixture Beta`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Fixture Beta"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#topBarTitle" --text "Fixture Beta"`. `Science Fixture` is on screen and `Algebra Fixture` is not the active board.
- **Confirm the course.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive visible --text "Science Fixture"`.
- **Return with the keyboard.** Press `1`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive press --key "1"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#topBarTitle" --text "Fixture Alpha"`. The title is `Fixture Alpha` and `Algebra Fixture` is visible again.
- **Proof.** Capture both titles. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/switch-student.png"`. The image shows `Fixture Alpha` in the top bar and `Algebra Fixture` on the board.

## Gotchas

- The picker is hidden when the payload has fewer than two students. This fixture has two.
- Keys `1` and `2` do nothing while a text field has focus.
- The active student resets to the first student on the next full load.
