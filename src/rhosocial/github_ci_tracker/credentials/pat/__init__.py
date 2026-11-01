# src/rhosocial/github_ci_tracker/credentials/pat/__init__.py
"""Fine-grained PAT provider."""

from __future__ import annotations

from ...models.pat_credential import PROVIDER_KIND, AsyncPatCredential, PatCredential
from . import schema
from .provider import AsyncPatProvider, PatProvider, async_provider, sync_provider

#: DDL fragment this provider contributes.
PROVIDER_DDL = schema.DDL

#: Tables this provider owns.
PROVIDER_TABLES = schema.TABLES

#: Models that must share the core connection pool.
PROVIDER_MODELS = (AsyncPatCredential,)

#: The instance the core resolves through the registry.
pat_provider = async_provider

__all__ = [
    "PROVIDER_DDL",
    "PROVIDER_KIND",
    "PROVIDER_MODELS",
    "PROVIDER_TABLES",
    "AsyncPatCredential",
    "AsyncPatProvider",
    "PatCredential",
    "PatProvider",
    "async_provider",
    "schema",
    "pat_provider",
    "sync_provider",
]
