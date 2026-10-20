# Roblox Account Manager

Version 2.0.0 (see [CHANGELOG.md](CHANGELOG.md))

A Windows desktop app for managing and running multiple of your own Roblox accounts at the same time. Save accounts once, launch them into any game with one click, and let the built-in auto-rejoin keep them connected.

Written in pure Python with no third-party dependencies required (the system tray icon is an optional extra).

---

## Features

**Account management**
- Store any number of accounts. Cookies are encrypted with Windows DPAPI (current-user scope), so the saved file can only be decrypted by the same Windows user on the same PC.
- Cookies are verified against Roblox when added, and re-checked on demand.
- Cookies are never printed, logged, or written to diagnostics reports.

**Multi-instance**
- One switch turns multi-instance on or off. The app holds Roblox's single-instance mutex and closes the singleton event handle inside each new client so several clients can run side by side.
- Singleton object names are configurable in Settings, so you can adapt if a Roblox update renames them.

**Launching**
- Launch one account, a selection, or all accounts into the same game, one after another.
- Save games by pasting a roblox.com link or a place ID. Names are looked up automatically.
- Optionally target a specific server by job ID.
- Authentication uses Roblox's own short-lived, single-use ticket flow (including the CSRF handshake). No passwords are handled.

**Auto-rejoin**
- Per-account switch. When a client disconnects it is relaunched automatically.
- Disconnects are detected three ways: process exit, Roblox log markers (confirmed against presence before acting, so teleports are not mistaken for crashes), and presence polling for clients stuck on an error screen.
- Safety limits: a 30 second grace period after launch, a cap of 5 attempts per account per 10 minutes, and the first retry returns to the same server while later retries accept any server.
- A "Stop" action closes a client on purpose without triggering a rejoin. "Resume" retries an account that gave up.

**Crash and restart recovery**
- Session state is saved continuously. After an app restart, clients that are still running are re-attached automatically (with protection against recycled process IDs).
- After an unclean exit such as a crash or power loss, dead clients for accounts with auto-rejoin on are relaunched. After a normal quit they are left alone.

**Groups and notes**
- The main window no longer shows or edits groups and notes. Values saved earlier are kept in your accounts file, and the console (`setgroup`, `setnote`, `group:<name>`) and the scheduler still understand them.

**Notifications**
- Alerts for disconnects, auto-rejoin giving up, and expired cookies, sent to a Discord webhook and/or the system tray. Each event is rate-limited per account (default 120 s).
- The webhook URL is stored encrypted with DPAPI in `secrets.json`, never in `settings.json`, and is redacted from logs and diagnostics. Messages never ping anyone.

**Join tools** (the left panel of the main window: one notebook with the tabs **Player | Finder | Private | Hopper | History | Current**, all on one screen; there is no separate Find server window)
- **Window:** the main window can be resized and maximized by dragging its edges (it never goes below a minimum size). Clicking buttons, searching or switching tabs never changes its size. Table column borders cannot be dragged. A tab scrolls by itself only when the screen is too short to show all of it.
- **Player tab:** look up any player by username, ID or profile link, or pick one from History or Online friends. Picking only selects them; the Launch selected button then reads "Join <name>" and joins their exact server (job ID) with every selected account Roblox lets in. **Stay and wait if the server is full** (on by default): the Roblox client is left open and this app never closes it while it waits to get into that server. There is no timeout; the "disconnected" checks are switched off for it until it is in the server; its status reads "Waiting to get in...". Press Stop to end the wait. Only if the client process itself exits is the same server launched again after 20 s, never another server. Untick the box and an account that cannot get in is closed after about 75 s. An account that is already playing is closed completely first, then rejoins in that server. "Unavailable" is shown when their privacy settings hide the server.
  - **Player history** keeps the last 30 players you actually **joined** (`join_history.json`, no cookies). Only pressing join saves someone: looking a player up, or picking them, does not.
  - **Launching normally:** once a join has started, the picked player is dropped by itself, so the next Launch selected is a normal launch from the Game box. To drop the pick earlier press **Clear player (launch normally)**, press Esc, or click the picked row again. While a player or server is picked, a **Launch normally** button under the Join button (and in the right-click menu) launches the highlighted accounts into the Game box and drops the pick.
