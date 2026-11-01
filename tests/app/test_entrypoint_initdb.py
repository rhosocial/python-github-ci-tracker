# tests/app/test_entrypoint_initdb.py
"""The container entrypoint is a generic init.d runner.

Schema creation and credential provisioning are operational data, not image
logic. These cases pin the contract the entrypoint owns: which boots run the
scripts, in what order, and that a broken script stops the container instead of
starting the service against an uninitialized database.

The runner is POSIX shell, so the execution cases run only where /bin/sh exists;
the CI job is Linux. The file-based assertions in test_image_hygiene.py cover
the shipped scripts on every platform.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
INIT_DIR = ROOT / "docker" / "init.d"

SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or SH is None,
    reason="the init.d runner is POSIX shell; exercised on Linux CI",
)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write a script with LF endings, independent of the host."""
    path.write_bytes(text.encode("utf-8"))
    path.chmod(mode)


def _run(
    tmp_path: Path,
    *,
    mode: str = "auto",
    legacy: bool = False,
    initialized: bool = False,
    scripts: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the entrypoint with a fake db CLI and a temp init.d directory."""
    bindir = tmp_path / "bin"
    initdir = tmp_path / "init.d"
    marker = tmp_path / "marker.txt"
    bindir.mkdir()

    code = 0 if initialized else 1
    _write(
        bindir / "rhosocial-github-ci-tracker-db",
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  is-initialized) exit {code} ;;\n"
        '  init) echo "schema initialized" ;;\n'
        "esac\n",
        0o755,
    )

    if scripts is not None:
        initdir.mkdir()
        for name, body in scripts.items():
            _write(initdir / name, body)

    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "CITRACKER_INITDB": mode,
        "CITRACKER_INITDB_DIR": str(initdir),
        "MARKER": str(marker),
    }
    if legacy:
        env["CITRACKER_LEGACY"] = "1"

    result = subprocess.run(
        [SH, str(ENTRYPOINT), "sh", "-c", "echo served"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, marker


def _marker(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []


TWO_SCRIPTS = {
    "10-first.sh": 'echo first >> "$MARKER"\n',
    "20-second.sh": 'echo second >> "$MARKER"\n',
}


def test_a_first_boot_runs_the_scripts_in_order(tmp_path: Path) -> None:
    result, marker = _run(tmp_path, initialized=False, scripts=TWO_SCRIPTS)

    assert result.returncode == 0, result.stderr
    assert _marker(marker) == ["first", "second"]
    assert "served" in result.stdout


def test_an_initialized_database_skips_the_scripts(tmp_path: Path) -> None:
    result, marker = _run(tmp_path, initialized=True, scripts=TWO_SCRIPTS)

    assert result.returncode == 0, result.stderr
    assert _marker(marker) == []
    assert "skipping init scripts" in result.stdout
    assert "served" in result.stdout


def test_force_reruns_the_scripts_on_an_initialized_database(tmp_path: Path) -> None:
    result, marker = _run(tmp_path, mode="force", initialized=True, scripts=TWO_SCRIPTS)

    assert result.returncode == 0, result.stderr
    assert _marker(marker) == ["first", "second"]


def test_skip_runs_nothing_and_starts_the_service(tmp_path: Path) -> None:
    result, marker = _run(tmp_path, mode="skip", initialized=False, scripts=TWO_SCRIPTS)

    assert result.returncode == 0, result.stderr
    assert _marker(marker) == []
    assert "served" in result.stdout


def test_legacy_is_a_spelling_for_skip(tmp_path: Path) -> None:
    result, marker = _run(tmp_path, legacy=True, initialized=False, scripts=TWO_SCRIPTS)

    assert result.returncode == 0, result.stderr
    assert _marker(marker) == []
    assert "legacy mode" in result.stdout


def test_a_failing_script_stops_the_container(tmp_path: Path) -> None:
    result, marker = _run(
        tmp_path,
        initialized=False,
        scripts={
            "10-first.sh": 'echo first >> "$MARKER"\n',
            "20-broken.sh": "exit 3\n",
        },
    )

    assert result.returncode != 0
    assert "served" not in result.stdout
    assert _marker(marker) == ["first"]


def test_a_missing_script_directory_fails_loudly(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, initialized=False, scripts=None)

    assert result.returncode != 0
    assert "no init script directory" in result.stdout
    assert "served" not in result.stdout


def test_an_empty_script_directory_fails_loudly(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, initialized=False, scripts={})

    assert result.returncode != 0
    assert "no *.sh scripts" in result.stdout
    assert "served" not in result.stdout


def test_an_unknown_mode_is_refused(tmp_path: Path) -> None:
    result, marker = _run(tmp_path, mode="sometimes", scripts=TWO_SCRIPTS)

    assert result.returncode != 0
    assert _marker(marker) == []
    assert "invalid CITRACKER_INITDB" in result.stdout


@pytest.mark.parametrize("script", sorted(INIT_DIR.glob("*.sh")), ids=lambda p: p.name)
def test_shipped_init_scripts_parse(script: Path) -> None:
    subprocess.run([SH, "-n", str(script)], check=True, timeout=30)


def test_the_entrypoint_parses() -> None:
    subprocess.run([SH, "-n", str(ENTRYPOINT)], check=True, timeout=30)


def _run_pat_script(tmp_path: Path, env: dict[str, str]) -> tuple[list[str], str]:
    """Run 20-pat.sh with a fake CLI that records argv and stdin."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    args_file = tmp_path / "args.txt"
    token_file = tmp_path / "token.txt"
    _write(
        bindir / "rhosocial-github-ci-tracker",
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_FILE"\ncat > "$TOKEN_FILE"\n',
        0o755,
    )

    process_env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "ARGS_FILE": str(args_file),
        "TOKEN_FILE": str(token_file),
        **env,
    }
    subprocess.run(
        [SH, str(INIT_DIR / "20-pat.sh")],
        env=process_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return (
        args_file.read_text(encoding="utf-8").splitlines() if args_file.exists() else [],
        token_file.read_text(encoding="utf-8") if token_file.exists() else "",
    )


def test_the_pat_script_passes_the_token_on_stdin(tmp_path: Path) -> None:
    args, stdin = _run_pat_script(
        tmp_path,
        {
            "CITRACKER_BOOTSTRAP_PAT": "github_pat_example",
            "CITRACKER_BOOTSTRAP_PAT_NAME": "work",
            "CITRACKER_BOOTSTRAP_PROBE_REPO": "owner/repo",
        },
    )

    assert args == [
        "credential",
        "pat",
        "ensure",
        "--name",
        "work",
        "--probe-repo",
        "owner/repo",
        "--token-stdin",
    ]
    # The token travels on stdin, never in argv, and keeps one trailing newline.
    assert stdin == "github_pat_example\n"


def test_the_pat_script_is_a_noop_without_a_token(tmp_path: Path) -> None:
    args, stdin = _run_pat_script(tmp_path, {})

    assert args == []
    assert stdin == ""
