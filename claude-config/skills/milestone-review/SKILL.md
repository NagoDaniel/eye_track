---
name: milestone-review
description: Review a finished milestone against docs/PLAN.md using the plan-reviewer subagent, check the acceptance criterion, and prepare it for merge.
argument-hint: [milestone-id]
disable-model-invocation: true
---

Milestone $ARGUMENTS is believed to be complete.

1. Check that every task in "Approved task list" in `docs/PROGRESS.md` is marked done. If not, list what is left and stop.
2. Delegate a review to the `plan-reviewer` subagent for this branch against `main` and milestone $ARGUMENTS. Do not review it yourself first; let the reviewer judge independently.
3. Show me the reviewer's report unchanged.
4. Run the milestone's "done when" criterion from PLAN §14 yourself and show the evidence (commands and output). If part of it needs a real webcam or hardware, write a short, concrete test script for me: what to do, what I should see, and what to report back.
5. Propose fixes for blocker and major findings as new tasks in `docs/PROGRESS.md`. Do not fix anything until I approve.

Do not merge. When I say the milestone is accepted: update `docs/PROGRESS.md` (milestone `done`, next milestone, next step), update `docs/DATA_FORMATS.md` / `docs/RUNNING_A_STUDY.md` if this milestone changed them, commit, and tell me the merge command.
