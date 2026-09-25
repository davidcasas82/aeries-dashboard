# Settings lock

Settings is a drawer with the keyboard shortcuts and a lock button. Lock clears the session and returns to the PIN screen.

## Sub-features

- `settings-open` opens the settings drawer from the header.
- `settings-lock` returns to the lock screen.
- `settings-escape` closes settings without locking.

## How to get to it (user POV)

- Unlock, then choose the header button named `Settings`.
- Choose `Lock dashboard` inside Settings.
- Press `Escape` to close Settings without locking. `Escape` closes an open detail drawer first when one is open.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The board is unlocked on Current standing.

- **Open settings.** Choose `Settings`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Settings"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#settingsOverlay.open h2" --text "Settings"`. The drawer shows the Shortcuts heading and `Lock dashboard`.
- **Lock.** Choose `Lock dashboard`. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Lock dashboard"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#lockScreen" --text "Family PIN"`. The Family PIN field is back and Current standing is gone.
- **Proof.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/settings-lock.png"`. The image shows the lock screen.

## Gotchas

- `Escape` while the detail drawer is open closes the drawer and leaves Settings alone. Close the drawer before using `Escape` as a settings test.
- Lock clears the token in that Chrome profile. The next open requires the fixture PIN again.
- The settings button's accessible name is `Settings`, not the gear icon.
