"""The seat policy itself: what to do when a user's seat type changes.

This module has no web framework in it on purpose. The same function is called by
  * seat_listener.py   - a small Flask server for running locally (with a tunnel)
  * aws/lambda_listener.py - an AWS Lambda behind a public HTTPS URL
so the decision logic is written, tested and read in exactly one place.

The policy:
    seat type changed
        -> not a billable seat?                     ignore (log it)
        -> user is in the Seat Approvals sheet?     keep the seat (log it)
        -> otherwise                                downgrade to CONTRIBUTOR (log it)
"""
from dataclasses import dataclass
from typing import Callable

import seat_common as sc


@dataclass
class Context:
    """Everything the policy needs, passed in so callers can supply their own pieces."""
    client: object                              # Smartsheet SDK client
    settings: sc.Settings
    apply: bool                                 # False = dry run: log decisions, change nothing
    # Returns True if this event was already handled (and remembers it if not). Smartsheet can
    # deliver the same event more than once. Local runs use a set in memory; AWS uses DynamoDB.
    seen_before: Callable[[tuple], bool]


def handle_seat_event(ctx: Context, event: dict) -> str | None:
    """Apply the seat policy to one user.seatType.updated event.

    Returns the decision that was logged (for tests and callers), or None if the event was a
    duplicate that was ignored.
    """
    user_id = event["userId"]
    email = event.get("emailAddress", "")
    seat = sc.normalize_seat(event.get("seatType"))

    # De-duplicate: the same event can arrive twice.
    if ctx.seen_before((user_id, seat, event.get("timestamp"))):
        return None

    def record(decision: str, reason: str, result: str = "") -> str:
        print(f"{email}: {seat} -> {decision} ({reason})")
        sc.log_event(ctx.client, ctx.settings, source=event.get("_source", "Webhook"), email=email,
                     trigger="Seat type changed", seat_type=seat, decision=decision,
                     reason=reason, dry_run=not ctx.apply, result=result)
        return decision

    # A change *to* a free seat needs no governance. This also stops an infinite loop:
    # our own downgrade fires another seat-type event, which lands here and is ignored.
    if seat not in sc.BILLABLE:
        return record("Skipped", f"{seat} is not a billable seat")

    # Already approved? Then the seat is legitimate; leave it alone.
    approved = sc.approved_emails(ctx.client, ctx.settings)
    if email.lower() in approved:
        return record("Approved - kept", "listed in Seat Approvals")

    # Not approved: check we are allowed to touch this account at all, then downgrade.
    user = sc.find_user(ctx.client, user_id=user_id)
    why_not = sc.check_target(ctx.client, ctx.settings, user)
    if why_not:
        return record("Skipped", why_not)

    if not ctx.apply:
        return record("Downgraded", f"no approval on file; would downgrade to {sc.DOWNGRADE_TO}")

    ctx.client.Users.downgrade_user(user_id, ctx.settings.plan_id, sc.DOWNGRADE_TO)
    return record("Downgraded", f"no approval on file; downgraded to {sc.DOWNGRADE_TO}", "OK")


def handle_plan_callback(ctx: Context, body: dict) -> int:
    """Process a plan webhook body: run the policy for every seat-type event in it.

    Returns how many events were handled. Errors on one event are printed, never raised, so
    one bad event cannot make us answer with an error (which would make Smartsheet retry).
    """
    handled = 0
    for event in body.get("events", []):
        if event.get("objectType") == "user.seatType" and event.get("eventType") == "updated":
            try:
                if handle_seat_event(ctx, event) is not None:
                    handled += 1
            except Exception as exc:
                print(f"error handling event: {exc!r}")
    return handled
