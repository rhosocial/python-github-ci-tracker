# tests/services/github/test_github_webhooks.py
"""``GithubWebhooks``: arm, inspect and disarm the push channel.

The group is deliberately thin -- list, create, delete, ping. Which hook is "ours"
is the caller's question, because answering it needs the URL the process is
configured with, and that is configuration rather than API shape.
"""

from __future__ import annotations

import pytest

from rhosocial.github_ci_tracker.services.github import GithubWebhooks, WebhookManager

REPO = "acme/demo"


def test_the_group_satisfies_its_protocol() -> None:
    """Structural check: the protocol is the contract, not a base class."""
    assert isinstance(GithubWebhooks(), WebhookManager)


class TestCreate:
    async def test_forwards_the_config_and_returns_what_github_stored(
        self, read_transport, credential
    ) -> None:
        config = {"url": "https://ci.example/hook", "events": ["workflow_run"]}

        created = await GithubWebhooks(read_transport, credential=credential).create_webhook(
            REPO, config
        )

        assert created["id"] == 31_337
        assert created["url"] == config["url"]
        assert read_transport.calls == [
            (
                "create_webhook",
                {"token": "ghp_test_token", "repo": REPO, "config": config},
            )
        ]


class TestList:
    async def test_returns_the_payloads_unchanged(self, read_transport) -> None:
        read_transport.webhook_payloads = [
            {"id": 1, "active": True, "config": {"url": "https://a"}},
            {"id": 2, "active": False, "config": {"url": "https://b"}},
        ]

        hooks = await GithubWebhooks(read_transport).list_webhooks(REPO)

        assert [hook["id"] for hook in hooks] == [1, 2]
        assert read_transport.calls == [("list_webhooks", {"token": None, "repo": REPO})]


class TestDeleteAndPing:
    async def test_delete_targets_the_hook_id(self, read_transport) -> None:
        await GithubWebhooks(read_transport).delete_webhook(REPO, 31_337)

        assert read_transport.calls == [
            ("delete_webhook", {"token": None, "repo": REPO, "hook_id": 31_337})
        ]

    async def test_ping_targets_the_hook_id(self, read_transport) -> None:
        await GithubWebhooks(read_transport).ping_webhook(REPO, 31_337)

        assert read_transport.calls == [
            ("ping_webhook", {"token": None, "repo": REPO, "hook_id": 31_337})
        ]


class TestTokenSelection:
    async def test_the_resolver_answers_per_repository(self, read_transport) -> None:
        asked: list[str] = []

        async def resolve(repo_full_name: str):
            asked.append(repo_full_name)

            class _Bound:
                token = "ghp_bound"

            return _Bound()

        await GithubWebhooks(read_transport, resolve=resolve).list_webhooks(REPO)

        assert asked == [REPO]
        assert read_transport.calls[0][1]["token"] == "ghp_bound"

    async def test_a_transport_error_propagates(self, read_transport) -> None:
        read_transport.error = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await GithubWebhooks(read_transport).list_webhooks(REPO)
