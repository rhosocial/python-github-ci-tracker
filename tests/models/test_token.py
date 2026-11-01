# tests/models/test_token.py
"""The PAT provider: add / probe / rotate / list / deactivate / delete / expiry.

The credential lifecycle moved out of the core and into ``credentials/pat``. The
contract is unchanged -- the same validations, the same exceptions, the same
"never expose the secret" invariant -- so these tests exercise the provider the
way the old model tests exercised the model.

Two behaviours changed shape with the provider split, and are asserted here
explicitly:

* registering a credential no longer binds repositories. ``add`` stores the
  credential; binding is a separate operation, because a credential exists
  independently of which repositories it happens to cover today;
* deletion still refuses while bindings point at the credential, but the count
  now comes from ``count_bindings`` rather than a foreign key.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeGithub

from rhosocial.github_ci_tracker.core.exceptions import (
    TokenAlreadyExistsError,
    TokenDeactivatedError,
    TokenExpiredError,
    TokenInUseError,
    TokenInvalidError,
    TokenNotFoundError,
    ValidationError,
)
from rhosocial.github_ci_tracker.credentials import AsyncPatCredential, get_provider
from rhosocial.github_ci_tracker.models import AsyncRepository
from rhosocial.github_ci_tracker.models.binding import AsyncRepositoryBinding

#: Whole seconds, so a round trip through a backend that drops sub-second
#: precision still compares equal. One instant for both fixtures, so they cannot
#: disagree by a millisecond.
_NOW = datetime.now(timezone.utc).replace(microsecond=0)
#: Three years out, computed rather than dated: valid, and it stays that way. A
#: literal year would eventually flip this fixture into "expired", and the
#: failure would look like a provider bug instead of a stale test.
FUTURE = _NOW + timedelta(days=365 * 3)
#: A year back: expired, and it stays expired for the same reason.
PAST = _NOW - timedelta(days=365)


def _provider():
    """The PAT provider the service uses."""
    return get_provider("pat")


async def _add(
    client: FakeGithub, token: str = "ghp_secret", name: str = "laptop"
) -> AsyncPatCredential:
    """Register a credential through the provider."""
    return await _provider().add(token=token, name=name, client=client)


async def _bind(credential: AsyncPatCredential, full_name: str = "acme/demo") -> AsyncRepository:
    """Subscribe a repository and bind it to ``credential``."""
    owner, _, name = full_name.partition("/")
    repo = AsyncRepository(
        github_repo_id=hash(full_name) % 10**9,
        full_name=full_name,
        owner=owner,
        name=name,
    )
    await repo.save()
    await AsyncRepositoryBinding.bind(repo.id or 0, "pat", credential.id or 0)
    return repo


class TestAdd:
    """Registering a credential."""

    async def test_registers_and_probes_capabilities(self, fake_client: FakeGithub, bound) -> None:
        record = await _add(fake_client)

        assert record.id is not None
        assert record.name == "laptop"
        assert record.is_active is True
        # The probe result is stored so gating a trigger needs no round trip.
        assert record.can_read_actions is True
        assert record.owner_login == "alice"
        assert record.capabilities_probed_at is not None

    async def test_without_verify_skips_probe(self, fake_client: FakeGithub, bound) -> None:
        record = await _provider().add(
            token="ghp_x", name="laptop", client=fake_client, verify=False
        )
        assert record.can_read_actions is None
        assert fake_client.verify_calls == 0

    async def test_invalid_token_is_rejected_before_storing(
        self, fake_client: FakeGithub, bound
    ) -> None:
        fake_client.token_valid = False
        with pytest.raises(TokenInvalidError):
            await _add(fake_client)
        assert await AsyncPatCredential.query().count() == 0

    async def test_duplicate_name_is_rejected(self, fake_client: FakeGithub, bound) -> None:
        await _add(fake_client, token="ghp_one", name="laptop")
        with pytest.raises(TokenAlreadyExistsError):
            await _add(fake_client, token="ghp_two", name="laptop")

    async def test_duplicate_value_under_other_name_is_rejected(
        self, fake_client: FakeGithub, bound
    ) -> None:
        await _add(fake_client, token="ghp_one", name="laptop")
        with pytest.raises(TokenAlreadyExistsError):
            await _add(fake_client, token="ghp_one", name="phone")

    async def test_blank_values_are_rejected(self, fake_client: FakeGithub, bound) -> None:
        with pytest.raises(ValidationError):
            await _add(fake_client, token="", name="laptop")
        with pytest.raises(ValidationError):
            await _add(fake_client, token="ghp_x", name="  ")

    async def test_declared_expiry_is_stored(self, fake_client: FakeGithub, bound) -> None:
        record = await _provider().add(
            token="ghp_x", name="laptop", client=fake_client, expires_at=FUTURE
        )
        assert record.expires_at == FUTURE

    async def test_registering_does_not_bind_repositories(
        self, fake_client: FakeGithub, bound
    ) -> None:
        """Binding is a separate operation now: a credential is not a subscription."""
        record = await _add(fake_client)
        assert await AsyncRepositoryBinding.count_for_provider("pat", record.id or 0) == 0


class TestRotate:
    """Replacing a credential's value in place."""

    async def test_replaces_value_keeping_identity_and_bindings(
        self, fake_client: FakeGithub, bound
    ) -> None:
        record = await _add(fake_client, token="ghp_old")
        repo = await _bind(record)

        rotated = await _provider().rotate(
            ref=record.id or 0, new_token="ghp_new", client=fake_client
        )
        assert rotated.id == record.id
        assert rotated.token == "ghp_new"

        # The binding survives, which is the point of rotating in place.
        binding = await AsyncRepositoryBinding.for_repository(repo.id or 0)
        assert binding is not None
        assert binding.provider_ref == record.id

    async def test_unknown_name_is_rejected(self, fake_client: FakeGithub, bound) -> None:
        with pytest.raises(TokenNotFoundError):
            await _provider().rotate(ref=4242, new_token="ghp_new", client=fake_client)

    async def test_duplicate_value_is_rejected(self, fake_client: FakeGithub, bound) -> None:
        await _add(fake_client, token="ghp_one", name="laptop")
        other = await _add(fake_client, token="ghp_two", name="phone")
        with pytest.raises(TokenAlreadyExistsError):
            await _provider().rotate(ref=other.id or 0, new_token="ghp_one", client=fake_client)

    async def test_keeps_declared_expiry_when_omitted(self, fake_client: FakeGithub, bound) -> None:
        record = await _provider().add(
            token="ghp_x", name="laptop", client=fake_client, expires_at=FUTURE
        )
        rotated = await _provider().rotate(
            ref=record.id or 0, new_token="ghp_y", client=fake_client
        )
        assert rotated.expires_at == FUTURE

    async def test_updates_expiry_when_given(self, fake_client: FakeGithub, bound) -> None:
        record = await _add(fake_client)
        later = FUTURE + timedelta(days=30)
        rotated = await _provider().rotate(
            ref=record.id or 0, new_token="ghp_y", client=fake_client, expires_at=later
        )
        assert rotated.expires_at == later

    async def test_invalid_new_value_keeps_the_old_one(
        self, fake_client: FakeGithub, bound
    ) -> None:
        record = await _add(fake_client, token="ghp_old")
        fake_client.token_valid = False
        with pytest.raises(TokenInvalidError):
            await _provider().rotate(ref=record.id or 0, new_token="ghp_new", client=fake_client)

        stored = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == record.id)
        assert stored is not None
        assert stored.token == "ghp_old"

    async def test_reactivates_a_deactivated_credential(
        self, fake_client: FakeGithub, bound
    ) -> None:
        record = await _add(fake_client)
        await _provider().deactivate(record.id or 0)

        rotated = await _provider().rotate(
            ref=record.id or 0, new_token="ghp_new", client=fake_client
        )
        assert rotated.is_active is True

    async def test_blank_value_is_rejected(self, fake_client: FakeGithub, bound) -> None:
        record = await _add(fake_client)
        with pytest.raises(ValidationError):
            await _provider().rotate(ref=record.id or 0, new_token="   ", client=fake_client)


