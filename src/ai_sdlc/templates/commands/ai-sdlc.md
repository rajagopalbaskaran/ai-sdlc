---
description: Run an ai-sdlc pipeline stage in this IDE session
argument-hint: init | analyze <file> | plan | develop | test | validate | status | branch | remote | push
allowed-tools: Bash(ai-sdlc:*), Read, Edit, Write, Glob, Grep
---

# ai-sdlc: $ARGUMENTS

Python owns the state machine. You execute tasks. Follow these rules exactly.

## Rules

1. NEVER run raw `git` mutations (commit, checkout, branch, push, revert). Every
   git change goes through an `ai-sdlc` command so it is audited.
2. NEVER edit `.ai-sdlc/plan/implementation-plan.md` yaml blocks or
   `.ai-sdlc/approvals.yaml`. The orchestrator is their only writer.
3. NEVER pass `--yes` or run `ai-sdlc approve` on your own initiative. Ask the
   human in chat first and act only on their answer.
4. Report honestly. `--result pass` when the work is not done is a lie the
   verification step will catch, and it wastes a retry.

## Dispatch on $ARGUMENTS

### init

Run `ai-sdlc init --workspace .`, then tell the human to fill in
`.ai-sdlc/project-profile.md` and `.ai-sdlc/knowledge-base/`.

### analyze <requirement-file>

Run `ai-sdlc analyze <requirement-file> --workspace .`. Show the human the
resulting `.ai-sdlc/plan/requirement-analysis.md` and ask them to resolve any
ambiguities before planning.

### plan

Run `ai-sdlc plan --workspace .`. Show the resulting task list with
`ai-sdlc status --json --workspace .`. Ask the human to approve the plan. Only if
they approve, run `ai-sdlc approve --gate plan --workspace .`.

### develop

1. `ai-sdlc branch --suggest --json --workspace .` - show the recommendation and
   ask the human which branch to use. Then
   `ai-sdlc branch --use <name> --workspace .`.
2. `ai-sdlc session start --json --workspace .`. If it reports `ok: false`, fix
   the listed reasons (usually a missing plan approval) and stop.
3. Loop:
   - `ai-sdlc next --json --workspace .`
   - If `done` is true, break.
   - Read the file at `briefing_path`. It contains the task instructions, the
     persona, the project profile, the knowledge base, and any previous failure.
   - Do the work with Edit/Write/Bash. The human can see and correct every edit.
   - `ai-sdlc report-task --task <id> --result pass|fail --error "<detail>" --json --workspace .`
   - If `status` is `pending`, the attempt failed and a retry remains: loop again.
   - If `status` is `blocked`, tell the human why and stop.
4. `ai-sdlc session end --json --workspace .`.

### test

Run `ai-sdlc test --json --workspace .`. On failure, show the failing output and
offer to fix it - but fixes belong to `develop`, not here.

### validate

Run `ai-sdlc validate --json --workspace .`. Summarize the MET / PARTIAL /
MISSING counts and point the human at `.ai-sdlc/plan/validation-report.md`.

### status

Run `ai-sdlc status --json --workspace .` and render it as a short table. Add
`ai-sdlc session status --json --workspace .` when a session may be in flight.

### branch

Run `ai-sdlc branch --suggest --json --workspace .`, show the recommendation, ask
the human, then `ai-sdlc branch --use <name> --workspace .`.

### remote

Run `ai-sdlc remote --json --workspace .`. If no origin is configured, tell the
human to create the GitHub repository and give you its URL, then run
`ai-sdlc remote --set <url> --workspace .`.

### push

1. `ai-sdlc remote --json --workspace .` - if there is no origin, handle `remote`
   above first.
2. Show the human what they are about to publish: `ai-sdlc status --json
   --workspace .` and the latest validation report.
3. Ask the human for explicit permission to push.
4. Only after they say yes: `ai-sdlc push --yes --json --workspace .`.

### anything else

Show this list of subcommands and ask what they meant.
