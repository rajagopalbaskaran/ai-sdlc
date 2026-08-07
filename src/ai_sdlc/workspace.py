"""The .ai-sdlc state folder planted into a target project workspace."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

STATE_DIR_NAME = ".ai-sdlc"
SUBDIRS = ("knowledge-base", "plan", "personas", "runs")


class Workspace:
    """A target project directory holding (or about to hold) .ai-sdlc state."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.state_dir = self.root / STATE_DIR_NAME

    def exists(self) -> bool:
        return self.state_dir.is_dir()

    @property
    def plan_path(self) -> Path:
        return self.state_dir / "plan" / "implementation-plan.md"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @classmethod
    def init(cls, root: Path) -> "Workspace":
        """Plant a fresh .ai-sdlc folder. Refuses to overwrite an existing one."""
        ws = cls(root)
        if ws.exists():
            raise FileExistsError(f"{ws.state_dir} already exists; refusing to overwrite")
        for sub in SUBDIRS:
            (ws.state_dir / sub).mkdir(parents=True)
        templates = resources.files("ai_sdlc") / "templates"
        shutil.copy(str(templates / "config.yaml"), ws.state_dir / "config.yaml")
        shutil.copy(str(templates / "profile-template.md"), ws.state_dir / "project-profile.md")
        shutil.copy(str(templates / "plan-template.md"), ws.plan_path)
        for persona in (templates / "personas").iterdir():
            shutil.copy(str(persona), ws.state_dir / "personas" / persona.name)
        return ws
