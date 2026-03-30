"""Authentication manager for Amazon Alexa."""

from __future__ import annotations

import base64
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlparse

import httpx
import pyotp
from bs4 import BeautifulSoup
from homeassistant.core import HomeAssistant

from .const import (
    AMAZON_APP_NAME,
    AMAZON_APP_VERSION,
    AMAZON_BASE_URL_TEMPLATE,
    AMAZON_DEVICE_MODEL,
    AMAZON_DEVICE_TYPE,
    AMAZON_EXCHANGE_TOKEN_URL_TEMPLATE,
    AMAZON_OS_VERSION,
    AMAZON_REGISTER_DEVICE_URL_TEMPLATE,
    AMAZON_SOFTWARE_VERSION,
    AMAZON_USER_AGENT,
    AMAZON_USER_AGENT_DEVICE,
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
        self._refresh_token: str | None = None
        self._device_serial: str | None = None

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

    def set_device_credentials(self, refresh_token: str, device_serial: str) -> None:
        """Store device credentials obtained during initial proxy login."""
        self._refresh_token = refresh_token
        self._device_serial = device_serial

    @property
    def has_refresh_token(self) -> bool:
        """Return whether we have a refresh token for silent session renewal."""
        return bool(self._refresh_token)

    async def async_try_token_exchange(self) -> bool:
        """Exchange the stored refresh token for fresh session cookies.

        Uses Amazon's /ap/exchangetoken/cookies endpoint with the exact payload
        format from alexapy.  The response is JSON (not HTTP Set-Cookie headers).
        Does NOT require the metadata1 browser fingerprint.

        Returns True on success (session marked authenticated), False otherwise.
        """
        if not self._refresh_token:
            return False

        url = AMAZON_EXCHANGE_TOKEN_URL_TEMPLATE.format(domain=self._amazon_domain)
        _LOGGER.warning("Attempting silent session renewal via token exchange")

        data = {
            "app_name": AMAZON_APP_NAME,
            "app_version": AMAZON_APP_VERSION,
            "di.sdk.version": "6.12.4",
            "domain": f".{self._amazon_domain}",
            "source_token": self._refresh_token,
            "package_name": "com.amazon.echo",
            "di.hw.version": AMAZON_DEVICE_MODEL,
            "platform": "iOS",
            "requested_token_type": "auth_cookies",
            "source_token_type": "refresh_token",
            "di.os.name": "iOS",
            "di.os.version": AMAZON_OS_VERSION,
            "current_version": "6.12.4",
            "previous_version": "6.12.4",
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": AMAZON_USER_AGENT_DEVICE,
                },
            ) as client:
                resp = await client.post(url, data=data)
                _LOGGER.debug(
                    "Token exchange: status=%d url=%s", resp.status_code, resp.url
                )

                if resp.status_code != 200:
                    _LOGGER.warning(
                        "Token exchange failed: status=%d body=%s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return False

                # Response is JSON, not HTTP Set-Cookie headers
                try:
                    response_json = resp.json()
                except Exception:
                    _LOGGER.warning(
                        "Token exchange: failed to parse JSON response: %s",
                        resp.text[:200],
                    )
                    return False

                cookies_by_domain = (
                    response_json.get("response", {})
                    .get("tokens", {})
                    .get("cookies", {})
                )
                if not cookies_by_domain:
                    _LOGGER.warning(
                        "Token exchange: no cookies in response: %s",
                        str(response_json)[:200],
                    )
                    return False

                new_cookies: dict[str, str] = {}
                for domain, cookie_list in cookies_by_domain.items():
                    for item in cookie_list:
                        name = item.get("Name", "")
                        value = item.get("Value", "")
                        if name and value:
                            # Amazon sometimes wraps values in quotes — strip them
                            if value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            new_cookies[name] = value
                    _LOGGER.debug(
                        "Token exchange: %d cookies for %s: %s",
                        len(cookie_list),
                        domain,
                        [c.get("Name") for c in cookie_list],
                    )

                if not new_cookies:
                    _LOGGER.warning(
                        "Token exchange: all cookies empty after parsing"
                    )
                    return False

                if not self._session or self._session.is_closed:
                    await self.async_create_session()

                self.mark_authenticated(new_cookies)

                if not await self.async_validate_session():
                    self._authenticated = False
                    _LOGGER.warning(
                        "Token exchange: cookies received but API validation failed"
                    )
                    return False

                _LOGGER.warning(
                    "Token exchange succeeded (%d cookies)", len(new_cookies)
                )
                return True
        except Exception as err:
            _LOGGER.warning("Token exchange failed: %s", err, exc_info=True)
            return False

    async def async_try_silent_relogin(self) -> bool:
        """Silently re-authenticate using stored credentials + TOTP.

        Called when the session expires. Uses a fresh httpx client to go
        through Amazon's sign-in form programmatically. If Amazon shows a
        CAPTCHA or any unexpected challenge the method returns False and the
        caller should fall back to the manual reauth flow.

        On success the new cookies are injected into the main session so
        subsequent API calls succeed immediately (next poll or mutation).
        """
        _LOGGER.warning("Attempting silent re-authentication with stored credentials")

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
                _LOGGER.debug(
                    "Silent relogin step 1: signin page status=%d url=%s",
                    resp.status_code,
                    resp.url,
                )
                if resp.status_code != 200:
                    _LOGGER.warning(
                        "Silent relogin failed: signin page returned %d", resp.status_code
                    )
                    return False

                # Step 2: Submit email + password
                resp = await self._async_submit_form(
                    tmp, resp, {"email": self._email, "password": self._password}
                )
                if resp is None:
                    _LOGGER.warning(
                        "Silent relogin failed: could not find login form on signin page"
                    )
                    return False
                _LOGGER.debug(
                    "Silent relogin step 2: after credentials status=%d url=%s",
                    resp.status_code,
                    resp.url,
                )

                # Step 3: Handle OTP challenge if present
                if "otpCode" in resp.text or "one-time" in resp.text.lower():
                    _LOGGER.debug("Silent relogin step 3: OTP challenge detected")
                    resp = await self._async_submit_form(
                        tmp, resp, {"otpCode": generate_otp(self._otp_secret)}
                    )
                    if resp is None:
                        _LOGGER.warning("Silent relogin failed: could not find OTP form")
                        return False
                    _LOGGER.debug(
                        "Silent relogin step 3: after OTP status=%d url=%s",
                        resp.status_code,
                        resp.url,
                    )
                else:
                    _LOGGER.debug("Silent relogin step 3: no OTP challenge detected")

                # Step 4: Detect failure states
                final_url = str(resp.url)
                if "/ap/signin" in final_url and "signin" in final_url:
                    _LOGGER.warning(
                        "Silent relogin failed: still on signin page after credentials "
                        "(wrong password, account locked, or unexpected challenge) url=%s",
                        final_url,
                    )
                    return False
                if check_page_for_captcha(resp.text):
                    _LOGGER.warning(
                        "Silent relogin failed: CAPTCHA detected at url=%s — "
                        "manual reauth required",
                        final_url,
                    )
                    return False

                # Step 5: Transfer new cookies into main session.
                # Use .items() (not dict()) to avoid CookieConflict from
                # duplicate cookie names across different domains.
                new_cookies = {k: v for k, v in tmp.cookies.items()}
                _LOGGER.debug(
                    "Silent relogin step 5: captured %d cookies, final url=%s",
                    len(new_cookies),
                    final_url,
                )
                if not new_cookies:
                    _LOGGER.warning("Silent relogin failed: no cookies captured after login")
                    return False

                # mark_authenticated clears old cookies before adding new ones
                self.mark_authenticated(new_cookies)

                # Step 6: Verify the new cookies actually work against the API.
                # The login page may redirect cleanly even with incomplete cookies
                # (e.g. missing metadata1 field), so we must confirm API access.
                _LOGGER.debug("Silent relogin step 6: validating new cookies against API")
                if not await self.async_validate_session():
                    self._authenticated = False
                    _LOGGER.warning(
                        "Silent relogin failed: login appeared successful but API "
                        "returned non-200 — cookies are likely incomplete (missing "
                        "metadata1 or similar browser-generated field)"
                    )
                    return False

                _LOGGER.warning(
                    "Silent re-authentication succeeded and API validated (%d cookies)",
                    len(new_cookies),
                )
                return True

        except Exception as err:
            _LOGGER.warning(
                "Silent re-authentication failed with exception: %s", err, exc_info=True
            )
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


async def async_register_device(
    amazon_domain: str,
    device_serial: str,
    cookies: dict[str, str],
    access_token: str | None = None,
) -> str | None:
    """Register this client as an Amazon device and return the refresh token.

    Mirrors alexapy's get_tokens() exactly:
    - frc: 313 random bytes, base64-encoded without padding (required)
    - website_cookies: empty list — session cookies sent as HTTP cookies
    - auth_data: empty when no access_token (Amazon authenticates via cookies)
    - Tries user domain first, falls back to amazon.com

    Returns the refresh_token string on success, None on failure (non-fatal).
    """
    # Required fingerprint value — must be real random bytes, not empty string
    frc = base64.b64encode(secrets.token_bytes(313)).decode("ascii").rstrip("=")

    auth_data: dict[str, Any] = {}
    if access_token:
        auth_data["access_token"] = access_token

    payload = {
        "requested_extensions": ["device_info", "customer_info"],
        "cookies": {
            "website_cookies": [],  # empty — auth via HTTP session cookies
            "domain": f".{amazon_domain}",
        },
        "registration_data": {
            "domain": "Device",
            "app_version": AMAZON_APP_VERSION,
            "device_type": AMAZON_DEVICE_TYPE,
            # Amazon replaces %FIRST_NAME% and %DUPE_STRATEGY_1ST% server-side
            "device_name": f"%FIRST_NAME%\u0027s%DUPE_STRATEGY_1ST%{AMAZON_APP_NAME}",
            "os_version": AMAZON_OS_VERSION,
            "device_serial": device_serial,
            "device_model": AMAZON_DEVICE_MODEL,
            "app_name": AMAZON_APP_NAME,
            "software_version": AMAZON_SOFTWARE_VERSION,
        },
        "auth_data": auth_data,
        "user_context_map": {"frc": frc},
        "requested_token_type": ["bearer", "mac_dms", "website_cookies"],
    }

    # Try user's domain first, then amazon.com (same fallback as alexapy)
    domains_to_try = [amazon_domain]
    if amazon_domain.lower() != "amazon.com":
        domains_to_try.append("amazon.com")

    for domain in domains_to_try:
        payload["cookies"]["domain"] = f".{domain}"
        _LOGGER.debug("Attempting device registration with api.%s", domain)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": AMAZON_USER_AGENT_DEVICE,
                    "x-amzn-identity-auth-domain": f"api.{domain}",
                },
                cookies=cookies,  # session cookies sent as HTTP cookies (not in JSON)
            ) as client:
                resp = await client.post(
                    f"https://api.{domain}/auth/register", json=payload
                )
                _LOGGER.debug(
                    "Device registration (%s): status=%d", domain, resp.status_code
                )

                if resp.status_code == 200:
                    data = resp.json()
                    refresh_token = (
                        data.get("response", {})
                        .get("success", {})
                        .get("tokens", {})
                        .get("bearer", {})
                        .get("refresh_token")
                    )
                    if refresh_token:
                        _LOGGER.debug(
                            "Device registration succeeded with %s", domain
                        )
                        return refresh_token
                    _LOGGER.warning(
                        "Device registration (%s): unexpected response: %s",
                        domain,
                        str(data)[:300],
                    )
                else:
                    _LOGGER.warning(
                        "Device registration failed: status=%d body=%s",
                        resp.status_code,
                        resp.text[:300],
                    )
        except Exception as err:
            _LOGGER.warning("Device registration (%s) exception: %s", domain, err)

    return None
