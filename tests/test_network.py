from __future__ import annotations

import io
import unittest
from email.message import Message

from code_agent.network import NetworkPolicyError, UrlFetcher


def _public_resolver(host, port, *, type):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def _private_resolver(host, port, *, type):
    return [(2, 1, 6, "", ("127.0.0.1", port))]


class _FakeResponse:
    def __init__(self, body: bytes, url: str = "https://example.com") -> None:
        self._body = io.BytesIO(body)
        self._url = url
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "text/plain; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, size: int) -> bytes:
        return self._body.read(size)


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        return self.response


class UrlFetcherTests(unittest.TestCase):
    def test_fetches_bounded_public_https_text(self) -> None:
        opener = _FakeOpener(_FakeResponse(b"documentation"))
        fetcher = UrlFetcher(resolver=_public_resolver, opener=opener)

        result = fetcher.fetch("https://example.com/docs", max_bytes=100)

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], "documentation")
        self.assertEqual(opener.calls, 1)

    def test_rejects_http_credentials_and_private_addresses(self) -> None:
        fetcher = UrlFetcher(resolver=_public_resolver)
        with self.assertRaisesRegex(NetworkPolicyError, "HTTPS"):
            fetcher.validate_url("http://example.com")
        with self.assertRaisesRegex(NetworkPolicyError, "credentials"):
            fetcher.validate_url("https://user:pass@example.com")

        private_fetcher = UrlFetcher(resolver=_private_resolver)
        with self.assertRaisesRegex(NetworkPolicyError, "non-public"):
            private_fetcher.validate_url("https://localhost.example")

    def test_rejects_oversized_response(self) -> None:
        opener = _FakeOpener(_FakeResponse(b"x" * 11))
        fetcher = UrlFetcher(resolver=_public_resolver, opener=opener)

        with self.assertRaisesRegex(NetworkPolicyError, "exceeds"):
            fetcher.fetch("https://example.com", max_bytes=10)


if __name__ == "__main__":
    unittest.main()

