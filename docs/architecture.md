# Architecture

The as-built architecture of the AI SDLC Platform: components, orchestration
model, control flow, and the key decisions with their rationale.

## Components

```
src/ai_sdlc/
  cli.py                 Command surface: init, analyze, plan, run, continue,
                         status, report, rollback, replan, push, summarize
  workspace.py           The .ai-sdlc state folder planted into any project;
                         analysis fingerprinting for staleness detection
  changes.py             Filesystem snapshot/diff - detects exactly which
                         files an agent touched
  state/
    plan.py              Implementation plan parser/writer: tasks as yaml
                         blocks in markdown, plan-level metadata (branch);
                         the orchestrator is the sole writer of yaml
    profile.py, kb.py    Project profile and knowledge base loading
  orchestrator/
    dag.py               Dependency resolution, cycle detection, eligibility
    gates.py             Entry/exit gates per stage
    engine.py            The run loop: pick -> dispatch -> verify -> record
    replan.py            Plan merge under change: protect completed work,
                         revise pending, cycle-check the result
  governance/
    approvals.py         Human approve/reject/modify prompts; rejects by
                         default when no interactive stdin exists
    retry.py             Bounded retries with failure context fed back
    fallback.py          Adapter fallback chain
    rollback.py          Per-task commits ([ai-sdlc:<id>]) and git revert
    branching.py         Feature-branch lifecycle and gated push
    policy.py            Guardrails: secrets, workspace containment, diff size
  adapters/
    base.py              One contract: execute(persona, context, task)
    claude_code.py       Claude Code headless; per-persona file permissions
    mock.py              Deterministic scripted adapter for tests/offline
  observability/
    audit.py             Append-only JSONL log; optional live echo
    metrics.py           Success rate, retries, rollbacks, MTTR, latency
                         (end-to-end and per stage)
    report.py            Markdown report: metrics, decisions, timeline
    summary.py           Engineering summary generated from project state
  templates/             config.yaml, plan/profile templates, six personas
```

## Orchestration model

The implementation plan IS the execution state. Each task carries a yaml
block (id, status, depends_on, persona, artifacts, derived_from). Agents are
stateless; every dispatch assembles context from the persona definition, the
project profile, and the knowledge base. The engine loop:

```
load plan -> cycle check -> crash recovery (in-flight -> pending)
  -> stale-analysis gate (fingerprint mismatch halts the run)
  -> plan approval gate (human)
  -> branch selection and checkout (recorded in the plan)
  -> loop until no eligible work:
       eligible = pending tasks whose dependencies are completed
       dispatch persona via adapter (parallel across independent tasks;
         the stage exit gate is the synchronization barrier)
       verify: snapshot diff -> policy guardrails
       record: status in plan, events in audit, per-task commit
       on failure: bounded retries with context feedback, then blocked
  -> exit gate -> deploy-ready approval (human) -> push approval (human)
```

Interrupting at any point is safe: state on disk stays consistent and
`continue` resumes from it. When the requirement changes mid-flight,
`replan` diffs the proposal against the plan, protects completed tasks,
revises pending ones, and routes the result through the approval gate again.

## Control flow of a full lifecycle

```
requirement file
  -> analyze   (requirement_analyst, text-only permissions)
       human reviews/edits the analysis            <- review gate
  -> plan      (implementation_planner, text-only permissions)
       human reviews the task breakdown            <- review gate
  -> run       plan approval                       <- human gate
       develop/validate/test tasks under governance
       exit gate, deploy-ready approval            <- human gate
       push approval                               <- human gate
  -> summarize (engineering summary from recorded state)
```

## Key decisions and rationale

1. Plan-as-state (markdown + yaml blocks) rather than a database: state is
   human-readable, diffable in git, and directly reviewable - the spec IS
   the execution record. Trade-off: parsing discipline (single writer rule).
2. Custom orchestration engine rather than an agent framework: every
   governance requirement maps to code we own and can defend; no framework
   opacity. Trade-off: we build gates/retries/replan ourselves.
3. Tool-independent adapters: one execute() contract; Claude Code today,
   any assistant tomorrow. The Mock adapter makes the entire engine
   testable offline and deterministic.
4. Installed module + per-project .ai-sdlc folder (the git model): engine
   code lives once, state travels with each project, enabling resume and
   per-project customization.
5. Per-persona permission boundaries enforced by the adapter: text
   personas (analyst, planner, validator) cannot edit files; code personas
   can, inside the workspace only. Autonomy limits are enforced, not
   requested.
6. One feature branch per requirement, commits per task: main is
   agent-proof; rollback is surgical (git revert of one task's commit);
   publishing is always a human decision.
7. Observability from a single append-only event stream: metrics, reports,
   decision records, and the engineering summary are all derived from the
   audit log plus plan state - no second bookkeeping system to drift.
