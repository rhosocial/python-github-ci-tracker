# src/rhosocial/github_ci_tracker/models/pat_credential.py
"""The credential table, sync and async twins -- one model like any other.

This row lives here because its table is defined in ``schema/model.py``
(``PAT_SCHEMA``) beside every other table, rendered by the same renderers and
created by the same ``db init``. Keeping the model in the provider while the
schema lived in core gave one table two homes, so the map in this directory's
README did not cover it.

What the provider owns is the *lifecycle* -- resolving a row into a
:class:`ResolvedCredential <rhosocial.github_ci_tracker.credentials.base.ResolvedCredential>`,
probing capabilities, rotating, deactivating -- not the columns. The core still
never reads them directly; it asks the provider, and the provider is free to mean
something else by "resolve".

The value is stored in plaintext. That is the project's existing decision for a
single-user self-hosted deployment, where encryption at rest would trade a real
operational cost (re-entering every credential after a restore) for a threat
model this shape of deployment does not have. It is not a default to inherit if
the project ever gains hosted multi-tenant mode.

The probe results (``can_read_actions`` / ``can_write_actions`` /
``can_manage_webhooks``) live here too, because they describe *this* credential
and nothing else. They are measured, not configured: a read-only PAT can never
report write capability, no matter what an operator or a model asks for.

A repository points at a row of this table only through
``ct_repository_bindings``, whose ``(provider_kind, provider_ref)`` pair stays
opaque: that indirection, not where this class sits, is what keeps a second
provider from reaching the core.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery

logger = logging.getLogger(__name__)

#: Registry key this provider registers under; persisted in binding rows.
PROVIDER_KIND = "pat"


def _clean(value: Any, default: str = "") -> str:
    """Return a trimmed string, falling back to ``default``."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


class PatCredential(DefaultTimestampMixin, ActiveRecord):
    """A fine-grained PAT, synchronous twin."""

    __table_name__ = "ct_pat_credentials"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Operator-facing alias; the CLI's primary key.
    name: str = ""
    #: The secret. Plaintext by design for this deployment shape.
    token: str = ""
    #: GitHub login probed at add time.
    owner_login: str | None = None
    scope: str = "repo,workflow"
    expires_at: datetime | None = None
    is_active: bool = True
    #: Probe results, cached so gating a trigger needs no network round trip.
    can_read_actions: bool | None = None
    can_write_actions: bool | None = None
    can_manage_webhooks: bool | None = None
    capabilities_probed_at: datetime | None = None

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_by_name(cls, name: str) -> PatCredential | None:
        """Return the credential registered under ``name``, or ``None``."""
        return cls.find_one(cls.c.name == name)

    @classmethod
    def active(cls) -> ActiveQuery:
        """Query builder over the credentials that are still active."""
        return cls.query().where(cls.c.is_active == True)  # noqa: E712 - ORM predicate


class AsyncPatCredential(DefaultTimestampMixin, AsyncActiveRecord):
    """A fine-grained PAT, asynchronous twin.

    Behavior-identical to :class:`PatCredential`; the service uses this twin.
    """

    __table_name__ = "ct_pat_credentials"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    name: str = ""
    token: str = ""
    owner_login: str | None = None
    scope: str = "repo,workflow"
    expires_at: datetime | None = None
    is_active: bool = True
    can_read_actions: bool | None = None
    can_write_actions: bool | None = None
    can_manage_webhooks: bool | None = None
    capabilities_probed_at: datetime | None = None

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_by_name(cls, name: str) -> AsyncPatCredential | None:
        """Return the credential registered under ``name``, or ``None``."""
        return await cls.find_one(cls.c.name == name)

    @classmethod
    def active(cls) -> AsyncActiveQuery:
        """Query builder over the credentials that are still active."""
        return cls.query().where(cls.c.is_active == True)  # noqa: E712 - ORM predicate


__all__ = ["PROVIDER_KIND", "AsyncPatCredential", "PatCredential"]
