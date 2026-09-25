# Aeries dashboard verification map

This directory is the maintained source for verifying the user-facing behavior of the Aeries family grade dashboard. Read the index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch with `.cursor/skills/verify-aeries-dashboard/verify.sh launch` from the repo root.
- The static page is served with `python3 -m http.server` on `127.0.0.1` (port `8765` when it is free).
- The fixture API owns `127.0.0.1:8787`. Do not start a second instance against that port.
- The fixture PIN is `246801`. The roster is synthetic: `Fixture Alpha` and `Fixture Beta`.
- Run `verify.sh doctor` and require `doctor ok` before driving.
- Never drive an instance that was not started by this verification run. Never point the browser at the production worker.

## Driving conventions

- Start every recipe from the lock screen unless its preconditions say otherwise.
- Prefer ids, ARIA roles, and accessible names.
- Treat every command as literal. Keep quoted names and flags unchanged.
- Run browser actions through `.cursor/skills/verify-aeries-dashboard/verify.sh drive`.
- Leave proof files in `/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/`. Cleanup must not remove them.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final screen.
- UI proof includes a screenshot of the action and a screenshot of the result, plus the text the recipe names.
- Record the feature file used with every artifact.
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.
- Do not write household names, student numbers, or live grade JSON into evidence.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with verify.sh` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

## Features

- [Unlock the dashboard](./unlock.md) covers the PIN gate, a rejected PIN, and a successful open.
- [Switch student](./switch-student.md) covers the header picker and the `1` / `2` keys.
- [Open standing detail](./standing-detail.md) covers the Current standing grade buttons and the detail drawer.
- [Due soon](./due-soon.md) covers the due-soon band on the unlocked board.
- [Settings lock](./settings-lock.md) covers the settings drawer and locking the board again.
