# AI SDLC Core Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the ai-sdlc core framework: a spec-driven, agent-executed SDLC orchestration engine with governed, stateful execution.

**Architecture:** Installable Python package (`src/ai_sdlc/`). The Implementation Plan markdown (with one fenced yaml block per task) is the single source of truth for execution state. A DAG orchestrator dispatches stateless personas through pluggable adapters (Mock, Claude Code), guarded by gates, approvals, retries, fallback, and rollback, with an append-only JSONL audit log and computed reliability metrics.

**Tech Stack:** Python 3.14 (`py` launcher on this machine), stdlib + PyYAML only, pytest for tests, argparse CLI, setuptools packaging.

## Global Constraints

- ALL files live under `C:\Users\Rajagopal Baskaran\workspaces_interview\ai-sdlc\`.
- NEVER `git commit` or `git push` unless the user explicitly instructs. Plan says "Commit" nowhere for this reason.
- Plain ASCII only in every file (no unicode arrows, em-dashes, box-drawing).
- Package name: `ai_sdlc`; CLI command: `ai-sdlc`; state dir in target workspaces: `.ai-sdlc/`.
- Task statuses (exact strings): `pending`, `in_progress`, `waiting_approval`, `completed`, `blocked`, `rolled_back`.
- Stages (exact order): `analyze`, `plan`, `develop`, `validate`, `test`, `deploy_ready`.
- Personas (exact names): `requirement_analyst`, `implementation_planner`, `developer`, `validator`, `tester`, `deployment_engineer`.
- Orchestrator is the SOLE writer of yaml blocks in implementation-plan.md.
- Audit log is append-only JSONL; never rewritten.

## File Structure

```
ai-sdlc/
  pyproject.toml
  src/ai_sdlc/
    __init__.py            # __version__
    cli.py                 # argparse entry: init/analyze/plan/run/continue/status/report
    workspace.py           # Workspace: locate/validate .ai-sdlc/, init scaffolding
    state/
      __init__.py
      plan.py              # Task dataclass, PlanDocument: parse/write yaml blocks
      profile.py           # load project-profile.md
      kb.py                # load knowledge-base/ docs
    orchestrator/
      __init__.py
      dag.py               # dependency resolution: eligible tasks, cycle detection
      gates.py             # entry/exit gate checks per stage
      engine.py            # the loop: pick -> dispatch -> validate -> update
    governance/
      __init__.py
      approvals.py         # CLI approve/reject/modify prompt
      retry.py             # bounded retry with context feedback
      rollback.py          # git-based per-task rollback in target workspace
      policy.py            # guardrail checks on task output
    adapters/
      __init__.py
      base.py              # Adapter ABC: execute(persona, context, task) -> AdapterResult
      mock.py              # MockAdapter: scripted results incl. on-demand failures
      claude_code.py       # ClaudeCodeAdapter: claude -p headless
    observability/
      __init__.py
      audit.py             # AuditLog: append events to runs/audit-<run-id>.jsonl
      metrics.py           # compute success rate, retry/rollback freq, MTTR, latency
      report.py            # render audit + metrics to markdown
    templates/
      config.yaml          # default config: adapter chain, retry budget, gates
      personas/            # 6 persona .md files
      plan-template.md
      profile-template.md
  tests/
    conftest.py            # tmp workspace fixture
    test_workspace.py
    test_plan.py
    test_dag.py
    test_gates.py
    test_engine.py
    test_governance.py
    test_adapters.py
    test_audit.py
    test_metrics.py
    test_cli.py
    test_integration.py    # full pipeline on MockAdapter
  docs/plans/2026-08-07-core-framework.md   # this file
