#!/usr/bin/env python3
"""Remove everything the AWS version of Demo 2 created, so nothing is left behind.

In order:
  1. Smartsheet: delete the plan webhook(s) this demo created (otherwise Smartsheet keeps
     calling an address that no longer exists).
  2. AWS: `cdk destroy` - the listener and sweep Lambdas, the public URL, the DynamoDB table,
     the schedule and the log groups.
  3. AWS: delete the two secrets that were created by hand (no recovery window).
  4. AWS: remove the CDK bootstrap resources that were created for this demo (storage bucket,
     image registry, roles) - skip with --keep-bootstrap if you want to use the CDK again.
  5. Check: list anything in the account still carrying this demo's tag.

It does not touch the Seat Approvals / Log / Leavers sheets in Smartsheet, or any Smartsheet user.

    python teardown.py            # asks you to type 'teardown'
    python teardown.py --yes --keep-bootstrap
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from stack import PROJECT_TAG, TOKEN_SECRET, WEBHOOK_SECRET  # noqa: E402

TOOLKIT_STACK = "CDKToolkit-seatdemo"
WEBHOOK_NAME = "Seat governance demo"          # the default name webhook_admin.py gives the webhook


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def remove_webhooks() -> None:
    """Delete the plan webhook(s) this demo created. Uses plain HTTP: the SDK cannot parse plan webhooks."""
    import requests
    try:
        import seat_common  # noqa: F401  (loads .env and the OS certificate store)
        headers = {"Authorization": f"Bearer {os.environ['SMARTSHEET_ACCESS_TOKEN']}"}
    except (SystemExit, KeyError) as exc:
        print(f"  skipped (Smartsheet token not available: {exc})")
        return
    base = "https://api.smartsheet.com/2.0"
    hooks = requests.get(f"{base}/webhooks?pageSize=100", headers=headers, timeout=30).json().get("data", [])
    mine = [h for h in hooks if h["name"] == WEBHOOK_NAME]
    for h in mine:
        requests.delete(f"{base}/webhooks/{h['id']}", headers=headers, timeout=30)
        print(f"  deleted webhook {h['id']}")
    if not mine:
        print("  no matching webhook found")


def remove_secrets() -> None:
    sm = boto3.client("secretsmanager")
    for name in (TOKEN_SECRET, WEBHOOK_SECRET):
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            print(f"  deleted secret {name}")
        except ClientError as exc:
            print(f"  {name}: {exc.response['Error']['Code']}")


def remove_bootstrap() -> None:
    cfn = boto3.client("cloudformation")
    try:
        resources = cfn.describe_stack_resources(StackName=TOOLKIT_STACK)["StackResources"]
    except ClientError:
        print("  no bootstrap stack found")
        return
    for r in resources:
        if r["ResourceType"] == "AWS::S3::Bucket":
            print(f"  emptying bucket {r['PhysicalResourceId']}")
            boto3.resource("s3").Bucket(r["PhysicalResourceId"]).object_versions.delete()
        if r["ResourceType"] == "AWS::ECR::Repository":
            print(f"  deleting image registry {r['PhysicalResourceId']}")
            try:
                boto3.client("ecr").delete_repository(repositoryName=r["PhysicalResourceId"], force=True)
            except ClientError:
                pass
    cfn.delete_stack(StackName=TOOLKIT_STACK)
    cfn.get_waiter("stack_delete_complete").wait(StackName=TOOLKIT_STACK)
    print(f"  deleted stack {TOOLKIT_STACK}")


def leftovers() -> list[str]:
    tagging = boto3.client("resourcegroupstaggingapi")
    arns = []
    for page in tagging.get_paginator("get_resources").paginate(TagFilters=[{"Key": "Project", "Values": [PROJECT_TAG]}]):
        arns += [r["ResourceARN"] for r in page["ResourceTagMappingList"]]
    return arns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--keep-bootstrap", action="store_true", help="leave the CDK bootstrap resources in place")
    args = ap.parse_args()

    ident = boto3.client("sts").get_caller_identity()
    print(f"AWS account {ident['Account']}, region {boto3.session.Session().region_name}")
    if not args.yes and input("Remove the seat-governance demo from AWS? Type 'teardown': ") != "teardown":
        sys.exit("Aborted.")

    step(1, "Smartsheet webhook")
    remove_webhooks()
    step(2, "CloudFormation stack (cdk destroy)")
    subprocess.run(["cdk", "destroy", "--force", "SeatGovernanceDemo"], cwd=HERE, check=False)
    step(3, "Secrets")
    remove_secrets()
    step(4, "CDK bootstrap resources")
    if args.keep_bootstrap:
        print("  kept (--keep-bootstrap)")
    else:
        remove_bootstrap()
    step(5, "Check for leftovers")
    left = leftovers()
    print("  nothing tagged for this demo remains" if not left else "  STILL PRESENT:\n   " + "\n   ".join(left))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
