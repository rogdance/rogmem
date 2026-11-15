"""Release source — URL validation, DNS-aware guard, and pinned HTTPS fetch.

Three layers, in order:

1. `validate_source_url`: structural checks on the URL string only.
   Scheme, userinfo, fragment, length, and *literal* host (so trailing
   dots, IPv4-mapped IPv6 literals, 0.0.0.0, 127.1 and the usual
   private-range literals are refused without any DNS lookup).

2. `resolve_host`: for a validated URL, resolve the hostname once via
   ``getaddrinfo`` and refuse if **any** answer falls into a blocked
   range (loopback, private, link-local, unspecified, reserved,
   ULA, …). IPv4-mapped IPv6 answers are normalised to v4 before the
   check. This closes DNS-based SSRF regardless of how a public host
   label spells itself.

3. `fetch_manifest`: opens a socket pinned to the already-validated IP,
   wraps TLS with the original hostname as SNI, and sends ``Host:``
   with the original hostname.  Each redirect hop re-runs the full
   check and re-pins, so DNS-rebinding between check and connect
   cannot slip through.

The loopback bypass (``ROGMEM_ALLOW_LOOPBACK_SOURCE=1``) is for the
tests only; it just re-labels loopback literals as "allowed for the
duration of this process", and plays no part in production.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from typing import Optional
from urllib.parse import urlsplit

from rogmem.release_manifest import MAX_RESPONSE_BYTES, MAX_URL_LENGTH

ENV_SOURCE_URL = "ROGMEM_RELEASES_URL"
ENV_ALLOW_LOOPBACK = "ROGMEM_ALLOW_LOOPBACK_SOURCE"
MAX_REDIRECTS = 3
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 5.0
USER_AGENT = "rogmem/release-probe"

_BLOCKED_NETS = [
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",            # "this network" / unspecified
        "10.0.0.0/8",
        "100.64.0.0/10",        # CGN
        "127.0.0.0/8",          # loopback
        "169.254.0.0/16",       # link-local (includes cloud metadata)
        "172.16.0.0/12",
        "192.0.0.0/24",         # IETF protocol assignments
        "192.168.0.0/16",
        "198.18.0.0/15",        # benchmarking
        "198.51.100.0/24",      # TEST-NET-2
        "203.0.113.0/24",       # TEST-NET-3
        "224.0.0.0/4",          # multicast
        "240.0.0.0/4",          # future use / broadcast
        "::/128",               # unspecified
        "::1/128",              # loopback v6
        "fc00::/7",             # ULA
        "fe80::/10",            # link-local v6
        "fec0::/10",            # site-local v6 (deprecated but still refused)
        "ff00::/8",             # multicast v6
    )
]


class SourceError(Exception):
    """A classified source failure (URL, DNS, or transport)."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


@dataclass(frozen=True)
class FetchOutcome:
    """What one fetch produced. `body is None` for non-ok/non-304."""

    kind: str
    body: Optional[bytes] = None
    etag: str = ""
    status: int = 0


def _is_literal_private_host(host: str) -> bool:
    """Literal check only — no DNS. Catches the tricky edge literals."""
    if not host:
        return True
    lowered = host.lower().rstrip(".")  # trailing dot (localhost.) is still local
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    text = lowered.strip("[]")
    # IPv4 shorthand (e.g. "127.1") that inet_aton accepts but ipaddress rejects.
    if "." in text and all(part.isdigit() or part == "" for part in text.split(".")):
        try:
            v4 = socket.inet_aton(text)
            return _is_blocked_ip(ipaddress.IPv4Address(v4))
        except OSError:
            pass
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return False
    return _is_blocked_ip(parsed)


