# Roblox Account Manager

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

**Diagnostics**
- One button checks everything the app depends on: Python bitness, administrator rights, data files, DPAPI, Roblox install (including the Microsoft Store version, which cannot multi-instance), log naming, Roblox web endpoints, multi-instance state, the Roblox mutex and event names present in running clients, and every saved cookie.
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

Optional, for the system tray:

```
pip install pystray pillow
```

---

## Installation

```
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
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
| Rejoin cooldown | 8 s | Wait before relaunching a disconnected client |
| Max attempts | 5 | Rejoin attempts allowed per window |
| Attempt window | 10 min | Window over which attempts are counted |
| Presence poll | 45 s | How often presence is checked |
| Launch pause | 6 s | Pause after each launch so the event closer can finish |
| Restore sessions | On | Re-attach to surviving clients on startup |
| Rejoin after crash | On | Relaunch dead clients after an unclean exit |
| Mutex names / Event names | Roblox defaults | Comma separated; change if a Roblox update renames them |

---

## Data and privacy

Everything is stored locally in `%APPDATA%\RobloxAccountManager\`:

| File | Contents |
|---|---|
| `accounts.json` | Accounts with DPAPI-encrypted cookies |
| `places.json` | Saved games |
| `settings.json` | Your settings |
| `sessions.json` | Which account is in which game and client, for recovery |

Log files (`multi_instance.log`, and `rejoin_debug.log` when a disconnect marker fires) are written next to the scripts, size-capped, and pass through the redactor. Launch tickets, cookies, CSRF tokens, and other long token-like strings are replaced with `<redacted>`.

The only network traffic goes to Roblox's own endpoints. Nothing is sent anywhere else.

---

## Console tools

The GUI is built on modules that also work on their own:

| Command | Purpose |
|---|---|
| `python launcher.py` | Console launcher with `launch`, `joinall`, `autorejoin`, `stop`, `resume`, `status`, and place commands |
| `python account_store.py` | Console account manager (`add`, `list`, `check`, `remove`) |
| `python multi_instance.py` | Standalone multi-instance holder and process watcher |
| `python diagnostics.py [--offline]` | Print the diagnostics report |

Do not run `launcher.py` or `multi_instance.py` while the GUI is open. They would compete for the same singleton objects.

---

## Project layout

```
gui.py            Tkinter desktop app
launcher.py       Auth tickets, launch links, console launcher
autorejoin.py     Disconnect detection and rejoin logic
multi_instance.py Singleton mutex/event handling and process watcher
account_store.py  DPAPI-encrypted account storage
places.py         Saved games and link/ID parsing
sessions.py       Crash and restart recovery
diagnostics.py    Environment and dependency checks
safelog.py        Secret redaction for logs and reports
test_phase6.py    Test suite
```

---

## Testing

The test suite needs neither Windows nor Roblox. The rejoin manager takes its clock, sleep, process list, launcher, and presence lookup as parameters, so a simulated machine stands in and time moves instantly.

```
python -m unittest test_phase6 -v
```

41 tests cover rejoin behavior (retry limits, same-server-first, cancellation, many accounts crashing at once, teleport handling), session recovery, secret redaction, link parsing, the CSRF handshake, log matching, and diagnostics.

---

## Troubleshooting

Open **Diagnostics...** first. It reports which piece broke.

- **Second client replaces the first.** Multi-instance is off, the Roblox singleton names changed, or you are not running as administrator. Diagnostics lists the names present in running clients; add any new ones in Settings.
- **"Microsoft Store version" failure.** Uninstall it and install Roblox from roblox.com.
- **Account shows an expired cookie.** Log in again in a private window, copy a fresh cookie, and use Add account. The existing entry is updated in place.
- **Cannot decrypt a cookie.** The file was saved by a different Windows user or on a different PC. Accounts must be re-added.
- **Auto-rejoin misfires.** Tune the disconnect markers in `autorejoin.py` using the lines saved to `rejoin_debug.log`.

---

## Disclaimer

This is an independent project and is not affiliated with, endorsed by, or sponsored by Roblox Corporation. Use it only with accounts you own. Running multiple clients and automating launches may be restricted by Roblox's Terms of Use, and Roblox updates can break this tool at any time. You are responsible for how you use it.

## License

Add your license here (for example MIT) and a `LICENSE` file to the repository.
