"""SSRF guard tests (A1).

Verifies:
1. validate_outbound_url rejects private IPs, non-https, unapproved hosts.
2. validate_outbound_url passes approved HTTPS hosts.
3. The Tavily search URL used by router.py is accepted by the guard.
4. User-supplied question text does NOT influence the outbound URL.
"""
from __future__ import annotations

import pytest

from src.security.ssrf import SSRFBlockedError, validate_outbound_url


class TestSSRFGuard:
    def test_approved_https_url_passes(self) -> None:
        validate_outbound_url("https://api.tavily.com/search")

    def test_approved_openai_url_passes(self) -> None:
        validate_outbound_url("https://api.openai.com/v1/completions")

    def test_http_scheme_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError, match="scheme"):
            validate_outbound_url("http://api.tavily.com/search")

    def test_file_scheme_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError, match="scheme"):
            validate_outbound_url("file:///etc/passwd")

    def test_unapproved_host_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError, match="allowlist"):
            validate_outbound_url("https://evil.internal/steal-data")

    def test_loopback_ip_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError):
            validate_outbound_url(
                "https://127.0.0.1/internal",
                approved_hosts=frozenset({"127.0.0.1"}),  # even if explicitly listed
            )

    def test_private_rfc1918_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError):
            validate_outbound_url(
                "https://192.168.1.1/admin",
                approved_hosts=frozenset({"192.168.1.1"}),
            )

    def test_link_local_blocked(self) -> None:
        with pytest.raises(SSRFBlockedError):
            validate_outbound_url(
                "https://169.254.169.254/latest/meta-data/",
                approved_hosts=frozenset({"169.254.169.254"}),
            )

    def test_custom_approved_host(self) -> None:
        validate_outbound_url(
            "https://my-trusted-host.example/api",
            approved_hosts=frozenset({"my-trusted-host.example"}),
        )


class TestTavilyURLNotUserControlled:
    """Verify that the URL sent to Tavily is fixed, not derived from user input."""

    def test_tavily_url_is_hardcoded(self) -> None:
        """The router module must use a literal string for the Tavily endpoint."""
        import inspect
        from src.rag import router

        source = inspect.getsource(router.tavily_search)
        assert "https://api.tavily.com/search" in source, (
            "tavily_search must use the literal URL 'https://api.tavily.com/search'"
        )

    def test_user_question_does_not_appear_in_url(self) -> None:
        """The question goes into the POST body, not the URL."""
        import inspect
        from src.rag import router

        source = inspect.getsource(router.tavily_search)
        # The Request() constructor is called with a fixed string, not f-string on question.
        # We verify that the URL string position does not contain {question} interpolation.
        lines_with_request_init = [
            line for line in source.splitlines()
            if "Request(" in line or '"https://api.tavily.com' in line
        ]
        for line in lines_with_request_init:
            assert "question" not in line, (
                f"User question must not appear in Tavily URL construction: {line!r}"
            )
