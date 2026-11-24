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
const WAIT_TIMEOUT_MS = 28000;
const POLL_INTERVAL_MS = 2000;
const AUTH_SCHEME = "bearer";

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

function safeKey(raw) {
  return encodeURIComponent(String(raw || "").trim()).replace(/%/g, "_");
}

async function readR2Json(bucket, key) {
  if (!bucket) return null;
  const obj = await bucket.get(key);
  if (!obj) return null;
  return obj.json();
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
  if (!ledger) {
    return jsonResponse({
      overall_status: "UNKNOWN",
      observed_until: "",
      domains: {},
      missing_items: [],
      honest_disclosure: "Coverage ledger is not yet published in cloud state.",
    });
  }
  return jsonResponse({
    overall_status: ledger.overall_status || "UNKNOWN",
    observed_until: ledger.observed_until || ledger.generated_at_utc || "",
    domains: ledger.domains || {},
    missing_items: ledger.missing_items || [],
    honest_disclosure: ledger.honest_disclosure || "",
  });
}

async function handleJobStatus(env, jobId) {
  const job = await readR2Json(env.CBA_KB_R2, `jobs/${jobId}.json`);
  if (!job) {
    return jsonResponse({ status: "NOT_FOUND", job_id: jobId }, 404);
  }
  if (job.status === "COMPLETED" && !job.card_markdown && job.person && job.season) {
    const card = await readR2Json(
      env.CBA_KB_R2,
      `cards/${job.season}/${safeKey(job.person)}.json`,
    );
    if (card && card.markdown) {
      job.card_markdown = card.markdown;
      job.card_sha256 = card.card_sha256 || job.card_sha256;
    }
  }
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
  const personKey = safeKey(person);

  // Fast path: fresh cached card in R2 (< 0.5s)
  if (!forceRefresh) {
    const cached = await readR2Json(env.CBA_KB_R2, `cards/${season}/${personKey}.json`);
    if (cached && cached.markdown && cached.updated_at_utc) {
      const ageMs = Date.now() - Date.parse(cached.updated_at_utc);
      if (Number.isFinite(ageMs) && ageMs <= ttlHours * 3600 * 1000) {
        return jsonResponse({
          status: "HIT_FRESH",
          person: cached.player_name || person,
          season,
          card_markdown: cached.markdown,
          card_sha256: cached.card_sha256 || "",
          observed_until: cached.observed_until || "",
          coverage_status: cached.coverage_status || "UNKNOWN",
          network_called: false,
        });
      }
    }
  }

  // Concurrency deduplication lock
  const lockKey = `locks/${season}/${personKey}.json`;
  const activeLock = await readR2Json(env.CBA_KB_R2, lockKey);
  let jobId = activeLock && activeLock.status === "RUNNING" ? activeLock.job_id : null;
  let deduplicated = Boolean(jobId);

  if (!jobId) {
    jobId = `job_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    const jobDoc = {
      job_id: jobId,
      status: "RUNNING",
      person,
      season,
      created_at_utc: new Date().toISOString(),
    };
    if (env.CBA_KB_R2) {
      await env.CBA_KB_R2.put(`jobs/${jobId}.json`, JSON.stringify(jobDoc));
      await env.CBA_KB_R2.put(lockKey, JSON.stringify(jobDoc));
    }
    await triggerCollectorRunner(env, { person, season, job_id: jobId, force_refresh: forceRefresh });
  }

  // Bounded poll up to 28s to avoid 45s client timeout
  const deadline = Date.now() + WAIT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(Math.min(POLL_INTERVAL_MS, Math.max(0, deadline - Date.now())));
    const polled = await readR2Json(env.CBA_KB_R2, `jobs/${jobId}.json`);
    if (polled && polled.status && polled.status !== "RUNNING") {
      if (polled.status === "COMPLETED" && !polled.card_markdown) {
        const card = await readR2Json(env.CBA_KB_R2, `cards/${season}/${personKey}.json`);
        if (card && card.markdown) {
          polled.card_markdown = card.markdown;
          polled.card_sha256 = card.card_sha256 || polled.card_sha256;
        }
      }
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

    if (method === "GET" && path === "/v1/coverage") {
      return handleCoverage(env);
    }
    if (method === "GET" && path.startsWith("/v1/jobs/")) {
      const jobId = decodeURIComponent(path.slice("/v1/jobs/".length)).replace(/^\/+|\/+$/g, "");
      if (!jobId || jobId.includes("/") || jobId.includes("\\") || jobId.includes("..")) {
        return jsonResponse({ error: "INVALID_JOB_ID" }, 400);
      }
      return handleJobStatus(env, jobId);
    }
    if (method === "POST" && path === "/v1/person") {
      return handlePersonQuery(request, env);
    }
    return jsonResponse({ error: "NOT_FOUND" }, 404);
  },
};
