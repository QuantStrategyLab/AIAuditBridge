"""HTTPS transport for server-configured callbacks; never follows redirects."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes


class TransportError(RuntimeError):
    pass


def public_destination(url, resolver=socket.getaddrinfo):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise ValueError('a public HTTPS destination without credentials or fragment is required')
    port = parsed.port or 443
    addresses = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('destination has no addresses')
    ips = []
    for family, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        # Reject mixed answers too: do not fall back to a private address.
        if not ip.is_global or (ip.version == 6 and ip.ipv4_mapped is not None):
            raise ValueError('destination resolves to a non-public address')
        ips.append((family, str(ip)))
    return parsed, ips


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, port, address, timeout):
        super().__init__(hostname, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family, ip = self.address
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((ip, self.port))
            # Verification and SNI use the original host, DNS isn't repeated.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class PublicHTTPS:
    def __init__(self, timeout=15, max_response_bytes=262144):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def request(self, url, *, method='POST', headers=None, body=b'', timeout=None):
        try:
            parsed, addresses = public_destination(url)
            connection = _PinnedConnection(parsed.hostname, parsed.port or 443, addresses[0], self.timeout if timeout is None else timeout)
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            try:
                # Exactly one connection attempt: a lost reply might already have triggered work.
                connection.request(method, target, body=body, headers=headers or {})
                reply = connection.getresponse()
                payload = reply.read(self.max_response_bytes + 1)
                if len(payload) > self.max_response_bytes:
                    raise TransportError('callback response exceeds limit')
                return HttpReply(reply.status, payload)
            finally:
                connection.close()
        except Exception:
            # URL, response body and credentials may be sensitive.
            raise TransportError('HTTPS request failed') from None


def json_body(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
