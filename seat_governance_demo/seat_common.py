"""Shared settings, safety checks and Smartsheet helpers for the seat-governance demo.

Every script in this folder (listener, sweep, simulator, reset) imports from here, so the
rules about *who may be acted on* and *how decisions are logged* live in exactly one place.

Configuration comes from environment variables, normally via a git-ignored ".env" file
(see .env.example). Nothing secret is stored in code.
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import smartsheet

import demo_env

demo_env.load()   # read .env if present; real environment variables take priority

# Use the operating system's certificate store (needed behind TLS-inspecting proxies).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Seat types
# ---------------------------------------------------------------------------

# Seat types that cost a licence. These are the ones governance cares about.
BILLABLE = {"MEMBER", "PROVISIONAL_MEMBER"}

# Seat type a user is downgraded to. CONTRIBUTOR is the free seat.
DOWNGRADE_TO = "CONTRIBUTOR"


def normalize_seat(seat: str | None) -> str:
    """Map legacy seat names to current ones.

    VIEWER is deprecated and becomes CONTRIBUTOR on 2026-10-30, so during the transition the
    API may return either. Treating them as the same value keeps the policy correct both sides
    of that date.
    """
    # The SDK returns seat types as enum objects (with a .name), the webhook as plain strings.
    seat = str(getattr(seat, "name", seat) or "").upper()
    return "CONTRIBUTOR" if seat == "VIEWER" else seat


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    plan_id: int
    approvals_sheet_id: int
    log_sheet_id: int
    leavers_sheet_id: int
    # Only these emails can ever be downgraded, deactivated or removed. This is the main
    # safety rail: the org has many real users, the demo only touches the ones listed here.
    allowlist: set[str] = field(default_factory=set)
    service_account_id: int | None = None   # receives assets when a leaver is removed
    inactive_days: int = 45
    max_actions: int = 5                    # cap on destructive actions per run
    shared_secret: str | None = None        # optional: verify webhook callback signatures


def _int(name: str, required: bool = True) -> int | None:
    v = os.getenv(name, "").strip()
    if not v:
        if required:
            sys.exit(f"Set {name} in .env (see .env.example)")
        return None
    return int(v)


def load_settings() -> Settings:
    """Read and validate configuration. Exits with a clear message if something is missing."""
    if not os.getenv("SMARTSHEET_ACCESS_TOKEN"):
        sys.exit("Set SMARTSHEET_ACCESS_TOKEN in .env (a System Admin's API token)")
    allow = {e.strip().lower() for e in os.getenv("ALLOWLIST_EMAILS", "").split(",") if e.strip()}
    return Settings(
        plan_id=_int("PLAN_ID"),
        approvals_sheet_id=_int("APPROVALS_SHEET_ID"),
        log_sheet_id=_int("LOG_SHEET_ID"),
        leavers_sheet_id=_int("LEAVERS_SHEET_ID"),
        allowlist=allow,
        service_account_id=_int("SERVICE_ACCOUNT_USER_ID", required=False),
        inactive_days=_int("INACTIVE_DAYS", required=False) or 45,
        max_actions=_int("MAX_ACTIONS", required=False) or 5,
        shared_secret=os.getenv("WEBHOOK_SHARED_SECRET") or None,
    )


def get_client() -> smartsheet.Smartsheet:
    """SDK client. Reads SMARTSHEET_ACCESS_TOKEN itself; errors raise instead of returning."""
    client = smartsheet.Smartsheet()
    client.errors_as_exceptions(True)
    return client


# ---------------------------------------------------------------------------
# Safety: who may we act on?
# ---------------------------------------------------------------------------

def find_user(client, *, user_id: int | None = None, email: str | None = None):
    """Look a user up by ID or by email. Returns None if there is no such user."""
    try:
        if user_id is not None:
            return client.Users.get_user(int(user_id))
        found = client.Users.list_users(email=email).data
        return found[0] if found else None
    except smartsheet.exceptions.ApiError:
        return None


def raw_status(user_id: int) -> str:
    """A user's account status straight from the API ("ACTIVE", "PENDING", "DEACTIVATED", ...).

    The SDK's status field cannot represent every value: for a deactivated user it comes back as
    None (in lists) or an object that raises KeyError when compared with "DEACTIVATED". Reading
    the plain JSON avoids both problems.
    """
    import requests
    r = requests.get(f"https://api.smartsheet.com/2.0/users/{int(user_id)}", timeout=30,
                     headers={"Authorization": f"Bearer {os.environ['SMARTSHEET_ACCESS_TOKEN']}"})
    return r.json().get("status", "") if r.ok else ""


def check_target(client, settings: Settings, user) -> str | None:
    """Return None if it is safe to act on this user, otherwise the reason we must not.

    A destructive action (downgrade, deactivate, remove) only goes ahead when ALL of these hold:
      * the user exists
      * their email is on the allowlist
      * they are not a System Admin
      * they are not the owner of the API token we are running with
    """
    if user is None:
        return "user not found"
    email = (user.email or "").lower()
    if email not in settings.allowlist:
        return "not on the allowlist (ALLOWLIST_EMAILS)"
    if user.admin:
        return "System Admin accounts are never touched"
    me = client.Users.get_current_user()
    if user.id_ == me.id_:
        return "this is the account running the script"
    return None


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _columns(client, sheet_id: int) -> dict[str, int]:
    """Column title -> column ID for a sheet (the API addresses columns by ID)."""
    sheet = client.Sheets.get_sheet(sheet_id, page_size=1)
    return {c.title: c.id_ for c in sheet.columns}


def rows_as_dicts(client, sheet_id: int) -> list[dict]:
    """Read a sheet into a list of {column title: value} dicts, plus the row ID as '_row_id'."""
    sheet = client.Sheets.get_sheet(sheet_id)
    titles = {c.id_: c.title for c in sheet.columns}
    out = []
    for row in sheet.rows:
        d = {titles[c.column_id]: c.value for c in row.cells}
        d["_row_id"] = row.id_
        out.append(d)
    return out


def approved_emails(client, settings: Settings) -> dict[str, str]:
    """Emails currently approved for a licensed seat -> the seat type they are approved for.

    This is the "approval queue" from the slide. It is read fresh on every event, so editing
    the Seat Approvals sheet changes the policy immediately, with no redeploy.
    """
    out = {}
    for r in rows_as_dicts(client, settings.approvals_sheet_id):
        if r.get("Email"):
            out[str(r["Email"]).strip().lower()] = r.get("Approved Seat Type") or "MEMBER"
    return out


def log_event(client, settings: Settings, *, source: str, email: str, trigger: str,
              seat_type: str, decision: str, reason: str, dry_run: bool, result: str = "") -> None:
    """Append one decision to the Seat Governance Log sheet (newest on top).

    The log is the audit trail: who, what triggered it, what we decided, why, and what the
    API said. In dry-run mode the row is still written (with the Dry Run box ticked) so you
    can see exactly what *would* have happened.
    """
    cols = _columns(client, settings.log_sheet_id)
    values = {
        "Event Time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Source": source, "Email": email, "Trigger": trigger, "Seat Type": seat_type,
        "Decision": decision, "Reason": reason, "Dry Run": dry_run, "API Result": result,
    }
    row = smartsheet.models.Row()
    row.to_top = True
    for title, value in values.items():
        cell = smartsheet.models.Cell()
        cell.column_id = cols[title]
        cell.value = value
        row.cells.append(cell)
    client.Sheets.add_rows(settings.log_sheet_id, [row])
