"""Constants for Alexa Shopping List Sync."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "alexa_shopping_sync"
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.mappings"

# Config keys
CONF_AMAZON_DOMAIN: Final = "amazon_domain"
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_OTP_SECRET: Final = "otp_secret"
CONF_HA_URL: Final = "ha_url"
CONF_PUBLIC_URL: Final = "public_url"
CONF_SYNC_MODE: Final = "sync_mode"
CONF_INITIAL_SYNC_MODE: Final = "initial_sync_mode"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_PRESERVE_DUPLICATES: Final = "preserve_duplicates"
CONF_MIRROR_COMPLETED: Final = "mirror_completed"
CONF_DEBUG_MODE: Final = "debug_mode"
CONF_SHOPPING_LIST_ID_OVERRIDE: Final = "shopping_list_id_override"

# Defaults
DEFAULT_AMAZON_DOMAIN: Final = "amazon.de"
DEFAULT_POLL_INTERVAL: Final = 60
MIN_POLL_INTERVAL: Final = 30
MAX_POLL_INTERVAL: Final = 600
DEFAULT_PRESERVE_DUPLICATES: Final = True
DEFAULT_MIRROR_COMPLETED: Final = True
DEFAULT_DEBUG_MODE: Final = False

# Amazon endpoints (verified against live API 2026-03)
# The shopping list API lives on www.amazon.de, NOT alexa.amazon.de (which is retired).
AMAZON_BASE_URL_TEMPLATE: Final = "https://www.{domain}"
AMAZON_SHOPPING_API_BASE: Final = "https://www.{domain}/alexashoppinglists/api"
AMAZON_API_GET_LIST_ITEMS: Final = "/getlistitems"
AMAZON_API_ADD_LIST_ITEM: Final = "/addlistitem/{list_id}"
AMAZON_API_UPDATE_LIST_ITEM: Final = "/updatelistitem"
AMAZON_API_DELETE_LIST_ITEM: Final = "/deletelistitem"

# PitanguiBridge User-Agent (mimics Alexa mobile app - required for API access)
AMAZON_USER_AGENT: Final = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 13_5_1 like Mac OS X)"
    " AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    " PitanguiBridge/2.2.345247.0-[HARDWARE=iPhone10_4][SOFTWARE=13.5.1]"
)

# Timeouts
HTTP_TIMEOUT: Final = 30
HTTP_RETRY_COUNT: Final = 3
HTTP_BACKOFF_FACTOR: Final = 1.5

# Sync
PENDING_OP_GRACE_SECONDS: Final = 30
MAX_PENDING_OPS: Final = 100
ECHO_SUPPRESSION_WINDOW: Final = 10

# Proxy auth
PROXY_PORT_RANGE_START: Final = 8700
PROXY_PORT_RANGE_END: Final = 8799

# OAuth Device Registration (for silent session renewal)
# Using /auth/register to obtain a long-lived refresh_token avoids the
# metadata1 browser-fingerprint requirement that breaks headless logins.
AMAZON_REGISTER_DEVICE_URL_TEMPLATE: Final = "https://api.{domain}/auth/register"
AMAZON_EXCHANGE_TOKEN_URL_TEMPLATE: Final = "https://www.{domain}/ap/exchangetoken/cookies"
AMAZON_DEVICE_TYPE: Final = "A2IVLV5VM2W81"
AMAZON_APP_NAME: Final = "HA Alexa Shopping Sync"
AMAZON_APP_VERSION: Final = "2.2.345247.0"
AMAZON_DEVICE_MODEL: Final = "Echo"
AMAZON_OS_VERSION: Final = "10.11.1"
AMAZON_SOFTWARE_VERSION: Final = "130050020"

# Passkey / unsupported flow detection patterns
PASSKEY_INDICATORS: Final = (
    "passkey",
    "fido",
    "webauthn",
    "does not support passkeys",
    "use your passkey",
    "biometric",
)

UNSUPPORTED_FLOW_INDICATORS: Final = (
    "ap_register_device",
    "claimspicker",
    "fwcim-form",
)

CAPTCHA_INDICATORS: Final = (
    "captcha",
    "image-captcha",
    "auth-captcha",
    "opfcaptcha",
)


class SyncMode(StrEnum):
    """Sync mode options."""

    TWO_WAY = "two_way"
    ALEXA_TO_HA = "alexa_to_ha"
    HA_TO_ALEXA = "ha_to_alexa"


class InitialSyncMode(StrEnum):
    """Initial sync mode options."""

    MERGE_UNION = "merge_union"
    ALEXA_WINS = "alexa_wins"
    HA_WINS = "ha_wins"


class PendingOpType(StrEnum):
    """Pending operation types."""

    ADD = "add"
    UPDATE = "update"
    COMPLETE = "complete"
    DELETE = "delete"
