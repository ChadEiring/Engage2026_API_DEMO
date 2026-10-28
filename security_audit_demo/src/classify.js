// Pure functions that turn raw API data into findings. No network calls here, which is
// what makes them easy to unit test (see test/classify.test.js) and to adapt: change the
// rules below to match your own security policy.

export const FINDING_EXTERNAL = "External share";
export const FINDING_PUBLIC = "Public link";

/** "Jane@Example.com" -> "example.com" */
export function domainOf(email) {
  return String(email ?? "").split("@")[1]?.trim().toLowerCase() ?? "";
}

/**
 * Risk of sharing a sheet with someone outside the company, by the access they hold.
 * Anyone who can edit or share can also change or spread the data further.
 */
export function riskForAccess(accessLevel) {
  return ["OWNER", "ADMIN", "EDITOR_SHARE", "EDITOR"].includes(accessLevel) ? "High" : "Medium";
}

/**
 * Read a sheet's share list.
 *
 * @param {Array}  shares  items from GET /shares
 * @param {object} policy
 * @param {Set<string>} policy.internalEmails          emails of everyone in your Smartsheet organisation
 * @param {Set<string>} policy.internalDomains         extra company email domains (optional)
 * @param {Set<string>} [policy.allowedExternalDomains] trusted partners that should not be flagged
 * @returns {{ owner: string, groupShares: number, findings: Array }}
 */
export function classifyShares(
  shares,
  { internalEmails = new Set(), internalDomains = new Set(), allowedExternalDomains = new Set() },
) {
  let owner = "";
  let groupShares = 0;
  const findings = [];

  for (const share of shares) {
    if (share.accessLevel === "OWNER" && share.email) {
      owner = share.email;      // the owner is who we report against, not a finding
      continue;
    }
    if (share.type === "GROUP") {
      groupShares++;            // group membership is not expanded in this demo
      continue;
    }
    const domain = domainOf(share.email);
    if (!domain || internalEmails.has(share.email.toLowerCase())
        || internalDomains.has(domain) || allowedExternalDomains.has(domain)) continue;
    findings.push({
      type: FINDING_EXTERNAL,
      keySuffix: `share|${share.email.toLowerCase()}`,
      detail: share.email,
      accessLevel: share.accessLevel,
      risk: riskForAccess(share.accessLevel),
    });
  }
  return { owner, groupShares, findings };
}

// The four kinds of publish link a sheet can have, from the GET publish response.
const PUBLISH_KINDS = [
  { flag: "readWriteEnabled", label: "Public link: anyone can edit", access: "EDIT", risk: "High" },
  { flag: "readOnlyFullEnabled", label: "Public link: anyone can view", access: "VIEW", risk: "Medium" },
  { flag: "readOnlyLiteEnabled", label: "Public link: anyone can view (lite)", access: "VIEW", risk: "Medium" },
  { flag: "icalEnabled", label: "Public iCal link", access: "VIEW", risk: "Low" },
];

/**
 * Read a sheet's publish settings. A link restricted to the organisation ("ORG") is not
 * public, so it is not reported; only links open to "ALL" are.
 */
export function classifyPublish(publish) {
  if (!publish) return [];
  return PUBLISH_KINDS.filter(
    (k) => publish[k.flag] && publish[k.flag.replace("Enabled", "AccessibleBy")] !== "ORG",
  ).map((k) => ({
    type: FINDING_PUBLIC,
    keySuffix: `public|${k.flag}`,
    detail: k.label,
    accessLevel: k.access,
    risk: k.risk,
  }));
}
