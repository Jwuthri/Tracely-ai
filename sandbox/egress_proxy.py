"""The only way out of an audit sandbox: a forward proxy that admits a short list of hosts.

A sandbox with `network: allowlist` sits on an internal Docker network whose single other member
is this process. It exists so ordinary work succeeds — `pip install`, reading documentation —
without giving an adversarially-prompted model the internet.

Rules, all enforced here:
- **Host allowlist** (`EGRESS_ALLOWLIST`, comma-separated; a name also covers its subdomains).
- **Plain HTTP: GET and HEAD only.** Over HTTPS the proxy sees only the host (`CONNECT`), not
  the method, so "read-only" there means "only to hosts with nothing worth writing to" — keep
  the list to registries and documentation. Strict method filtering on HTTPS needs TLS
  inspection, which this does not do.
- **Public addresses only.** The name is resolved HERE and the connection goes to that address:
  an allowlisted name that resolves to a private, loopback or link-local address is refused, so
  the list cannot be used to reach the host's own network.
- **Every request is logged** as one JSON line — allowed or not. A refused request is evidence:
  it is what the audited model tried to reach.

Standard library only; runs as its own container (see docker-compose.sandbox.yml).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import sys
import time

ALLOWLIST = [h.strip().lower() for h in os.environ.get("EGRESS_ALLOWLIST", "").split(",") if h.strip()]
PORT = int(os.environ.get("EGRESS_PORT", "3128"))
READ_METHODS = {"GET", "HEAD"}


def allowed_host(host: str, allowlist: list[str] | None = None) -> bool:
    """`host` is on the list, or is a subdomain of an entry. Never a bare IP."""
    host = (host or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return False  # an address is not a name on the list, whatever it points at
    except ValueError:
        pass
    return any(host == entry or host.endswith("." + entry) for entry in (ALLOWLIST if allowlist is None else allowlist))


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def decide(method: str, host: str, port: int, allowlist: list[str] | None = None) -> tuple[bool, str]:
    """`(allowed, reason)` for one request, before any address is resolved."""
    if not allowed_host(host, allowlist):
        return False, "host not on the allowlist"
    if method == "CONNECT":
        return (port == 443, "CONNECT is only allowed to port 443")
    if method not in READ_METHODS:
        return False, f"{method} is not allowed (read-only)"
    return (port == 80, "plain HTTP is only allowed to port 80")


def _log(**fields) -> None:
    sys.stdout.write(json.dumps({"ts": round(time.time(), 3), **fields}) + "\n")
    sys.stdout.flush()


async def _resolve_public(host: str, port: int) -> str | None:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for info in infos:
        if is_public(info[4][0]):
            return info[4][0]
    return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _refuse(writer: asyncio.StreamWriter, status: str, reason: str) -> None:
    body = f"{reason}\n".encode()
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()
    writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    client = (writer.get_extra_info("peername") or ("?",))[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        method, target, _version = request_line.split(" ", 2)
        method = method.upper()
        if method == "CONNECT":
            host, _, port_s = target.rpartition(":")
            port = int(port_s or 443)
        else:
            rest = target.split("://", 1)[1] if "://" in target else target
            hostport, _, _path = rest.partition("/")
            host, _, port_s = hostport.partition(":")
            port = int(port_s or 80)
        ok, reason = decide(method, host, port)
        address = await _resolve_public(host, port) if ok else None
        if ok and address is None:
            ok, reason = False, "the host does not resolve to a public address"
        _log(client=client, method=method, host=host, port=port, allowed=ok, **({} if ok else {"reason": reason}))
        if not ok:
            await _refuse(writer, "403 Forbidden", f"Blocked by proxy policy: {reason}.")
            return
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(address, port), timeout=15)
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            up_writer.write(head)  # the request as sent; the origin reads the Host header
            await up_writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    except Exception as exc:  # noqa: BLE001 — one bad client must not take the proxy down
        _log(client=client, error=f"{type(exc).__name__}: {exc}"[:200])
        try:
            await _refuse(writer, "502 Bad Gateway", "Proxy error.")
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)  # noqa: S104 — container-internal network only
    _log(event="listening", port=PORT, allowlist=ALLOWLIST)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
