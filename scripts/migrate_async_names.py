"""Migrate core/services.py and core/facade.py from sync to async model names.

Uses word-boundary regex so exception names (e.g. RepositoryNotFoundError)
and lowercase variable names (e.g. repository) are not affected.
"""

import re
from pathlib import Path

FILES = [
    str(
        Path(__file__).parent.parent
        / "src"
        / "rhosocial"
        / "github_ci_tracker"
        / "core"
        / "services.py"
    ),
    str(
        Path(__file__).parent.parent
        / "src"
        / "rhosocial"
        / "github_ci_tracker"
        / "core"
        / "facade.py"
    ),
]

# Order matters: longer names first to avoid partial matches
REPLACEMENTS = [
    ("WorkflowRun", "AsyncWorkflowRun"),
    ("StepLog", "AsyncStepLog"),
    ("GithubToken", "AsyncGithubToken"),
    ("Workflow", "AsyncWorkflow"),
    ("Job", "AsyncJob"),
    ("Step", "AsyncStep"),
    ("Repository", "AsyncRepository"),
]


def main() -> None:
    for path in FILES:
        content = open(path, encoding="utf-8").read()
        for old, new in REPLACEMENTS:
            content = re.sub(rf"\b{old}\b", new, content)
        open(path, "w", encoding="utf-8", newline="\n").write(content)
        print(f"  {Path(path).name}: done")
    print("All files migrated.")


if __name__ == "__main__":
    main()
