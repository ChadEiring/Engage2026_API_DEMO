import test from "node:test";
import assert from "node:assert/strict";
import { diffFindings, COL } from "../src/findings.js";

const f = (key, extra = {}) => ({ key, sheetId: "1", sheetName: "S", owner: "o@x.com", type: "External share",
  detail: "a@b.com", accessLevel: "VIEWER", risk: "Medium", ...extra });
const row = (rowId, values) => ({ rowId, values: { [COL.sheetId]: "1", [COL.status]: "Open", ...values } });

test("new, unchanged and resolved findings are told apart", () => {
  const existing = new Map([
    ["keep", row("r1", { [COL.change]: "New", [COL.lastSeen]: "2026-01-01", [COL.risk]: "Medium", [COL.access]: "VIEWER", [COL.owner]: "o@x.com", [COL.sheetName]: "S" })],
    ["gone", row("r2", {})],
  ]);
  const d = diffFindings(existing, [f("keep"), f("fresh")], new Set(["1"]), "2026-02-02");
  assert.equal(d.adds.length, 1);
  assert.equal(d.adds[0][COL.change], "New");
  assert.deepEqual(d.counts, { new: 1, unchanged: 1, resolved: 1, reopened: 0 });
  const resolved = d.updates.find((u) => u.rowId === "r2");
  assert.equal(resolved.values[COL.status], "Resolved");
  assert.equal(resolved.values[COL.resolvedOn], "2026-02-02");
});

test("a finding on a sheet we did not scan is never marked resolved", () => {
  const existing = new Map([["gone", row("r2", {})]]);
  const d = diffFindings(existing, [], new Set(), "2026-02-02");
  assert.equal(d.updates.length, 0);
});

test("a resolved finding that returns is reopened", () => {
  const existing = new Map([["k", row("r3", { [COL.status]: "Resolved", [COL.resolvedOn]: "2026-01-05" })]]);
  const d = diffFindings(existing, [f("k")], new Set(["1"]), "2026-02-02");
  assert.equal(d.counts.reopened, 1);
  assert.equal(d.updates[0].values[COL.status], "Open");
});

test("nothing to change means nothing to write", () => {
  const same = { [COL.change]: "Unchanged", [COL.lastSeen]: "2026-02-02", [COL.risk]: "Medium", [COL.access]: "VIEWER", [COL.owner]: "o@x.com", [COL.sheetName]: "S" };
  const d = diffFindings(new Map([["k", row("r4", same)]]), [f("k")], new Set(["1"]), "2026-02-02");
  assert.equal(d.updates.length, 0);
});
