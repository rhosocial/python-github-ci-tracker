# src/rhosocial/github_ci_tracker/credentials/__init__.py
"""Credential providers.

Importing this package registers the providers that ship in this build, so the
core can resolve any binding it finds in ``ct_repository_bindings``. Adding a
provider means adding its package here; nothing in ``core`` changes.

The dependency runs one way: a provider owns the lifecycle of its own table (and
imports the schema definition for that table from :mod:`schema.model`), and this
package never imports the core's services.

The credential row itself is a model like any other -- ``PatCredential`` /
``AsyncPatCredential`` live in :mod:`models.pat_credential`, because the table
is defined in ``schema/model.py`` beside every other one. Both are re-exported
here so provider code and existing importers keep working; the canonical home
is ``models``.
"""

from __future__ import annotations

from typing import Any

from ..models.pat_credential import AsyncPatCredential, PatCredential
from .base import (
    Capabilities,
    IAsyncCredentialProvider,
    ResolvedCredential,
    get_provider,
    get_sync_provider,
    normalize_capabilities,
    parse_credential_ref,
    provider_kinds,
    register_provider,
)
from .pat import PROVIDER_MODELS, pat_provider
from .redaction import redact_credential_logging

#: Every provider-owned table name, for teardown.
PROVIDER_TABLES: tuple[str, ...] = ("ct_pat_credentials",)

#: Provider models that must be bound to the same connection as the core ones.
PROVIDER_MODELS: tuple[type, ...] = PROVIDER_MODELS


def describe_credential(entry: dict[str, Any]) -> str:
    """Render a credential metadata row as ``kind/name``."""
    kind = entry.get("kind", "")
    name = entry.get("name") or entry.get("token_name") or ""
    return f"{kind}/{name}" if kind else str(name)


__all__ = [
    "Capabilities",
    "IAsyncCredentialProvider",
    "PROVIDER_MODELS",
    "PROVIDER_TABLES",
    "AsyncPatCredential",
    "PatCredential",
    "ResolvedCredential",
    "describe_credential",
    "get_provider",
    "get_sync_provider",
    "normalize_capabilities",
    "parse_credential_ref",
    "pat_provider",
    "provider_kinds",
    "redact_credential_logging",
    "register_provider",
]
