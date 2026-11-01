# tests/core/test_credential_ensure.py
"""Idempotent credential provisioning: create on first boot, rotate on re-run.

The init.d script calls ``ensure_token`` during first-boot provisioning. A fresh
database must get the credential; a forced re-run must reconcile the same alias
in place so its id -- and therefore every repository binding -- survives. Both
paths verify the token with GitHub before writing.
"""

from __future__ import annotations

import logging

import pytest
from conftest import FakeGithub, make_token_service

from rhosocial.github_ci_tracker.core.exceptions import TokenInvalidError
from rhosocial.github_ci_tracker.credentials import AsyncPatCredential

REPO = "acme/demo"


class TestEnsureToken:
    async def test_first_boot_creates_and_verifies(self, fake_client: FakeGithub, bound) -> None:
        record, created = await make_token_service(fake_client).ensure_token(
            name="work", token="ghp_first", probe_repo=REPO
        )

        assert created is True
        assert record.id is not None
        assert record.name == "work"
        # The service hands back metadata only; the value stays in the row.
        assert not hasattr(record, "token")
        stored = await AsyncPatCredential.find_by_name("work")
        assert stored is not None and stored.token == "ghp_first"
        assert fake_client.verify_calls == 1

    async def test_a_forced_rerun_rotates_in_place(self, fake_client: FakeGithub, bound) -> None:
        service = make_token_service(fake_client)
        first, _ = await service.ensure_token(name="work", token="ghp_first", probe_repo=REPO)
        second, created = await service.ensure_token(
            name="work", token="ghp_second", probe_repo=REPO
        )

        assert created is False
        # Same row: bindings made against the first value keep working.
        assert second.id == first.id
        stored = await AsyncPatCredential.find_by_name("work")
        assert stored is not None and stored.token == "ghp_second"
        assert fake_client.verify_calls == 2

    async def test_invalid_token_is_not_stored(self, fake_client: FakeGithub, bound) -> None:
        fake_client.token_valid = False
        service = make_token_service(fake_client)

        with pytest.raises(TokenInvalidError):
            await service.ensure_token(name="work", token="ghp_bad", probe_repo=REPO)

        assert await service.list_tokens() == []

    async def test_token_never_reaches_the_query_log(self, fake_client: FakeGithub, bound) -> None:
        """The ORM logs parameters at INFO; a credential write must not leak one."""
        model_logger = AsyncPatCredential.get_logger()
        captured: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        handler = _Capture()
        previous = model_logger.level
        model_logger.addHandler(handler)
        model_logger.setLevel(logging.DEBUG)
        try:
            service = make_token_service(fake_client)
            await service.ensure_token(name="work", token="ghp_secret", probe_repo=REPO)
            await service.ensure_token(name="work", token="ghp_rotated", probe_repo=REPO)
            # The handler is live: without the redaction the query logger would
            # have captured the token parameters above.
            model_logger.info("sentinel")
        finally:
            model_logger.removeHandler(handler)
            model_logger.setLevel(previous)

        assert any("sentinel" in message for message in captured)
        assert not any("ghp_secret" in message or "ghp_rotated" in message for message in captured)


class TestProbeRepoForwarding:
    """The service must forward what its own CLI accepts.

    ``--probe-repo`` is how an operator turns "can this token dispatch?" from
    unknown into measured, so a service that quietly dropped it left the
    capability unmeasured with no error to point at.
    """

    async def test_add_token_accepts_probe_repo(self, fake_client: FakeGithub, bound) -> None:
        record = await make_token_service(fake_client).add_token(
            name="work", token="ghp_new", probe_repo=REPO
        )

        assert record.name == "work"

    async def test_capabilities_accepts_probe_repo(self, fake_client: FakeGithub, bound) -> None:
        service = make_token_service(fake_client)
        await service.add_token(name="work", token="ghp_new")

        capabilities = await service.capabilities("work", probe_repo=REPO)

        assert "can_write_actions" in capabilities
