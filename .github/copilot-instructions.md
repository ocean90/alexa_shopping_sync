# Copilot Instructions

## Project Overview

This is a **Home Assistant HACS custom integration** (`alexa_shopping_sync`) that bidirectionally syncs the Amazon Alexa shopping list with a Home Assistant list (built-in Shopping List or any `todo.*` entity). It uses cookie-based authentication via `authcaptureproxy` for initial login, with PKCE OAuth device registration for long-lived refresh tokens.

**Primary target domain: `amazon.de`** (other Amazon domains may work but are untested). Only 2SV via TOTP authenticator app is supported — no SMS OTP, passkeys, or CAPTCHA solving.

## Repository Layout

```
custom_components/alexa_shopping_sync/   # All integration source code
├── __init__.py          # Entry setup/unload, config migration
├── config_flow.py       # Multi-step login flow using authcaptureproxy
├── auth.py              # AuthManager — session lifecycle, token exchange, device registration
├── amazon_client.py     # AmazonShoppingClient — REST API wrapper for Alexa Shopping List
├── coordinator.py       # DataUpdateCoordinator — polling, event handling, sync orchestration
├── sync_engine.py       # SyncEngine — core bidirectional sync logic (the most complex module)
├── models.py            # Data models: AlexaShoppingItem, HAShoppingItem, ItemMapping, SyncState
├── const.py             # Constants, enums (SyncMode, InitialSyncMode, PendingOpType)
├── exceptions.py        # Exception hierarchy (SessionExpiredError, ThrottledError, etc.)
├── ha_list_bridge.py    # HAListBridge protocol + snapshot hash utility
├── shopping_list_bridge.py  # Bridge to HA built-in Shopping List (internal API)
├── todo_list_bridge.py  # Bridge to any HA todo entity (via service calls)
├── sensor.py            # Sensor entities (last_success, last_error, pending_ops, item counts)
├── binary_sensor.py     # Connection status binary sensor
├── button.py            # Sync Now button entity
├── switch.py            # Sync enabled/disabled switch entity
├── services.py          # Service handlers (force_refresh, full_resync, etc.)
├── services.yaml        # Service definitions
├── diagnostics.py       # Diagnostics export (secrets redacted)
├── strings.json         # UI strings
├── translations/        # de.json, en.json
└── manifest.json        # HA integration manifest
tests/                   # pytest test suite
├── conftest.py          # Shared fixtures (mock_hass, mock_amazon_client, mock_ha_bridge, sync_engine)
├── test_sync_engine.py  # Sync engine tests (most critical)
├── test_amazon_client.py
├── test_auth.py
├── test_models.py
└── test_todo_list_bridge.py
test_login_flow.py       # Standalone CLI login test (not run by pytest, requires real credentials)
test_sync_logic.py       # Standalone sync test (stubs HA imports, runnable without HA installed)
pyproject.toml           # Build config, dependencies, ruff + pytest settings
CLAUDE.md                # Additional architecture docs (useful context)
```

## Commands

```bash
# Lint
ruff check .

# Format
ruff format .

# Run all tests
pytest

# Run a single test file
pytest tests/test_sync_engine.py

# Run a specific test
pytest tests/test_sync_engine.py::TestClassName::test_method -v
```

## Coding Conventions

