"""
Account groups: small pure helpers (no Tk, no Windows, no network).

An account's group is just a text label (Account.group). These helpers list the
groups in use, pick the accounts that belong to one, and decide which game a
"launch group" action should use.

Group names are compared case-insensitively ("Farm" and "farm" are one group).
"""

from __future__ import annotations

ALL = "All groups"  # the filter value that means "no filtering"


def names(accounts) -> list[str]:
    """Group names in use, sorted, one spelling per case-insensitive name."""
    seen: dict[str, str] = {}
    for a in accounts:
        group = (a.group or "").strip()
        if group and group.lower() not in seen:
            seen[group.lower()] = group
    return sorted(seen.values(), key=str.lower)


def filter_accounts(accounts, group: str | None) -> list:
    """Accounts in `group`. None, an empty string or ALL means every account."""
    if not group or group == ALL:
        return list(accounts)
    wanted = group.strip().lower()
    return [a for a in accounts if (a.group or "").strip().lower() == wanted]


def normalize_group_games(raw) -> tuple[dict[str, tuple[int, str | None]], list[str]]:
    """Validates the settings value {"farm": {"placeId": 123, "jobId": None}}.
    Returns ({lowercase group name: (place_id, job_id)}, [problem descriptions]).
    Bad entries are skipped and reported, never raised."""
    clean: dict[str, tuple[int, str | None]] = {}
    problems: list[str] = []
    if not isinstance(raw, dict):
        return clean, problems
    for name, entry in raw.items():
        label = name.strip() if isinstance(name, str) else ""
        if not label:
            problems.append("A saved group game has an empty group name.")
            continue
        place = entry.get("placeId") if isinstance(entry, dict) else None
        if isinstance(place, bool) or not isinstance(place, int) or place <= 0:
            problems.append(f"The saved game for group '{label}' has no valid place ID.")
            continue
        job = entry.get("jobId")
        clean[label.lower()] = (place, job if isinstance(job, str) and len(job) >= 8 else None)
    return clean, problems


def resolve_target(group, ui_place_id, ui_job_id, group_games, accounts) -> tuple[int, str | None] | None:
    """Which (place_id, job_id) a group launch should use, or None if there is none:
      1. the game chosen in the UI, if any;
      2. the group's saved default game (settings "groupGames");
      3. the place a member account last played."""
    if ui_place_id:
        return int(ui_place_id), ui_job_id
    defaults, _problems = normalize_group_games(group_games)
    key = (group or "").strip().lower()
    if key in defaults:
        return defaults[key]
    for account in filter_accounts(accounts, group):
        if account.savedPlaceId:
            return int(account.savedPlaceId), None
    return None