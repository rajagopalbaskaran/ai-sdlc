"""Adapter for Claude Code running headless (claude -p)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ai_sdlc.adapters.base import Adapter, AdapterResult

if TYPE_CHECKING:
    from ai_sdlc.state.plan import Task

PROMPT_TEMPLATE = """You are acting as the {persona} persona of the ai-sdlc framework.

Follow the persona definition, project profile, and knowledge base below.
Complete ONLY the single task given. Report what you changed.

{context}

## Your task

{task_title}

{task_body}
"""


# personas whose deliverable is their response text; they get NO file-edit
# permission - the boundary is enforced, not just requested in the persona
TEXT_PERSONAS = frozenset({"requirement_analyst", "implementation_planner", "validator"})


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"

    def __init__(
        self,
        command: str = "claude",
        timeout: int = 600,
        workdir: Path | None = None,
        persona_permissions: dict[str, str] | None = None,
    ):
        self.command = command
        self.timeout = timeout
        self.workdir = Path(workdir) if workdir else None
        # optional config override: {persona: "edit" | "text"}
        self.persona_permissions = persona_permissions or {}

    def _allows_edits(self, persona: str) -> bool:
        override = self.persona_permissions.get(persona)
        if override == "edit":
            return True
        if override == "text":
            return False
        return persona not in TEXT_PERSONAS

    def execute(self, persona: str, context: str, task: "Task") -> AdapterResult:
        prompt = PROMPT_TEMPLATE.format(
            persona=persona,
            context=context,
            task_title=task.title,
            task_body=task.body,
        )
        # the prompt goes through stdin, never argv: Windows caps a process
        # command line at ~32K characters, and prompts with a knowledge base
        # plus a large plan exceed that
        argv = [self.command, "-p", "--output-format", "text"]
        if self._allows_edits(persona):
            # file edits inside the workspace auto-approved for personas that
            # legitimately write code and docs (developer, tester, deployment)
            argv += ["--permission-mode", "acceptEdits"]
        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                # Claude emits UTF-8; Windows would otherwise decode with the
                # legacy codepage and crash on multi-byte characters
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                cwd=self.workdir,
            )
        except FileNotFoundError as exc:
            return AdapterResult(ok=False, error=f"claude binary not found: {exc}")
        except subprocess.TimeoutExpired:
            return AdapterResult(ok=False, error=f"claude timed out after {self.timeout}s")
        stdout = proc.stdout or ""
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return AdapterResult(ok=False, output=stdout, error=stderr or f"exit {proc.returncode}")
        return AdapterResult(ok=True, output=stdout)
