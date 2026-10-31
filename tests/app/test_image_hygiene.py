# tests/app/test_image_hygiene.py
"""The published image must not carry deployment-local configuration.

The image is published to users worldwide. A proxy is a mainland-China runtime
choice, and schema/credential provisioning is operational data: both belong in
the deployment environment, never in the build inputs or the image defaults.
These checks are deliberately file-based: they run on every interpreter and
need no Docker daemon.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Files that become part of the image or its build/publish path. The workflow
#: file is excluded because it contains the guard patterns themselves.
BUILD_INPUTS = (
    "Dockerfile",
    "docker/entrypoint.sh",
    ".dockerignore",
    "docker-compose.yml",
    "docker-compose.bundled.yml",
)

#: Any of these in a build input means a proxy or mirror leaked into the image.
FORBIDDEN = re.compile(
    r"(?i)(https?_proxy|all_proxy|no_proxy|pip_index_url|pip_trusted_host|"
    r"mirrors\.aliyun\.com|127\.0\.0\.1:7897)"
)


def test_build_inputs_carry_no_proxy_or_mirror_configuration() -> None:
    offenders: dict[str, list[str]] = {}
    for relative in BUILD_INPUTS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        hits = FORBIDDEN.findall(text)
        if hits:
            offenders[relative] = sorted(set(hits))
    assert not offenders, f"proxy or mirror configuration in build inputs: {offenders}"


def test_image_publishes_every_listener_port() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    expose = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.strip().upper().startswith("EXPOSE ")
    ]
    assert expose, "Dockerfile does not declare EXPOSE"
    ports = set(expose[-1].split()[1:])
    # REST, MCP and the webhook receiver: one image serves all three roles.
    assert {"8000", "8001", "18080"} <= ports


def _documented_text() -> str:
    """Every markdown file that documents the deployment.

    The front README states what the project is; the deployment pages under
    ``docs/`` carry the detail. The rule under test is "this is documented
    somewhere", not "it is on the first page a reader opens".
    """
    pages = [ROOT / "README.md", ROOT / "README.zh-CN.md"]
    pages += sorted((ROOT / "docs").rglob("*.md"))
    return "\n".join(page.read_text(encoding="utf-8") for page in pages if page.exists())


def test_tls_directory_is_documented_rather_than_baked_in() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    documented = _documented_text()
    assert "/certs" in dockerfile
    assert "CITRACKER_WEBHOOK_SSL_CERTFILE" in documented
    assert "CITRACKER_WEBHOOK_SSL_KEYFILE" in documented
    # Nothing may copy certificate material into the image.
    assert not re.search(r"(?im)^\s*(COPY|ADD).*certs", dockerfile)


def test_entrypoint_is_a_generic_init_runner() -> None:
    """Schema and credential work is operational data, not entrypoint code."""
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    for name in ("CITRACKER_INITDB", "CITRACKER_INITDB_DIR", "CITRACKER_LEGACY"):
        assert name in entrypoint
    assert "/docker-entrypoint-init.d" in entrypoint
    assert "is-initialized" in entrypoint
    # The operations themselves belong to docker/init.d, mounted at run time.
    assert "CITRACKER_BOOTSTRAP" not in entrypoint
    assert "rhosocial-github-ci-tracker credential" not in entrypoint
    assert "github-ci-tracker-db init" not in entrypoint


def test_init_scripts_carry_the_operations() -> None:
    schema = (ROOT / "docker" / "init.d" / "10-schema.sh").read_text(encoding="utf-8")
    pat = (ROOT / "docker" / "init.d" / "20-pat.sh").read_text(encoding="utf-8")

    assert "rhosocial-github-ci-tracker-db init" in schema
    assert "credential" in pat and "pat ensure" in pat
    # The token must never reach the process list.
    assert "--token-file" in pat and "--token-stdin" in pat


def test_init_scripts_are_mounted_and_not_baked_in() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "./docker/init.d:/docker-entrypoint-init.d:ro" in compose
    assert "CITRACKER_INITDB: ${CITRACKER_INITDB:-auto}" in compose
    # Both long-running roles must stay out of the initialization path.
    assert compose.count('CITRACKER_INITDB: "skip"') == 2
    assert "CITRACKER_BOOTSTRAP_ONLY" not in compose
    assert "docker/init.d" not in dockerfile


def test_compose_splits_web_and_mcp_and_adds_an_init_step() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "\n  web:\n" in compose
    assert "\n  mcp:\n" in compose
    assert "\n  init:\n" in compose
    assert "serve" in compose and "all" in compose
    assert "serve" in compose and "mcp" in compose
    # The bundled overlay is what adds the database service; the base file must
    # not define one (a comment may mention postgres:18).
    bundled = (ROOT / "docker-compose.bundled.yml").read_text(encoding="utf-8")
    assert "image: postgres:18" in bundled
    assert "\n  postgres:\n" not in compose
