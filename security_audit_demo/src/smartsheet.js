// A small Smartsheet API client built on Node's built-in fetch (no npm packages needed).
//
// The Smartsheet API is plain HTTPS + JSON, so a client is mostly: add the auth header,
// stay under the rate limit, retry when told to, and parse the reply. Three details are
// worth understanding if you write your own:
//
//   1. RATE LIMIT. Smartsheet allows 300 requests per minute per token. We space request
//      starts out (see #throttle) and honour 429 replies with their Retry-After header.
//   2. BIG IDs. Smartsheet IDs can have 16 digits, which is more than a JavaScript number
//      can hold exactly (the limit is 9,007,199,254,740,991). JSON.parse would silently
//      change the last digits and every later call would 404. We therefore turn long
//      numbers into strings while parsing (parseJson) and back into numbers when sending
//      (toJson). Skipping this is the classic Node + Smartsheet bug.
//   3. PAGING. Two styles exist: page numbers (org sheets) and a lastKey token (shares).

const BASE = "https://api.smartsheet.com/2.0";

export class ApiError extends Error {
  constructor(status, body, method, path) {
    super(`${method} ${path} -> ${status} ${body?.message ?? ""}`.trim());
    this.status = status;
    this.body = body;
  }
}

/** Parse JSON, keeping 16+ digit integers (Smartsheet IDs) exact by returning them as strings. */
export function parseJson(text) {
  if (!text) return null;
  return JSON.parse(text.replace(/([:\[,]\s*)(\d{16,})(?=\s*[,}\]])/g, '$1"$2"'));
}

/** Serialise JSON, turning ID fields that hold digit-strings back into real numbers. */
export function toJson(value) {
  return JSON.stringify(value).replace(
    /"(id|columnId|parentId|siblingId)":"(\d+)"/g,
    '"$1":$2',
  );
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class SmartsheetClient {
  #token;
  #minGapMs;
  #nextSlot = 0;
  #source;

  /**
   * @param {object} opts
   * @param {string} opts.token                API access token
   * @param {number} [opts.requestsPerSecond]  stay under 5/s (300 per minute)
   * @param {string} [opts.source]             value for the smartsheet-integration-source header
   */
  constructor({ token, requestsPerSecond = 4, source = "APPLICATION,SecurityAuditDemo,audit" }) {
    if (!token) throw new Error("A Smartsheet API token is required");
    this.#token = token;
    this.#minGapMs = 1000 / requestsPerSecond;
    this.#source = source;
  }

  /** Space request *starts* at least #minGapMs apart, even when many run concurrently. */
  async #throttle() {
    const now = Date.now();
    const wait = Math.max(0, this.#nextSlot - now);
    this.#nextSlot = Math.max(now, this.#nextSlot) + this.#minGapMs;
    if (wait) await sleep(wait);
  }

  /**
   * One API call with retries. Returns parsed JSON; throws ApiError for non-2xx replies.
   * Retries 429 (rate limited) and 5xx (server trouble) with backoff.
   * `assumeUser` makes the call run as another user (System Admin impersonation).
   */
  async request(method, path, { query, body, assumeUser } = {}) {
    const url = new URL(BASE + path);
    for (const [k, v] of Object.entries(query ?? {})) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
    const headers = {
      Authorization: `Bearer ${this.#token}`,
      "smartsheet-integration-source": this.#source,
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (assumeUser) headers["Assume-User"] = encodeURIComponent(assumeUser);

    for (let attempt = 0; ; attempt++) {
      await this.#throttle();
      const res = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : toJson(body),
      });
      const parsed = parseJson(await res.text());
      if (res.ok) return parsed;

      const retryable = res.status === 429 || res.status >= 500;
      if (retryable && attempt < 5) {
        const retryAfter = Number(res.headers.get("retry-after"));
        await sleep((retryAfter > 0 ? retryAfter * 1000 : 2000 * 2 ** attempt));
        continue;
      }
      throw new ApiError(res.status, parsed, method, path);
    }
  }

  // ---- the handful of endpoints the audit needs ---------------------------------

  /** GET /users/me : who the token belongs to. */
  currentUser() {
    return this.request("GET", "/users/me");
  }

  /**
   * GET /users : every member of the organisation. Used to decide who is "inside" the
   * company, which is more reliable than guessing from email domains (an org can have
   * members on several domains).
   */
  async listUsers({ pageSize = 100 } = {}) {
    const users = [];
    for (let page = 1; ; page++) {
      const res = await this.request("GET", "/users", { query: { page, pageSize } });
      users.push(...(res.data ?? []));
      if (page >= (res.totalPages ?? 1) || !(res.data ?? []).length) return users;
    }
  }

  /**
   * GET /users/sheets : every sheet owned by someone in the organisation (System Admin only).
   * Page-number paging; this endpoint reports totalPages as -1, so we stop at a short page.
   */
  async listOrgSheets({ pageSize = 100 } = {}) {
    const sheets = [];
    for (let page = 1; ; page++) {
      const res = await this.request("GET", "/users/sheets", { query: { page, pageSize } });
      sheets.push(...(res.data ?? []));
      if ((res.data ?? []).length < pageSize) return sheets;
    }
  }

  /**
   * GET /shares : who a sheet is shared with. Works for any sheet in the org when called by
   * a System Admin, even sheets the admin cannot open. Token paging via lastKey.
   */
  async listShares(assetType, assetId, { sharingInclude } = {}) {
    const items = [];
    let lastKey;
    do {
      const res = await this.request("GET", "/shares", {
        query: { assetType, assetId, sharingInclude, lastKey },
      });
      items.push(...(res.items ?? []));
      lastKey = res.lastKey;
    } while (lastKey);
    return items;
  }

  /** GET /sheets/{id}/publish : is the sheet published to a public link? */
  getSheetPublish(sheetId, { assumeUser } = {}) {
    return this.request("GET", `/sheets/${sheetId}/publish`, { assumeUser });
  }

  /** GET /sheets/{id} : columns and rows (used to read the findings sheet back). */
  getSheet(sheetId) {
    return this.request("GET", `/sheets/${sheetId}`);
  }

  /** POST /sheets/{id}/rows : append rows. Max 500 per call. */
  addRows(sheetId, rows) {
    return this.request("POST", `/sheets/${sheetId}/rows`, { body: rows });
  }

  /** DELETE /sheets/{id}/rows?ids=... : the ids travel in the URL, so keep batches modest. */
  deleteRows(sheetId, rowIds) {
    return this.request("DELETE", `/sheets/${sheetId}/rows`, { query: { ids: rowIds.join(",") } });
  }

  /** PUT /sheets/{id}/rows : update existing rows by row id. Max 500 per call. */
  updateRows(sheetId, rows) {
    return this.request("PUT", `/sheets/${sheetId}/rows`, { body: rows });
  }
}
