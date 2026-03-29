#!/usr/bin/env python3
"""Standalone CLI test for the Amazon proxy login flow.

Usage:
    python test_login_flow.py --email you@example.com --password secret \
        --otp-secret JBSW... --domain amazon.de

Opens a browser window, autofills credentials, waits for login success,
then prints the captured cookies. No Home Assistant required.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from functools import partial

import httpx
from aiohttp import web
from bs4 import BeautifulSoup
from yarl import URL

import base64
import re

import pyotp
from authcaptureproxy import AuthCaptureProxy


def normalize_otp_secret(secret: str) -> str:
    cleaned = re.sub(r"[\s\-]", "", secret.strip().upper())
    padding = (8 - len(cleaned) % 8) % 8
    base64.b32decode(cleaned + "=" * padding)  # validate
    return cleaned


def generate_otp(secret: str) -> str:
    return pyotp.TOTP(secret).now()

HOST = "127.0.0.1"
PORT = 18123
PROXY_PATH = "/auth/proxy/alexa_shopping_sync"
SUCCESS_PATH = "/auth/success"

_success_event: asyncio.Event = asyncio.Event()
_captured_cookies: dict[str, str] = {}
_proxy: AuthCaptureProxy | None = None


def _autofill(items: dict[str, str], html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for item, value in items.items():
        for tag in soup.find_all(attrs={"name": item}):
            if not tag.get("value"):
                tag["value"] = value
    return str(soup)


async def _test_login_success(
    resp: httpx.Response, data: dict, query: dict
) -> URL | str | None:
    global _captured_cookies

    if not resp.url:
        return None

    resp_url = URL(str(resp.url))
    resp_path = resp_url.path

    # Success paths — must be checked BEFORE passkey scan
    if resp_path in ["/ap/maplanding", "/spa/index.html"]:
        print(f"\n[OK] Login success detected at {resp_path}")
        _captured_cookies = _collect_cookies(resp)
        await _proxy.reset_data()
        return URL(f"http://{HOST}:{PORT}{SUCCESS_PATH}")

    if "action=sign-out" in resp.text.lower() or (
        resp_path == "/" and "session-id" in str(resp.headers.get("set-cookie", ""))
    ):
        print(f"\n[OK] Login success detected (main page, path={resp_path})")
        _captured_cookies = _collect_cookies(resp)
        await _proxy.reset_data()
        return URL(f"http://{HOST}:{PORT}{SUCCESS_PATH}")

    # Passkey / unsupported flow detection — only on auth pages, never on success pages
    if "/ap/" in resp_path:
        html_lower = resp.text.lower()
        passkey_indicators = ["passkey", "fido", "webauthn"]
        for indicator in passkey_indicators:
            if indicator in html_lower:
                print(f"\n[WARN] Passkey indicator '{indicator}' on {resp_path} — ignored (not a login blocker)")
                break

    return None


def _collect_cookies(resp: httpx.Response) -> dict[str, str]:
    cookies: dict[str, str] = {}
    try:
        if _proxy and _proxy.session:
            cookies.update({k: v for k, v in _proxy.session.cookies.items()})
    except Exception:
        pass
    try:
        cookies.update({k: v for k, v in resp.cookies.items()})
    except Exception:
        pass
    return cookies


async def _success_handler(request: web.Request) -> web.Response:
    _success_event.set()
    return web.Response(
        content_type="text/html",
        text="<h1>Login successful!</h1><p>You can close this window.</p><script>window.close()</script>",
    )


async def _proxy_handler(request: web.Request) -> web.Response:
    if _proxy:
        return await _proxy.all_handler(request)
    return web.Response(status=503, text="Proxy not ready")


async def main(email: str, password: str, otp_secret: str, domain: str) -> None:
    global _proxy

    try:
        otp_secret = normalize_otp_secret(otp_secret)
    except Exception as e:
        print(f"[ERROR] Invalid OTP secret: {e}")
        sys.exit(1)

    login_url = (
        f"https://www.{domain}/ap/signin"
        f"?openid.pape.max_auth_age=0"
        f"&openid.return_to=https%3A%2F%2Fwww.{domain}%2F"
        f"&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
        f"&openid.assoc_handle=deflex"
        f"&openid.mode=checkid_setup"
        f"&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
        f"&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
    )

    proxy_base = f"http://{HOST}:{PORT}{PROXY_PATH}"

    _proxy = AuthCaptureProxy(URL(proxy_base), URL(login_url))
    _proxy.session_factory = lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        follow_redirects=True,
    )
    _proxy.tests = {"test_login_success": _test_login_success}
    _proxy.modifiers = {
        "autofill": partial(
            _autofill,
            {
                "email": email,
                "password": password,
                "otpCode": generate_otp(otp_secret),
            },
        )
    }

    # Build aiohttp app
    app = web.Application()
    app.router.add_route("*", SUCCESS_PATH, _success_handler)
    app.router.add_route("*", PROXY_PATH, _proxy_handler)
    app.router.add_route("*", PROXY_PATH + "/{tail:.*}", _proxy_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()

    proxy_url = _proxy.access_url()
    print(f"\nProxy running at {proxy_base}")
    print(f"Opening browser: {proxy_url}")
    webbrowser.open(str(proxy_url))
    print("Waiting for login (Ctrl+C to abort)...\n")

    try:
        await asyncio.wait_for(_success_event.wait(), timeout=300)
    except asyncio.TimeoutError:
        print("[ERROR] Timeout waiting for login")
        await runner.cleanup()
        sys.exit(1)

    await runner.cleanup()

    print(f"\nCaptured {len(_captured_cookies)} cookies:")
    for k, v in _captured_cookies.items():
        masked = v[:6] + "..." if len(v) > 6 else "***"
        print(f"  {k} = {masked}")

    print("\n[SUCCESS] Login flow completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Amazon proxy login flow")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--otp-secret", required=True)
    parser.add_argument("--domain", default="amazon.de")
    args = parser.parse_args()

    asyncio.run(main(args.email, args.password, args.otp_secret, args.domain))
