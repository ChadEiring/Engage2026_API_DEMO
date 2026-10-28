#!/usr/bin/env python3
"""Send a fake plan webhook to your local listener, for rehearsal and as a demo fallback.

Real seat-type events only arrive when someone really changes a seat and the webhook is
live. This script posts the same kind of JSON body Smartsheet would send, straight to
seat_listener.py, so you can:
  * develop and test the policy without a public tunnel or a real webhook
  * rehearse the demo, or recover if the live webhook misbehaves while recording
  * let attendees without System Admin rights try the pattern (use --user-id and --email)

Examples:
    python simulate_event.py --email test.user@example.com --seat-type MEMBER
    python simulate_event.py --challenge abc123       # test the verification handshake only
    python simulate_event.py --email x@y.com --user-id 123 --url https://<tunnel>/webhook

The listener decides what to do exactly as it would for a real event. With the listener in
dry-run mode (its default) nothing is changed in Smartsheet except the log sheet.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

import seat_common as sc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8080/webhook", help="listener URL")
    ap.add_argument("--email", help="user the event is about")
    ap.add_argument("--user-id", type=int, help="their user ID (looked up from the email if omitted)")
    ap.add_argument("--seat-type", default="MEMBER",
                    choices=["MEMBER", "PROVISIONAL_MEMBER", "CONTRIBUTOR", "VIEWER", "GUEST"])
    ap.add_argument("--challenge", help="send a verification challenge instead of an event")
    args = ap.parse_args()

    # Verification handshake: the listener must echo the challenge in a response header.
    if args.challenge:
        r = requests.post(args.url, json={"challenge": args.challenge}, timeout=15)
        echoed = r.headers.get("Smartsheet-Hook-Response")
        print(f"{r.status_code}  echoed={echoed!r}  -> {'PASS' if echoed == args.challenge else 'FAIL'}")
        return 0 if echoed == args.challenge else 1

    if not args.email:
        sys.exit("--email is required (or use --challenge)")

    settings = sc.load_settings()
    user_id = args.user_id
    if user_id is None:
        user = sc.find_user(sc.get_client(), email=args.email)
        if user is None:
            sys.exit(f"No user with email {args.email}; pass --user-id to send anyway")
        user_id = user.id_

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # Same shape as a real plan callback (fields per Smartsheet's plan webhook documentation).
    payload = {
        "scope": "plan",
        "scopeObjectId": settings.plan_id,
        "webhookId": 0,
        "newWebhookStatus": "ENABLED",
        "nonce": f"sim-{int(time.time() * 1000)}",
        "timestamp": now,
        "events": [{
            "objectType": "user.seatType",
            "eventType": "updated",
            "planId": settings.plan_id,
            "userId": user_id,
            "emailAddress": args.email,
            "seatType": args.seat_type,
            "isInternal": True,
            "lastBillableActivity": now,
            "timestamp": now,
            "updatedByUserId": user_id,
            "_source": "Simulator",    # not a real field: lets the log show this was a rehearsal
        }],
    }
    # If the listener verifies signatures (a webhook shared secret is set), sign the body the way
    # Smartsheet does. Put the secret in WEBHOOK_SHARED_SECRET to rehearse against such a listener.
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.getenv("WEBHOOK_SHARED_SECRET")
    if secret:
        headers["Smartsheet-Hmac-SHA256"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = requests.post(args.url, data=body, headers=headers, timeout=60)
    print(f"{r.status_code} {r.text.strip()}   (check the Seat Governance Log sheet)")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
