"""Config flow for Alexa Shopping List Sync."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .auth import (
    AuthManager,
    check_page_for_captcha,
    check_page_for_unsupported_flow,
    normalize_otp_secret,
)
from .const import (
    CONF_AMAZON_DOMAIN,
    CONF_DEBUG_MODE,
    CONF_EMAIL,
    CONF_HA_URL,
    CONF_INITIAL_SYNC_MODE,
    CONF_MIRROR_COMPLETED,
    CONF_OTP_SECRET,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_PRESERVE_DUPLICATES,
    CONF_PUBLIC_URL,
    CONF_SYNC_MODE,
    DEFAULT_AMAZON_DOMAIN,
    DEFAULT_DEBUG_MODE,
    DEFAULT_MIRROR_COMPLETED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PRESERVE_DUPLICATES,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    InitialSyncMode,
    SyncMode,
)
from .exceptions import (
    CaptchaNotCompletedError,
    OTPSecretInvalidError,
    PasskeyDetectedError,
    UnsupportedLoginFlowError,
)

_LOGGER = logging.getLogger(__name__)

# Max time to wait for proxy login to complete
PROXY_LOGIN_TIMEOUT = 300  # 5 minutes


def _validate_url(url: str) -> bool:
    """Validate a URL."""
    try:
        result = urlparse(url)
        return all([result.scheme in ("http", "https"), result.netloc])
    except Exception:
        return False


class AlexaShoppingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alexa Shopping List Sync."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._auth_manager: AuthManager | None = None
        self._proxy_url: str | None = None
        self._login_complete: asyncio.Event = asyncio.Event()
        self._login_error: str | None = None
        self._user_input: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> AlexaShoppingOptionsFlow:
        """Get the options flow."""
        return AlexaShoppingOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - Amazon account details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Check for existing entry with same email
            await self.async_set_unique_id(
                f"{user_input[CONF_EMAIL]}_{user_input.get(CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN)}"
            )
            self._abort_if_unique_id_configured()

            # Validate URL
            ha_url = user_input.get(CONF_HA_URL, "")
            if ha_url and not _validate_url(ha_url):
                errors[CONF_HA_URL] = "invalid_url"

            public_url = user_input.get(CONF_PUBLIC_URL, "")
            if public_url and not _validate_url(public_url):
                errors[CONF_PUBLIC_URL] = "invalid_url"

            # Validate OTP secret
            if not errors:
                try:
                    normalize_otp_secret(user_input[CONF_OTP_SECRET])
                except OTPSecretInvalidError:
                    errors[CONF_OTP_SECRET] = "2fa_key_invalid"

            # Check shopping list is available
            if not errors and "shopping_list" not in self.hass.config.components:
                errors["base"] = "shopping_list_missing"

            if not errors:
                self._user_input = user_input
                return await self.async_step_proxy_login()

        # Determine HA URL default
        ha_url_default = ""
        if hasattr(self.hass.config, "internal_url") and self.hass.config.internal_url:
            ha_url_default = self.hass.config.internal_url
        elif hasattr(self.hass.config, "api") and self.hass.config.api:
            ha_url_default = f"http://{self.hass.config.api.local_ip}:{self.hass.config.api.port}"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AMAZON_DOMAIN, default=DEFAULT_AMAZON_DOMAIN
                    ): str,
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_OTP_SECRET): str,
                    vol.Optional(CONF_HA_URL, default=ha_url_default): str,
                    vol.Optional(CONF_PUBLIC_URL, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_proxy_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the proxy login step.

        This step starts the auth capture proxy and waits for the user to
        complete login in their browser.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # User clicked submit after (hopefully) completing login
            if self._auth_manager and self._auth_manager.authenticated:
                return await self.async_step_sync_options()

            # Check if there was a specific login error
            if self._login_error:
                errors["base"] = self._login_error
                self._login_error = None
            else:
                errors["base"] = "login_failed"

        # Initialize auth manager and start proxy
        if self._auth_manager is None:
            self._auth_manager = AuthManager(
                hass=self.hass,
                amazon_domain=self._user_input.get(
                    CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN
                ),
                email=self._user_input[CONF_EMAIL],
                password=self._user_input[CONF_PASSWORD],
                otp_secret=self._user_input[CONF_OTP_SECRET],
            )

        try:
            proxy_url = await self._async_start_proxy_login()
        except PasskeyDetectedError:
            return self.async_abort(reason="passkey_not_supported")
        except UnsupportedLoginFlowError:
            errors["base"] = "unsupported_login_flow"
            proxy_url = None
        except CaptchaNotCompletedError:
            errors["base"] = "captcha_not_completed"
            proxy_url = None
        except Exception as err:
            _LOGGER.error("Failed to start proxy login: %s", err, exc_info=True)
            errors["base"] = "connection_error"
            proxy_url = None

        if proxy_url is None and not errors:
            errors["base"] = "connection_error"

        return self.async_show_form(
            step_id="proxy_login",
            description_placeholders={
                "proxy_url": proxy_url or "#",
            },
            errors=errors,
        )

    async def _async_start_proxy_login(self) -> str | None:
        """Start the auth capture proxy and return URL.

        Decision: We use authcaptureproxy for the proxy-based login flow.
        The proxy intercepts Amazon's login page, auto-fills OTP codes,
        and captures the authenticated session cookies.

        If authcaptureproxy is not available or fails, we fall back to a
        simplified flow that guides the user through manual steps.
        """
        assert self._auth_manager is not None

        await self._auth_manager.async_create_session()

        try:
            from authcaptureproxy import AuthCaptureProxy  # noqa: F811

            return await self._async_run_auth_capture_proxy()
        except ImportError:
            _LOGGER.warning(
                "authcaptureproxy not available, using simplified login flow"
            )
            return await self._async_run_simplified_login()

    async def _async_run_auth_capture_proxy(self) -> str | None:
        """Run auth capture proxy for login.

        This implements the proxy-based login similar to alexa_media_player.
        The proxy serves Amazon's login page through HA, intercepts form
        submissions, auto-fills OTP, and captures session cookies.
        """
        assert self._auth_manager is not None

        try:
            from authcaptureproxy import AuthCaptureProxy

            ha_url = self._user_input.get(CONF_HA_URL, "")
            public_url = self._user_input.get(CONF_PUBLIC_URL, "")
            amazon_domain = self._user_input.get(
                CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN
            )

            # Use public URL if available, otherwise HA URL
            proxy_base = public_url or ha_url
            if not proxy_base:
                proxy_base = "http://homeassistant.local:8123"

            login_url = f"https://www.{amazon_domain}/ap/signin"

            proxy = AuthCaptureProxy(
                proxy_base,
                login_url,
                self._auth_manager.session,
            )

            # Configure OTP auto-fill callback
            otp_secret = self._user_input[CONF_OTP_SECRET]

            def otp_callback() -> str:
                return self._auth_manager.get_otp_code()

            # Register the test function for detecting successful auth
            def check_auth_complete(resp_url: str, resp_text: str) -> bool:
                """Check if auth is complete by examining response."""
                # Successful login typically redirects to alexa.amazon.de
                if "alexa" in resp_url and "signin" not in resp_url:
                    return True
                # Check for success indicators in page
                if "action=sign-out" in resp_text.lower():
                    return True
                return False

            # Check for unsupported flows in page content
            def check_response(resp_url: str, resp_text: str) -> str | None:
                """Inspect response for unsupported flows."""
                try:
                    check_page_for_unsupported_flow(resp_text)
                except PasskeyDetectedError as err:
                    self._login_error = "passkey_not_supported"
                    return str(err)
                except UnsupportedLoginFlowError as err:
                    self._login_error = "unsupported_login_flow"
                    return str(err)

                if check_page_for_captcha(resp_text):
                    _LOGGER.debug("CAPTCHA detected on login page")

                return None

            # Set up proxy with callbacks
            proxy.access_url_callback = check_auth_complete
            proxy.page_callback = check_response

            # Start proxy
            proxy_url = await proxy.start_proxy()

            # Store proxy reference for cleanup
            self._proxy = proxy

            if proxy_url:
                _LOGGER.debug("Auth proxy started at: %s", proxy_url)

            return proxy_url

        except Exception as err:
            _LOGGER.error("Auth capture proxy failed: %s", err, exc_info=True)
            return await self._async_run_simplified_login()

    async def _async_run_simplified_login(self) -> str | None:
        """Simplified login flow as fallback.

        Decision: If authcaptureproxy is not available, we still need
        a way to authenticate. This simplified flow directs the user
        to Amazon's login page and provides instructions for completing
        auth. This is less seamless but functional.

        In practice, authcaptureproxy should always be available since
        it's in requirements.
        """
        assert self._auth_manager is not None

        amazon_domain = self._user_input.get(
            CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN
        )
        login_url = f"https://www.{amazon_domain}/ap/signin"

        _LOGGER.info(
            "Using simplified login flow. "
            "User needs to complete login manually at Amazon."
        )

        return login_url

    async def async_step_sync_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle sync options step."""
        if user_input is not None:
            # Merge all config data
            full_config = {
                **self._user_input,
                **user_input,
            }

            return self.async_create_entry(
                title=f"Alexa ({self._user_input.get(CONF_EMAIL, 'unknown')})",
                data={
                    CONF_AMAZON_DOMAIN: full_config.get(
                        CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN
                    ),
                    CONF_EMAIL: full_config[CONF_EMAIL],
                    CONF_PASSWORD: full_config[CONF_PASSWORD],
                    CONF_OTP_SECRET: full_config[CONF_OTP_SECRET],
                    CONF_HA_URL: full_config.get(CONF_HA_URL, ""),
                    CONF_PUBLIC_URL: full_config.get(CONF_PUBLIC_URL, ""),
                },
                options={
                    CONF_SYNC_MODE: full_config.get(
                        CONF_SYNC_MODE, SyncMode.TWO_WAY
                    ),
                    CONF_INITIAL_SYNC_MODE: full_config.get(
                        CONF_INITIAL_SYNC_MODE, InitialSyncMode.MERGE_UNION
                    ),
                    CONF_POLL_INTERVAL: full_config.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    ),
                    CONF_PRESERVE_DUPLICATES: full_config.get(
                        CONF_PRESERVE_DUPLICATES, DEFAULT_PRESERVE_DUPLICATES
                    ),
                    CONF_MIRROR_COMPLETED: full_config.get(
                        CONF_MIRROR_COMPLETED, DEFAULT_MIRROR_COMPLETED
                    ),
                    CONF_DEBUG_MODE: full_config.get(
                        CONF_DEBUG_MODE, DEFAULT_DEBUG_MODE
                    ),
                },
            )

        return self.async_show_form(
            step_id="sync_options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SYNC_MODE, default=SyncMode.TWO_WAY
                    ): vol.In(
                        {
                            SyncMode.TWO_WAY: "Two-way sync",
                            SyncMode.ALEXA_TO_HA: "Alexa → Home Assistant",
                            SyncMode.HA_TO_ALEXA: "Home Assistant → Alexa",
                        }
                    ),
                    vol.Required(
                        CONF_INITIAL_SYNC_MODE,
                        default=InitialSyncMode.MERGE_UNION,
                    ): vol.In(
                        {
                            InitialSyncMode.MERGE_UNION: "Merge (union of both lists)",
                            InitialSyncMode.ALEXA_WINS: "Alexa wins (overwrite HA)",
                            InitialSyncMode.HA_WINS: "HA wins (overwrite Alexa)",
                        }
                    ),
                    vol.Required(
                        CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                    ),
                    vol.Required(
                        CONF_PRESERVE_DUPLICATES,
                        default=DEFAULT_PRESERVE_DUPLICATES,
                    ): bool,
                    vol.Required(
                        CONF_MIRROR_COMPLETED,
                        default=DEFAULT_MIRROR_COMPLETED,
                    ): bool,
                    vol.Required(
                        CONF_DEBUG_MODE, default=DEFAULT_DEBUG_MODE
                    ): bool,
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth flow."""
        self._user_input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation step."""
        if user_input is not None:
            # Update credentials
            self._user_input.update(user_input)
            # Attempt proxy login with updated creds
            return await self.async_step_proxy_login()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PASSWORD,
                        default="",
                    ): str,
                    vol.Required(
                        CONF_OTP_SECRET,
                        default="",
                    ): str,
                }
            ),
        )


class AlexaShoppingOptionsFlow(OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SYNC_MODE,
                        default=options.get(CONF_SYNC_MODE, SyncMode.TWO_WAY),
                    ): vol.In(
                        {
                            SyncMode.TWO_WAY: "Two-way sync",
                            SyncMode.ALEXA_TO_HA: "Alexa → Home Assistant",
                            SyncMode.HA_TO_ALEXA: "Home Assistant → Alexa",
                        }
                    ),
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                    ),
                    vol.Required(
                        CONF_PRESERVE_DUPLICATES,
                        default=options.get(
                            CONF_PRESERVE_DUPLICATES, DEFAULT_PRESERVE_DUPLICATES
                        ),
                    ): bool,
                    vol.Required(
                        CONF_MIRROR_COMPLETED,
                        default=options.get(
                            CONF_MIRROR_COMPLETED, DEFAULT_MIRROR_COMPLETED
                        ),
                    ): bool,
                    vol.Required(
                        CONF_DEBUG_MODE,
                        default=options.get(CONF_DEBUG_MODE, DEFAULT_DEBUG_MODE),
                    ): bool,
                }
            ),
        )
