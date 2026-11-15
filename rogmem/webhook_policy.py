"""Outbound alert webhook policy. Refuse loopback, private, link-local, and metadata targets."""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

import http.client

_BLOCKED_NAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.google.com",
    "instance-data",
}
_ALIBABA_METADATA = ipaddress.IPv4Address("100.100.100.200")
_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")


def alert_payload(message: str, *, now: str | None = None) -> bytes:
    text = (message or "").strip().splitlines()[0][:200] if message else ""
    stamp = now or datetime.now(timezone.utc).isoformat()
    return json.dumps({"at": stamp, "text": text}, ensure_ascii=False).encode()


def validate_webhook_url(url: str, *, resolve=None) -> str | None:
    _planned, error = _plan(url, resolve=resolve)
    return error


def deliver(url: str, body: bytes, *, resolve=None, dial=None, timeout: float = 5.0) -> int:
    planned, error = _plan(url, resolve=resolve)
    if error or planned is None:
        raise ValueError(error or "地址解析失败")
    if dial is not None:
        return dial(planned["ip"], planned["port"], planned["scheme"], planned["host"], planned["path"], body, timeout)
    return post_once(
        planned["ip"],
        planned["port"],
        planned["scheme"],
        planned["host"],
        planned["path"],
        body,
        timeout=timeout,
    )


def _plan(url: str, *, resolve=None) -> tuple[dict | None, str | None]:
    if not url or not str(url).strip():
        return None, "要填写 http 或 https 地址"
    parts = urlsplit(str(url).strip())
    if parts.scheme not in {"http", "https"}:
        return None, "只接受 http 或 https"
    if parts.username is not None or parts.password is not None:
        return None, "地址里不能带账号"
    host = parts.hostname
    if not host:
        return None, "缺少主机名"
    host = host.lower().rstrip(".")
    if _name_blocked(host):
        return None, "这个地址指向本机、内网或云元数据"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ips, addr_error = _public_ips(host, port, resolve)
    if addr_error:
        return None, addr_error
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return {"ip": ips[0], "port": port, "scheme": parts.scheme, "host": host, "path": path}, None


def _public_ips(host: str, port: int, resolve) -> tuple[list[str], str | None]:
    """One lookup. Any blocked address rejects the name. The first public address is the pin."""
    literal = _literal_ip(host)
    if literal is not None:
        if _is_blocked(literal):
            return [], "这个地址指向本机、内网或云元数据"
        return [str(literal)], None
    try:
        infos = _resolve(host, port, resolve)
    except OSError:
        return [], "地址解析失败"
    if not infos:
        return [], "地址解析失败"
    ips: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except (ValueError, IndexError):
            return [], "地址解析失败"
        if _is_blocked(ip):
            return [], "这个地址指向本机、内网或云元数据"
        ips.append(str(ip))
    if not ips:
        return [], "地址解析失败"
    return ips, None


def post_once(ip: str, port: int, scheme: str, host: str, path: str, body: bytes, timeout: float = 5.0) -> int:
    if scheme == "https":
        conn = _PinnedHTTPS(ip, port, host, timeout)
    else:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
    host_header = host if port in {80, 443} else f"{host}:{port}"
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Host": host_header,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "User-Agent": "rogmem-webhook",
            },
        )
        response = conn.getresponse()
        status = response.status
        response.read()
        return status
    finally:
        conn.close()


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, ip: str, port: int, server_hostname: str, timeout: float):
        super().__init__(ip, port, timeout=timeout, context=ssl.create_default_context())
        self._sni = server_hostname

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._sni)


def _resolve(host: str, port: int, resolve):
    resolver = resolve or socket.getaddrinfo
    return resolver(host, port, type=socket.SOCK_STREAM)


def _name_blocked(host: str) -> bool:
    if host in _BLOCKED_NAMES:
        return True
    return host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal")


def _literal_ip(host: str):
    text = host.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    if ":" in text:
        return None
    return _loose_ipv4(text)


def _loose_ipv4(host: str):
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers = []
    for part in parts:
        value = _part_value(part)
        if value is None:
            return None
        numbers.append(value)
    if len(numbers) == 4:
        if any(number > 255 for number in numbers):
            return None
        packed = (numbers[0] << 24) | (numbers[1] << 16) | (numbers[2] << 8) | numbers[3]
    elif len(numbers) == 3:
        if numbers[0] > 255 or numbers[1] > 255 or numbers[2] > 0xFFFF:
            return None
        packed = (numbers[0] << 24) | (numbers[1] << 16) | numbers[2]
    elif len(numbers) == 2:
        if numbers[0] > 255 or numbers[1] > 0xFFFFFF:
            return None
        packed = (numbers[0] << 24) | numbers[1]
    else:
        packed = numbers[0]
        if packed > 0xFFFFFFFF:
            return None
    return ipaddress.IPv4Address(packed)


def _part_value(part: str) -> int | None:
    if not part:
        return None
    lowered = part.lower()
    if lowered.startswith("0x"):
        try:
            return int(lowered, 16)
        except ValueError:
            return None
    if len(part) > 1 and part[0] == "0" and all(ch in "01234567" for ch in part):
        return int(part, 8)
    if part.isdigit():
        return int(part, 10)
    return None


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and (ip == _ALIBABA_METADATA or ip in _CGNAT):
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    )
