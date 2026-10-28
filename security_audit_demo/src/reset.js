#!/usr/bin/env node
// Put Demo 3 back to its starting state: empty the Findings, Audit Runs and Exposure Summary
// sheets so the next recording starts from nothing and the audit "fills itself".
//
//   node src/reset.js          asks you to type "reset"
//   node src/reset.js --yes    no prompt
//
// It only ever touches the sheets named in .env, and never any sheet that was audited.

import { createInterface } from "node:readline/promises";
import { parseArgs } from "node:util";
import { SmartsheetClient } from "./smartsheet.js";

try {
  process.loadEnvFile(new URL("../.env", import.meta.url));
} catch {
  /* no .env file */
}

const { values: opt } = parseArgs({ options: { yes: { type: "boolean", default: false } } });
const client = new SmartsheetClient({ token: process.env.SMARTSHEET_ACCESS_TOKEN });
const targets = ["FINDINGS_SHEET_ID", "RUNS_SHEET_ID", "EXPOSURE_SHEET_ID"]
  .map((name) => process.env[name])
  .filter(Boolean);

if (!opt.yes) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const answer = await rl.question(`Delete every row on ${targets.length} audit sheets? Type 'reset': `);
  rl.close();
  if (answer !== "reset") {
    console.log("Aborted.");
    process.exit(1);
  }
}

for (const id of targets) {
  const sheet = await client.getSheet(id);
  const ids = (sheet.rows ?? []).map((r) => r.id);
  for (let i = 0; i < ids.length; i += 200) await client.deleteRows(id, ids.slice(i, i + 200));
  console.log(`cleared ${ids.length} rows from "${sheet.name}"`);
}
