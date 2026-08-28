"""Approved HTTPS retrieval with SSRF-oriented URL checks."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


class NetworkPolicyError(ValueError):
    pass


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, validator: Callable[[str], None]) -> None:
        super().__init__()
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrlFetcher:
    def __init__(
        self,
        *,
        resolver: Callable[..., list] = socket.getaddrinfo,
        opener: Any | None = None,
    ) -> None:
        self._resolver = resolver
        self._opener = opener

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        max_bytes: int = 200_000,
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        self.validate_url(url)
        method = method.upper()
        if method not in {"GET", "HEAD"}:
            raise NetworkPolicyError("fetch_url only supports GET and HEAD")
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= 1_000_000:
            raise NetworkPolicyError("max_bytes must be between 1 and 1000000")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 30:
            raise NetworkPolicyError("timeout_seconds must be between 1 and 30")

        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": "text/plain,text/html,application/json,application/xml",
                "User-Agent": "LcTiCodeAgent/0.1",
            },
        )
        opener = self._opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _ValidatingRedirectHandler(self.validate_url),
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                final_url = response.geturl()
                self.validate_url(final_url)
                content_type = response.headers.get_content_type()
                if not self._is_text_content(content_type):
                    raise NetworkPolicyError(
                        f"response content type is not text: {content_type}"
                    )
                body = b"" if method == "HEAD" else response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise NetworkPolicyError("response exceeds max_bytes")
                charset = response.headers.get_content_charset() or "utf-8"
                text = body.decode(charset, errors="replace")
                return {
                    "url": final_url,
                    "status": response.status,
                    "content_type": content_type,
                    "bytes": len(body),
                    "body": text,
                }
        except urllib.error.URLError as error:
            raise NetworkPolicyError(f"HTTPS request failed: {error.reason}") from error

    def validate_url(self, url: str) -> None:
        if not isinstance(url, str) or not url.strip():
            raise NetworkPolicyError("url must be a non-empty string")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https":
            raise NetworkPolicyError("only HTTPS URLs are allowed")
        if not parsed.hostname:
            raise NetworkPolicyError("URL must include a hostname")
        if parsed.username or parsed.password:
            raise NetworkPolicyError("credentials in URLs are not allowed")
        try:
            addresses = self._resolver(
                parsed.hostname,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as error:
            raise NetworkPolicyError("hostname could not be resolved") from error
        if not addresses:
            raise NetworkPolicyError("hostname resolved to no addresses")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise NetworkPolicyError(
                    f"hostname resolves to a non-public address: {ip}"
                )

    @staticmethod
    def _is_text_content(content_type: str) -> bool:
        return (
            content_type.startswith("text/")
            or content_type
            in {
                "application/json",
                "application/xml",
                "application/xhtml+xml",
            }
        )

