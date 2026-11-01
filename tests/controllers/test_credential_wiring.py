# tests/controllers/test_credential_wiring.py
"""Who answers a GitHub call when the process has no ambient PAT.

The composition root hands each group a resolver that reads
``ct_repository_bindings``. Without it a deployment that binds per repository
(the CLI, or a server started without ``CITRACKER_PAT_NAME``) would read
anonymously -- a private repository would answer 404 and a dispatch would be
refused, with the binding sitting there unused.
"""

from __future__ import annotations

from conftest import FakeGithub, FakeGithubReads

from rhosocial.github_ci_tracker.container import Container
from rhosocial.github_ci_tracker.credentials import get_provider
from rhosocial.github_ci_tracker.models import AsyncRepository
from rhosocial.github_ci_tracker.models.binding import AsyncRepositoryBinding

REPO = "acme/demo"


async def _bound_repository(token: str = "ghp_bound") -> AsyncRepository:
    """A subscription bound to a stored credential."""
    credential = await get_provider("pat").add(token=token, name="work", client=FakeGithub())
    repo = AsyncRepository(github_repo_id=1, full_name=REPO, owner="acme", name="demo")
    await repo.save()
    await AsyncRepositoryBinding.bind(repo.id or 0, "pat", credential.id or 0)
    return repo


async def test_a_bound_repository_uses_its_credential(bound) -> None:
    """No ambient PAT: the binding decides, and the token reaches the transport."""
    await _bound_repository("ghp_bound")
    transport = FakeGithubReads()

    services = Container.build(client=transport)  # no ambient credential
    await services.query._readers.get_run(REPO, 90_001)  # noqa: SLF001 - the seam

    assert transport.calls[0][1]["token"] == "ghp_bound"


async def test_an_ambient_credential_wins_over_the_binding(bound) -> None:
    """A single-PAT deployment does not pay a lookup per call."""
    await _bound_repository("ghp_bound")
    ambient = await get_provider("pat").resolve(1)
    transport = FakeGithubReads()

    services = Container.build(client=transport, credential=ambient)
    await services.query._readers.get_run(REPO, 90_001)  # noqa: SLF001

    assert transport.calls[0][1]["token"] == "ghp_bound"


async def test_an_unbound_repository_reads_anonymously(bound) -> None:
    """Anonymous is a supported mode, not a failure."""
    repo = AsyncRepository(github_repo_id=2, full_name="acme/public", owner="acme", name="public")
    await repo.save()
    transport = FakeGithubReads()

    services = Container.build(client=transport)
    await services.query._readers.get_run("acme/public", 90_001)  # noqa: SLF001

    assert transport.calls[0][1]["token"] is None
