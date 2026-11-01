# src/rhosocial/github_ci_tracker/adapters/cli/commands/credential.py
"""``credential`` command: manage provider-owned credentials.

Credentials are an operator concern, not a user-facing one: the MCP surface has
no tool for them, and this CLI is how a self-hosted deployment gets one in place.

The command is namespaced by provider kind (``credential pat ...``) so a second
provider adds a subcommand rather than a parallel command tree -- and so the
handler table stays one dict per provider instead of a branch per provider
inside one function.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..formatters import format_output

#: A subcommand handler: given the parsed arguments and a service, do the work.
_Handler = Callable[[argparse.Namespace, object], Awaitable[None]]


def _token_value(args: argparse.Namespace) -> str:
    """Read a token from the argument, a file, or stdin.

    A file keeps the value out of ``ps`` and out of shell history; stdin is the
    same guarantee for a container entrypoint, which writes the environment
    value into a pipe instead of onto a command line.
    """
    if getattr(args, "token_stdin", False):
        value = sys.stdin.readline().strip()
    elif getattr(args, "token_file", None):
        try:
            value = Path(args.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"Error: cannot read token file: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    else:
        value = (getattr(args, "token", None) or "").strip()
    if not value:
        print(
            "Error: no token given (use --token, --token-file or --token-stdin)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value


async def _add(args: argparse.Namespace, service: object) -> None:
    """Register a credential, probing it first to learn what it may do."""
    record = await service.add_token(  # type: ignore[attr-defined]
        name=args.name,
        token=args.token,
        scope=args.scope,
        probe_repo=getattr(args, "probe_repo", None),
    )
    print(f"Credential 'pat/{record.name}' added.")
    if record.owner_login:
        print(f"  owner:       {record.owner_login}")
    if record.can_write_actions is not None:
        # The probe result, not a configuration flag: this is what decides
        # whether the deployment can trigger CI at all.
        print(f"  can trigger: {'yes' if record.can_write_actions else 'no'}")


async def _ensure(args: argparse.Namespace, service: object) -> None:
    """Create the credential or reconcile an existing one.

    The container's first-boot init script calls this: a fresh database gets a
    credential, a later forced run reconciles the stored value in place. Both
    paths verify the token against GitHub before the row is written, and
    ``--probe-repo`` additionally measures the repository-scoped Actions write
    permission.
    """
    record, created = await service.ensure_token(  # type: ignore[attr-defined]
        name=args.name,
        token=_token_value(args),
        scope=args.scope,
        probe_repo=getattr(args, "probe_repo", None),
    )
    action = "added" if created else "rotated"
    print(f"Credential 'pat/{record.name}' {action} and verified.")
    if record.owner_login:
        print(f"  owner:       {record.owner_login}")
    if record.can_write_actions is not None:
        print(f"  can trigger: {'yes' if record.can_write_actions else 'no'}")


async def _list(args: argparse.Namespace, service: object) -> None:
    """List credentials without exposing the token values."""
    rows = [
        {
            "credential": row["credential"],
            "owner_login": row["owner_login"],
            "is_active": row["is_active"],
            "can_write_actions": row["can_write_actions"],
            "expires_at": row["expires_at"],
        }
        for row in await service.list_tokens()  # type: ignore[attr-defined]
    ]
    print(format_output({"credentials": rows}, args.format, args.verbose))


async def _show(args: argparse.Namespace, service: object) -> None:
    """Report one credential's capabilities, re-probing when a repo is given."""
    print(
        format_output(
            await service.capabilities(  # type: ignore[attr-defined]
                args.name, probe_repo=getattr(args, "probe_repo", None)
            ),
            args.format,
            args.verbose,
        )
    )


async def _rotate(args: argparse.Namespace, service: object) -> None:
    """Replace a credential's value, keeping its id and bindings."""
    record = await service.rotate_token(  # type: ignore[attr-defined]
        name=args.name,
        token=args.token,
        probe_repo=getattr(args, "probe_repo", None),
    )
    print(f"Credential 'pat/{record.name}' rotated; bindings kept.")


async def _remove(args: argparse.Namespace, service: object) -> None:
    """Delete a credential; refused while repositories still use it."""
    await service.delete_token(args.name)  # type: ignore[attr-defined]
    print(f"Credential 'pat/{args.name}' removed.")


#: Subcommand name -> handler for the ``pat`` provider.
_PAT_HANDLERS: dict[str, _Handler] = {
    "add": _add,
    "ensure": _ensure,
    "list": _list,
    "show": _show,
    "rotate": _rotate,
    "remove": _remove,
}


async def run(args: argparse.Namespace, container: Any) -> None:
    """Dispatch a ``credential`` subcommand.

    Args:
        args: Parsed command-line arguments.
        api_key: API key for authentication.
    """

    kind = getattr(args, "credential_kind", None) or "pat"
    handlers = {"pat": _PAT_HANDLERS}.get(kind)
    if handlers is None:
        print(f"Error: unknown credential kind '{kind}' (known: pat)", file=sys.stderr)
        raise SystemExit(2)

    handler = handlers.get(getattr(args, "credential_command", None) or "")
    if handler is None:
        print(
            f"Usage: credential {kind} {{{'|'.join(sorted(handlers))}}}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    await handler(args, container.token)
