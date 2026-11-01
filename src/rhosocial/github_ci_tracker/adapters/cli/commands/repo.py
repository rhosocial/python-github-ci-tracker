# src/rhosocial/github_ci_tracker/adapters/cli/commands/repo.py
"""``repo`` command: subscribe, bind, unbind, list, discover.

Subscribing and binding are separate steps on purpose. A subscription says
"track this repository"; a binding says "which credential answers for it". The
common case does both at once (``subscribe --credential pat/work``), but keeping
them separable is what lets a repository change credentials without losing its
history -- runs and jobs are keyed by the repository, not by the credential.

Each subcommand is a small handler in :data:`_HANDLERS`; ``run`` only dispatches.
That keeps the per-command shape obvious and stops the shared parts (resolving a
``kind/name`` reference, refusing an unknown repository) from being repeated in
every branch.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from ..formatters import format_output

#: A subcommand handler: given the parsed arguments and a service, do the work.
_Handler = Callable[[argparse.Namespace, object], Awaitable[None]]


def _fail(message: str) -> NoReturn:
    """Report a usage or lookup failure and exit non-zero."""
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


async def _resolve_credential(value: str) -> tuple[str, int]:
    """Resolve ``kind/name`` to ``(kind, provider_ref)``.

    Args:
        value: A ``kind/name`` reference, for example ``pat/work``.

    Returns:
        The opaque pair a binding row stores.

    Raises:
        LookupError: The credential name is unknown for that provider.
        ValueError: The reference is not ``kind/name``.
    """
    from ....credentials import get_provider, parse_credential_ref

    kind, name = parse_credential_ref(value)
    record = await get_provider(kind).find_by_name(name)
    if record is None or record.id is None:
        raise LookupError(f"credential '{value}' not found")
    return kind, record.id


async def _subscribe(args: argparse.Namespace, service: object) -> None:
    """Track a repository, optionally binding a credential at the same time."""
    kind = ref = None
    if args.credential:
        kind, ref = await _resolve_credential(args.credential)

    repo = await service.subscribe_repository(full_name=args.name, token_id=ref)  # type: ignore[attr-defined]
    bound = f" with {args.credential}" if kind else " anonymously"
    print(f"Subscribed to '{repo.full_name}'{bound}.")


async def _unsubscribe(args: argparse.Namespace, service: object) -> None:
    """Stop tracking a repository and drop its cached CI data."""
    await service.unsubscribe_repository(full_name=args.name)  # type: ignore[attr-defined]
    print(f"Unsubscribed from '{args.name}'.")


async def _bind(args: argparse.Namespace, service: object) -> None:
    """Point a subscribed repository at a credential, replacing any previous one."""
    kind, ref = await _resolve_credential(args.credential)
    await service.bind_credential(args.name, kind, ref)  # type: ignore[attr-defined]
    print(f"Bound '{args.name}' to {args.credential}.")


async def _unbind(args: argparse.Namespace, service: object) -> None:
    """Return a repository to anonymous access."""
    removed = await service.unbind_credential(args.name)  # type: ignore[attr-defined]
    print(f"Unbound '{args.name}'." if removed else f"'{args.name}' had no binding.")


async def _list(args: argparse.Namespace, service: object) -> None:
    """List subscriptions with the credential each answers to."""
    from ....credentials.resolver import credential_summary

    rows = []
    for repo in await service.list_repositories():  # type: ignore[attr-defined]
        summary = await credential_summary(repo)
        rows.append(
            {
                "full_name": repo.full_name,
                "private": repo.is_private,
                "default_branch": repo.default_branch,
                "credential": summary["credential"] if summary else None,
                # Whether this credential may trigger, straight from the probe.
                "can_write": (
                    summary.get("capabilities", {}).get("can_write_actions")
                    if summary and summary.get("available")
                    else None
                ),
            }
        )
    print(format_output({"repositories": rows}, args.format, args.verbose))


async def _discover(args: argparse.Namespace, service: object) -> None:
    """List the repositories the bound credential can reach on GitHub."""
    rows = [
        {
            "full_name": item.get("full_name"),
            "private": item.get("private"),
            "default_branch": item.get("default_branch"),
        }
        for item in await service.discover_repositories()  # type: ignore[attr-defined]
    ]
    print(format_output({"repositories": rows}, args.format, args.verbose))


#: Subcommand name -> handler. Adding a verb means adding one entry here.
_HANDLERS: dict[str, _Handler] = {
    "subscribe": _subscribe,
    "unsubscribe": _unsubscribe,
    "bind": _bind,
    "unbind": _unbind,
    "list": _list,
    "discover": _discover,
}


async def run(args: argparse.Namespace, container: Any) -> None:
    """Dispatch a ``repo`` subcommand.

    Args:
        args: Parsed command-line arguments.
        api_key: API key for authentication.
    """

    handler = _HANDLERS.get(getattr(args, "repo_command", None) or "")
    if handler is None:
        verbs = "|".join(sorted(_HANDLERS))
        print(f"Usage: repo {{{verbs}}} --name OWNER/REPO", file=sys.stderr)
        raise SystemExit(2)

    from ....core.exceptions import RepositoryNotFoundError

    try:
        await handler(args, container.repository)
    except RepositoryNotFoundError as exc:
        # Every verb but subscribe answers "no such subscription" the same way:
        # the service refuses, and the operator is told which step they skipped.
        _fail(f"{exc}; run 'repo subscribe --name {args.name}' first")