class TestList:
    """Listing credentials without exposing the secrets."""

    async def test_never_exposes_token_values(self, fake_client: FakeGithub, bound) -> None:
        await _add(fake_client, token="ghp_secret_value", name="laptop")

        rows = await _provider().list_metadata()
        assert len(rows) == 1
        assert rows[0]["name"] == "laptop"
        assert rows[0]["credential"] == "pat/laptop"
        assert rows[0]["token_name"] == "laptop"  # REST contract alias
        # The REST contract reads these, and stamps are UTC with an offset.
        assert rows[0]["created_at"].endswith("+00:00")
        assert rows[0]["updated_at"].endswith("+00:00")

        rendered = json.dumps(rows, default=str)
        assert "ghp_secret_value" not in rendered


class TestDeactivate:
    """Soft delete."""

    async def test_marks_the_credential_inactive(self, fake_client: FakeGithub, bound) -> None:
        record = await _add(fake_client)
        updated = await _provider().deactivate(record.id or 0)
        assert updated.is_active is False

    async def test_resolution_refuses_a_deactivated_credential(
        self, fake_client: FakeGithub, bound
    ) -> None:
        record = await _add(fake_client)
        await _provider().deactivate(record.id or 0)
        with pytest.raises(TokenDeactivatedError):
            await _provider().resolve(record.id or 0)


