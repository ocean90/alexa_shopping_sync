# Alexa Shopping List Sync

A Home Assistant HACS custom integration for bidirectional synchronization between the **Amazon Alexa shopping list** and a **Home Assistant list** — either the built-in Shopping List or any to-do list entity (e.g. Cookidoo, Google Tasks, Local To-do).

## Support Matrix

| Feature | Supported |
|---|---|
| Amazon Domain | `amazon.de` (primary) |
| Target List | Built-in Shopping List, any `todo.*` entity |
| 2SV via Authenticator App | Yes |
| 2SV via SMS OTP | No |
| Passkey Login | No |
| Cookie Import | No |
| YAML Configuration | No |

**This integration only works with Amazon accounts that use 2-Step Verification via an Authenticator App (TOTP).** You need the 52-character secret key from your authenticator app setup.

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations**
3. Click the three dots menu → **Custom repositories**
4. Add this repository URL and select **Integration** as category
5. Click **Install**
6. Restart Home Assistant

### Manual

1. Copy the `custom_components/alexa_shopping_sync` folder to your `config/custom_components/` directory
2. Restart Home Assistant

## Prerequisites

1. A **target list** in Home Assistant — one of:
   - The **built-in Shopping List** (Settings → Integrations → Add → Shopping List)
   - Any **to-do list entity** from another integration (e.g. Cookidoo, Google Tasks, Local To-do)
2. An Amazon account with **Authenticator App 2SV** enabled
3. The **52-character TOTP secret key** from your authenticator app setup

### How to get the Authenticator App Secret

1. Go to [Amazon Account Settings](https://www.amazon.de/a/settings) → Login & Security
2. Under "Two-Step Verification", click **Manage**
3. Add a new Authenticator App
4. When Amazon shows the QR code, look for **"Can't scan the barcode?"** or similar
5. Copy the secret key shown (typically 52 characters, letters and numbers)
6. You can still scan the QR code with your authenticator app as usual

## Configuration

1. Go to **Settings → Integrations → Add Integration**
2. Search for **Alexa Shopping List Sync**
3. Enter:
   - **Amazon Domain**: `amazon.de` (default)
   - **Email**: Your Amazon account email
   - **Password**: Your Amazon account password
   - **Authenticator App Secret**: The 52-character key
   - **Home Assistant URL**: Your HA internal URL (auto-detected)
4. Complete the Amazon login in the browser window
5. **Choose a target list**: The built-in Shopping List or any available to-do list entity
6. Configure sync options:
   - **Sync Mode**: Two-way, Alexa→HA, or HA→Alexa
   - **Initial Sync Mode**: Merge (union), Alexa wins, or HA wins
   - **Poll Interval**: 30-600 seconds (default: 60)

The target list can be changed later in the integration options. Changing it will clear all item mappings and trigger a full resync.

## Sync Modes

| Mode | Description |
|---|---|
| **Two-way** | Changes sync in both directions |
| **Alexa → HA** | Only Alexa changes appear in HA |
| **HA → Alexa** | Only HA changes appear in Alexa |

### Initial Sync

| Mode | Description |
|---|---|
| **Merge (union)** | Both lists are merged, no deletions (default, safest) |
| **Alexa wins** | HA list is replaced with Alexa items |
| **HA wins** | Alexa list is replaced with HA items |

## Entities

### Controls

| Entity | Type | Description |
|---|---|---|
| `switch.*_sync_enabled` | Switch | Enable/disable sync (persists across restarts) |
| `button.*_sync_now` | Button | Trigger an immediate sync (works even when sync is paused) |

### Sensors

| Entity | Type | Description |
|---|---|---|
| `sensor.*_pending_operations` | Sensor | Number of pending sync operations |
| `sensor.*_alexa_items` | Sensor | Number of items on Alexa list |
| `sensor.*_ha_items` | Sensor | Number of items on HA list |

### Diagnostics

| Entity | Type | Description |
|---|---|---|
| `binary_sensor.*_connected` | Binary Sensor | Connection status to Amazon |
| `sensor.*_last_success` | Sensor | Timestamp of last successful sync |
| `sensor.*_last_error` | Sensor | Last error message |
| `sensor.*_target_list` | Sensor | The configured target list |

## Services

| Service | Description |
|---|---|
| `alexa_shopping_sync.force_refresh` | Force immediate sync |
| `alexa_shopping_sync.full_resync` | Clear mappings and full resync |
| `alexa_shopping_sync.clear_local_mapping` | Clear local mapping store |
| `alexa_shopping_sync.mark_reauth_needed` | Manually trigger re-auth |
| `alexa_shopping_sync.export_sanitized_diagnostics` | Export diagnostics (no secrets) |

## Security Warning

This integration stores your Amazon email, password, and authenticator secret in the Home Assistant configuration database. Ensure your Home Assistant instance is properly secured:

- Use HTTPS
- Keep your HA instance updated
- Restrict access to trusted users
- Consider using a dedicated Amazon account

**Credentials are never logged or included in diagnostics.**

## Re-authentication

If your Amazon session expires:

1. You'll see a **repair notification** in Home Assistant
2. Go to **Settings → Integrations → Alexa Shopping List Sync**
3. Click **Re-authenticate**
4. Complete the login again

You can also manually trigger re-auth via the `mark_reauth_needed` service.

## Known Limitations

- **Amazon.de only** (primary target; other domains may work but are untested)
- **No Passkey support** — Amazon must be configured to use Authenticator App
- **No SMS OTP** — only TOTP authenticator apps
- **No CAPTCHA solving** — if Amazon shows a CAPTCHA during login, it must be solved manually in the proxy window
- **Polling-based** — Alexa changes are detected by polling (default: every 60 seconds)
- **Rate limiting** — Amazon may throttle requests; the integration backs off automatically
- **Session expiry** — Amazon sessions expire periodically; re-authentication is required
- **To-do list renames** — when syncing to a `todo.*` entity, item renames are detected by polling (not instantly), since the to-do platform does not emit item-level events

## Troubleshooting

### "Passkey login detected"
Disable Passkeys in your Amazon account: Amazon Settings → Login & Security → Passkeys → Remove all passkeys, then retry.

### "No target list available"
Set up either the built-in Shopping List integration or a to-do list integration (e.g. Local To-do) before configuring this integration.

### "Could not find Alexa shopping list"
The integration couldn't discover your Alexa shopping list. Check that your Alexa account has an active shopping list (say "Alexa, what's on my shopping list?").

### "Session expired"
Normal behavior — Amazon sessions expire. Follow the re-auth flow.

### Items not syncing
1. Check the `binary_sensor.*_connected` entity
2. Check `sensor.*_last_error` for error details
3. Try the `force_refresh` service or the **Sync Now** button
4. If persistent, use `full_resync`

## License

MIT License - see [LICENSE](LICENSE) file.
