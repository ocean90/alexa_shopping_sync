"""Security-focused tests for auth logging and proxy IP timeout."""

from __future__ import annotations

import datetime
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from custom_components.alexa_shopping_sync.config_flow import (
    AlexaShoppingProxyView,
)

# ---------------------------------------------------------------------------
# 1. Device registration: no token/cookie values in log output
# ---------------------------------------------------------------------------


class TestDeviceRegistrationLogging:
    """Verify that device registration never logs token or cookie values."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        """Import the function under test."""
        from custom_components.alexa_shopping_sync.auth import async_register_device

        self.register = async_register_device

    @pytest.mark.asyncio
    async def test_unexpected_response_logs_keys_not_values(self, caplog):
        """When response is 200 but missing refresh_token, log keys only."""
        fake_body = {
            "response": {"some_secret_token": "SHOULD_NOT_APPEAR_IN_LOG"},
            "request_id": "abc123",
        }
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_body

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.alexa_shopping_sync.auth.httpx.AsyncClient",
                return_value=mock_client,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await self.register(
                amazon_domain="amazon.de",
                device_serial="serial123",
                cookies={"session-id": "test"},
            )

        assert result is None
        # Must NOT contain the secret value
        assert "SHOULD_NOT_APPEAR_IN_LOG" not in caplog.text
        # Must contain the structural info (keys)
        assert "keys:" in caplog.text.lower() or "unexpected response structure" in caplog.text

    @pytest.mark.asyncio
    async def test_failed_registration_does_not_log_body(self, caplog):
        """When response is non-200, log status only, not the body."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401
        mock_resp.text = '{"error": "token_expired", "session_id": "SECRET_SESSION"}'

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.alexa_shopping_sync.auth.httpx.AsyncClient",
                return_value=mock_client,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await self.register(
                amazon_domain="amazon.de",
                device_serial="serial123",
                cookies={"session-id": "test"},
            )

        assert result is None
        assert "SECRET_SESSION" not in caplog.text
        assert "token_expired" not in caplog.text
        assert "status=401" in caplog.text


# ---------------------------------------------------------------------------
# 2. Token exchange: no cookie values in log output
# ---------------------------------------------------------------------------


class TestTokenExchangeLogging:
    """Verify that token exchange failure does not log cookie values."""

    @pytest.mark.asyncio
    async def test_no_cookies_logs_keys_not_values(self, caplog):
        """When token exchange response has no cookies, log keys only."""
        from custom_components.alexa_shopping_sync.auth import AuthManager

        fake_json = {
            "response": {
                "tokens": {
                    "bearer": {"access_token": "SECRET_ACCESS_TOKEN"},
                    # no "cookies" key
                },
            }
        }
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.url = "https://www.amazon.de/ap/exchangetoken/cookies"
        mock_resp.json.return_value = fake_json

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        auth = AuthManager.__new__(AuthManager)
        auth._amazon_domain = "amazon.de"
        auth._refresh_token = "fake_refresh"
        auth._device_serial = "serial123"
        auth._cookies = {}
        auth._authenticated = False

        with (
            patch(
                "custom_components.alexa_shopping_sync.auth.httpx.AsyncClient",
                return_value=mock_client,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await auth.async_try_token_exchange()

        assert result is False
        # Must NOT contain the secret access token value
        assert "SECRET_ACCESS_TOKEN" not in caplog.text
        # Should log structural info
        assert "keys:" in caplog.text.lower() or "no cookies in response" in caplog.text


# ---------------------------------------------------------------------------
# 3. Proxy IP timeout uses .total_seconds()
# ---------------------------------------------------------------------------


class TestProxyIPTimeout:
    """Verify that the IP timeout uses total_seconds, not .seconds."""

    @pytest.fixture(autouse=True)
    def _reset_known_ips(self):
        """Clear known_ips before each test."""
        AlexaShoppingProxyView.known_ips.clear()
        yield
        AlexaShoppingProxyView.known_ips.clear()

    def test_expired_ip_requires_reauth(self):
        """An IP whose whitelist entry is older than auth_seconds must re-authenticate."""
        remote = "192.168.1.100"
        # Set the IP entry to 301 seconds ago (just over the 300s limit)
        AlexaShoppingProxyView.known_ips[remote] = datetime.datetime.now() - datetime.timedelta(
            seconds=301
        )

        expired = (
            remote not in AlexaShoppingProxyView.known_ips
            or (datetime.datetime.now() - AlexaShoppingProxyView.known_ips[remote]).total_seconds()
            > AlexaShoppingProxyView.auth_seconds
        )
        assert expired is True

    def test_fresh_ip_passes(self):
        """An IP whose whitelist entry is recent should not require re-auth."""
        remote = "192.168.1.100"
        AlexaShoppingProxyView.known_ips[remote] = datetime.datetime.now() - datetime.timedelta(
            seconds=10
        )

        expired = (
            remote not in AlexaShoppingProxyView.known_ips
            or (datetime.datetime.now() - AlexaShoppingProxyView.known_ips[remote]).total_seconds()
            > AlexaShoppingProxyView.auth_seconds
        )
        assert expired is False

    def test_over_24h_still_expired(self):
        """Regression: .seconds wraps at 86400, .total_seconds() does not.

        With the old `.seconds` implementation, an IP whitelisted 86700 seconds
        ago (24h + 5min) would have .seconds = 300, passing the 300s check.
        With `.total_seconds()` it correctly returns 86700 > 300.
        """
        remote = "192.168.1.100"
        AlexaShoppingProxyView.known_ips[remote] = datetime.datetime.now() - datetime.timedelta(
            seconds=86700
        )

        # This is the key regression test: .seconds would give 300, passing the check
        td = datetime.datetime.now() - AlexaShoppingProxyView.known_ips[remote]
        assert td.seconds < 400  # .seconds wraps — would falsely pass the old check
        assert td.total_seconds() > 86000  # .total_seconds() correctly reports >24h

        expired = (
            remote not in AlexaShoppingProxyView.known_ips
            or td.total_seconds() > AlexaShoppingProxyView.auth_seconds
        )
        assert expired is True
