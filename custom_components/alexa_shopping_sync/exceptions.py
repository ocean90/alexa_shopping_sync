"""Exceptions for Alexa Shopping List Sync."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class AlexaShoppingSyncError(HomeAssistantError):
    """Base exception."""


class AuthenticationError(AlexaShoppingSyncError):
    """Authentication failed."""


class LoginFlowError(AlexaShoppingSyncError):
    """Login flow entered unsupported state."""


class PasskeyDetectedError(LoginFlowError):
    """Passkey flow detected - not supported."""


class UnsupportedLoginFlowError(LoginFlowError):
    """Unsupported login flow detected."""


class CaptchaNotCompletedError(LoginFlowError):
    """CAPTCHA state could not be resolved."""


class OTPSecretInvalidError(AlexaShoppingSyncError):
    """OTP secret is invalid."""


class ShoppingListMissingError(AlexaShoppingSyncError):
    """HA Shopping List integration not found."""


class AmazonListNotFoundError(AlexaShoppingSyncError):
    """Amazon shopping list ID could not be discovered."""


class ThrottledError(AlexaShoppingSyncError):
    """Amazon returned 429."""


class SessionExpiredError(AuthenticationError):
    """Session expired, reauth required."""


class ConnectionError(AlexaShoppingSyncError):
    """Connection to Amazon failed."""