- **Finder tab:** pick the emptiest, fullest, "best" (fullest that is not full) or a minimum-player server and click it to join it. Lists are cached for 2 minutes per game (*Refresh* forces a new one) and requests pause for 90 s after a rate limit, so Roblox's 429 is rarely hit. Clicking a server also closes and moves running accounts. *Only fill Server box* puts the ID into the Game panel's Server box without launching.
- **Hopper tab:** move the selected accounts to a new server every N minutes, or once with *Hop now*. The old game is fully closed before the new one starts, so an account never has two clients.
- **Private tab (experimental):** pick a game and choose one of your own or your friends' private servers, then Arm it. The code is kept in memory only, so after a crash the client is re-attached but not relaunched.
- **Current tab:** only the servers where your accounts are in right now (Game, Server ID, Accounts), updated by itself. Click one to pick it, then use the Join button, exactly like History. Servers listed here can not be cleared: History's *Clear all* and *Remove from history*, and an account's *Clear this account's history*, skip any server an account is in right now. They become normal rows once the account leaves.
- **History tab:** every server any of your accounts got into, all games together, newest first: Game, Server ID, When, Accounts. A server is listed only after Roblox confirms the account is in it, so random joins and teleports are included and failed joins are not. Up to 200 servers are kept in `server_history.json` (no cookies). Click a server to pick it (nothing launches), select accounts, then press the Join button (Launch selected). Each selected account goes to that exact server; one already playing is closed completely first. If the server is full the client stays open and waits until it gets in or you press Stop. Click the picked row again to un-pick it; the pick is also dropped once the join has started. Right-click copies the full server ID or removes the row. **Where is my account now?** Servers that have an account in them right now are listed first, in green, with *Online now* and the online account names; the accounts table shows the same short server ID in its *Server · Last game* column. This comes from the presence check that already runs for every client, It refreshes by itself: a new join shows within about 5-20 seconds (a confirmed account is re-checked every 15 s, one that is still loading every 5 s), using a light background check that never triggers a rejoin.

**Per-account server history and following another account**
- Every account keeps its own list of servers with the date and time (`account_history.json`, last 50 per account, no cookies). Right-click an account > *Server history...* to see it; pick a row and press *Join with selected accounts*.
- Right-click > *Join same server as* > an account: the selected accounts join the exact server that account is in right now. Your own account's server is read with its own login, so this works even when that account's privacy hides it from other people, and no friend request or friend-join is needed. Works for public servers only, and a full server is waited for like any other history join.

**Resource saver**
- Normal, Low and Minimum profiles write client flags (currently an FPS cap) before each launch. The original `ClientAppSettings.json` is backed up once and **Restore original** puts back the exact bytes (or deletes the file if there was none).
- Roblox honours only some flags and can change them; the profile table in `resourcesaver.py` is meant to be edited.

**Window layout**
- Tile all client windows in a grid on a chosen monitor (columns and gap configurable), or minimize and restore them all. Optional auto-tile re-tiles when a client starts or rejoins. Fullscreen clients are skipped.

**Scheduler**
- Start or stop an account or group at a time of day on chosen weekdays. A stop is intentional, so auto-rejoin does not revive it.
- A run missed while the app was closed or the PC slept fires once if it is still inside the missed-run window (default 30 minutes); it never repeats.
- Scheduled starts skip accounts already running and, if multi-instance is off, skip multi-client launches instead of prompting.

**Diagnostics**
- One button checks everything the app depends on (including groups, notifications, join tools, resource saver, layout and scheduler): Python bitness, administrator rights, data files, DPAPI, Roblox install (including the Microsoft Store version, which cannot multi-instance), log naming, Roblox web endpoints, multi-instance state, the Roblox mutex and event names present in running clients, and every saved cookie.
- The report is passed through a redactor and is safe to paste when asking for help.

