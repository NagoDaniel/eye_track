---
name: milestone-start
description: Plan a milestone from docs/PLAN.md — read the relevant sections and existing code, propose a task list for approval. Use at the start of every milestone.
argument-hint: [milestone-id, e.g. 1a, 2, 3]
disable-model-invocation: true
---

We are starting Milestone $ARGUMENTS from `docs/PLAN.md` §14.

1. Read `CLAUDE.md`, `docs/PROGRESS.md`, `docs/DECISIONS.md`.
2. Read the milestone's entry in PLAN §14, every PLAN section it refers to, and §1.
3. Read the existing code this milestone builds on.
4. Create the branch `m$ARGUMENTS-<short-slug>` from `main` (do not switch if there are uncommitted changes; tell me instead).
5. Propose a task list: 5–15 small tasks in dependency order. For each task give:
   - what it implements, with the PLAN section reference;
   - the test(s) that prove it works (name real fixtures where the plan requires them);
   - files it creates or touches;
   - whether it needs human testing (webcam, real hardware).
6. Map the milestone's "done when" criterion to the task(s) that demonstrate it.
7. List everything in the plan that is ambiguous, contradictory, or that you believe is wrong, as numbered questions. Do not resolve them yourself.

Do not write any code yet. After I approve (possibly with changes), write the approved list into `docs/PROGRESS.md` under "Approved task list", mark the milestone `in progress`, and commit that change.
