# Coming up order

The class drawer groups work into Past due, Missing, Coming up, Turned in, and Marked. Coming up reads the soonest due date first, and work with no due date comes after every dated row. The other groups keep the newest date first. The fixture gives Science Fixture (`Fixture Beta`) `Field Notes` on the second school day from today, `Unit Project` on the fourth, and an undated Classroom card `Study Guide`.

## Sub-features

- `coming-up-soonest` puts the nearest due date at the top of Coming up.
- `coming-up-undated-last` lists work with no due date after every dated Coming up row.

## How to get to it (user POV)

- Unlock, choose `Fixture Beta` in the header, then choose the Science Fixture grade in Current standing.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The browser is on the lock screen.

- **Unlock.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive fill --selector "#pinInput" --value "246801"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open grades"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "h2" --text "Current standing"`. The Current standing heading is visible.
- **Switch student.** Choose `Fixture Beta`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Fixture Beta"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#topBarTitle" --text "Fixture Beta"`. `Science Fixture` is on screen.
- **Open the class.** Choose the Science Fixture grade. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open detail for Science Fixture, A, 91%, no baseline"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#glanceDrawerTitle" --text "Science Fixture"`. The drawer title is `Science Fixture`.
- **Read the order.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive text --selector "#glanceWorkList"` and write stdout to `/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/coming-up-order.txt`. Under `COMING UP`, `Field Notes` comes before `Unit Project`, and `Study Guide` is the last Coming up row, above `TURNED IN`.
- **Proof.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/coming-up-drawer.png"`. The image shows `Field Notes` above `Unit Project` above `Study Guide` in the drawer.

## Gotchas

- `innerText` upper-cases the group labels. Match `COMING UP` and `TURNED IN`, not `Coming up`, when reading `#glanceWorkList`.
- All three Science rows are also in Fixture Beta's Due soon band, which already read soonest first before this rule. The drawer order is the proof for this feature, not the band.
- The Algebra Fixture drawer has one Coming up row, so it cannot show this order. Use Fixture Beta.
