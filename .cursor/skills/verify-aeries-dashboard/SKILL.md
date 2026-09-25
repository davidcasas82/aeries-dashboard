---
name: verify-aeries-dashboard
description: Drive the Aeries family grade dashboard in a browser against a local fixture and the repo static server. Use to prove the lock screen, student switch, standing detail, due-soon list, or settings lock.
---

# Verify aeries-dashboard

The primary surface is the static parent dashboard in `index.html` (GitHub Pages). A second surface is the Python scraper; this skill does not drive it. The page unlocks with a PIN, then reads grades from family-data. Verification never calls the production worker and never uses a live scrape. A local fixture on `127.0.0.1:8787` serves a synthetic roster (`Fixture Alpha`, `Fixture Beta`) so proof screenshots do not contain household names or student numbers.

Run every command from the repo root. The harness is `.cursor/skills/verify-aeries-dashboard/verify.sh`.

## Launch

Start one isolated instance. The static server is the repo's own preview command (`python3 -m http.server`), bound to `127.0.0.1`. Port `8765` is preferred; a higher free port is used when `8765` is taken. The page always calls `http://127.0.0.1:8787` from localhost, so the fixture API must own that port. A second launch is refused while a state file exists or while `8787` is taken. Right after `cleanup`, `8787` can stay taken by TIME_WAIT sockets for up to a minute; wait before launching again. Chrome uses a throwaway profile under `/tmp/aeries-dashboard-verify/`.

```bash
.cursor/skills/verify-aeries-dashboard/verify.sh launch
```

Ready means the command prints `doctor ok` and `launched http://127.0.0.1:<port>/`, and the browser is on the lock screen (`#pinInput` visible). Fixture PIN: `246801`. That PIN is not a household PIN.

Teardown is `verify.sh cleanup`. It stops only the processes recorded for this run.

## Doctor

Read-only check. Run it before driving whenever the page looks wrong.

```bash
.cursor/skills/verify-aeries-dashboard/verify.sh doctor
```

Exit 0 prints `doctor ok` with the static origin. The check requires all three of these:

- The fixture API, static server, and Chrome are the processes this launch started.
- `http://127.0.0.1:8787/health` returns `{"ok": true, "fixture": true}`.
- The static origin serves `index.html` (lock field `#pinInput`, button text `Open grades`), the fixture roster is the two synthetic students, and Chrome is open on that origin.

Doctor does not print the roster. A mismatch is reported as `fixture roster mismatch`.

## Drive

`verify.sh drive` talks to the Chrome launch started. Prefer ids, ARIA roles, and accessible names. Desktop layout is a 1400×1000 viewport, so the detail drawer opens from the right.

| Command | Effect |
| --- | --- |
| `verify.sh drive fill --selector "#pinInput" --value "246801"` | Set the Family PIN field |
| `verify.sh drive click --role button --name "Open grades"` | Submit the lock form |
| `verify.sh drive click --role button --name "Fixture Beta"` | Switch student |
| `verify.sh drive click --role button --name "Open detail for Algebra Fixture, A, 94%, no baseline"` | Open that class drawer |
| `verify.sh drive click --role button --name "Settings"` | Open settings |
| `verify.sh drive click --role button --name "Lock dashboard"` | Lock from settings |
| `verify.sh drive click --role button --name "Close details"` | Close the drawer |
| `verify.sh drive press --key "1"` | Keyboard switch to the first student |
| `verify.sh drive wait --selector "h2" --text "Current standing"` | Wait until that heading is showing |
| `verify.sh drive visible --text "Lab Worksheet"` | Fail unless that text is on screen |
| `verify.sh drive text --selector "#glanceDrawerTitle"` | Print the drawer title |
| `verify.sh drive screenshot --path "<evidence path>"` | Write a PNG of the viewport |

Recipes and observable results live in `features/`. Drive one feature from the lock screen unless that feature says it continues from another.

## Evidence

Proof shows the user action and the resulting state. For the standing-detail feature that is the unlocked board and then the open drawer, plus the drawer title text. Screenshots and text files go to:

`/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/`

Use filenames that do not contain student names or student numbers. `standing-board.png`, `standing-drawer.png`, and `standing-drawer-title.txt` are the standing-detail set. Do not capture production data. The fixture is the production boundary: the dashboard's real fetch path runs, and the worker is replaced only at `127.0.0.1:8787`. Confirm the browser origin is the local static server (doctor does this) so a run cannot silently hit `family-data.davidcasas.workers.dev`.

## Cleanup

```bash
.cursor/skills/verify-aeries-dashboard/verify.sh cleanup
```

Cleanup kills the Chrome, static server, and fixture API process groups recorded in `/tmp/aeries-dashboard-verify/state.json`, deletes the throwaway Chrome profile, and removes the state file. It does not delete the evidence directory. It does not kill processes by name.

## Helpers

- `.cursor/skills/verify-aeries-dashboard/verify.sh` — `launch`, `doctor`, `drive`, `cleanup`
- `.cursor/skills/verify-aeries-dashboard/helpers/fixture_api.py` — synthetic family-data API, started by `launch` (not run on its own)
- `.cursor/skills/verify-aeries-dashboard/helpers/drive.mjs` — Chrome DevTools commands, invoked by `verify.sh drive`

Keep the feature map honest with `/maintain-verification-skill` when the dashboard's user-facing behavior changes.
