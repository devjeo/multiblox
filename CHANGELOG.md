# Changelog

## 2.0.0 - 2026-10-16

### Added
- **Account groups and notes.** Group and note columns, a group filter, "Launch group", per-group default game, `group:<name>` in the console.
- **Notifications.** Discord webhook and system tray alerts for disconnects, give-ups and expired cookies, with a per-event cooldown and a Test button. The webhook URL is stored DPAPI-encrypted in `secrets.json` and is redacted from logs and reports.
- **Join tools** (Find server...): server finder (emptiest, fullest, best, minimum players), friend join, and private-server links (experimental).
- **Resource saver.** Normal, Low and Minimum client-flag profiles applied before launch, with an exact-bytes "Restore original".
- **Window layout.** Tile, minimize and restore all client windows on a chosen monitor, with optional auto-tile for new clients.
- **Scheduler.** Start and stop rules for an account or group on chosen days and times. A run missed while the app was closed or the PC slept fires once if it is inside the missed-run window (default 30 minutes).
- Diagnostics checks for every new feature; the report header now shows the app version.
- Shared infrastructure: `settings.py`, `secrets_store.py`, `net.py` (retries and CSRF handshake), `workers.py`, `jsonio.py`.
- `RobloxAccountManager.spec` and build instructions for a one-file Windows exe.
- `installer.iss`: an Inno Setup script that produces a single Setup installer exe.
- MIT `LICENSE`, also shown as a page in the installer.

### Changed
- `multi_instance.log` and `rejoin_debug.log` now live in `%APPDATA%\RobloxAccountManager\` instead of next to the scripts, so they work from a packaged exe.
- Settings moved out of `gui.py` into `settings.py` (same behaviour).

### Notes
- Existing `accounts.json` files open unchanged; the new `group` and `note` keys are added on the next save and ignored by older versions.
- Private servers and the resource-saver flags depend on Roblox behaviour that Roblox can change. Both are marked experimental or unverified in the app.

## 1.0.0
- Initial release: multi-instance, launching, auto-rejoin, crash recovery, diagnostics.
