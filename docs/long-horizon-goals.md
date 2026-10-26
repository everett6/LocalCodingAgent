# Goals and bug-hunting teams

Open **Goals & milestones** to create a goal and acceptance criteria, then attach
milestones with a parent goal. Choose **Work on this** to attach subsequent jobs.
The private SQLite ledger retains agent tasks, attempts, decisions, next actions,
blockers and an append-only event history across sessions. A job receives a bounded
resume briefing; delegated tasks are recorded under their parent task.

Successful agent output without passing checks remains **verifying**. Completion
requires recorded checks tied to the current Git workspace fingerprint. Changing
tracked or untracked non-ignored files makes that evidence stale. A parent can be
completed once all children have current passing evidence. Criteria are supplied
to agents as context; matching arbitrary prose criteria to test coverage still
requires review. Three or more unsuccessful attempts flag a stalled plan in the
briefing. This is a progress heuristic, not an autonomous scheduling service.

The `/goals` API supports creation and listing, including dependencies on existing
goals. `/goals/update` records status, next action, blocker or decision changes.
It cannot accept forged verification evidence. State changes and events commit
in the same SQLite transaction, using the existing private memory storage.

In **Bug hunting**, expand **Bug-hunting team** and select saved provider profiles:

- **Goal tracking & planning**: GLM 5.3 is suggested. A planning pass prepares
  milestones and checks before the investigation.
- **Delegated tasks**: DeepSeek 4.1 is suggested. Eligible leaf agents inherit
  the parent's local scope and shared budget.
- **Exploit validation**: the selected profile serves bug discovery, security,
  and exploit-validator roles. Existing authorization and reproduction limits apply.

Save profiles in Model settings first, using the exact model IDs supported by your
provider and environment-variable credential references. Assignments persist in
`.local-coder/bug-models.json` and cannot change while a job is active. Blank
assignments retain the workspace model. Selection does not verify provider access
or guarantee that an advertised model exists. Live multi-model inference requires
working credentials and sufficient request/cost budgets.
