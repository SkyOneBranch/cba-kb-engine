/**
 * CBA-KB v2.3.0 Clean-Room Cloudflare Worker Edge Gateway Template
 *
 * Bindings expected in Cloudflare Worker environment (configured privately):
 * - env.CBA_KB_R2: R2Bucket binding to the private cloud state bucket
 * - env.GATEWAY_AUTH_SECRET: Secret string for HTTP authorization verification
 * - env.GITHUB_DISPATCH_REPO: Private collector repository ("owner/repo")
 * - env.GITHUB_DISPATCH_PAT: Fine-grained token with repository_dispatch scope
 *
 * Routes:
 * - POST /v1/person
 * - GET  /v1/coverage
 * - GET  /v1/jobs/<job_id>
 */

const DEFAULT_SEASON = "2026-2027";
const DEFAULT_FRESHNESS_TTL_HOURS = 24;
const DEFAULT_STALE_AFTER_HOURS = 48;
export const DEFAULT_LOCK_TTL_SECONDS = 900;
const WAIT_TIMEOUT_MS = 28000;
const POLL_INTERVAL_MS = 2000;
const AUTH_SCHEME = "bearer";
const SHA256_HEX_RE = /^[0-9a-f]{64}$/;
const UNICODE_TRIM_RE = /^[\s\u001c-\u001f\u0085\ufeff]+|[\s\u001c-\u001f\u0085\ufeff]+$/g;
const ACTIVE_LOCK_STATUSES = new Set(["RUNNING", "PENDING", "ACQUIRING_IN_CLOUD"]);
const VALID_COVERAGE_STATUSES = new Set(["COMPLETE", "PARTIAL", "STALE", "UNKNOWN"]);
const LEAGUE_COVERAGE_DOMAINS = [
  ["domestic_registration", "国内球员注册"],
  ["foreign_registration", "外籍球员注册"],
  ["club_announcements", "俱乐部官方公告"],
  ["league_rules", "联赛规程与规则"],
  ["game_stats", "官方比赛技术统计"],
];

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const ab = enc.encode(String(a || ""));
  const bb = enc.encode(String(b || ""));
  if (ab.length !== bb.length || ab.length === 0) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < ab.length; i += 1) {
    diff |= ab[i] ^ bb[i];
  }
  return diff === 0;
}

function verifyAuthorization(request, env) {
  const expected = env && typeof env.GATEWAY_AUTH_SECRET === "string"
    ? env.GATEWAY_AUTH_SECRET.trim()
    : "";
  if (!expected) {
    return false;
  }
  const header = request.headers.get("Authorization") || "";
  const parts = header.trim().split(/\s+/);
  if (parts.length !== 2) {
    return false;
  }
  if (parts[0].toLowerCase() !== AUTH_SCHEME) {
    return false;
  }
  return timingSafeEqual(parts[1], expected);
}

