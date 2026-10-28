#!/usr/bin/env python3
"""CDK app: deploys the seat-governance demo to AWS.

    cd aws && ./build.sh                       # package the Lambda code
    cdk bootstrap --toolkit-stack-name CDKToolkit-seatdemo --qualifier seatdemo   (once)
    cdk deploy                                 # dry-run listener + sweep
    cdk deploy -c apply=true                   # listener really downgrades
    python teardown.py                         # remove everything, including the bootstrap

Plain configuration (plan ID, sheet IDs, allowlist) is read from ../.env at synth time. The
Smartsheet API token is NOT: it lives in AWS Secrets Manager and is never written into the stack.
"""
import sys
from pathlib import Path

import aws_cdk as cdk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import demo_env  # noqa: E402

demo_env.load(Path(__file__).resolve().parent.parent / ".env")

from stack import SeatGovernanceStack  # noqa: E402

app = cdk.App()
SeatGovernanceStack(
    app,
    "SeatGovernanceDemo",
    # Own bootstrap qualifier, so the bootstrap resources are clearly ours and can be removed
    # later without touching anything else in the account.
    synthesizer=cdk.DefaultStackSynthesizer(qualifier="seatdemo"),
)
app.synth()
