# Demo 3: tenant-wide security audit (Node.js)

Sample code from **From One to Many: Solving Scale with the Smartsheet API** (Scenario 3:
"Who shared what outside the company?").

One script walks every sheet in your Smartsheet organisation, finds **external shares** and
**public links**, rates the risk, and writes the result to a Smartsheet sheet. Run it weekly
and each finding is marked **New**, **Unchanged** or **Resolved**, giving you a baseline you can
show an auditor and a trend line of exposure.

```
list every sheet -> read shares + publish settings -> flag external / public -> diff vs last run
                 -> Findings sheet (New / Unchanged / Resolved) + one summary row per run
```

Plain Node.js, **no npm packages**: it uses the built-in `fetch`. The Smartsheet API is just
HTTPS and JSON; the client in `src/smartsheet.js` is about 150 lines, so you can read all of it.

Endpoints used: `GET /users/sheets`, `GET /shares`, `GET /sheets/{id}/publish`, `GET /users`,
and `POST` / `PUT /sheets/{id}/rows` to write results.

## Files

| File | Purpose |
|---|---|
| `src/audit.js` | The audit. Start here; it is commented step by step. |
| `src/classify.js` | Rules: what counts as external, public, and how risky. Edit to match your policy. |
| `src/findings.js` | The weekly diff and the sheet read/write helpers. |
| `src/smartsheet.js` | Tiny API client: auth, rate limiting, retries, paging, big-ID handling. |
| `src/reset.js` | Empties the result sheets between recordings. |
| `test/` | Unit tests for the rules, the diff and the ID handling (`npm test`). |

## Safe by design

* It only **reads** your sheets, shares and publish settings. The only things it writes are the
  two audit sheets.
* `--dry-run` shows everything it found and writes nothing.
* It stays under the API limit (300 requests per minute) and retries when told to slow down.
* A finding is only marked *Resolved* if its sheet was actually inspected this run.

## 1. Set up

You need a **System Admin** API token and two sheets.

**Security Audit Findings** (all columns text unless noted): Finding Key *(primary)*, Sheet Name,
Sheet ID, Owner, Finding Type *(dropdown: External share, Public link)*, Detail, Access Level,
Risk *(dropdown: High, Medium, Low)*, Status *(dropdown: Open, Resolved)*, Change *(dropdown:
New, Unchanged, Resolved)*, First Seen *(date)*, Last Seen *(date)*, Resolved On *(date)*.

**Audit Runs**: Run Time *(primary)*, Sheets Scanned, Sheets With Findings, External Shares,
Public Links, High Risk, New Findings, Resolved Findings, Publish Not Checked.

**Exposure Summary** *(optional, feeds the dashboard)*: Owner *(primary)*, High, Medium, Low, Total.
The audit keeps it up to date (one row per owner) because dashboard charts plot cells; they do not
count rows. Set `EXPOSURE_SHEET_ID` to switch this on.

```bash
cp .env.example .env     # add your token and the sheet IDs
```

Needs Node 20.12 or newer. There is nothing to install.

## 2. Run it

```bash
node src/audit.js --dry-run          # see the findings, write nothing
node src/audit.js                    # write findings + a run summary
node src/audit.js --limit 20         # just the first 20 sheets (quick rehearsal)
```

Run it again straight away and everything is *Unchanged* with 0 new. Remove someone's access to a
sheet (or unpublish it), run again, and that finding flips to *Resolved*.

Start over (empties the Findings, Audit Runs and Exposure Summary sheets, so the audit fills them
again on camera): `node src/reset.js`.

Options: `--no-publish` (skip publish checks, about twice as fast), `--concurrency N`,
`--impersonate` (see below).

## The dashboard

Build a Smartsheet dashboard on the three result sheets: exposure by owner (stacked by risk), findings
by risk, findings by New / Unchanged / Resolved, and the run trend from Audit Runs (external shares and
high risk per run). Because the charts read the sheets, the dashboard updates every time the audit runs.

## What "external" means

Anyone with access to a sheet who is **not a member of your Smartsheet organisation** is external.
The script reads your org's user list once, so members on several email domains are handled
correctly. Note the flip side: an address on your company's domain that is *not* an org member
(for example a former employee who still holds a share) is reported, which is usually what a
security review wants. Add extra internal domains with `INTERNAL_DOMAINS`, and trusted partners you do not
want flagged with `ALLOWED_EXTERNAL_DOMAINS`.

Risk: an outsider who can edit or share is **High**; a viewer or commenter is **Medium**. A public
link anyone can edit is **High**; a view-only public link is **Medium**; a public iCal link is
**Low**. Links limited to your organisation are not public and are not reported.

## Limits worth knowing

* **Publish status needs access to the sheet.** A System Admin can list the shares of *every*
  sheet in the org, but can only read publish settings for sheets they can open. The run summary
  reports how many sheets could not be checked ("Publish Not Checked"). With `--impersonate` the
  script retries as the sheet's owner using System Admin impersonation (the `Assume-User` header);
  those calls are made as that user, so use it deliberately.
* **The share list lags behind changes.** `GET /shares` is eventually consistent: in testing, a removed
  share kept appearing for about 100 seconds. A finding therefore flips to *Resolved* on the first run
  *after* the lag, not instantly. Wait a couple of minutes between removing access and re-running when
  you demo this.
* **Group shares are counted, not expanded.** Add a `GET /groups/{id}` lookup in `audit.js` if
  you want to check group members.
* **Only sheets** are audited here. Reports, dashboards and workspaces have their own shares
  (`GET /shares` takes `assetType=report|sight|workspace`), so the same pattern extends to them.
* **Who did what?** With the Event Reporting add-on, `GET /events` records publish and share
  actions with the user who made them: a good follow-up to add context to a finding.

## Scheduling

```
0 6 * * 1 cd /path/to/security_audit_demo && node src/audit.js
```

## Troubleshooting

* **`UNABLE_TO_GET_ISSUER_CERT_LOCALLY` / certificate errors:** your network inspects TLS. Run with
  `NODE_USE_SYSTEM_CA=1 node src/audit.js` so Node trusts your operating system's certificates.
* **403 on `/users/sheets`:** the token's user is not a System Admin.
* **Sheet has no column titled ...:** a column title differs from the list above.
* **IDs look wrong or calls 404:** never parse Smartsheet IDs with plain `JSON.parse` in Node; they
  can exceed what a JavaScript number holds. `src/smartsheet.js` handles this and explains how.

API reference: https://developers.smartsheet.com/api/smartsheet
