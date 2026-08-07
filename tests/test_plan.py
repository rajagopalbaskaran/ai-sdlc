import pytest

from ai_sdlc.state.plan import PlanDocument, Task

SAMPLE = """# Implementation Plan

Some intro prose that must be preserved.

## Notes

A section without yaml is not a task.

### Task 1: Create database schema

```yaml
id: T1
status: pending
depends_on: []
persona: developer
artifacts: []
derived_from: [analysis.md#storage]
```

Create the sqlite schema for links.

### Task 2: Shorten endpoint

```yaml
id: T2
status: pending
depends_on: [T1]
persona: developer
artifacts: []
derived_from: [analysis.md#shorten]
```

Implement POST /shorten.

### Task 3: Redirect endpoint

```yaml
id: T3
status: completed
depends_on: [T1]
persona: developer
artifacts: [src/app.py]
derived_from: [analysis.md#redirect]
```

Implement GET /{code}.
"""


@pytest.fixture
def plan_file(tmp_path):
    p = tmp_path / "implementation-plan.md"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_parse_tasks(plan_file):
    doc = PlanDocument.load(plan_file)
    assert [t.id for t in doc.tasks] == ["T1", "T2", "T3"]
    assert doc.get("T2").depends_on == ["T1"]
    assert doc.get("T3").status == "completed"
    assert doc.get("T3").artifacts == ["src/app.py"]
    assert doc.get("T1").derived_from == ["analysis.md#storage"]
    assert "sqlite schema" in doc.get("T1").body


def test_set_status_roundtrip_preserves_prose(plan_file):
    doc = PlanDocument.load(plan_file)
    doc.set_status("T2", "in_progress")
    doc.save()
    text = plan_file.read_text(encoding="utf-8")
    assert "Some intro prose that must be preserved." in text
    assert "Implement POST /shorten." in text
    reloaded = PlanDocument.load(plan_file)
    assert reloaded.get("T2").status == "in_progress"
    assert reloaded.get("T1").status == "pending"


def test_invalid_status_rejected(plan_file):
    doc = PlanDocument.load(plan_file)
    with pytest.raises(ValueError):
        doc.set_status("T1", "done")


def test_unknown_task_rejected(plan_file):
    doc = PlanDocument.load(plan_file)
    with pytest.raises(KeyError):
        doc.set_status("T99", "completed")


def test_sections_without_yaml_ignored(plan_file):
    doc = PlanDocument.load(plan_file)
    assert len(doc.tasks) == 3
