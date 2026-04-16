"""Config flow for Alexa Shopping List Sync.

Login flow based on authcaptureproxy (same approach as alexa_media_player).
Uses HA's external step mechanism: user is redirected to the proxy URL,
completes Amazon login there, proxy detects success and calls back to HA.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import logging
import os
from functools import partial
from typing import Any, Optional, Union
from urllib.parse import urlparse

import httpx
import voluptuous as vol
from aiohttp import web, web_response
from aiohttp.web_exceptions import HTTPBadRequest
from authcaptureproxy import AuthCaptureProxy
from bs4 import BeautifulSoup
from homeassistant.components.http.view import HomeAssistantView
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import Unauthorized
from yarl import URL

from .auth import (
    async_register_device,
    check_page_for_captcha,
    check_page_for_unsupported_flow,
    generate_otp,
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
    CONF_TARGET_LIST,
    DEFAULT_AMAZON_DOMAIN,
    DEFAULT_DEBUG_MODE,
    DEFAULT_MIRROR_COMPLETED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PRESERVE_DUPLICATES,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    PASSKEY_INDICATORS,
    TARGET_SHOPPING_LIST,
    InitialSyncMode,
    SyncMode,
)
from .exceptions import (
    OTPSecretInvalidError,
    PasskeyDetectedError,
    UnsupportedLoginFlowError,
)

_LOGGER = logging.getLogger(__name__)

AUTH_PROXY_PATH = f"/auth/proxy/{DOMAIN}"
AUTH_PROXY_NAME = f"auth:proxy:{DOMAIN}"
AUTH_CALLBACK_PATH = f"/auth/callback/{DOMAIN}"
AUTH_CALLBACK_NAME = f"auth:callback:{DOMAIN}"


def _validate_url(url: str) -> bool:
    """Validate a URL."""
    try:
        result = urlparse(url)
        return all([result.scheme in ("http", "https"), result.netloc])
    except Exception:
        return False


def _autofill(items: dict[str, str], html: str) -> str:
    """Autofill input tags in HTML forms.

    Fills email, password, and OTP code into Amazon login forms.
    Based on alexapy's autofill approach.
    """
    soup = BeautifulSoup(html, "html.parser")
    for item, value in items.items():
        for html_tag in soup.find_all(attrs={"name": item}):
            if not html_tag.get("value"):
                html_tag["value"] = value
    return str(soup)


class AlexaShoppingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alexa Shopping List Sync."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize."""
        self._proxy: AuthCaptureProxy | None = None
        self._proxy_view: AlexaShoppingProxyView | None = None
        self._user_input: dict[str, Any] = {}
        self._login_error: str | None = None
        self._captured_cookies: dict[str, str] = {}
        # PKCE + device credentials for OAuth device registration
        # Generated once per flow instance so proxy and registration always match.
        self._device_serial: str = binascii.b2a_hex(os.urandom(16)).decode("utf-8")
        _cv_raw = os.urandom(32)
        self._code_verifier: str = (
            base64.urlsafe_b64encode(_cv_raw).rstrip(b"=").decode()
        )
        self._code_challenge: str = (
            base64.urlsafe_b64encode(
                hashlib.sha256(self._code_verifier.encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        self._authorization_code: str = ""

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
            await self.async_set_unique_id(
                f"{user_input[CONF_EMAIL]}_{user_input.get(CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN)}"
            )
            self._abort_if_unique_id_configured()

            ha_url = user_input.get(CONF_HA_URL, "")
            if ha_url and not _validate_url(ha_url):
                errors[CONF_HA_URL] = "invalid_url"

            public_url = user_input.get(CONF_PUBLIC_URL, "")
            if public_url and not _validate_url(public_url):
                errors[CONF_PUBLIC_URL] = "invalid_url"

            if not errors:
                try:
                    normalize_otp_secret(user_input[CONF_OTP_SECRET])
                except OTPSecretInvalidError:
                    errors[CONF_OTP_SECRET] = "2fa_key_invalid"

            if not errors:
                self._user_input = user_input
                return await self.async_step_start_proxy()

        ha_url_default = ""
        try:
            from homeassistant.helpers.network import get_url

            ha_url_default = get_url(self.hass, prefer_external=False)
        except Exception:
            pass

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

    async def async_step_start_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the auth capture proxy and redirect user to it.

        Flow: user -> start_proxy -> [external browser] -> check_proxy -> finish_proxy -> sync_options
        """
        amazon_domain = self._user_input.get(
            CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN
        )
        email = self._user_input[CONF_EMAIL]
        password = self._user_input[CONF_PASSWORD]
        otp_secret = normalize_otp_secret(self._user_input[CONF_OTP_SECRET])

        ha_url = self._user_input.get(CONF_HA_URL, "")
        if not ha_url:
            try:
                from homeassistant.helpers.network import get_url

                ha_url = get_url(self.hass, prefer_external=False)
            except Exception:
                ha_url = "http://homeassistant.local:8123"

        # OAuth PKCE login URL — same approach as alexapy/alexa_media_player.
        # /ap/register with response_type=code causes Amazon to include an
        # authorization_code in the /ap/maplanding redirect, which is then
        # exchanged for a long-lived refresh_token via device registration.
        amazon_tld = "." + amazon_domain.split(".", 1)[-1]  # ".de" from "amazon.de"
        _locale_map = {
            ".de": "de_DE", ".com.au": "en_AU", ".ca": "en_CA", ".co.uk": "en_GB",
            ".in": "en_IN", ".com": "en_US", ".es": "es_ES", ".fr": "fr_FR",
            ".it": "it_IT", ".co.jp": "ja_JP", ".com.br": "pt_BR",
        }
        language = _locale_map.get(amazon_tld, "en_US")
        # Always use amazon.com — /ap/register only exists on .com (not .de etc.)
        # Amazon's unified auth handles all regional accounts. language= param
        # ensures the login page is shown in the user's language.
        login_url = (
            f"https://www.amazon.com/ap/register"
            f"?openid.return_to=https%3A%2F%2Fwww.amazon.com%2Fap%2Fmaplanding"
            f"&openid.assoc_handle=amzn_dp_project_dee_ios"
            f"&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
            f"&pageId=amzn_dp_project_dee_ios"
            f"&accountStatusPolicy=P1"
            f"&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
            f"&openid.mode=checkid_setup"
            f"&openid.ns.oa2=http%3A%2F%2Fwww.amazon.com%2Fap%2Fext%2Foauth%2F2"
            f"&openid.oa2.client_id=device%3A{self._device_serial}"
            f"&openid.ns.pape=http%3A%2F%2Fspecs.openid.net%2Fextensions%2Fpape%2F1.0"
            f"&openid.oa2.response_type=code"
            f"&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
            f"&openid.pape.max_auth_age=0"
            f"&openid.oa2.scope=device_auth_access%20offline_access"
            f"&openid.oa2.code_challenge_method=S256"
            f"&openid.oa2.code_challenge={self._code_challenge}"
            f"&language={language}"
        )

        proxy_base_url = str(URL(ha_url).with_path(AUTH_PROXY_PATH))

        if not self._proxy:
            try:
                self._proxy = AuthCaptureProxy(
                    URL(proxy_base_url),
                    URL(login_url),
                )
                self._proxy.session_factory = lambda: httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=30.0, read=120.0, write=30.0, pool=30.0
                    ),
                )
            except ValueError as ex:
                _LOGGER.error("Failed to create proxy: %s", ex)
                return self.async_show_form(
                    step_id="user",
                    errors={"base": "invalid_url"},
                )

        # Configure test: detect successful login
        self._proxy.tests = {
            "test_login_success": self._test_login_success,
        }

        # Configure modifier: autofill email, password, OTP
        self._proxy.modifiers = {
            "autofill": partial(
                _autofill,
                {
                    "email": email,
                    "password": password,
                    "otpCode": generate_otp(otp_secret),
                },
            ),
        }

        # Register HA views for proxy and callback
        if not self._proxy_view:
            self._proxy_view = AlexaShoppingProxyView(self._proxy.all_handler)
        else:
            self._proxy_view.handler = self._proxy.all_handler

        self.hass.http.register_view(AlexaShoppingCallbackView())
        self.hass.http.register_view(self._proxy_view)

        # Build callback URL that HA will hit when login succeeds
        callback_url = (
            URL(ha_url)
            .with_path(AUTH_CALLBACK_PATH)
            .with_query({"flow_id": self.flow_id})
        )

        # Build proxy URL with flow ID and callback
        proxy_url = self._proxy.access_url().with_query(
            {"config_flow_id": self.flow_id, "callback_url": str(callback_url)}
        )

        _LOGGER.debug("Proxy started, directing user to: %s", proxy_url)

        # Use external step: opens browser, waits for callback
        return self.async_external_step(
            step_id="check_proxy", url=str(proxy_url)
        )

    async def _test_login_success(
        self, resp: httpx.Response, data: dict, query: dict
    ) -> Optional[Union[URL, str]]:
        """Test if Amazon login was successful.

        Called by authcaptureproxy for each response.
        Returns a URL to redirect to on success, None to continue.
        """
        if not resp.url:
            return None

        resp_url = URL(str(resp.url))
        resp_path = resp_url.path

        # Successful login lands on /ap/maplanding (OAuth PKCE flow) or /spa/index.html
        if resp_path in ["/ap/maplanding", "/spa/index.html"]:
            _LOGGER.info("Amazon login successful (path: %s)", resp_path)
            config_flow_id = self._proxy.init_query.get("config_flow_id")
            callback_url = self._proxy.init_query.get("callback_url")

            # Extract authorization_code for PKCE device registration
            auth_code = resp_url.query.get("openid.oa2.authorization_code")
            if auth_code:
                self._authorization_code = auth_code
                _LOGGER.debug(
                    "Captured OAuth authorization_code for device registration"
                )
            else:
                _LOGGER.debug(
                    "No authorization_code in maplanding URL "
                    "(device registration will use cookie auth)"
                )

            self._login_error = None  # clear any earlier false-positive
            self._captured_cookies = self._extract_proxy_cookies(resp)
            await self._proxy.reset_data()

            if callback_url:
                return URL(callback_url)
            return (
                f"Successfully logged in for flow {config_flow_id}. "
                "Please close this window."
            )

        # Also check if we ended up on the main site (authenticated)
        if (
            "action=sign-out" in resp.text.lower()
            or resp_path == "/"
            and "session-id" in str(resp.headers.get("set-cookie", ""))
        ):
            _LOGGER.info("Amazon login successful (main page)")
            callback_url = self._proxy.init_query.get("callback_url")
            self._login_error = None  # clear any earlier false-positive
            self._captured_cookies = self._extract_proxy_cookies(resp)
            await self._proxy.reset_data()
            if callback_url:
                return URL(callback_url)
            return "Login successful. Please close this window."

        # Check for passkey/unsupported flows only on non-success auth pages.
        # Must come AFTER success checks so /ap/maplanding is never scanned.
        if "/ap/" in resp_path:
            try:
                check_page_for_unsupported_flow(resp.text)
            except PasskeyDetectedError:
                self._login_error = "passkey_not_supported"
                # WARNING not ERROR: Amazon's login pages often mention "passkey"
                # even when password auth is in progress — this may be a false
                # positive that gets cleared when the success page is reached.
                _LOGGER.warning(
                    "Passkey indicator detected on %s (may be false positive — "
                    "will be cleared if login completes successfully)",
                    resp_path,
                )
            except UnsupportedLoginFlowError:
                self._login_error = "unsupported_login_flow"
                _LOGGER.warning(
                    "Unsupported Amazon login flow detected on %s", resp_path
                )
            except Exception:
                pass

        return None

    def _extract_proxy_cookies(self, last_resp: httpx.Response) -> dict[str, str]:
        """Extract cookies from the proxy httpx session.

        Tries the session cookie jar first (all accumulated cookies),
        falls back to the last response's cookies.
        """
        cookies: dict[str, str] = {}
        # Try to get accumulated cookies from the httpx client session
        try:
            session = self._proxy.session  # type: ignore[union-attr]
            if session is not None:
                cookies = {k: v for k, v in session.cookies.items()}
        except Exception:
            pass

        # Merge/override with cookies from the last response
        try:
            for k, v in last_resp.cookies.items():
                cookies[k] = v
        except Exception:
            pass

        _LOGGER.debug("Captured %d cookies from proxy session", len(cookies))
        return cookies

    async def async_step_check_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check proxy result after callback.

        This step is reached when the callback URL is hit (login success)
        or when the user manually returns.
        """
        if self._proxy_view:
            self._proxy_view.reset()

        if self._login_error:
            error = self._login_error
            self._login_error = None
            return self.async_abort(reason=error)

        return self.async_external_step_done(next_step_id="finish_proxy")

    async def async_step_finish_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish proxy login and extract session cookies."""
        cookies = self._captured_cookies
        if not cookies:
            # Last-chance fallback: try reading from proxy session directly
            try:
                if self._proxy and self._proxy.session:
                    cookies = {k: v for k, v in self._proxy.session.cookies.items()}
            except Exception:
                pass

        if not cookies:
            _LOGGER.error("No session cookies captured after proxy login")
            return self.async_abort(reason="login_failed")

        # Attempt OAuth device registration to obtain a long-lived refresh_token.
        # Uses session cookies as auth (no access_token required) — same approach
        # as alexapy. If successful, silent session renewal will use token exchange
        # instead of programmatic login (which fails due to missing metadata1).
        amazon_domain = self._user_input.get(CONF_AMAZON_DOMAIN, DEFAULT_AMAZON_DOMAIN)
        refresh_token = await async_register_device(
            amazon_domain=amazon_domain,
            device_serial=self._device_serial,
            cookies=cookies,
            authorization_code=self._authorization_code or None,
            code_verifier=self._code_verifier,
        )
        if refresh_token:
            _LOGGER.info(
                "Device registration succeeded — silent token refresh enabled"
            )
        else:
            _LOGGER.warning(
                "Device registration failed — silent refresh unavailable "
                "(session will require manual re-authentication when it expires)"
            )

        self._user_input["_cookies"] = cookies
        if refresh_token:
            self._user_input["_refresh_token"] = refresh_token
            self._user_input["_device_serial"] = self._device_serial

        # Reauth: update existing entry (skip target selection)
        if self.source == SOURCE_REAUTH:
            reauth_entry = self._get_reauth_entry()
            data_updates: dict[str, Any] = {
                CONF_PASSWORD: self._user_input.get(
                    CONF_PASSWORD, reauth_entry.data[CONF_PASSWORD]
                ),
                CONF_OTP_SECRET: self._user_input.get(
                    CONF_OTP_SECRET, reauth_entry.data[CONF_OTP_SECRET]
                ),
                "_cookies": cookies,
            }
            if refresh_token:
                data_updates["_refresh_token"] = refresh_token
                data_updates["_device_serial"] = self._device_serial
            return self.async_update_reload_and_abort(
                reauth_entry,
                data_updates=data_updates,
            )

        return await self.async_step_select_target()

    async def async_step_select_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let user choose the HA list to sync with."""
        if user_input is not None:
            self._user_input[CONF_TARGET_LIST] = user_input[CONF_TARGET_LIST]
            return await self.async_step_sync_options()

        # Build options: built-in shopping list + all todo.* entities
        options: dict[str, str] = {}
        if "shopping_list" in self.hass.config.components:
            options[TARGET_SHOPPING_LIST] = "Built-in Shopping List"

        # Discover todo entities from the entity registry
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(self.hass)
        for entity in ent_reg.entities.values():
            if entity.domain == "todo" and not entity.disabled:
                friendly = entity.name or entity.original_name or entity.entity_id
                options[entity.entity_id] = friendly

        # Auto-select if only one option
        if len(options) == 1:
            self._user_input[CONF_TARGET_LIST] = next(iter(options))
            return await self.async_step_sync_options()

        if not options:
            return self.async_abort(reason="no_target_list_available")

        return self.async_show_form(
            step_id="select_target",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_LIST): vol.In(options),
                }
            ),
        )

    async def async_step_sync_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle sync options step."""
        if user_input is not None:
            full_config = {**self._user_input, **user_input}

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
                    CONF_TARGET_LIST: full_config.get(
                        CONF_TARGET_LIST, TARGET_SHOPPING_LIST
                    ),
                    "_cookies": full_config.get("_cookies", {}),
                    "_refresh_token": full_config.get("_refresh_token", ""),
                    "_device_serial": full_config.get("_device_serial", ""),
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
                            SyncMode.ALEXA_TO_HA: "Alexa \u2192 Home Assistant",
                            SyncMode.HA_TO_ALEXA: "Home Assistant \u2192 Alexa",
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
            self._user_input.update(user_input)
            return await self.async_step_start_proxy()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD, default=""): str,
                    vol.Required(CONF_OTP_SECRET, default=""): str,
                }
            ),
        )


# ---------------------------------------------------------------------------
# HA Views for proxy routing
# ---------------------------------------------------------------------------


class AlexaShoppingCallbackView(HomeAssistantView):
    """Handle callback from proxy when login succeeds."""

    url = AUTH_CALLBACK_PATH
    name = AUTH_CALLBACK_NAME
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Receive authorization confirmation."""
        hass = request.app["hass"]
        try:
            await hass.config_entries.flow.async_configure(
                flow_id=request.query["flow_id"], user_input=None
            )
        except (KeyError, UnknownFlow) as ex:
            _LOGGER.debug("Callback flow_id is invalid: %s", ex)
            raise HTTPBadRequest() from ex
        return web_response.Response(
            headers={"content-type": "text/html"},
            text="<script>window.close()</script>Success! This window can be closed.",
        )


