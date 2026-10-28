#!/usr/bin/env node
// Tenant-wide security audit: "Who shared what outside the company?"
//
// This is Scenario 3. Same read -> decide -> write pattern as the other demos:
//
//   list every sheet in the org
//     -> read who each sheet is shared with, and whether it is published publicly
//     -> flag external shares and public links, and rate the risk
//     -> compare with last run and write the result to an audit sheet (New / Unchanged / Resolved)
//     -> append a one-line summary to the Audit Runs sheet (the trend a dashboard charts)
//
// Endpoints used: GET /users/sheets, GET /shares, GET /sheets/{id}/publish, and
// POST/PUT /sheets/{id}/rows to write the results.
//
// Read-only against your sheets: the only thing this script changes is the two audit sheets.
//
//   node src/audit.js --dry-run          show the findings, write nothing
//   node src/audit.js                    write findings + run summary to Smartsheet
//   node src/audit.js --limit 20         audit just the first 20 sheets (quick rehearsal)

import { parseArgs } from "node:util";
import { SmartsheetClient } from "./smartsheet.js";
import { classifyShares, classifyPublish } from "./classify.js";
import { diffFindings, readFindings, toCells, writeDiff, writeExposure } from "./findings.js";

// Load .env if there is one (built into Node 20.12+; no dotenv package needed).
// Variables already set in your shell take priority over the file.
try {
  process.loadEnvFile(new URL("../.env", import.meta.url));
} catch {
  /* no .env file: fall back to the real environment */
}

const C = { green: "\x1b[92m", yellow: "\x1b[93m", cyan: "\x1b[96m", red: "\x1b[91m", dim: "\x1b[2m", off: "\x1b[0m" };
const need = (name) => process.env[name] || (console.error(`Set ${name} in .env (see .env.example)`), process.exit(1));
const list = (name) => new Set((process.env[name] ?? "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean));

const { values: opt } = parseArgs({
  options: {
    "dry-run": { type: "boolean", default: false },
    limit: { type: "string" },
    concurrency: { type: "string", default: "4" },
    "no-publish": { type: "boolean", default: false },
    impersonate: { type: "boolean", default: false },
  },
});

/** Run fn over items with at most `n` in flight at once. */
async function pool(items, n, fn) {
  const results = new Array(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: n }, async () => {
      while (next < items.length) {
        const i = next++;
        results[i] = await fn(items[i], i);
      }
    }),
  );
  return results;
}