def _normalise(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    normalised = _normalise(ip)
    return any(normalised in net for net in _BLOCKED_NETS)


def _allow_loopback() -> bool:
    return os.environ.get(ENV_ALLOW_LOOPBACK) == "1"


def validate_source_url(url: str) -> str:
    """Structural validation. Returns the URL unchanged, or raises.

    DNS is **not** consulted here.  Literal-host SSRF edges (localhost,
    localhost., 127.1, 0.0.0.0, IPv4-mapped IPv6, link-local, …) are
    all refused at this layer; hostnames that *look* public still get a
    DNS check at fetch time (`resolve_host`).
    """
    if not isinstance(url, str):
        raise SourceError("config_invalid", "URL 必须是字符串")
    text = url.strip()
    if not text:
        raise SourceError("config_invalid", "URL 不得为空")
    if len(text) > MAX_URL_LENGTH:
        raise SourceError("config_invalid", f"URL 超过 {MAX_URL_LENGTH} 字符上限")
    try:
        parts = urlsplit(text)
    except ValueError as error:
        raise SourceError("config_invalid", "URL 无法解析") from error
    if parts.username or parts.password:
        raise SourceError("config_invalid", "URL 不允许包含 userinfo")
    if parts.fragment:
        raise SourceError("config_invalid", "URL 不允许包含 fragment")
    if "@" in (parts.query or ""):
        raise SourceError("config_invalid", "URL query 中不允许出现 @")
    try:
        if parts.port is not None and (parts.port < 1 or parts.port > 65535):
            raise SourceError("config_invalid", "URL 端口非法")
    except ValueError as error:
        raise SourceError("config_invalid", "URL 端口非法") from error
    if not parts.hostname:
        raise SourceError("config_invalid", "URL 缺少主机名")
    literal_private = _is_literal_private_host(parts.hostname)
    allow_loopback = _allow_loopback() and literal_private
    if parts.scheme == "http":
        if not allow_loopback:
            raise SourceError("config_invalid", "URL 必须使用 https 协议")
    elif parts.scheme != "https":
        raise SourceError("config_invalid", "URL 必须使用 https 协议")
    if literal_private and not allow_loopback:
        raise SourceError("config_invalid", "URL 指向回环或私有网段")
    return text


def resolve_host(hostname: str, port: int) -> list[tuple[int, int, str]]:
    """Resolve and vet every answer. Raises SourceError('network') if any
    answer lies in a blocked range. Returns tuples the connect path can
    use directly: ``(family, socket_type, pinned_ip_literal)``.
    """
    if _is_literal_private_host(hostname) and not _allow_loopback():
        raise SourceError("network", "主机解析到回环或私有网段")
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise SourceError("network", f"DNS 解析失败：{error}") from error
    except OSError as error:
        raise SourceError("network", f"DNS 查询错误：{error}") from error
    if not infos:
        raise SourceError("network", "DNS 返回空结果")
    answers: list[tuple[int, int, str]] = []
    for family, socktype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as error:
            raise SourceError("network", f"DNS 回答含非法 IP：{ip_str!r}") from error
        normalised = _normalise(ip_obj)
        if _is_blocked_ip(normalised) and not _allow_loopback():
            raise SourceError("network", f"主机解析到受限地址段 {normalised}")
        answers.append((family, socktype, str(normalised)))
    return answers


def _open_pinned(
    hostname: str,
    port: int,
    scheme: str,
    timeout: float,
):
    """Open one connection pinned to a resolved IP, with TLS SNI =
    original hostname when the scheme is https.

    The socket is opened manually (``socket.socket`` + ``connect``) to
    the already-resolved IP so that no second DNS lookup can happen
    between the vetting step and the connect step — ``create_connection``
    would otherwise call ``getaddrinfo`` internally, opening a window
    for DNS rebinding.
    """
    answers = resolve_host(hostname, port)
    family, socktype, ip = answers[0]
    sock = socket.socket(family, socktype)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
    except Exception:
        sock.close()
        raise
    if scheme == "https":
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=hostname)
        connection = HTTPSConnection(hostname, port, timeout=timeout)
    else:
        connection = HTTPConnection(hostname, port, timeout=timeout)
    connection.sock = sock
    return connection


