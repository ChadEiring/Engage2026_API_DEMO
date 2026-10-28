import test from "node:test";
import assert from "node:assert/strict";
import { classifyShares, classifyPublish, domainOf, riskForAccess } from "../src/classify.js";
import { parseJson, toJson } from "../src/smartsheet.js";

const policy = {
  internalEmails: new Set(["boss@acme.com", "colleague@other-brand.com"]),
  internalDomains: new Set(["acme.com"]),
  allowedExternalDomains: new Set(["partner.com"]),
};

test("domainOf lower-cases and handles junk", () => {
  assert.equal(domainOf("Jane@Example.COM"), "example.com");
  assert.equal(domainOf(undefined), "");
});

test("external users are flagged; org members (any domain), internal domains and allowed partners are not", () => {
  const { owner, findings, groupShares } = classifyShares([
    { type: "USER", email: "boss@acme.com", accessLevel: "OWNER" },
    { type: "USER", email: "ally@partner.com", accessLevel: "EDITOR" },
    { type: "USER", email: "Colleague@other-brand.com", accessLevel: "EDITOR" },
    { type: "USER", email: "Stranger@evil.io", accessLevel: "EDITOR_SHARE" },
    { type: "USER", email: "viewer@other.org", accessLevel: "VIEWER" },
    { type: "GROUP", name: "Everyone", accessLevel: "VIEWER" },
  ], policy);
  assert.equal(owner, "boss@acme.com");
  assert.equal(groupShares, 1);
  assert.deepEqual(findings.map((f) => [f.detail, f.risk]), [
    ["Stranger@evil.io", "High"],
    ["viewer@other.org", "Medium"],
  ]);
  assert.equal(findings[0].keySuffix, "share|stranger@evil.io");
});

test("risk depends on whether the outsider can change things", () => {
  assert.equal(riskForAccess("ADMIN"), "High");
  assert.equal(riskForAccess("COMMENTER"), "Medium");
});

test("publish links: edit is high, org-only links are ignored", () => {
  const found = classifyPublish({
    readWriteEnabled: true,
    readOnlyFullEnabled: true, readOnlyFullAccessibleBy: "ORG",
    icalEnabled: true,
    readOnlyLiteEnabled: false,
  });
  assert.deepEqual(found.map((f) => [f.keySuffix, f.risk]), [
    ["public|readWriteEnabled", "High"],
    ["public|icalEnabled", "Low"],
  ]);
  assert.deepEqual(classifyPublish(null), []);
});

test("16-digit IDs survive a JSON round trip exactly", () => {
  const big = "9876543210987654"; // larger than Number.MAX_SAFE_INTEGER
  const parsed = parseJson(`{"id":${big},"items":[${big}]}`);
  assert.equal(parsed.id, big);
  assert.equal(toJson({ id: parsed.id, columnId: big, name: big }), `{"id":${big},"columnId":${big},"name":"${big}"}`);
});
