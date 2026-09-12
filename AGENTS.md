# aeries-dashboard

Static parent dashboard for TUSD Aeries grades, live at
https://davidcasas82.github.io/aeries-dashboard/ (GitHub Pages, publishes from
`main`). The scrape workflows (`scrape.yml`, `scrape-after-school.yml`) have no
cron on purpose; `family-data` `scheduled()` dispatches them. Do not re-add
Actions schedules. Do not commit `grades_data.json` or `grade_history.json`.
Never log student names or student numbers. After a scrape change, confirm
the Action POSTs to `family-data` instead of committing JSON.

## How to land work here

GitHub is the source of truth. This repo is worked on by Cursor Cloud Agents
(started from Grok on a phone or directly in Cursor) and by local sessions in
the Cursor IDE. The only thing all of them can see is what has been pushed here.

- Before starting, run `gh pr list` and check `git branch -r` for a `cursor/*`
  branch that already covers the task. Build on it or say it exists. Do not
  redo it from `main`.
- Work on a branch and open a PR. Never commit directly to `main`, even for a
  scaffold or a one-line fix.
- End every session with the work committed, pushed, and the PR open. If
  anything could not be pushed, say so in the final message.
- After a merge, confirm the branch was deleted (automatic) and, if this repo
  deploys from CI, that the run passed. A failing deploy is the next task.
- Never deploy by hand to work around a failing workflow. Fix the workflow.
- Do not commit `.DS_Store`, `node_modules`, build output, or secrets
  (`.env`, `.dev.vars`, `config.local.json`).
