---
name: milestone-next
description: Implement the next approved task(s) of the current milestone from docs/PROGRESS.md, test-first, one commit per task.
argument-hint: [number of tasks, default 1]
disable-model-invocation: true
---

Implement the next $ARGUMENTS unfinished task(s) (if no number was given: 1) from "Approved task list" in `docs/PROGRESS.md`.

For each task:
1. Re-read the PLAN sections the task references.
2. Write the tests first where practical (pure logic always; UI and integration where feasible).
3. Implement the smallest thing that satisfies the plan (KISS, YAGNI).
4. Run `make lint` and `make test` (and `make e2e` if the participant flow changed). Fix until green. Do not weaken or delete tests to make them pass.
5. Commit: `M<milestone>: <what> (PLAN §x.y)`.
6. Update `docs/PROGRESS.md`: move the task to "Done", add any open problems, anything needing human testing, and the next step. Commit.

Stop and ask me if:
- the plan does not work as written, or two plan sections conflict;
- a task turns out much bigger than expected (propose a split);
- you would need to deviate from the plan (propose a `DECISIONS.md` entry instead of just doing it).

At the end, show the final test output and a 3-line summary of what changed.