- **Python 3.12+** required. Use `from __future__ import annotations` in every module.
- **Ruff** for linting and formatting — config in `pyproject.toml`: line-length 100, target `py312`, select rules `E, F, W, I`.
- **Conventional Commits**: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:` — lowercase, imperative, no period.
- **Never commit directly to `main`** — always branch and merge via PR.
- Every module has a module-level docstring.
- Public functions/methods have docstrings (brief, Google-style).
- Logging uses `_LOGGER = logging.getLogger(__name__)` at module level.
- Secrets are never logged — `auth.py::sanitize_log_data()` redacts sensitive keys.
- Async methods are prefixed with `async_` following Home Assistant conventions.
- Type hints are used throughout; `typing.Any` is acceptable for HA internal data structures.
- Enums use `StrEnum` (from Python 3.11+).
- Dataclasses are used for models with `to_dict()` / `from_dict()` class methods for serialization.

## Architecture Essentials

### Authentication Flow
1. `config_flow.py` starts an `AuthCaptureProxy` to proxy Amazon's login page
2. Proxy autofills credentials + TOTP, captures session cookies on successful login (`/ap/maplanding`)
3. `auth.py::async_register_device()` exchanges an OAuth authorization code + PKCE code_verifier for a long-lived `refresh_token`
4. `AuthManager` (in `auth.py`) handles session renewal via `/ap/exchangetoken/cookies` using the stored refresh_token

### Sync Engine (most complex module)
- `SyncEngine` in `sync_engine.py` is the core logic
- **ItemMapping** links Alexa item IDs ↔ HA item IDs
- **Echo suppression**: `PendingOperation` list prevents re-syncing changes the engine itself made
- **Warm start**: after HA restart, `_previous_*_items` are empty; only unmapped items sync to avoid duplicates
- **Initial sync strategies**: `merge_union` (default), `alexa_wins`, `ha_wins`
- **Incremental sync**: snapshot diffing to detect adds/removes/modifications
- State persisted via `homeassistant.helpers.storage.Store`

### Data Flow
- **Alexa → HA**: `DataUpdateCoordinator._async_update_data()` polls Amazon → `SyncEngine.async_sync_alexa_to_ha()`
- **HA → Alexa**: `shopping_list_updated` event → debounced mutation queue → `SyncEngine.async_sync_ha_to_alexa()`
- `_sync_lock` (`asyncio.Lock`) prevents concurrent sync operations

### Amazon API
- `AmazonShoppingClient` calls `https://www.amazon.de/alexashoppinglists/api/`
- Update and delete require the **full item dict** — client maintains an `_item_cache`
- Requires `PitanguiBridge` User-Agent header
- 401/403 → `SessionExpiredError`; 429 → `ThrottledError`; retry/backoff on 5xx

### HA List Bridges
- `HAListBridge` is a Protocol in `ha_list_bridge.py`
- Two implementations: `ShoppingListBridge` (built-in, internal API) and `TodoListBridge` (any `todo.*` entity, via service calls)

## Testing Patterns

- All tests are in `tests/` and run with `pytest`; `asyncio_mode = "auto"` in `pyproject.toml`.
- Tests use `unittest.mock` (`MagicMock`, `AsyncMock`) extensively — no real network calls.
- Shared fixtures in `tests/conftest.py`: `mock_hass`, `mock_auth_manager`, `mock_amazon_client`, `mock_ha_bridge`, `sync_engine`.
- Helper factories: `make_alexa_item()` and `make_ha_item()` in `conftest.py`.
- Tests are organized in classes (e.g., `TestInitialSyncMergeUnion`, `TestNormalizeName`).
- Each test method is marked `@pytest.mark.asyncio` for async tests.
- The `sync_engine` fixture patches `engine._store` to avoid file I/O.
- Root-level `test_sync_logic.py` and `test_login_flow.py` are standalone scripts, **not** collected by pytest.

## Key Dependencies

| Package | Purpose |
|---|---|
| `homeassistant` | Core HA framework (dev dependency, required for tests) |
| `authcaptureproxy` | Browser proxy for Amazon login flow |
| `httpx` | HTTP client for Amazon API calls |
| `pyotp` | TOTP generation for 2SV |
| `beautifulsoup4` | HTML parsing for login form autofill |
| `ruff` | Linting and formatting |
| `pytest` + `pytest-asyncio` | Testing |

## Important Caveats

- The integration accesses HA's internal `ShoppingData` (`hass.data["shopping_list"]`) which is not a public API — changes in HA core may require updates to `shopping_list_bridge.py`.
- `config_flow.py` is tightly coupled to Amazon's login page structure — any Amazon UI changes may break the proxy login flow.
- The `test_login_flow.py` script requires real Amazon credentials and a browser — it cannot run in CI.
- `manifest.json` version and `pyproject.toml` version should be kept in sync (currently both `0.2.0`).
