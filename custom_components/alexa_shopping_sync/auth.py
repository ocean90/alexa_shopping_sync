"""Authentication manager for Amazon Alexa."""

from __future__ import annotations

import base64
import logging
import re
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlparse

import pyotp
from aiohttp import ClientSession, CookieJar
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    AMAZON_BASE_URL_TEMPLATE,
    AMAZON_USER_AGENT,
    CAPTCHA_INDICATORS,
    HTTP_TIMEOUT,
    PASSKEY_INDICATORS,
    UNSUPPORTED_FLOW_INDICATORS,
)
from .exceptions import (
    AuthenticationError,
    CaptchaNotCompletedError,
    OTPSecretInvalidError,
    PasskeyDetectedError,
    SessionExpiredError,
    UnsupportedLoginFlowError,
)

_LOGGER = logging.getLogger(__name__)

# Secrets that must never be logged
_SECRET_KEYS = {"password", "otp_secret", "cookie", "token", "csrf", "session"}


def sanitize_log_data(data: dict[str, Any]) -> dict[str, Any]:
    """Remove secrets from data before logging."""
    sanitized = {}
    for key, value in data.items():
        if any(s in key.lower() for s in _SECRET_KEYS):
            sanitized[key] = "***REDACTED***"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_log_data(value)
        else:
            sanitized[key] = value
    return sanitized


def normalize_otp_secret(secret: str) -> str:
    """Normalize and validate an OTP secret.

    Accepts base32-encoded secrets (typically 52 chars for 256-bit keys).
    Strips spaces, dashes, converts to uppercase.
    """
    cleaned = re.sub(r"[\s\-]", "", secret.strip().upper())

    # Validate base32 — authenticator app secrets are unpadded, so add padding
    try:
        padding = (8 - len(cleaned) % 8) % 8
        base64.b32decode(cleaned + "=" * padding)
    except Exception as err:
        raise OTPSecretInvalidError(
            f"Invalid OTP secret: not valid base32 encoding"
        ) from err

    # Validate reasonable length (16-64 chars covers common TOTP secrets)
    if len(cleaned) < 16 or len(cleaned) > 64:
        raise OTPSecretInvalidError(
            f"OTP secret length {len(cleaned)} is outside expected range (16-64)"
        )

    return cleaned


def generate_otp(secret: str) -> str:
    """Generate a TOTP code from the secret."""
    totp = pyotp.TOTP(secret)
    return totp.now()


def check_page_for_unsupported_flow(html: str) -> None:
    """Analyze page HTML for unsupported login flows.

    Raises specific exceptions if passkey, unsupported flow, or
    unresolvable CAPTCHA states are detected.
    """
    html_lower = html.lower()

    # Check for passkey indicators
    for indicator in PASSKEY_INDICATORS:
        if indicator in html_lower:
            raise PasskeyDetectedError(
                f"Passkey login flow detected (indicator: '{indicator}'). "
                "Only Authenticator App 2SV is supported."
            )

    # Check for unsupported flow indicators
    for indicator in UNSUPPORTED_FLOW_INDICATORS:
        if indicator in html_lower:
            raise UnsupportedLoginFlowError(
                f"Unsupported Amazon login flow detected (indicator: '{indicator}')."
            )


def check_page_for_captcha(html: str) -> bool:
    """Check if page contains a CAPTCHA challenge."""
    html_lower = html.lower()
    return any(indicator in html_lower for indicator in CAPTCHA_INDICATORS)


class AuthManager:
    """Manages Amazon authentication session."""

    def __init__(
        self,
        hass: HomeAssistant,
        amazon_domain: str,
        email: str,
        password: str,
        otp_secret: str,
    ) -> None:
        """Initialize auth manager."""
        self._hass = hass
        self._amazon_domain = amazon_domain
        self._email = email
        self._password = password
        self._otp_secret = normalize_otp_secret(otp_secret)
        self._session: ClientSession | None = None
        self._cookies: dict[str, str] = {}
        self._csrf_token: str | None = None
        self._authenticated = False
        self._base_url = AMAZON_BASE_URL_TEMPLATE.format(domain=amazon_domain)
        self._login_attempt_count = 0
        self._max_login_attempts = 5

    @property
    def authenticated(self) -> bool:
        """Return whether we have an active session."""
        return self._authenticated

    @property
    def session(self) -> ClientSession | None:
        """Return the active aiohttp session."""
        return self._session

    @property
    def base_url(self) -> str:
        """Return the Amazon base URL."""
        return self._base_url

    @property
    def amazon_domain(self) -> str:
        """Return the Amazon domain."""
        return self._amazon_domain

    def get_otp_code(self) -> str:
        """Generate current TOTP code."""
        return generate_otp(self._otp_secret)

    async def async_create_session(self) -> ClientSession:
        """Create a new aiohttp session with cookie jar."""
        if self._session and not self._session.closed:
            await self._session.close()

        jar = CookieJar(unsafe=True)
        self._session = async_create_clientsession(
            self._hass,
            cookie_jar=jar,
        )
        return self._session

    async def async_close(self) -> None:
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._authenticated = False

    def mark_authenticated(self, cookies: dict[str, str] | None = None) -> None:
        """Mark the session as authenticated after proxy login completes."""
        self._authenticated = True
        if cookies:
            self._cookies.update(cookies)
            # Inject cookies into the aiohttp session cookie jar
            if self._session is not None:
                from yarl import URL

                self._session.cookie_jar.update_cookies(
                    cookies, URL(self._base_url)
                )
        _LOGGER.debug(
            "Session marked as authenticated (cookies=%d)", len(cookies or {})
        )

    def mark_session_expired(self) -> None:
        """Mark the session as expired."""
        self._authenticated = False
        _LOGGER.warning("Amazon session marked as expired")

    async def async_validate_session(self) -> bool:
        """Check if the current session is still valid.

        Makes a lightweight request to Amazon to verify cookies are still accepted.
        Uses the shopping list API (alexa.amazon.de is retired).
        """
        if not self._session or not self._authenticated:
            return False

        try:
            url = f"https://www.{self._amazon_domain}/alexashoppinglists/api/getlistitems"
            async with self._session.get(
                url,
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
                headers={"User-Agent": AMAZON_USER_AGENT},
            ) as resp:
                if resp.status == 200:
                    return True
                if resp.status in (301, 302):
                    location = resp.headers.get("Location", "")
                    if "signin" in location.lower() or "ap/signin" in location.lower():
                        self.mark_session_expired()
                        return False
                if resp.status in (401, 403):
                    self.mark_session_expired()
                    return False
        except Exception:
            _LOGGER.debug("Session validation failed", exc_info=True)
            return False

        return False

    def extract_cookies_dict(self) -> dict[str, str]:
        """Extract cookies from session as dict (no secrets logged)."""
        if not self._session:
            return {}
        cookies = {}
        for cookie in self._session.cookie_jar:
            cookies[cookie.key] = cookie.value
        return cookies

    async def async_get_authenticated_session(self) -> ClientSession:
        """Return the authenticated session or raise."""
        if not self._session or not self._authenticated:
            raise SessionExpiredError("No authenticated session available")
        return self._session
