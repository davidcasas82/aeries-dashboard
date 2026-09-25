# Unlock the dashboard

The lock screen asks for the household PIN and opens the grade board only after the fixture accepts it.

## Sub-features

- `unlock-rejected` shows an error and stays on the lock screen for a wrong PIN.
- `unlock-open` accepts the fixture PIN and shows the grade board.

## How to get to it (user POV)

- Open the local dashboard. The lock screen is the first screen.
- Choose `Lock dashboard` in Settings to return here after a successful unlock.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The browser is on the lock screen, with the Family PIN field visible.

- **Reject a PIN.** Type `000000` and choose `Open grades`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive fill --selector "#pinInput" --value "000000"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open grades"`. Then run `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#lockError" --text "That PIN did not work."`. The error is visible and the Family PIN field is still on screen.
- **Enter the fixture PIN.** Replace the field with `246801`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive fill --selector "#pinInput" --value "246801"`.
- **Open the board.** Choose `Open grades`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open grades"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "h2" --text "Current standing"`. The Current standing heading is visible.
- **Proof.** Capture the unlocked board. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/unlock-board.png"`. The image shows Current standing and does not show the Family PIN field.

## Gotchas

- `Keep me unlocked on this computer` is checked by default. The throwaway Chrome profile is deleted on cleanup, so a later launch starts locked again.
- A wrong PIN leaves the previous value in the field. Fill replaces it; do not append.
- The production worker is not a stand-in. Doctor must report the fixture before this recipe counts.
