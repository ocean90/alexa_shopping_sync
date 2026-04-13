# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant HACS custom integration that bidirectionally syncs the Amazon Alexa shopping list with the HA built-in shopping list. Uses cookie-based authentication via an `authcaptureproxy` browser proxy for initial login, with PKCE OAuth device registration for long-lived refresh tokens.

**Primary target: amazon.de** (other domains may work but are untested). Only supports 2SV via TOTP authenticator app — no SMS OTP, no passkeys, no CAPTCHA solving.

## Commit Convention

Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:` — lowercase, imperative, no period.

## Commands

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_sync_engine.py

# Run a specific test
pytest tests/test_sync_engine.py::test_name -v

# Lint
ruff check .

# Format
ruff format .
```

Python 3.12+ required. Key dependencies: `httpx`, `authcaptureproxy`, `pyotp`, `beautifulsoup4`.

## Architecture

All integration code lives under `custom_components/alexa_shopping_sync/`.

### Authentication Flow

`config_flow.py` orchestrates a multi-step login using `authcaptureproxy`:
1. User enters Amazon credentials + TOTP secret in HA UI
2. HA starts an `AuthCaptureProxy` that proxies Amazon's login page, autofilling credentials and OTP
3. Proxy detects successful login (landing on `/ap/maplanding`), captures session cookies and an OAuth authorization code
4. `auth.py::async_register_device()` exchanges the authorization code + PKCE code_verifier for a long-lived `refresh_token` via Amazon's `/auth/register` endpoint
5. Cookies + refresh_token + device_serial are persisted in the config entry

### Session Renewal

`AuthManager` (in `auth.py`) handles session lifecycle:
- **Primary**: Token exchange via `/ap/exchangetoken/cookies` using the stored refresh_token (no browser fingerprint needed)
- **Fallback**: Programmatic form-fill re-login (often fails due to missing `metadata1` browser fingerprint)
- The coordinator (`coordinator.py`) triggers silent refresh on `SessionExpiredError`, persists new cookies to the config entry

### Sync Engine

`sync_engine.py::SyncEngine` is the core sync logic:
- Maintains `ItemMapping` list (Alexa ID ↔ HA ID) and `PendingOperation` list for echo suppression
- **Initial sync**: three strategies — merge_union (default), alexa_wins, ha_wins
- **Incremental sync**: snapshot diffing — compares previous and current item lists to detect adds/removes/modifications
- **Echo suppression**: when the engine writes to side A, it registers a `PendingOperation` so the echo from side A's next poll is suppressed
- **Warm start**: after HA restart, `_previous_*_items` lists are empty; only unmapped items are synced to avoid duplicating everything
- State persisted via `homeassistant.helpers.storage.Store`

### Data Flow

- **Alexa→HA**: `DataUpdateCoordinator._async_update_data()` polls Amazon every N seconds → `SyncEngine.async_sync_alexa_to_ha()`
- **HA→Alexa**: `shopping_list_updated` event → debounced mutation queue → `SyncEngine.async_sync_ha_to_alexa()`
- A `_sync_lock` (asyncio.Lock) prevents concurrent execution of polls and mutations

### Amazon API

`amazon_client.py::AmazonShoppingClient` wraps the Alexa Shopping List REST API at `https://www.amazon.de/alexashoppinglists/api/`. The update and delete endpoints require the **full item dict** (not just the ID) — the client maintains an `_item_cache` for this. Retry/backoff on 5xx and 429; 401/403 triggers session expiry.

### Key Models (`models.py`)

- `AlexaShoppingItem` / `HAShoppingItem`: item representations with `normalized_name` for case-insensitive Unicode-aware matching
- `ItemMapping`: bidirectional ID link between Alexa and HA items
- `SyncState`: the full persisted state (mappings, pending ops, list ID, hashes)

### HA Platform Entities

- `binary_sensor.py`: connection status
- `sensor.py`: last_success, last_error, pending_operations, alexa_items, ha_items
- `services.py`: force_refresh, full_resync, clear_local_mapping, mark_reauth_needed, export_sanitized_diagnostics
