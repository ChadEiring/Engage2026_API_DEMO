"""The AWS resources for the seat-governance demo.

    Smartsheet --HTTPS--> Lambda Function URL --> listener Lambda --> Smartsheet API
                                                       |
                                                       +--> DynamoDB (duplicate-event filter)
    EventBridge schedule (nightly) --> sweep Lambda --> Smartsheet API
    Secrets Manager: API token, webhook shared secret

Everything is tagged and set to be deleted with the stack, so `cdk destroy` leaves nothing behind
(the two secrets are created by hand and removed by teardown.py).
"""
import os

from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack, Tags,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

# Names of the two secrets (created by hand, see README). Same names are used by teardown.py.
TOKEN_SECRET = "seatdemo/smartsheet-token"
WEBHOOK_SECRET = "seatdemo/webhook-shared-secret"
PROJECT_TAG = "engage-2026-seat-governance-demo"

# Plain (non-secret) settings copied from .env into the Lambda environment.
PASS_THROUGH = ["PLAN_ID", "APPROVALS_SHEET_ID", "LOG_SHEET_ID", "LEAVERS_SHEET_ID",
                "ALLOWLIST_EMAILS", "SERVICE_ACCOUNT_USER_ID", "INACTIVE_DAYS", "MAX_ACTIONS"]


class SeatGovernanceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Safe by default: both functions only LOG decisions unless you deploy with these flags.
        apply_listener = str(self.node.try_get_context("apply") or "false").lower()
        apply_sweep = str(self.node.try_get_context("sweep_apply") or "false").lower()

        token = secretsmanager.Secret.from_secret_name_v2(self, "Token", TOKEN_SECRET)
        webhook_secret = secretsmanager.Secret.from_secret_name_v2(self, "WebhookSecret", WEBHOOK_SECRET)

        # Remembers recent events so a repeated delivery is only acted on once. Items expire by themselves.
        dedupe = dynamodb.Table(
            self, "Dedupe",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        env = {k: os.environ[k] for k in PASS_THROUGH if os.environ.get(k)}
        env["TOKEN_SECRET_ID"] = TOKEN_SECRET
        code = lambda_.Code.from_asset("build/lambda")        # produced by build.sh
        common = dict(
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            code=code,
            memory_size=256,
        )

        listener = lambda_.Function(
            self, "Listener",
            handler="lambda_listener.handler",
            timeout=Duration.seconds(30),
            environment={**env, "APPLY": apply_listener, "WEBHOOK_SECRET_ID": WEBHOOK_SECRET,
                         "DEDUPE_TABLE": dedupe.table_name},
            log_group=logs.LogGroup(self, "ListenerLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=RemovalPolicy.DESTROY),
            **common,
        )
        dedupe.grant_read_write_data(listener)
        token.grant_read(listener)
        webhook_secret.grant_read(listener)

        # The public HTTPS address Smartsheet calls. Anyone can reach it, which is why the handler
        # checks Smartsheet's signature on every event.
        url = listener.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        sweep = lambda_.Function(
            self, "Sweep",
            handler="lambda_sweep.handler",
            timeout=Duration.minutes(10),
            environment={**env, "SWEEP_APPLY": apply_sweep},
            log_group=logs.LogGroup(self, "SweepLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=RemovalPolicy.DESTROY),
            **common,
        )
        token.grant_read(sweep)

        # 09:00 UTC = 2am Pacific, every night.
        events.Rule(self, "Nightly", schedule=events.Schedule.cron(minute="0", hour="9"),
                    targets=[targets.LambdaFunction(sweep)])

        # Tags make it easy to find everything this demo created, and show who owns it.
        Tags.of(self).add("Project", PROJECT_TAG)
        Tags.of(self).add("Owner", os.environ.get("OWNER_TAG", "demo-presenter"))   # set OWNER_TAG in .env
        Tags.of(self).add("Purpose", "Engage 2026 conference demo")
        Tags.of(self).add("TeardownBy", str(self.node.try_get_context("teardown_by") or "after-engage-2026"))

        CfnOutput(self, "WebhookCallbackUrl", value=url.url + "webhook",
                  description="Use this as --url when creating the Smartsheet webhook")
        CfnOutput(self, "ListenerFunction", value=listener.function_name)
        CfnOutput(self, "SweepFunction", value=sweep.function_name)
