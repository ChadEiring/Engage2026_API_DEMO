# Smartsheet API demos: From One to Many

Sample code from the ENGAGE 2026 session **From One to Many: Solving Scale with the Smartsheet API**.
Three small, working automations that all follow the same pattern: **read, decide, write**, on a schedule
or on an event.

| # | Demo | Language | What it shows |
|---|---|---|---|
| 1 | [`erp_demo`](erp_demo) | Python (Smartsheet SDK) | Keep a sheet in sync with an ERP extract: match on an ID, update what changed, add what is new, export for BI. |
| 2 | [`seat_governance_demo`](seat_governance_demo) | Python, optional AWS | React to seat-type changes with a plan webhook, and sweep for inactive users and leavers. |
| 3 | [`security_audit_demo`](security_audit_demo) | Node.js (no dependencies) | Audit the whole org for external shares and public links, and track the weekly diff. |

Each folder has its own README with setup, run steps and troubleshooting. Start with the one that matches
your problem; they do not depend on each other.

## Before you run anything

* **Use a test or sandbox Smartsheet org.** Demo 2 can downgrade, deactivate and permanently remove users,
  and Demo 3 reads sharing for every sheet in the org. Both need a **System Admin** API token.
* **Keep your token secret.** Each demo reads it from a git-ignored `.env` file (copy `.env.example`).
  Never commit a real token.
* **Safe by default.** The demos that can change accounts run as a dry run unless you opt in, and only act on
  an explicit allowlist of test users.
* The mock data (customers, orders) is fictional.

## Why these demos use the API directly

The Smartsheet API is HTTPS and JSON. Demos 1 and 2 use the official Python SDK where it fits, and Demo 3
uses plain `fetch` to show there is nothing magic underneath. Each README notes the places where the SDK
needed a workaround (for example, it cannot create plan-level webhooks).

## Disclaimer

This is sample code for learning, provided as is, without warranty or support. Review it and test it in a
non-production environment before adapting it.

API reference: https://developers.smartsheet.com/api/smartsheet

## License

[MIT](LICENSE)
