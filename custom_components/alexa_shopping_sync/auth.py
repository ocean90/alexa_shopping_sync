"""Authentication manager for Amazon Alexa."""

from __future__ import annotations

import base64
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
import pyotp
from bs4 import BeautifulSoup
from homeassistant.core import HomeAssistant

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
        self._session: httpx.AsyncClient | None = None
        self._cookies: dict[str, str] = {}
        self._authenticated = False
        self._base_url = AMAZON_BASE_URL_TEMPLATE.format(domain=amazon_domain)
        self._login_attempt_count = 0
        self._max_login_attempts = 5

    @property
    def authenticated(self) -> bool:
        """Return whether we have an active session."""
        return self._authenticated

    @property
    def session(self) -> httpx.AsyncClient | None:
        """Return the active httpx session."""
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

    async def async_create_session(self) -> httpx.AsyncClient:
        """Create a new httpx session."""
        if self._session and not self._session.is_closed:
            await self._session.aclose()

        self._session = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
            follow_redirects=True,
            headers={"User-Agent": AMAZON_USER_AGENT},
        )
        return self._session

    async def async_close(self) -> None:
        """Close the session."""
        if self._session and not self._session.is_closed:
            await self._session.aclose()
        self._session = None
        self._authenticated = False

    def mark_authenticated(self, cookies: dict[str, str] | None = None) -> None:
        """Mark the session as authenticated after proxy login completes.

        Clears existing session cookies before applying new ones to prevent
        httpx.CookieConflict: Amazon sends Set-Cookie headers (with domain
        info) even on 401 responses, which creates duplicate entries alongside
        our domain-less restored cookies.
        """
        self._authenticated = True
        if cookies:
            self._cookies.update(cookies)
            if self._session is not None:
                self._session.cookies.clear()
                for k, v in cookies.items():
                    self._session.cookies.set(k, v)
        _LOGGER.debug(
            "Session marked as authenticated (cookies=%d)", len(cookies or {})
        )

    def mark_session_expired(self) -> None:
        """Mark the session as expired."""
        self._authenticated = False
        _LOGGER.warning("Amazon session marked as expired")

    async def async_validate_session(self) -> bool:
        """Check if the current session is still valid."""
        if not self._session or not self._authenticated:
            return False

        try:
            url = f"https://www.{self._amazon_domain}/alexashoppinglists/api/getlistitems"
            resp = await self._session.get(url, follow_redirects=False)
            if resp.status_code == 200:
                return True
            if resp.status_code in (301, 302):
                location = resp.headers.get("location", "")
                if "signin" in location.lower() or "ap/signin" in location.lower():
                    self.mark_session_expired()
                    return False
            if resp.status_code in (401, 403):
                self.mark_session_expired()
                return False
        except Exception:
            _LOGGER.debug("Session validation failed", exc_info=True)
            return False

        return False

    def extract_cookies_dict(self) -> dict[str, str]:
        """Extract cookies from session as dict (no secrets logged).

        Uses .items() rather than dict() to avoid httpx.CookieConflict when
        multiple cookies share the same name across different domains.
        Last value wins for each name, which is acceptable for persistence.
        """
        if not self._session:
            return {}
        return {k: v for k, v in self._session.cookies.items()}

    async def async_get_authenticated_session(self) -> httpx.AsyncClient:
        """Return the authenticated httpx session or raise."""
        if not self._session or not self._authenticated:
            raise SessionExpiredError("No authenticated session available")
        return self._session

    async def async_try_silent_relogin(self) -> bool:
        """Silently re-authenticate using stored credentials + TOTP.

        Called when the session expires. Uses a fresh httpx client to go
        through Amazon's sign-in form programmatically. If Amazon shows a
        CAPTCHA or any unexpected challenge the method returns False and the
        caller should fall back to the manual reauth flow.

        On success the new cookies are injected into the main session so
        subsequent API calls succeed immediately (next poll or mutation).
        """
        _LOGGER.info("Attempting silent re-authentication with stored credentials")

        signin_url = (
            f"https://www.{self._amazon_domain}/ap/signin"
            f"?openid.pape.max_auth_age=0"
            f"&openid.return_to=https%3A%2F%2Fwww.{self._amazon_domain}%2F"
            f"&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
            f"&openid.assoc_handle=deflex"
            f"&openid.mode=checkid_setup"
            f"&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
            f"&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
        )

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
                follow_redirects=True,
                headers={"User-Agent": AMAZON_USER_AGENT},
            ) as tmp:
                # Step 1: Load sign-in page
                resp = await tmp.get(signin_url)
                if resp.status_code != 200:
                    _LOGGER.debug("Silent relogin: signin page returned %d", resp.status_code)
                    return False

                # Step 2: Submit email + password
                resp = await self._async_submit_form(
                    tmp, resp, {"email": self._email, "password": self._password}
                )
                if resp is None:
                    _LOGGER.debug("Silent relogin: could not find login form")
                    return False

                # Step 3: Handle OTP challenge if present
                if "otpCode" in resp.text or "one-time" in resp.text.lower():
                    resp = await self._async_submit_form(
                        tmp, resp, {"otpCode": generate_otp(self._otp_secret)}
                    )
                    if resp is None:
                        _LOGGER.debug("Silent relogin: could not find OTP form")
                        return False

                # Step 4: Detect failure states
                final_url = str(resp.url)
                if "/ap/signin" in final_url and "signin" in final_url:
                    _LOGGER.debug("Silent relogin: still on signin page — login failed")
                    return False
                if check_page_for_captcha(resp.text):
                    _LOGGER.debug("Silent relogin: CAPTCHA detected, falling back to manual reauth")
                    return False

                # Step 5: Transfer new cookies into main session.
                # Use .items() (not dict()) to avoid CookieConflict from
                # duplicate cookie names across different domains.
                new_cookies = {k: v for k, v in tmp.cookies.items()}
                if not new_cookies:
                    _LOGGER.debug("Silent relogin: no cookies captured")
                    return False

                # mark_authenticated clears old cookies before adding new ones
                self.mark_authenticated(new_cookies)
                _LOGGER.info(
                    "Silent re-authentication succeeded (%d cookies)", len(new_cookies)
                )
                return True

        except Exception as err:
            _LOGGER.warning("Silent re-authentication failed: %s", err)
            return False

    async def _async_submit_form(
        self,
        client: httpx.AsyncClient,
        resp: httpx.Response,
        overrides: dict[str, str],
    ) -> httpx.Response | None:
        """Extract form fields from *resp* and POST them with *overrides* applied."""
        soup = BeautifulSoup(resp.text, "html.parser")

        form = (
            soup.find("form", {"id": "ap-signin-form"})
            or soup.find("form", {"id": "auth-login-form"})
            or soup.find("form")
        )
        if not form:
            return None

        action: str = form.get("action", "") or ""
        if action.startswith("/"):
            action = f"https://www.{self._amazon_domain}{action}"
        if not action:
            action = f"https://www.{self._amazon_domain}/ap/signin"

        fields: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                fields[name] = inp.get("value", "")

        fields.update(overrides)
        return await client.post(action, data=fields)