export async function sha256Hex(input) {
  const bytes = input instanceof Uint8Array
    ? input
    : (input instanceof ArrayBuffer
      ? new Uint8Array(input)
      : new TextEncoder().encode(String(input || "")));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Key parity with Python `cba_kb.cloud_state._safe_key_segment`:
 * - trim Unicode whitespace including U+0085 and U+001C..U+001F + lowercase
 * - if /^[a-z0-9._-]+$/ return normalized ASCII directly
 * - otherwise return "k_" + sha256(utf8(norm)).slice(0, 24)
 */
export async function safeKey(value) {
  const norm = String(value ?? "").replace(UNICODE_TRIM_RE, "").toLowerCase();
  if (!norm) {
    throw new Error("R2_KEY_SEGMENT_EMPTY");
  }
  if (/^[a-z0-9._-]+$/.test(norm)) {
    return norm;
  }
  const hex = await sha256Hex(new TextEncoder().encode(norm));
  return `k_${hex.slice(0, 24)}`;
}

function extractDeclaredSha256(obj) {
  const custom = (obj && obj.customMetadata) || {};
  for (const [k, v] of Object.entries(custom)) {
    const lk = String(k).toLowerCase();
    if ((lk === "sha256" || lk === "x-amz-meta-sha256") && typeof v === "string") {
      return v.trim().toLowerCase();
    }
  }
  return "";
}

async function readR2ObjectWithMeta(bucket, key) {
  if (!bucket) return null;
  const obj = await bucket.get(key);
  if (!obj) return null;

  const rawBuf = await obj.arrayBuffer();
  const rawBytes = new Uint8Array(rawBuf);
  const declaredSha = extractDeclaredSha256(obj);
  if (!declaredSha || !SHA256_HEX_RE.test(declaredSha)) {
    throw new Error(`R2_OBJECT_SHA256_MISMATCH:missing_or_malformed_metadata:${key}`);
  }
  const actualSha = await sha256Hex(rawBytes);
  if (actualSha !== declaredSha) {
    throw new Error(`R2_OBJECT_SHA256_MISMATCH:digest_mismatch:${key}`);
  }
  const data = JSON.parse(new TextDecoder("utf-8").decode(rawBytes));
  return { data, etag: obj.etag || obj.httpEtag || null };
}

export async function readR2Json(bucket, key) {
  const loaded = await readR2ObjectWithMeta(bucket, key);
  return loaded ? loaded.data : null;
}

export async function writeR2Json(bucket, key, data, extraOpts = {}) {
  if (!bucket) return null;
  const encoded = new TextEncoder().encode(JSON.stringify(data));
  const sha256 = await sha256Hex(encoded);
  const putResult = await bucket.put(key, encoded, {
    ...extraOpts,
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: { sha256 },
  });
  return { key, sha256, putResult };
}

export async function verifyCardRecord(card) {
  if (!card || typeof card !== "object") {
    throw new Error("R2_OBJECT_SHA256_MISMATCH:card_record_invalid");
  }
  const markdown = typeof card.markdown === "string"
    ? card.markdown
    : (typeof card.card_markdown === "string" ? card.card_markdown : null);
  const declared = typeof card.card_sha256 === "string"
    ? card.card_sha256.trim().toLowerCase()
    : "";
  if (markdown === null || !SHA256_HEX_RE.test(declared)) {
    throw new Error("R2_OBJECT_SHA256_MISMATCH:card_sha256");
  }
  const actual = await sha256Hex(new TextEncoder().encode(markdown));
  if (actual !== declared) {
    throw new Error("R2_OBJECT_SHA256_MISMATCH:card_sha256");
  }
  return { markdown, card_sha256: actual };
}

async function verifyAndAttachJobCard(env, job, fallbackSeasonKey = null, fallbackPersonKey = null) {
  if (!job || typeof job !== "object") return job;
  // Unconditional SHA-256 verification whenever card markdown is embedded in jobs/<id>.json
  if (typeof job.card_markdown === "string" || typeof job.markdown === "string") {
    const verified = await verifyCardRecord(job);
    job.card_markdown = verified.markdown;
    job.card_sha256 = verified.card_sha256;
    return job;
  }
  if (job.status === "COMPLETED") {
    const seasonKey = fallbackSeasonKey || (job.season ? await safeKey(job.season) : null);
    const personKey = fallbackPersonKey || (job.person ? await safeKey(job.person) : null);
    if (seasonKey && personKey) {
      const card = await readR2Json(env.CBA_KB_R2, `cards/${seasonKey}/${personKey}.json`);
      if (card) {
        const verified = await verifyCardRecord(card);
        job.card_markdown = verified.markdown;
        job.card_sha256 = verified.card_sha256;
      }
    }
  }
  return job;
}

export async function acquireLockAtomic(
  bucket,
  lockKey,
  { person, season, nowMs = Date.now(), lockTtlSeconds = DEFAULT_LOCK_TTL_SECONDS },
) {
  const jobId = `job_${nowMs}_${Math.random().toString(36).slice(2, 10)}`;
  const ownerId = crypto.randomUUID();
  const createdAt = new Date(nowMs).toISOString();
  const expiresAt = new Date(nowMs + Number(lockTtlSeconds) * 1000).toISOString();
  const lockDoc = {
    job_id: jobId,
    owner_id: ownerId,
    status: "RUNNING",
    person,
    season,
    created_at_utc: createdAt,
    expires_at_utc: expiresAt,
  };

  if (!bucket) {
    return { acquired: true, jobDoc: lockDoc };
  }

  const encoded = new TextEncoder().encode(JSON.stringify(lockDoc));
  const lockSha = await sha256Hex(encoded);
  const createRes = await bucket.put(lockKey, encoded, {
    onlyIf: { etagDoesNotMatch: "*" },
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: { sha256: lockSha },
  });
  if (createRes !== null && createRes !== undefined) {
    return { acquired: true, jobDoc: lockDoc };
  }

  // Lock already exists: inspect whether it is active and unexpired
  const existing = await readR2ObjectWithMeta(bucket, lockKey);
  if (existing && existing.data) {
    const exDoc = existing.data;
    const status = String(exDoc.status || "").trim().toUpperCase();
    const expMs = exDoc.expires_at_utc ? Date.parse(exDoc.expires_at_utc) : NaN;
    const isExpired = Number.isFinite(expMs) && nowMs >= expMs;
    const isReleased = status === "RELEASED";
    if (!isReleased && ACTIVE_LOCK_STATUSES.has(status) && !isExpired && exDoc.job_id) {
      return { acquired: false, jobDoc: exDoc };
    }

    // Fail-closed if ETag is missing on existing lock (cannot perform CAS takeover safely)
    if (!existing.etag) {
      return { acquired: false, jobDoc: exDoc };
    }

    // Released, expired, or non-active lock: atomic takeover via etagMatches
    const takeoverOpts = {
      onlyIf: { etagMatches: existing.etag },
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: { sha256: lockSha },
    };
    const takeoverRes = await bucket.put(lockKey, encoded, takeoverOpts);
    if (takeoverRes !== null && takeoverRes !== undefined) {
      return { acquired: true, jobDoc: lockDoc };
    }

    // Another worker won the takeover race: re-read and deduplicate
    const winner = await readR2ObjectWithMeta(bucket, lockKey);
    if (winner && winner.data && winner.data.job_id) {
      return { acquired: false, jobDoc: winner.data };
    }
  }

  // Fallback retry if lock vanished between create and read
  const retryRes = await bucket.put(lockKey, encoded, {
    onlyIf: { etagDoesNotMatch: "*" },
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: { sha256: lockSha },
  });
  if (retryRes !== null && retryRes !== undefined) {
    return { acquired: true, jobDoc: lockDoc };
  }
  const finalLock = await readR2Json(bucket, lockKey);
  return { acquired: false, jobDoc: finalLock || lockDoc };
}

export function evaluateLeagueCoverage(
  ledger,
  nowMs = Date.now(),
  staleAfterHours = DEFAULT_STALE_AFTER_HOURS,
) {
  const domainsOut = {};
  if (!ledger || typeof ledger !== "object") {
    for (const [domKey, label] of LEAGUE_COVERAGE_DOMAINS) {
      domainsOut[domKey] = {
        domain: domKey,
        label,
        observed_until: null,
        coverage_status: "UNKNOWN",
        missing_items: [],
        source_count: 0,
        honest_disclosure: `${label}缺少覆盖账本（status=UNKNOWN）。`,
      };
    }
    return {
      overall_status: "UNKNOWN",
      observed_until: null,
      domains: domainsOut,
      missing_items: [],
      honest_disclosure:
        "未提供有效覆盖账本（coverage_ledger），当前各命题域覆盖状态均为 UNKNOWN。 Statement != Fact. Evidence != Fact. Unknown != 0.",
    };
  }

  const grouped = {};
  if (Array.isArray(ledger.sources)) {
    for (const src of ledger.sources) {
      if (!src || typeof src !== "object") continue;
      const dom = String(src.domain || "").trim();
      if (!dom) continue;
      if (!grouped[dom]) grouped[dom] = [];
      grouped[dom].push(src);
    }
  } else if (ledger.domains && typeof ledger.domains === "object") {
    for (const [dom, entry] of Object.entries(ledger.domains)) {
      if (entry && typeof entry === "object") {
        grouped[dom] = [entry];
      }
    }
  }

  const staleMs = Number(staleAfterHours) * 3600 * 1000;
  const allMissing = [];
  const seenMissing = new Set();
  const overallObsCandidates = [];

  for (const [domKey, label] of LEAGUE_COVERAGE_DOMAINS) {
    const domSources = grouped[domKey] || [];
    if (domSources.length === 0) {
      domainsOut[domKey] = {
        domain: domKey,
        label,
        observed_until: null,
        coverage_status: "UNKNOWN",
        missing_items: [],
        source_count: 0,
        honest_disclosure: `${label}尚未记录有效观测来源（status=UNKNOWN）。`,
      };
      continue;
    }

    const domMissing = [];
    const seenDomMissing = new Set();
    const domObsCandidates = [];
    const rawStatuses = [];

    for (const src of domSources) {
      let rawSt = String(src.status || src.coverage_status || "UNKNOWN").trim().toUpperCase();
      if (!VALID_COVERAGE_STATUSES.has(rawSt)) rawSt = "UNKNOWN";
      rawStatuses.push(rawSt);

      if (typeof src.observed_until === "string" && src.observed_until.trim()) {
        const obsStr = src.observed_until.trim();
        const obsMs = Date.parse(obsStr);
        if (Number.isFinite(obsMs)) {
          domObsCandidates.push([obsMs, obsStr]);
          overallObsCandidates.push([obsMs, obsStr]);
        }
      }

      const mList = Array.isArray(src.missing)
        ? src.missing
        : (Array.isArray(src.missing_items) ? src.missing_items : []);
      for (const mItem of mList) {
        const mStr = String(mItem || "").trim();
        if (!mStr) continue;
        if (!seenDomMissing.has(mStr)) {
          seenDomMissing.add(mStr);
          domMissing.push(mStr);
        }
        if (!seenMissing.has(mStr)) {
          seenMissing.add(mStr);
          allMissing.push(mStr);
        }
      }
    }

    let domObsMs = null;
    let domObsStr = null;
    for (const [ms, str] of domObsCandidates) {
      if (domObsMs === null || ms > domObsMs) {
        domObsMs = ms;
        domObsStr = str;
      }
    }

    let domStatus;
    if (rawStatuses.every((st) => st === "UNKNOWN")) {
      domStatus = "UNKNOWN";
    } else if (
      rawStatuses.some((st) => st === "PARTIAL")
      || domMissing.length > 0
      || rawStatuses.some((st) => st === "UNKNOWN")
    ) {
      domStatus = "PARTIAL";
    } else if (rawStatuses.some((st) => st === "STALE")) {
      domStatus = "STALE";
    } else {
      domStatus = "COMPLETE";
      if (domObsMs !== null && (nowMs - domObsMs) > staleMs) {
        domStatus = "STALE";
      }
    }

    domainsOut[domKey] = {
      domain: domKey,
      label,
      observed_until: domObsStr,
      coverage_status: domStatus,
      missing_items: domMissing,
      source_count: domSources.length,
      honest_disclosure: `${label}覆盖状态：${domStatus}（observed_until=${domObsStr || "UNKNOWN"}）。`,
    };
  }

  const domStatuses = Object.values(domainsOut).map((d) => d.coverage_status);
  let overallStatus;
  if (domStatuses.every((st) => st === "UNKNOWN")) {
    overallStatus = "UNKNOWN";
  } else if (domStatuses.every((st) => st === "COMPLETE")) {
    overallStatus = "COMPLETE";
  } else if (domStatuses.some((st) => st === "PARTIAL" || st === "UNKNOWN")) {
    overallStatus = "PARTIAL";
  } else {
    overallStatus = "STALE";
  }

  let overallObs = null;
  let maxObsMs = null;
  for (const [ms, str] of overallObsCandidates) {
    if (maxObsMs === null || ms > maxObsMs) {
      maxObsMs = ms;
      overallObs = str;
    }
  }
  if (!overallObs && typeof ledger.generated_at_utc === "string" && ledger.generated_at_utc.trim()) {
    overallObs = ledger.generated_at_utc.trim();
  }

  return {
    overall_status: overallStatus,
    observed_until: overallObs,
    domains: domainsOut,
    missing_items: allMissing,
    honest_disclosure:
      `已评估 5 个命题域（observed_until=${overallObs || "UNKNOWN"}），当前覆盖状态为 ${overallStatus}。 Statement != Fact. Evidence != Fact. Unknown != 0.`,
  };
}

async function sleep(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function triggerCollectorRunner(env, payload) {
  if (!env.GITHUB_DISPATCH_REPO || !env.GITHUB_DISPATCH_PAT) {
    return { dispatched: false, reason: "DISPATCH_NOT_CONFIGURED" };
  }
  const url = `https://api.github.com/repos/${env.GITHUB_DISPATCH_REPO}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `token ${env.GITHUB_DISPATCH_PAT}`,
      "Content-Type": "application/json",
      "User-Agent": "cba-kb-snowball-worker/2.3.0",
    },
    body: JSON.stringify({
      event_type: "cba_kb_snowball_acquire",
      client_payload: payload,
    }),
  });
  return { dispatched: resp.ok, status: resp.status };
}

async function handleCoverage(env) {
  const ledger = await readR2Json(env.CBA_KB_R2, "coverage_ledger.json");
  return jsonResponse(evaluateLeagueCoverage(ledger));
}

async function handleJobStatus(env, jobId) {
  const job = await readR2Json(env.CBA_KB_R2, `jobs/${jobId}.json`);
  if (!job) {
    return jsonResponse({ status: "NOT_FOUND", job_id: jobId }, 404);
  }
  await verifyAndAttachJobCard(env, job);
  return jsonResponse(job, 200);
}

async function handlePersonQuery(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ error: "INVALID_JSON" }, 400);
  }
  const person = body && typeof body.person === "string" ? body.person.trim() : "";
  if (!person) {
    return jsonResponse({ error: "PERSON_REQUIRED" }, 400);
  }
  const season = String((body && body.season) || DEFAULT_SEASON);
  const forceRefresh = Boolean(body && body.force_refresh);
  const ttlHours = Number((body && body.freshness_ttl_hours) || DEFAULT_FRESHNESS_TTL_HOURS);
  const seasonKey = await safeKey(season);
  const personKey = await safeKey(person);

  // Fast path: fresh cached card in R2 (< 0.5s) with fail-closed SHA-256 verification
  if (!forceRefresh) {
    const cached = await readR2Json(env.CBA_KB_R2, `cards/${seasonKey}/${personKey}.json`);
    if (cached && cached.updated_at_utc) {
      const verified = await verifyCardRecord(cached);
      const ageMs = Date.now() - Date.parse(cached.updated_at_utc);
      if (Number.isFinite(ageMs) && ageMs <= ttlHours * 3600 * 1000) {
        return jsonResponse({
          status: "HIT_FRESH",
          person: cached.player_name || person,
          season,
          card_markdown: verified.markdown,
          card_sha256: verified.card_sha256,
          observed_until: cached.observed_until || "",
          coverage_status: cached.coverage_status || "UNKNOWN",
          network_called: false,
        });
      }
    }
  }

  // Atomic concurrency deduplication lock via R2 conditional put
  const lockKey = `locks/${seasonKey}/${personKey}.json`;
  const lockResult = await acquireLockAtomic(env.CBA_KB_R2, lockKey, { person, season });
  const jobId = lockResult.jobDoc.job_id;
  const ownerId = lockResult.jobDoc.owner_id || "";
  const deduplicated = !lockResult.acquired;

  if (lockResult.acquired) {
    await writeR2Json(env.CBA_KB_R2, `jobs/${jobId}.json`, lockResult.jobDoc);
    await triggerCollectorRunner(env, {
      person,
      season,
      job_id: jobId,
      owner_id: ownerId,
      force_refresh: forceRefresh,
    });
  }

  // Bounded poll up to 28s to avoid 45s client timeout
  const waitTimeoutMs = Number((env && env.WAIT_TIMEOUT_MS) ?? WAIT_TIMEOUT_MS);
  const pollIntervalMs = Number((env && env.POLL_INTERVAL_MS) ?? POLL_INTERVAL_MS);
  const deadline = Date.now() + Math.max(0, waitTimeoutMs);
  while (Date.now() < deadline) {
    await sleep(Math.min(pollIntervalMs, Math.max(0, deadline - Date.now())));
    const polled = await readR2Json(env.CBA_KB_R2, `jobs/${jobId}.json`);
    if (polled && polled.status && polled.status !== "RUNNING") {
      await verifyAndAttachJobCard(env, polled, seasonKey, personKey);
      return jsonResponse(polled, 200);
    }
  }

  return jsonResponse({
    status: "ACQUIRING_IN_CLOUD",
    job_id: jobId,
    person,
    season,
    deduplicated,
    next_action: "call check_acquisition_job",
  }, 200);
}

export default {
  async fetch(request, env) {
    if (!verifyAuthorization(request, env)) {
      return jsonResponse({ error: "UNAUTHORIZED" }, 401);
    }
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    try {
      if (method === "GET" && path === "/v1/coverage") {
        return await handleCoverage(env);
      }
      if (method === "GET" && path.startsWith("/v1/jobs/")) {
        const jobId = decodeURIComponent(path.slice("/v1/jobs/".length)).replace(/^\/+|\/+$/g, "");
        if (!jobId || jobId.includes("/") || jobId.includes("\\") || jobId.includes("..")) {
          return jsonResponse({ error: "INVALID_JOB_ID" }, 400);
        }
        return await handleJobStatus(env, jobId);
      }
      if (method === "POST" && path === "/v1/person") {
        return await handlePersonQuery(request, env);
      }
      return jsonResponse({ error: "NOT_FOUND" }, 404);
    } catch (err) {
      const msg = err && err.message ? String(err.message) : "INTERNAL_ERROR";
      if (msg.includes("R2_OBJECT_SHA256_MISMATCH")) {
        return jsonResponse({ error: "R2_OBJECT_SHA256_MISMATCH" }, 500);
      }
      return jsonResponse({ error: "INTERNAL_ERROR" }, 500);
    }
  },
};
