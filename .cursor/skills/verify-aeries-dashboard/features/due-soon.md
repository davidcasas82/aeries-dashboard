# Due soon

Due soon lists work that is not turned in and is due within the next five school days. The fixture places `Lab Worksheet` in that window for Algebra Fixture.

## Sub-features

- `due-soon-item` shows the assignment title on the unlocked board.
- `due-soon-open` opens the same class drawer as Current standing.

## How to get to it (user POV)

- Unlock. The Due soon section is above Current standing.
- Choose the assignment row. Its button opens the class detail.

## Driving it with verify.sh

Preconditions:

- `verify.sh doctor` has printed `doctor ok`.
- The browser is on the lock screen.

- **Unlock.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive fill --selector "#pinInput" --value "246801"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open grades"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "h2" --text "Due soon"`. The Due soon heading is visible.
- **See the assignment.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive visible --text "Lab Worksheet"`. The row title is `Lab Worksheet`.
- **Read the band.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive text --selector ".glance-bands"`. The text includes `Lab Worksheet` and does not include `Optional Warmup` or `Exit Ticket`.
- **Read the hint.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive text --selector ".glance-look-next"`. The text is `Algebra Fixture has 1 turned in, no score yet.`
- **Open it.** Choose the `Lab Worksheet` row. Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive click --role button --name "Open Algebra Fixture"` and `.cursor/skills/verify-aeries-dashboard/verify.sh drive wait --selector "#glanceDrawerTitle" --text "Algebra Fixture"`. The drawer title is `Algebra Fixture`.
- **Proof.** Run `.cursor/skills/verify-aeries-dashboard/verify.sh drive screenshot --path "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard/due-soon.png"`. The image shows the Due soon heading and `Lab Worksheet`.

## Gotchas

- The due-soon button's accessible name is `Open Algebra Fixture`, from `aria-label="Open ${course}"`. It is not the assignment title. The assignment title is the visible row text.
- The five-school-day window is computed in the browser from today's Pacific date. The fixture dates `Lab Worksheet` on the second school day from today so it stays inside that window.
- Scored work and Aeries-flagged missing work do not appear in this band. `Chapter Quiz` is scored. `Reading Log` is flagged missing.
- `Optional Warmup` has the same due date as `Lab Worksheet` and `score_raw` `NA`. It does not appear in this band. The Algebra drawer lists it under Marked with score `NA`.
- `Exit Ticket` is Classroom-only, state `TURNED_IN`, due on that same day. It does not appear in this band. The hint under Due soon says `Algebra Fixture has 1 turned in, no score yet.`
