# tests/services/github/test_github_credentials.py
"""``GithubCredentials``: what a token is, and what it may do.

The token is the subject of both questions, which is why it is a parameter here
and absent everywhere else -- the other groups resolve a credential for a
repository; these ask *about* one.
"""

from __future__ import annotations

import pytest

from rhosocial.github_ci_tracker.core.exceptions import TokenRequiredError
from rhosocial.github_ci_tracker.services.github import CredentialProbe, GithubCredentials

REPO = "acme/demo"


def test_the_group_satisfies_its_protocol() -> None:
    """Structural check: the protocol is the contract, not a base class."""
    assert isinstance(GithubCredentials(), CredentialProbe)


class TestVerifyToken:
    async def test_forwards_the_token_and_the_probe_repository(self, read_transport) -> None:
        read_transport.verify_payload = {"login": "alice", "can_write_actions": True}

        raw = await GithubCredentials(read_transport).verify_token("ghp_candidate", REPO)

        assert raw["login"] == "alice"
        assert read_transport.calls == [
            ("verify_token", {"token": "ghp_candidate", "probe_repo": REPO})
        ]

    async def test_without_a_probe_repository_the_write_capability_is_unmeasured(
        self, read_transport
    ) -> None:
        # No repository means no measurement: the caller must not read a missing
        # key as "allowed".
        read_transport.verify_payload = {"login": "alice", "can_write_actions": None}

        raw = await GithubCredentials(read_transport).verify_token("ghp_candidate")

        assert raw["can_write_actions"] is None
        assert read_transport.calls == [
            ("verify_token", {"token": "ghp_candidate", "probe_repo": None})
        ]


class TestProbeActionsWrite:
    async def test_asks_about_the_credential_that_answers_for_the_repository(
        self, read_transport, credential
    ) -> None:
        read_transport.can_write = True

        allowed = await GithubCredentials(
            read_transport, credential=credential
        ).probe_actions_write(REPO)

        assert allowed is True
        assert read_transport.calls == [
            ("probe_actions_write", {"token": "ghp_test_token", "repo": REPO})
        ]

    async def test_a_read_only_credential_reports_false(self, read_transport, credential) -> None:
        read_transport.can_write = False

        allowed = await GithubCredentials(
            read_transport, credential=credential
        ).probe_actions_write(REPO)

        assert allowed is False

    async def test_no_credential_at_all_is_an_error_not_an_anonymous_probe(
        self, read_transport
    ) -> None:
        # An anonymous probe would answer 401 and read as a GitHub failure; the
        # caller needs to know it forgot to bind a credential.
        with pytest.raises(TokenRequiredError):
            await GithubCredentials(read_transport).probe_actions_write(REPO)

        assert read_transport.calls == []
