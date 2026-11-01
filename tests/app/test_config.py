# tests/app/test_config.py
"""Service settings: the webhook listener's TLS material.

GitHub delivers only to an https callback with a publicly trusted certificate,
so the receiver terminates TLS itself. These cases pin the plumbing that feeds
the certificate and key to the listener: absent by default (local REST and MCP
stay plain HTTP), sourced from the environment when configured.
"""

from __future__ import annotations

import pytest

from rhosocial.github_ci_tracker.config import ServiceSettings


def test_webhook_tls_defaults_to_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent variables mean no TLS, not a fallback path."""
    monkeypatch.delenv("CITRACKER_WEBHOOK_SSL_CERTFILE", raising=False)
    monkeypatch.delenv("CITRACKER_WEBHOOK_SSL_KEYFILE", raising=False)

    settings = ServiceSettings.from_env()

    assert settings.webhook_ssl_certfile == ""
    assert settings.webhook_ssl_keyfile == ""


def test_webhook_tls_reads_cert_and_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both pieces come from the environment, independently of each other."""
    monkeypatch.setenv("CITRACKER_WEBHOOK_SSL_CERTFILE", "/etc/certs/fullchain.pem")
    monkeypatch.setenv("CITRACKER_WEBHOOK_SSL_KEYFILE", "/etc/certs/privkey.pem")

    settings = ServiceSettings.from_env()

    assert settings.webhook_ssl_certfile == "/etc/certs/fullchain.pem"
    assert settings.webhook_ssl_keyfile == "/etc/certs/privkey.pem"