class AlexaShoppingProxyView(HomeAssistantView):
    """Route proxy requests through HA's HTTP server."""

    url: str = AUTH_PROXY_PATH
    extra_urls: list[str] = [f"{AUTH_PROXY_PATH}/{{tail:.*}}"]
    name: str = AUTH_PROXY_NAME
    requires_auth: bool = False
    handler: web.RequestHandler = None
    known_ips: dict[str, datetime.datetime] = {}
    auth_seconds: int = 300

    def __init__(self, handler: web.RequestHandler) -> None:
        """Initialize proxy view."""
        AlexaShoppingProxyView.handler = handler
        for method in ("get", "post", "delete", "put", "patch", "head", "options"):
            setattr(self, method, self.check_auth())

    def reset(self) -> None:
        """Reset known IPs."""
        self.known_ips.clear()

    @classmethod
    def check_auth(cls):
        """Wrap authentication check into the handler.

        Only allows requests from IPs that provided a valid config_flow_id
        within the last auth_seconds.
        """

        async def wrapped(request: web.Request, **kwargs: Any) -> web.Response:
            """Check auth and forward to proxy handler."""
            hass = request.app["hass"]
            remote = request.remote

            if (
                remote not in cls.known_ips
                or (datetime.datetime.now() - cls.known_ips[remote]).seconds
                > cls.auth_seconds
            ):
                try:
                    flow_id = request.url.query["config_flow_id"]
                except KeyError as ex:
                    raise Unauthorized() from ex

                success = False
                for flow in hass.config_entries.flow.async_progress():
                    if flow["flow_id"] == flow_id:
                        success = True
                        break

                if not success:
                    raise Unauthorized()

                cls.known_ips[remote] = datetime.datetime.now()

            return await cls.handler(request)

        return wrapped


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
                            SyncMode.ALEXA_TO_HA: "Alexa \u2192 Home Assistant",
                            SyncMode.HA_TO_ALEXA: "Home Assistant \u2192 Alexa",
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