async function main() {
  const client = new SmartsheetClient({ token: need("SMARTSHEET_ACCESS_TOKEN") });
  const findingsSheetId = need("FINDINGS_SHEET_ID");
  const runsSheetId = need("RUNS_SHEET_ID");
  const today = new Date().toISOString().slice(0, 10);

  // --- Policy: what counts as "outside the company"? --------------------------------
  // "Inside" means: a member of your Smartsheet organisation. We read the org's user list
  // once and treat everyone on it as internal, plus any extra domains in INTERNAL_DOMAINS.
  // Anyone else with access to a sheet is external.
  const me = await client.currentUser();
  const members = await client.listUsers();
  const policy = {
    internalEmails: new Set(members.map((u) => u.email.toLowerCase())),
    internalDomains: list("INTERNAL_DOMAINS"),
    allowedExternalDomains: list("ALLOWED_EXTERNAL_DOMAINS"),
  };
  console.log(`${C.cyan}Audit as${C.off} ${me.email}   ${C.cyan}org members:${C.off} ${members.length}` +
    `   ${C.cyan}mode:${C.off} ${opt["dry-run"] ? "DRY RUN" : "WRITE"}`);

  // --- 1. List every sheet in the organisation ---------------------------------------
  let sheets = await client.listOrgSheets();
  if (opt.limit) sheets = sheets.slice(0, Number(opt.limit));
  console.log(`${C.cyan}Sheets to audit:${C.off} ${sheets.length}`);

  // --- 2. Inspect each sheet: shares + publish status --------------------------------
  let done = 0;
  const inspected = await pool(sheets, Number(opt.concurrency), async (sheet) => {
    const r = { sheet, owner: "", findings: [], scanned: false, publishChecked: false, error: null };
    try {
      // Works for every sheet in the org for a System Admin, even ones they cannot open.
      const shares = await client.listShares("sheet", sheet.id);
      const share = classifyShares(shares, policy);
      r.owner = share.owner;
      r.findings.push(...share.findings);
      r.scanned = true;

      // Publish status needs access to the sheet. If the token cannot open it, optionally
      // retry as the sheet's owner (System Admin impersonation, off unless --impersonate).
      if (!opt["no-publish"]) {
        let publish = null;
        try {
          publish = await client.getSheetPublish(sheet.id);
          r.publishChecked = true;
        } catch (e) {
          if (opt.impersonate && r.owner && [403, 404].includes(e.status)) {
            try {
              publish = await client.getSheetPublish(sheet.id, { assumeUser: r.owner });
              r.publishChecked = true;
            } catch { /* still unreadable: counted as "not checked" */ }
          }
        }
        r.findings.push(...classifyPublish(publish));
      }
    } catch (e) {
      r.error = e.message;                      // one bad sheet must not stop the audit
    }
    if (++done % 25 === 0 || done === sheets.length) process.stdout.write(`\r${C.dim}inspected ${done}/${sheets.length}${C.off}   `);
    return r;
  });
  console.log();

  // --- 3. Flatten into findings with a stable key per finding ------------------------
  const findings = inspected.flatMap((r) =>
    r.findings.map((f) => ({
      key: `${r.sheet.id}|${f.keySuffix}`,
      sheetId: String(r.sheet.id),
      sheetName: r.sheet.name,
      owner: r.owner || r.sheet.owner || "",
      ...f,
    })),
  );
  const scanned = new Set(inspected.filter((r) => r.scanned).map((r) => String(r.sheet.id)));
  const errors = inspected.filter((r) => r.error);

  // --- 4. Diff against the Findings sheet ---------------------------------------------
  const { columns, existing } = await readFindings(client, findingsSheetId);
  const diff = diffFindings(existing, findings, scanned, today);

  // --- 5. Report -----------------------------------------------------------------------
  const external = findings.filter((f) => f.type === "External share");
  const publicLinks = findings.filter((f) => f.type === "Public link");
  const high = findings.filter((f) => f.risk === "High");
  const withFindings = new Set(findings.map((f) => f.sheetId)).size;
  const notChecked = inspected.filter((r) => r.scanned && !r.publishChecked).length;

  console.log(`\n${C.cyan}Exposure by owner${C.off}`);
  const byOwner = new Map();
  for (const f of findings) byOwner.set(f.owner || "(unknown)", (byOwner.get(f.owner || "(unknown)") ?? 0) + 1);
  for (const [owner, n] of [...byOwner].sort((a, b) => b[1] - a[1]).slice(0, 8)) {
    console.log(`  ${String(n).padStart(4)}  ${owner}`);
  }

  console.log(`\n${C.cyan}Result${C.off}`);
  console.log(`  sheets scanned        ${scanned.size} of ${sheets.length}${errors.length ? `  ${C.red}(${errors.length} errors)${C.off}` : ""}`);
  console.log(`  sheets with findings  ${withFindings}`);
  console.log(`  external shares       ${external.length}`);
  console.log(`  public links          ${publicLinks.length}   ${C.dim}(publish status not checkable on ${notChecked} sheets)${C.off}`);
  console.log(`  ${C.red}high risk${C.off}             ${high.length}`);
  console.log(`  ${C.yellow}new${C.off} ${diff.counts.new + diff.counts.reopened}   ${C.green}resolved${C.off} ${diff.counts.resolved}   ${C.dim}unchanged ${diff.counts.unchanged}${C.off}`);

  if (opt["dry-run"]) {
    console.log(`\n${C.cyan}Dry run - nothing written.${C.off}`);
    return;
  }

  // --- 6. Write the findings, then the run summary ------------------------------------
  await writeDiff(client, findingsSheetId, columns, diff);
  console.log(`\n${C.green}Findings sheet updated:${C.off} ${diff.adds.length} added, ${diff.updates.length} updated`);

  const runs = await client.getSheet(runsSheetId);
  const runColumns = Object.fromEntries(runs.columns.map((c) => [c.title, c.id]));
  await client.addRows(runsSheetId, [{
    toBottom: true,
    cells: toCells(runColumns, {
      "Run Time": new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC",
      "Sheets Scanned": scanned.size,
      "Sheets With Findings": withFindings,
      "External Shares": external.length,
      "Public Links": publicLinks.length,
      "High Risk": high.length,
      "New Findings": diff.counts.new + diff.counts.reopened,
      "Resolved Findings": diff.counts.resolved,
      "Publish Not Checked": notChecked,
    }),
  }]);
  console.log(`${C.green}Run summary appended to Audit Runs.${C.off}`);

  // Optional: refresh the per-owner counts that the dashboard charts read.
  if (process.env.EXPOSURE_SHEET_ID) {
    const { owners } = await writeExposure(client, process.env.EXPOSURE_SHEET_ID, findings);
    console.log(`${C.green}Exposure Summary refreshed:${C.off} ${owners} owners`);
  }
}

main().catch((e) => {
  console.error(`${C.red}${e.message}${C.off}`);
  process.exit(1);
});
