// Compare what the audit found today with what the Findings sheet already holds, and work
// out the minimum set of row changes. This is the "weekly diff" from the slide: each run
// marks findings as New, Unchanged or Resolved, so the sheet is a living record instead of
// a pile of snapshots.
//
// diffFindings() is pure (no network), so it is unit tested in test/findings.test.js.

// Column titles on the Security Audit Findings sheet. They must match the sheet exactly.
export const COL = {
  key: "Finding Key",
  sheetName: "Sheet Name",
  sheetId: "Sheet ID",
  owner: "Owner",
  type: "Finding Type",
  detail: "Detail",
  access: "Access Level",
  risk: "Risk",
  status: "Status",
  change: "Change",
  firstSeen: "First Seen",
  lastSeen: "Last Seen",
  resolvedOn: "Resolved On",
};

/** A finding as the audit produces it -> the cell values for its row. */
function toValues(f, today) {
  return {
    [COL.key]: f.key,
    [COL.sheetName]: f.sheetName,
    [COL.sheetId]: f.sheetId,
    [COL.owner]: f.owner || "(unknown)",
    [COL.type]: f.type,
    [COL.detail]: f.detail,
    [COL.access]: f.accessLevel,
    [COL.risk]: f.risk,
    [COL.status]: "Open",
    [COL.change]: "New",
    [COL.firstSeen]: today,
    [COL.lastSeen]: today,
  };
}

/**
 * @param {Map<string,{rowId:string, values:object}>} existing  rows already on the sheet, by Finding Key
 * @param {Array}  current        today's findings (each with a unique `key`)
 * @param {Set<string>} scanned   sheet IDs we actually inspected this run
 * @param {string} today          YYYY-MM-DD
 * @returns {{adds:object[], updates:{rowId:string, values:object}[], counts:object}}
 */
export function diffFindings(existing, current, scanned, today) {
  const adds = [];
  const updates = [];
  const counts = { new: 0, unchanged: 0, resolved: 0, reopened: 0 };
  const seen = new Set();

  for (const f of current) {
    seen.add(f.key);
    const row = existing.get(f.key);

    if (!row) {                                   // never seen before -> add
      adds.push(toValues(f, today));
      counts.new++;
      continue;
    }

    const want = {};                              // only the cells that must change
    const set = (title, value) => {
      if ((row.values[title] ?? "") !== value) want[title] = value;
    };

    if (row.values[COL.status] === "Resolved") {  // it came back -> reopen
      set(COL.status, "Open");
      set(COL.change, "New");
      set(COL.resolvedOn, "");
      counts.reopened++;
    } else {
      set(COL.change, "Unchanged");
      counts.unchanged++;
    }
    set(COL.lastSeen, today);
    set(COL.risk, f.risk);                        // keep descriptive columns current
    set(COL.access, f.accessLevel);
    set(COL.owner, f.owner || "(unknown)");
    set(COL.sheetName, f.sheetName);
    if (Object.keys(want).length) updates.push({ rowId: row.rowId, values: want });
  }

  // Open findings that did not show up again -> resolved. We only do this for sheets we
  // really inspected: if a sheet errored or was skipped, silence is not proof it was fixed.
  for (const [key, row] of existing) {
    if (seen.has(key) || row.values[COL.status] !== "Open") continue;
    if (!scanned.has(String(row.values[COL.sheetId]))) continue;
    updates.push({
      rowId: row.rowId,
      values: { [COL.status]: "Resolved", [COL.change]: "Resolved", [COL.resolvedOn]: today },
    });
    counts.resolved++;
  }
  return { adds, updates, counts };
}

// ---- reading and writing the sheet ---------------------------------------------------

/** Read a sheet into { columns: {title: columnId}, existing: Map<Finding Key, {rowId, values}> }. */
export async function readFindings(client, sheetId) {
  const sheet = await client.getSheet(sheetId);
  const titles = Object.fromEntries(sheet.columns.map((c) => [c.id, c.title]));
  const columns = Object.fromEntries(sheet.columns.map((c) => [c.title, c.id]));
  const existing = new Map();
  for (const row of sheet.rows ?? []) {
    const values = {};
    for (const cell of row.cells) values[titles[cell.columnId]] = cell.value ?? "";
    if (values[COL.key]) existing.set(values[COL.key], { rowId: row.id, values });
  }
  return { columns, existing };
}

/** { "Column title": value } -> [{ columnId, value }] using the sheet's column IDs. */
export function toCells(columns, values) {
  return Object.entries(values).map(([title, value]) => {
    if (!(title in columns)) throw new Error(`Sheet has no column titled "${title}"`);
    return { columnId: columns[title], value };
  });
}

const chunks = (arr, n) => Array.from({ length: Math.ceil(arr.length / n) }, (_, i) => arr.slice(i * n, i * n + n));

/** Write the diff: updates first, then new rows, in batches of 500 (the API maximum). */
export async function writeDiff(client, sheetId, columns, { adds, updates }) {
  for (const batch of chunks(updates, 500)) {
    await client.updateRows(sheetId, batch.map((u) => ({ id: u.rowId, cells: toCells(columns, u.values) })));
  }
  for (const batch of chunks(adds, 500)) {
    await client.addRows(sheetId, batch.map((v) => ({ toBottom: true, cells: toCells(columns, v) })));
  }
}

/**
 * Keep the Exposure Summary sheet in step with today's findings: one row per owner with how
 * many High / Medium / Low findings they hold. Dashboard charts cannot count rows themselves,
 * so we do the counting here and the chart simply plots this small sheet.
 * Owners who no longer have findings are kept, set to zero, so their bars fall instead of vanishing.
 */
export async function writeExposure(client, sheetId, findings) {
  const counts = new Map();
  for (const f of findings) {
    const owner = f.owner || "(unknown)";
    const c = counts.get(owner) ?? { High: 0, Medium: 0, Low: 0 };
    c[f.risk]++;
    counts.set(owner, c);
  }
  const asValues = (c) => ({ High: c.High, Medium: c.Medium, Low: c.Low, Total: c.High + c.Medium + c.Low });

  const sheet = await client.getSheet(sheetId);
  const titles = Object.fromEntries(sheet.columns.map((c) => [c.id, c.title]));
  const columns = Object.fromEntries(sheet.columns.map((c) => [c.title, c.id]));
  const updates = [];
  const seen = new Set();
  for (const row of sheet.rows ?? []) {
    const cur = {};
    for (const cell of row.cells) cur[titles[cell.columnId]] = cell.value ?? 0;
    const owner = cur.Owner;
    seen.add(owner);
    const want = asValues(counts.get(owner) ?? { High: 0, Medium: 0, Low: 0 });
    const changed = Object.fromEntries(Object.entries(want).filter(([k, v]) => Number(cur[k] ?? 0) !== v));
    if (Object.keys(changed).length) updates.push({ rowId: row.id, values: changed });
  }
  const adds = [...counts].filter(([owner]) => !seen.has(owner)).map(([owner, c]) => ({ Owner: owner, ...asValues(c) }));
  await writeDiff(client, sheetId, columns, { adds, updates });
  return { owners: counts.size };
}