def _status_kind(status: int, headers) -> str:
    if status == 200:
        return "ok"
    if status == 304:
        return "not_modified"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status == 403:
        remaining = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
        if remaining == "0":
            return "rate_limited"
        return "not_found"
    if 500 <= status < 600:
        return "server_error"
    return "unknown"


def _read_capped(response: HTTPResponse) -> tuple[Optional[bytes], str]:
    advertised = response.getheader("Content-Length")
    if advertised is not None:
        try:
            length = int(advertised)
        except ValueError:
            length = -1
        if length > MAX_RESPONSE_BYTES:
            return None, "too_large"
    buffer = bytearray()
    while True:
        chunk = response.read(16 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_RESPONSE_BYTES:
            return None, "too_large"
    return bytes(buffer), "ok"


def _one_hop(
    url: str,
    etag: Optional[str],
    timeout_connect: float,
    timeout_read: float,
) -> tuple[Optional[FetchOutcome], Optional[str]]:
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme not in ("http", "https"):
        return FetchOutcome(kind="network"), None
    hostname = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    try:
        connection = _open_pinned(hostname, port, scheme, timeout_connect)
    except SourceError as error:
        return FetchOutcome(kind=error.kind), None
    except socket.timeout:
        return FetchOutcome(kind="timeout"), None
    except (ssl.SSLError, ConnectionError, OSError) as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, socket.timeout):
            return FetchOutcome(kind="timeout"), None
        return FetchOutcome(kind="network"), None
    try:
        if hasattr(connection.sock, "settimeout"):
            connection.sock.settimeout(timeout_read)
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Host": hostname if port in (80, 443) else f"{hostname}:{port}",
        }
        if etag:
            headers["If-None-Match"] = etag
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        if status in (301, 302, 303, 307, 308):
            location = response.getheader("Location") or ""
            try:
                response.read()
            except Exception:
                pass
            return None, location
        kind = _status_kind(status, response.headers)
        if kind == "ok":
            body, body_kind = _read_capped(response)
            if body_kind == "too_large":
                return FetchOutcome(kind="too_large", status=status), None
            etag_new = response.getheader("ETag") or ""
            return FetchOutcome(kind="ok", body=body, etag=etag_new, status=status), None
        if kind == "not_modified":
            try:
                response.read()
            except Exception:
                pass
            return FetchOutcome(kind="not_modified", etag=etag or "", status=status), None
        try:
            response.read()
        except Exception:
            pass
        return FetchOutcome(kind=kind, status=status), None
    except socket.timeout:
        return FetchOutcome(kind="timeout"), None
    except (ssl.SSLError, ConnectionError, OSError) as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, socket.timeout):
            return FetchOutcome(kind="timeout"), None
        return FetchOutcome(kind="network"), None
    except Exception:
        return FetchOutcome(kind="unknown"), None
    finally:
        try:
            connection.close()
        except Exception:
            pass


def fetch_manifest(
    url: str,
    *,
    etag: Optional[str] = None,
    timeout_connect: float = DEFAULT_CONNECT_TIMEOUT,
    timeout_read: float = DEFAULT_READ_TIMEOUT,
) -> FetchOutcome:
    """Fetch with budgets, ETag, SSRF-aware DNS check, and pinned socket.

    URL validation runs on every hop; DNS resolution runs on every hop;
    the socket is pinned to a resolved IP so DNS rebinding between
    check and connect cannot flip the answer. Redirects to http:// or
    hosts that resolve to private ranges are refused as `network`.
    """
    try:
        current = validate_source_url(url)
    except SourceError as error:
        return FetchOutcome(kind=error.kind)
    for _ in range(MAX_REDIRECTS + 1):
        outcome, location = _one_hop(current, etag, timeout_connect, timeout_read)
        if outcome is not None:
            return outcome
        if not location:
            return FetchOutcome(kind="network")
        if location.startswith("/"):
            parts = urlsplit(current)
            current = f"{parts.scheme}://{parts.netloc}{location}"
        else:
            current = location
        try:
            validate_source_url(current)
        except SourceError:
            return FetchOutcome(kind="network")
    return FetchOutcome(kind="network")
