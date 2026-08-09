"""Implementation plan parsing and writing.

The implementation plan markdown is the single source of truth for execution
state. Each task is a level-3 heading followed by one fenced yaml block. The
orchestrator (through this module) is the sole writer of those yaml blocks;
all surrounding prose is preserved byte-for-byte on save.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_STATUSES = (
    "pending",
    "in_progress",
    "waiting_approval",
    "completed",
    "blocked",
    "rolled_back",
)

_YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _as_list(value) -> list[str]:
    """Planner agents sometimes emit a scalar where a list is expected;
    normalize both forms to a list of strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


@dataclass
class Task:
    id: str
    title: str
    status: str
    depends_on: list[str] = field(default_factory=list)
    persona: str = "developer"
    artifacts: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    body: str = ""
    retries: int = 0


class PlanDocument:
    """Reads and rewrites yaml task blocks inside the plan markdown."""

    def __init__(self, path: Path, text: str):
        self.path = Path(path)
        self._text = text
        self.tasks: list[Task] = []
        self.meta: dict = {}
        self._has_meta_block = False
        self._parse()

    @classmethod
    def load(cls, path: Path) -> "PlanDocument":
        return cls(path, Path(path).read_text(encoding="utf-8"))

    def get(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(f"no task with id {task_id!r}")

    def set_status(self, task_id: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {VALID_STATUSES}")
        self.get(task_id).status = status

    def set_meta(self, **fields) -> None:
        """Update plan-level metadata (e.g. branch). Inserts a yaml block
        after the title line when the plan has none yet."""
        self.meta = {**self.meta, **fields}
        if self._has_meta_block:
            return
        import yaml as _yaml

        dumped = _yaml.safe_dump(self.meta, sort_keys=False).strip()
        block = f"\n\n```yaml\n{dumped}\n```\n"
        first_newline = self._text.find("\n")
        if first_newline == -1:
            self._text = self._text + block
        else:
            self._text = self._text[:first_newline] + block + self._text[first_newline + 1 :]
        self._has_meta_block = True

    def save(self) -> None:
        """Rewrite only the yaml blocks; every other byte is preserved."""
        by_id = {t.id: t for t in self.tasks}
        first_task_offset = self._text.find("\n### ")

        def replace(match: re.Match) -> str:
            data = yaml.safe_load(match.group(1))
            if not isinstance(data, dict):
                return match.group(0)
            if "id" not in data:
                # the header block (before any task heading) is plan metadata
                in_header = first_task_offset == -1 or match.start() < first_task_offset
                if in_header and self._has_meta_block:
                    dumped = yaml.safe_dump(self.meta, sort_keys=False).strip()
                    return f"```yaml\n{dumped}\n```"
                return match.group(0)
            task = by_id.get(data["id"])
            if task is None:
                return match.group(0)
            data["status"] = task.status
            data["artifacts"] = task.artifacts
            dumped = yaml.safe_dump(data, sort_keys=False, default_flow_style=None).strip()
            return f"```yaml\n{dumped}\n```"

        self._text = _YAML_BLOCK.sub(replace, self._text)
        self.path.write_text(self._text, encoding="utf-8")

    def _parse(self) -> None:
        self.tasks = []
        sections = re.split(r"(?m)^### ", self._text)
        header_match = _YAML_BLOCK.search(sections[0])
        if header_match:
            header_data = yaml.safe_load(header_match.group(1))
            if isinstance(header_data, dict) and "id" not in header_data:
                self.meta = header_data
                self._has_meta_block = True
        for section in sections[1:]:
            heading, _, rest = section.partition("\n")
            match = _YAML_BLOCK.search(rest)
            if not match:
                continue
            data = yaml.safe_load(match.group(1))
            if not isinstance(data, dict) or "id" not in data:
                continue
            status = data.get("status", "pending")
            if status not in VALID_STATUSES:
                raise ValueError(f"task {data['id']}: invalid status {status!r}")
            body = rest[match.end():].strip()
            self.tasks.append(
                Task(
                    id=str(data["id"]),
                    title=heading.strip(),
                    status=status,
                    depends_on=_as_list(data.get("depends_on")),
                    persona=data.get("persona", "developer"),
                    artifacts=_as_list(data.get("artifacts")),
                    derived_from=_as_list(data.get("derived_from")),
                    body=body,
                )
            )