```

---

### Task 1: Package skeleton and `init` command

**Files:**
- Create: `pyproject.toml`, `src/ai_sdlc/__init__.py`, `src/ai_sdlc/cli.py`, `src/ai_sdlc/workspace.py`, `src/ai_sdlc/templates/*` (config.yaml, plan-template.md, profile-template.md, personas/*.md), `tests/conftest.py`, `tests/test_workspace.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `Workspace(root: Path)` with `.state_dir` (root/.ai-sdlc), `.exists()`, `Workspace.init(root) -> Workspace` (copies templates, creates dirs: knowledge-base/, plan/, personas/, runs/); `main(argv) -> int` CLI entry.

- [ ] Write failing tests: `test_init_creates_state_dir`, `test_init_copies_personas_and_config`, `test_init_is_idempotent_refuses_overwrite`, `test_cli_init_exit_zero`
- [ ] Run tests, verify fail (module not found)
- [ ] Implement pyproject.toml (setuptools, package-data for templates, console_script `ai-sdlc = ai_sdlc.cli:main`, dependency: pyyaml, pytest as dev extra)
- [ ] Implement workspace.py + cli.py `init` subcommand + templates
- [ ] `pip install -e .[dev]` using py -m pip; run pytest, verify pass

### Task 2: State layer - plan parser/writer, profile, KB

**Files:**
- Create: `src/ai_sdlc/state/plan.py`, `state/profile.py`, `state/kb.py`, `tests/test_plan.py`

**Interfaces:**
- Produces: `Task` dataclass (`id, title, status, depends_on: list[str], persona, artifacts: list[str], derived_from: list[str], body: str, retries: int = 0`); `PlanDocument.load(path) -> PlanDocument`, `.tasks: list[Task]`, `.get(task_id)`, `.set_status(task_id, status)`, `.save()` (rewrites ONLY yaml blocks, preserves prose); `load_profile(state_dir) -> dict`, `load_kb(state_dir) -> dict[str, str]`.

- [ ] Write failing tests: parse plan md with 3 tasks (statuses, deps read correctly); set_status round-trips and preserves surrounding prose byte-exact; unknown status string raises ValueError; missing yaml block ignored as non-task section
- [ ] Run tests, verify fail
- [ ] Implement plan.py (regex for fenced yaml blocks under `###` headings, yaml.safe_load, strict status validation), profile.py, kb.py
- [ ] Run tests, verify pass

### Task 3: DAG resolution and gates

**Files:**
- Create: `src/ai_sdlc/orchestrator/dag.py`, `orchestrator/gates.py`, `tests/test_dag.py`, `tests/test_gates.py`

**Interfaces:**
- Produces: `eligible_tasks(tasks) -> list[Task]` (status pending AND all deps completed); `detect_cycles(tasks) -> list[list[str]]` (raises CycleError on cycles at load); `terminal(tasks) -> bool`; `GateResult(passed: bool, reasons: list[str])`; `entry_gate(stage, ctx) -> GateResult`, `exit_gate(stage, ctx) -> GateResult` where ctx bundles plan/profile/artifacts.

- [ ] Write failing tests: diamond dependency eligibility; cycle detection raises; exit gate fails when a task is blocked; entry gate for develop requires approved plan artifact
- [ ] Run tests, verify fail
- [ ] Implement dag.py (Kahn topological sort), gates.py (per-stage requirement table)
- [ ] Run tests, verify pass

### Task 4: Adapters - base, Mock, Claude Code

**Files:**
- Create: `src/ai_sdlc/adapters/base.py`, `adapters/mock.py`, `adapters/claude_code.py`, `tests/test_adapters.py`

**Interfaces:**
- Produces: `AdapterResult(ok: bool, output: str, files_changed: list[str], error: str | None)`; `Adapter.execute(persona: str, context: str, task: Task) -> AdapterResult`; `MockAdapter(script: dict[str, list[AdapterResult]])` popping scripted results per task id (default: success); `ClaudeCodeAdapter(command: str = "claude")` invoking `claude -p <prompt> --output-format text` via subprocess with cwd=workspace root, timeout from config; `build_adapter(name, config) -> Adapter` factory.

- [ ] Write failing tests: mock returns scripted failure then success; factory builds by name; claude_code builds correct argv and handles missing binary as ok=False (mock subprocess with monkeypatch, no real claude call)
- [ ] Run tests, verify fail
- [ ] Implement base.py, mock.py, claude_code.py
- [ ] Run tests, verify pass

### Task 5: Observability - audit log and metrics

**Files:**
- Create: `src/ai_sdlc/observability/audit.py`, `observability/metrics.py`, `observability/report.py`, `tests/test_audit.py`, `tests/test_metrics.py`

**Interfaces:**
- Produces: `AuditLog(runs_dir, run_id)` with `.event(type: str, **fields)` appending one JSON line `{ts, run_id, type, ...}` (types used: run_started, task_started, task_completed, task_failed, retry, fallback, rollback, gate, approval, decision, run_stopped, run_completed); `compute_metrics(jsonl_path) -> dict` (success_rate, retries, rollbacks, mttr_seconds, e2e_seconds, per_stage_seconds); `render_report(jsonl_path) -> str` markdown.

- [ ] Write failing tests: events append as valid JSONL and file is never truncated; metrics computed from a synthetic log (2 failures 1 recovery -> mttr computed; success_rate correct)
- [ ] Run tests, verify fail
- [ ] Implement audit.py, metrics.py, report.py
- [ ] Run tests, verify pass

### Task 6: Governance - approvals, retry, fallback, rollback, policy

**Files:**
- Create: `src/ai_sdlc/governance/approvals.py`, `governance/retry.py`, `governance/rollback.py`, `governance/policy.py`, `tests/test_governance.py`

**Interfaces:**
- Produces: `request_approval(prompt: str, input_fn=input) -> Literal["approve","reject","modify"]` (injectable input for tests; EOFError -> safe-stop); `RetryPolicy(budget: int)` with `.attempt(task, fn) -> AdapterResult` feeding last error into context on retry, audit `retry` events; `FallbackChain(adapters: list[Adapter])` trying next adapter on ok=False, audit `fallback`; `rollback_task(workspace_root, task_id) -> bool` using `git revert --no-edit` of the commit whose message contains `[ai-sdlc:<task_id>]`, plus `commit_task(workspace_root, task_id, message)` for per-task local commits in TARGET workspace only (never the framework repo, never push); `check_policies(files_changed, workspace_root, diff_limit=500) -> list[str]` (violations: secret patterns, writes outside workspace, oversized diff).

- [ ] Write failing tests: approval parses inputs a/r/m and full words; retry stops at budget and marks blocked; fallback chain switches adapter and audits; policy flags secret string and out-of-root path; rollback finds tagged commit (use a tmp git repo fixture)
- [ ] Run tests, verify fail
- [ ] Implement all four modules
- [ ] Run tests, verify pass

### Task 7: Orchestrator engine + CLI wiring + integration test

**Files:**
- Create: `src/ai_sdlc/orchestrator/engine.py`, `tests/test_engine.py`, `tests/test_integration.py`
- Modify: `src/ai_sdlc/cli.py` (add analyze/plan/run/continue/status/report subcommands)

**Interfaces:**
- Consumes: everything above.
- Produces: `Engine(workspace, adapter, config, audit, input_fn=input)` with `.run(parallel: bool = False) -> RunSummary(status, completed, blocked, rolled_back)`; loop = eligible -> (ThreadPoolExecutor if parallel and >1 eligible) dispatch persona via adapter -> policy check -> update plan -> audit; exit-gate barrier per stage; approval gates at plan acceptance and deploy_ready; KeyboardInterrupt -> consistent state + `run_stopped` audit (safe-stop); `continue` = same as run (state-driven resume). CLI maps subcommands to engine calls; `status` prints task table; `report` writes runs/report-<run-id>.md.

- [ ] Write failing tests: engine completes 3-task mock plan sequentially; parallel run executes independent tasks concurrently (assert overlap via timestamps) and exit gate waits for all; failure -> retry -> blocked path audited; safe-stop leaves plan parseable and resumable; integration: init tmp workspace, seed plan, run with MockAdapter to completion, metrics file exists
- [ ] Run tests, verify fail
- [ ] Implement engine.py and cli.py wiring
- [ ] Run full pytest suite, verify all pass

---

## Self-review notes

- Spec coverage: parallel+sync (T7), lineage via derived_from + decision events (T2/T5), fallback (T6), approvals (T6/T7), rollback (T6), safe-stop/resume (T7), audit+metrics (T5), policy guardrails (T6), init/packaging (T1). Personas ship as templates (T1); analyze/plan stages execute as persona tasks through the same engine (T7 CLI).
- No commits anywhere by design (user rule). Per-task commits happen only in TARGET demo workspaces at demo time, never in this repo.
- Demos and Claude Code live runs are OUT of this plan (separate phase after framework).
