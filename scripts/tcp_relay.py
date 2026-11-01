# scripts/tcp_relay.py
"""Forward a Windows-owned TCP port to a Docker-published one.

Docker Desktop on WSL with ``networkingMode=mirrored`` reserves every published
port in the Windows network stack without serving it: ``bind`` on the same port
from Windows fails with ``WSAEADDRINUSE``, and connections to the host's LAN
address are dropped rather than refused. Loopback keeps working, because WSL
mirrors it.

A port Docker never published belongs to Windows, so the router and the LAN can
reach it. This relay accepts on such a port and pipes the bytes to the published
one over loopback, which is the only path that survives the mirrored stack.

    python scripts/tcp_relay.py --listen 18444 --target 127.0.0.1:18080

Then forward the router's public port to the relay port: the webhook keeps its
public URL, and only the internal hop changes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging

log = logging.getLogger("tcp_relay")

BUFFER = 64 * 1024


async def _pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    peer: str,
) -> None:
    """Copy one direction until the far end closes, then half-close."""
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        log.debug("%s: closing direction", peer)
        with contextlib.suppress(Exception):
            writer.write_eof()


async def _handle(
    client: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    peer = client_writer.get_extra_info("peername")
    peer_name = f"{peer[0]}:{peer[1]}" if peer else "?"
    try:
        server_reader, server_writer = await asyncio.open_connection(target_host, target_port)
    except OSError as exc:
        log.warning("%s: target refused: %s", peer_name, exc)
        client_writer.close()
        return
    log.info("%s -> %s:%s", peer_name, target_host, target_port)
    try:
        await asyncio.gather(
            _pump(client, server_writer, peer_name),
            _pump(server_reader, client_writer, peer_name),
        )
    finally:
        for writer in (server_writer, client_writer):
            with contextlib.suppress(Exception):
                writer.close()


async def _serve(listen_host: str, listen_port: int, target: str) -> None:
    target_host, _, target_port = target.rpartition(":")
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, target_host, int(target_port)),
        host=listen_host,
        port=listen_port,
    )
    log.info("relaying %s:%s -> %s", listen_host, listen_port, target)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0", help="address to accept on")
    parser.add_argument("--port", type=int, default=18444, help="port to accept on")
    parser.add_argument(
        "--target",
        default="127.0.0.1:18080",
        help="host:port to forward to",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(_serve(args.listen, args.port, args.target))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