**Interface**
- Live account table with status, last game, auto-rejoin state, and uptime.
- Multi-select with Ctrl/Shift-click and Ctrl+A, plus a right-click menu.
- Live color-coded log panel.
- Optional system tray with close-to-tray.
- Single-instance guard so the manager cannot be started twice.
- Settings for rejoin cooldown, retry limits, polling interval, launch pause, and more, saved to `settings.json`.

---

## Requirements

- Windows 10 or 11
- 64-bit Python 3.10 or newer (64-bit is required to read Roblox's handle table)
- Roblox installed from [roblox.com](https://www.roblox.com). The Microsoft Store version is not supported because it cannot run multiple clients.
- Administrator rights are recommended. Closing the singleton event inside Roblox clients can fail without them.

Optional, for the system tray (and tray notifications):

```
pip install pystray pillow
```

Or skip Python entirely and use the packaged exe (see **Building the exe**).

---

## Installation

```
git clone https://github.com/devjeo/multiblox.git
cd multiblox
python gui.py
```

Run your terminal as administrator for best results. All modules must stay in the same folder.

---

## Quick start

1. Run `python gui.py`.
2. Click **Add account...** and paste the `.ROBLOSECURITY` cookie for one of your own accounts (see below). Repeat for each account.
3. Click **Save game...** and paste a game link or place ID.
4. Turn on **Multi-instance** in the top bar.
5. Select accounts and click **Launch selected**, or click **Join all accounts**.
6. Turn on auto-rejoin for the accounts you want kept online.

### Getting a cookie for your own account

1. Log in at roblox.com in a private or incognito window.
2. Press F12, open Application, then Cookies, then `https://www.roblox.com`.
3. Copy the value of `.ROBLOSECURITY`.
4. Close the window without clicking "Log out". Logging out invalidates the cookie.

Treat a cookie like a password. Anyone who has it can use that account. Never share it or paste it into a chat or issue.

---

## Settings

| Setting | Default | Purpose |
|---|---|---|
| Auto-enable multi-instance | On | Turn multi-instance on at startup |
| Close to tray | On | Closing the window keeps the app running in the tray |
| Theme | Dark | Dark or light; the moon/sun button in the header switches it |
| Rejoin cooldown | 8 s | Wait before relaunching a disconnected client |
| Max attempts | 5 | Rejoin attempts allowed per window |
| Attempt window | 10 min | Window over which attempts are counted |
| Presence poll | 45 s | How often presence is checked |
| Launch pause | 6 s | Pause after each launch so the event closer can finish |
| Restore sessions | On | Re-attach to surviving clients on startup |
| Rejoin after crash | On | Relaunch dead clients after an unclean exit |
| Mutex names / Event names | Roblox defaults | Comma separated; change if a Roblox update renames them |
| Confirm actions | On | Ask before stopping clients and bulk launches |
| Notify events | disconnect, rejoin_gaveup, cookie_invalid | Which events send a notification |
| Discord / Tray notifications | Off / On | Channels to send to (webhook URL is stored encrypted, not here) |
| Notification cooldown | 120 s | Minimum gap between the same event for the same account (1-3600) |
| Group games | none | Default game per group, set from the right-click menu |
| Performance profile | Normal | Normal, Low or Minimum client flags |
| Apply profile before launch | Off | Write the profile before every launch, rejoin and scheduled start |
| Layout monitor / columns / gap | 0 / 3 / 4 px | Where and how Tile windows arranges clients |
| Auto-tile | Off | Re-tile when a client starts or rejoins |
| Scheduler enabled | On | Run schedule rules (checked every 30 s) |
| Missed-run window | 30 min | How late a missed scheduled run may still fire |
| Macros on | On | Master switch for macro hotkeys (the Macros window has the checkbox; Ctrl+Shift+F12 always stops every macro) |
| Macro max run | 60 min | A macro still running after this long stops by itself (`macroMaxRunMinutes` in settings.json) |

---

## Data and privacy

Everything is stored locally in `%APPDATA%\RobloxAccountManager\`:

| File | Contents |
|---|---|
| `accounts.json` | Accounts with DPAPI-encrypted cookies |
| `places.json` | Saved games |
| `settings.json` | Your settings |
| `sessions.json` | Which account is in which game and client, for recovery (private-server codes are never saved) |
| `secrets.json` | DPAPI-encrypted secrets, currently the Discord webhook URL |
| `schedules.json` | Scheduler rules and when each last ran |
| `macros.json` | Hotkey macros (hotkey, click type, position, repeat, interval, run count, recordings) |
| `resource_backup.json` | Original client-settings backup for Restore original |
| `multi_instance.log`, `rejoin_debug.log` | Size-capped logs (the second only when a disconnect marker fires) |
| `diagnostics_report.txt` | Last saved diagnostics report |

Back up this folder before upgrading; `accounts.json` holds your encrypted cookies. Everything passes through the redactor: launch tickets, cookies, CSRF tokens, Discord webhook URLs, private-server codes and other long token-like strings become `<redacted>`.

Network traffic goes to Roblox's own endpoints, plus the Discord webhook you configure if you turn Discord notifications on. Nothing else is contacted.

---

## Console tools

The GUI is built on modules that also work on their own:

| Command | Purpose |
|---|---|
| `python launcher.py` | Console launcher with `launch`, `joinall`, `autorejoin`, `stop`, `resume`, `status`, `setgroup`, `setnote`, and place commands (select with numbers, names, `all` or `group:<name>`) |
| `python account_store.py` | Console account manager (`add`, `list`, `check`, `remove`) |
| `python multi_instance.py` | Standalone multi-instance holder and process watcher |
| `python diagnostics.py [--offline]` | Print the diagnostics report |
| `python resourcesaver.py apply <profile>` / `restore` | Apply or undo a performance profile |

Do not run `launcher.py` or `multi_instance.py` while the GUI is open. They would compete for the same singleton objects.

---

## Project layout

```
gui.py              Tkinter desktop app
theme.py            Design tokens: spacing, type scale, contrast-checked colours (pure)
ui_style.py         Applies the tokens to ttk (styles, tooltips, menus)
icons.py            Icon set drawn from geometry, encoded as PNG (no image files)
join_dialog.py      Finder / Private / Hopper tabs of the left panel
join_panel.py       Player tab: look up a player, history of joined players, online friends, join their server
join_history.py     Saved list of players you looked up or joined (join_history.json)
server_history.py   Servers accounts got into, all games (server_history.json)
server_history_panel.py  The History tab of the left panel
current_servers_panel.py  The Current tab of the left panel (servers your accounts are in now)
schedule_window.py  Schedule list and rule editor
settings.py         Settings and defaults (no Tk, testable anywhere)
version.py          App version
launcher.py         Auth tickets, launch links, console launcher
autorejoin.py       Disconnect detection and rejoin logic
multi_instance.py   Singleton mutex/event handling and process watcher
account_store.py    DPAPI-encrypted account storage, groups and notes
groups.py           Group filtering and launch-target resolution (pure)
places.py           Saved games and link/ID parsing
sessions.py         Crash and restart recovery
notifier.py         Notification queue, cooldown and filtering
notify_discord.py   Discord webhook channel
notify_tray.py      System tray channel
secrets_store.py    DPAPI-encrypted named secrets
server_finder.py    Public server list and selection
friends.py          User lookup and friend presence
private_links.py    Private-server and share-link parsing (experimental)
resourcesaver.py    Client-flag profiles with backup and restore
winapi.py           Win32 window and monitor access (lazy, Windows only)
winlayout.py        Grid maths and tiling (pure)
scheduler.py        Rule store and due-run logic
macros.py           Macro record, macros.json, hotkey parsing and validation (pure)
macro_engine.py     Runs macros: one worker per macro, hotkey toggle, safeguards, recording control (no Win32)
hotkeys.py          Global hotkeys (RegisterHotKey thread) and a fake for tests
recorder.py         Records mouse and keyboard input for recorded macros (low-level hooks) and a fake for tests
macros_window.py    Macros dashboard and add/edit form
net.py              HTTP with retries and the CSRF handshake (new code)
workers.py          Background worker thread
jsonio.py           Atomic JSON read/write
diagnostics.py      Environment and dependency checks
safelog.py          Secret redaction for logs and reports
RobloxAccountManager.spec   PyInstaller build recipe
installer.iss       Inno Setup script that wraps the exe into a Setup installer
CHANGELOG.md        Release notes
LICENSE             MIT license
test_*.py           Test suites
```

---

## Building the exe

On Windows, in a clean virtual environment with 64-bit Python:

```
pip install pyinstaller pystray pillow
pyinstaller --clean --noconfirm RobloxAccountManager.spec
```

The result is `dist\RobloxAccountManager.exe`. The spec lists every module explicitly because some dialogs are imported on demand. Data and logs go to `%APPDATA%\RobloxAccountManager\`, not next to the exe. Run the exe as administrator for best multi-instance results (or set `uac_admin=True` in the spec). To make a shareable installer, install [Inno Setup 6](https://jrsoftware.org/isinfo.php) and run `ISCC.exe installer.iss` (or open `installer.iss` in Inno Setup and press F9). The result is `installer_output\RobloxAccountManager-Setup-2.0.0.exe`. Uninstalling removes the program but keeps `%APPDATA%\RobloxAccountManager`. Antivirus tools sometimes flag unsigned PyInstaller apps; building from source yourself is the safest way to trust the result.

---

## Testing

The test suite needs neither Windows nor Roblox nor a network. The rejoin manager and scheduler take their clock, sleep, process list, launcher and presence lookup as parameters, so a simulated machine stands in and time moves instantly.

```
python -m unittest discover -p "test_*.py"
```

317 tests cover rejoin behaviour, session recovery, secret redaction, link parsing, the CSRF handshake, groups, notifications, server finding, resource-saver backup and restore, window grid maths, scheduling (including missed runs, midnight and daylight-saving changes), diagnostics, and release hygiene.

---

## Troubleshooting

Open **Diagnostics...** first. It reports which piece broke.

- **Second client replaces the first.** Multi-instance is off, the Roblox singleton names changed, or you are not running as administrator. Diagnostics lists the names present in running clients; add any new ones in Settings.
- **"Microsoft Store version" failure.** Uninstall it and install Roblox from roblox.com.
- **Account shows an expired cookie.** Log in again in a private window, copy a fresh cookie, and use Add account. The existing entry is updated in place.
- **Cannot decrypt a cookie.** The file was saved by a different Windows user or on a different PC. Accounts must be re-added.
- **Auto-rejoin misfires.** Tune the disconnect markers in `autorejoin.py` using the lines saved to `rejoin_debug.log`.
- **No Discord message.** Press Test in Settings. A webhook must be a `discord.com` or `discordapp.com` webhook URL.
- **Resource saver does nothing.** Roblox ignores flags it does not allow. Check Diagnostics for the file it found, and use Restore original to undo.
- **Windows will not tile.** Fullscreen clients are skipped, and clients take a few seconds to create their window.
- **Scheduled run did not start.** Check the rule's days, that the app was running within the missed-run window, and the log for "skipped" lines.

---

## Disclaimer

This is an independent project and is not affiliated with, endorsed by, or sponsored by Roblox Corporation. Use it only with accounts you own. Running multiple clients and automating launches may be restricted by Roblox's Terms of Use, and Roblox updates can break this tool at any time. You are responsible for how you use it.

## License

MIT. See [LICENSE](LICENSE). The license covers this code only; it gives no rights to Roblox or its trademarks.