#!/usr/bin/env python3
"""Create, enable, inspect and delete the plan-level webhook that feeds the seat listener.

A plan webhook tells Smartsheet: "send events about this plan to my URL". Creating one requires a
System Admin token. The steps, in order:

    python webhook_admin.py create --url https://<your-address>/webhook
    python webhook_admin.py enable <webhook id>     # triggers the verification handshake
    python webhook_admin.py list
    python webhook_admin.py disable <webhook id>
    python webhook_admin.py delete <webhook id>

"enable" only succeeds if the listener (seat_listener.py locally, or the AWS Lambda) is running and
reachable at the URL, because Smartsheet verifies the endpoint first.

This script talks to the API with plain HTTP requests rather than the Smartsheet Python SDK: the
SDK's webhook model only supports sheet-level webhooks (scope "sheet"), and a plan webhook has
scope "plan".

The create response contains a "sharedSecret" used to sign every callback. It is never printed.
Pass --store-secret NAME to save it to AWS Secrets Manager (for the Lambda), or --save-env to
append it to .env as WEBHOOK_SHARED_SECRET (for the local listener).
"""
import argparse
import os
import sys
from pathlib import Path

import requests

import seat_common as sc

BASE = "https://api.smartsheet.com/2.0"
EVENTS = ["user.seatType.updated"]       # "*.*" would subscribe to every plan event
NAME = "Seat governance demo"            # teardown.py looks for this name


def call(method: str, path: str, body: dict | None = None):
    """One API call. Raises with Smartsheet's error message if it fails."""
    r = requests.request(method, BASE + path, json=body, timeout=30, headers={
        "Authorization": f"Bearer {os.environ['SMARTSHEET_ACCESS_TOKEN']}",
        "smartsheet-integration-source": "APPLICATION,SeatGovernanceDemo,webhook_admin",
    })
    data = r.json() if r.content else {}
    if not r.ok:
        sys.exit(f"{method} {path} -> {r.status_code}: {data.get('message', data)}")
    return data


def show(h: dict) -> None:
    print(f"  id={h['id']}  name={h['name']!r}  scope={h['scope']}  status={h['status']}  enabled={h['enabled']}")
    print(f"  callback={h['callbackUrl']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create the webhook (starts disabled)")
    c.add_argument("--url", required=True, help="public HTTPS URL of the listener's /webhook")
    c.add_argument("--name", default=NAME)
    c.add_argument("--store-secret", metavar="NAME", help="save the shared secret in AWS Secrets Manager under NAME")
    c.add_argument("--save-env", action="store_true", help="append the shared secret to .env as WEBHOOK_SHARED_SECRET")
    for name in ("enable", "disable", "delete"):
        sub.add_parser(name).add_argument("webhook_id", type=int)
    sub.add_parser("list")
    args = ap.parse_args()

    settings = sc.load_settings()

    if args.cmd == "list":
        hooks = call("GET", "/webhooks?pageSize=100").get("data", [])
        print(f"{len(hooks)} webhook(s)")
        for h in hooks:
            show(h)

    elif args.cmd == "create":
        hook = call("POST", "/webhooks", {
            "name": args.name, "callbackUrl": args.url, "scope": "plan",
            "scopeObjectId": settings.plan_id, "events": EVENTS, "version": 1,
        })["result"]
        print("created (disabled, not yet verified):")
        show(hook)
        secret = hook.get("sharedSecret")
        print(f"shared secret received: {bool(secret)} (not shown)")
        if secret and args.store_secret:
            import boto3
            from botocore.exceptions import ClientError
            sm = boto3.client("secretsmanager")
            tags = [{"Key": "Project", "Value": "engage-2026-seat-governance-demo"}]
            try:
                sm.create_secret(Name=args.store_secret, SecretString=secret, Tags=tags)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceExistsException":
                    raise
                sm.put_secret_value(SecretId=args.store_secret, SecretString=secret)
            print(f"stored in Secrets Manager as {args.store_secret}")
        if secret and args.save_env:
            with open(Path(__file__).parent / ".env", "a") as f:
                f.write(f"WEBHOOK_SHARED_SECRET={secret}\n")
            print("appended WEBHOOK_SHARED_SECRET to .env")
        print(f"next: python webhook_admin.py enable {hook['id']}")

    elif args.cmd in ("enable", "disable"):
        show(call("PUT", f"/webhooks/{args.webhook_id}", {"enabled": args.cmd == "enable"})["result"])

    elif args.cmd == "delete":
        call("DELETE", f"/webhooks/{args.webhook_id}")
        print("deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
