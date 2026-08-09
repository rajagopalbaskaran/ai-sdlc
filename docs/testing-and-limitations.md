# Testing Approach, Limitations, and Trade-offs

## Testing approach

The framework was built test-first: each module's tests were written before
its implementation, and the suite runs entirely offline in seconds.

- 91 tests, no network, no LLM required. The Mock adapter plays the role of
  a crash-test dummy: it can be scripted to fail on command, which is what
  makes retry, fallback, rollback, and blocking testable at all.
- Git behavior (per-task commits, revert-based rollback, branch lifecycle,
  push) is tested against real temporary git repositories, including a
  local bare repository standing in for a remote - real pushes, no network.
- Covered behaviors include: plan parsing round-trips that preserve prose
  byte-for-byte; plan metadata; DAG eligibility and cycle detection; gate
  logic; scripted adapter failures and retry exhaustion; fallback chain
  switching; workspace change detection; policy violations (secrets,
  out-of-workspace writes); parallel execution overlap (proven via
  timestamps) with exit-gate synchronization; safe-stop and resume;
  approval persistence and reject-by-default in non-interactive sessions;
  replan merge rules (completed work protected, cycles rejected); the
  stale-analysis gate; per-stage metrics; decision records; the engineering
  summary; and a full pipeline integration run.
- Run it: `py -m pip install -e .[dev]` then `py -m pytest`.

Applications built BY the framework carry their own testing approach,
declared in the project profile. The default expectation written into the
profile template: unit and integration tests for all new behavior; manual
test-case documents may complement automated tests, never replace them.

## Limitations

- One project per run: the engine operates on a single workspace at a time;
  multi-repo coordination requires running the framework per repository.
- The deployment stage produces readiness artifacts (runbook, checklist),
  not real infrastructure deployment.
- The Claude Code adapter requires a local, authenticated Claude Code
  install; live-run behavior depends on the model following persona
  instructions. Retries, validation gates, and human review exist precisely
  to catch deviations, and early live runs did surface (and fix) real
  integration issues such as headless permission handling.
- Parallel execution is proven with the Mock adapter and disabled by
  default for LLM runs (cost and rate-limit prudence). It is a config
  switch, not a rewrite.
- Adapter fallback to Mock is intended for demonstrations and tests; in
  live builds the recommended fallback list is empty so a failed task
  blocks honestly rather than fake-succeeding.
- Decision records cover the framework's own decision points (branch
  selection, staleness halts, crash recovery, replan diffs) plus all
  approvals; free-form design decisions made by agents inside a task are
  captured in their artifacts, not as structured records.
- Task-level artifacts depend on agents reporting them and on change
  detection; deleted-file changes are detected but their content cannot be
  policy-scanned after the fact.

## Trade-offs

- Markdown-plus-yaml state vs a database: chosen for reviewability and git
  diffs; costs strict single-writer discipline over the yaml blocks.
- Custom engine vs an orchestration framework: chosen for defensibility
  and exact fit to the governance requirements; costs building primitives
  ourselves.
- Headless CLI invocation of the AI tool vs SDK integration: chosen for
  tool independence (any assistant with a CLI can be adapted); costs
  streaming/token-level control.
- Reject-by-default approvals: safest posture for unattended runs; costs
  convenience (unattended runs cannot approve anything, by design).
- One feature branch per requirement rather than per module: avoids
  cross-branch dependency breakage and merge conflicts; per-module
  visibility comes from task labels instead.