class TestDelete:
    """Hard delete, gated on bindings."""

    async def test_unknown_credential_is_rejected(self, fake_client: FakeGithub, bound) -> None:
        with pytest.raises(TokenNotFoundError):
            await _provider().delete(4242)

    async def test_credential_in_use_is_rejected(self, fake_client: FakeGithub, bound) -> None:
        """The binding discipline replaces the foreign key the core omits."""
        record = await _add(fake_client)
        await _bind(record)
        with pytest.raises(TokenInUseError):
            await _provider().delete(record.id or 0)

    async def test_free_credential_is_deleted(self, fake_client: FakeGithub, bound) -> None:
        record = await _add(fake_client)
        await _provider().delete(record.id or 0)
        assert await AsyncPatCredential.query().count() == 0

    async def test_delete_succeeds_after_the_binding_is_gone(
        self, fake_client: FakeGithub, bound
    ) -> None:
        record = await _add(fake_client)
        repo = await _bind(record)

        await AsyncRepositoryBinding.unbind(repo.id or 0)
        await _provider().delete(record.id or 0)
        assert await AsyncPatCredential.query().count() == 0

    async def test_release_drops_every_binding(self, fake_client: FakeGithub, bound) -> None:
        record = await _add(fake_client)
        await _bind(record, "acme/one")
        await _bind(record, "acme/two")

        removed = await _provider().release(record.id or 0)
        assert removed == 2
        assert await AsyncRepositoryBinding.count_for_provider("pat", record.id or 0) == 0


class TestExpiry:
    """A declared expiry is enforced locally, before any GitHub call."""

    async def test_expired_credential_refuses_resolution(
        self, fake_client: FakeGithub, bound
    ) -> None:
        record = await _provider().add(
            token="ghp_x", name="laptop", client=fake_client, expires_at=PAST
        )
        with pytest.raises(TokenExpiredError):
            await _provider().resolve(record.id or 0)

    async def test_resolved_credential_never_prints_the_secret(
        self, fake_client: FakeGithub, bound
    ) -> None:
        record = await _add(fake_client, token="ghp_secret_value")
        credential = await _provider().resolve(record.id or 0)

        assert credential.token == "ghp_secret_value"
        assert "ghp_secret_value" not in repr(credential)

    async def test_resolution_reports_capabilities(self, fake_client: FakeGithub, bound) -> None:
        record = await _add(fake_client)
        credential = await _provider().resolve(record.id or 0)
        assert credential.identity == "alice"
        assert credential.can_read is True
        assert credential.can_write is False
