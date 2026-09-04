// Vercel Node.js serverless function (CommonJS, zero npm deps — this is a
// bare static-site deploy, no package.json). Backs the "flag a problem"
// feature: reads/writes a single repo-root file, drug-flags.json, via the
// GitHub REST API using global fetch (Node 18+ on Vercel).
//
// Mirrors life-log-app's lib/github.ts + lib/passcode.ts patterns —
// read-back-then-merge so two devices flagging at once never clobber each
// other, 404-is-empty reads, constant-time passcode compare, token never
// reaches the client — but talks to GitHub directly instead of through
// octokit, since there's no dependency install step here.

const FLAGS_PATH = "drug-flags.json";
const MAX_FLAGS = 1000;
const MAX_FIELD_LEN = 2000;

function isConfigured() {
  return Boolean(process.env.GITHUB_TOKEN && process.env.GITHUB_REPO && process.env.FLAG_PASSCODE);
}

function ownerRepo() {
  const parts = String(process.env.GITHUB_REPO || "").split("/");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    throw new Error("GITHUB_REPO must be 'owner/repo'");
  }
  return { owner: parts[0], name: parts[1] };
}

// Constant-time-ish compare (length + char accumulation), same shape as
// life-log's checkPasscode — good enough for a one-user PIN gate.
function checkPasscode(provided) {
  const expected = process.env.FLAG_PASSCODE || "";
  if (typeof provided !== "string" || !provided) return false;
  if (provided.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < provided.length; i++) diff |= provided.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

async function ghContents(method, body, query) {
  const { owner, name } = ownerRepo();
  const url =
    `https://api.github.com/repos/${owner}/${name}/contents/${FLAGS_PATH}` + (query || "");
  const res = await fetch(url, {
    method: method,
    headers: {
      "Authorization": `Bearer ${process.env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "drug-webapp-flags",
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  return res;
}

// Read the flags file + its blob sha. 404 -> empty list + null sha, same
// "404-is-null" contract as life-log's readDay/readWorkout — a brand-new repo
// with no drug-flags.json yet is not an error.
async function readFlagsRaw() {
  const branch = process.env.GITHUB_BRANCH || "main";
  const res = await ghContents("GET", null, `?ref=${encodeURIComponent(branch)}`);
  if (res.status === 404) return { flags: [], sha: null };
  if (!res.ok) {
    const err = new Error(`github read failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  const data = await res.json();
  let parsed = null;
  try {
    parsed = JSON.parse(Buffer.from(data.content, "base64").toString("utf-8"));
  } catch (e) {
    parsed = null;
  }
  const flags = parsed && Array.isArray(parsed.flags) ? parsed.flags : [];
  return { flags: flags, sha: data.sha };
}

async function writeFlagsRaw(flags, sha) {
  const branch = process.env.GITHUB_BRANCH || "main";
  const payload = {
    message: `flags: ${flags.length} flag(s)`,
    content: Buffer.from(
      JSON.stringify({ updatedAt: new Date().toISOString(), flags: flags }, null, 2),
      "utf-8"
    ).toString("base64"),
    branch: branch,
  };
  if (sha) payload.sha = sha;
  const res = await ghContents("PUT", payload);
  if (!res.ok) {
    const err = new Error(`github write failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
}

// Keep only known string fields; drop anything without id+text; cap length
// on every free-text field so a bad client can't bloat the committed file.
function sanitizeFlag(input) {
  if (!input || typeof input !== "object") return null;
  const str = (v) => (typeof v === "string" ? v : "");
  const cap = (v, n) => (v.length > n ? v.slice(0, n) : v);
  const id = str(input.id).trim();
  const text = cap(str(input.text).trim(), MAX_FIELD_LEN);
  if (!id || !text) return null;
  return {
    id: id,
    text: text,
    page: cap(str(input.page), 200),
    section: cap(str(input.section), 300),
    path: cap(str(input.path), 500),
    context: cap(str(input.context), MAX_FIELD_LEN),
    note: cap(str(input.note), MAX_FIELD_LEN),
    ts: str(input.ts) || new Date().toISOString(),
    resolved: Boolean(input.resolved),
  };
}

// Apply one op against a freshly-read list. Returns `changed` explicitly so
// the caller can skip the PUT on a true no-op (a delete of an id that's
// already gone, or a delete call with no id at all) — NOT reference equality
// on `next`, because Array.prototype.filter always returns a NEW array even
// when it removed nothing, so `next !== flags` was true (and the GitHub
// write fired) on every delete-not-found call. Caught by Latte 2026-09-04.
function applyOp(flags, op, flagIn) {
  if (op === "delete") {
    const id = flagIn && typeof flagIn.id === "string" ? flagIn.id : null;
    if (!id) return { next: flags, error: null, changed: false };
    const next = flags.filter((f) => f.id !== id);
    return { next: next, error: null, changed: next.length !== flags.length };
  }
  const clean = sanitizeFlag(flagIn);
  if (!clean) return { next: flags, error: "bad flag", changed: false };
  const idx = flags.findIndex((f) => f.id === clean.id);
  if (idx >= 0) {
    const next = flags.slice();
    next[idx] = clean;
    return { next: next, error: null, changed: true };
  }
  if (flags.length >= MAX_FLAGS) return { next: flags, error: "flag limit reached", changed: false };
  return { next: flags.concat([clean]), error: null, changed: true };
}

module.exports = async (req, res) => {
  try {
    if (req.method === "GET") {
      if (!isConfigured()) {
        return res.status(200).json({ ok: true, configured: false, flags: [] });
      }
      try {
        const read = await readFlagsRaw();
        return res.status(200).json({ ok: true, configured: true, flags: read.flags });
      } catch (err) {
        console.error("flags GET failed:", err);
        return res.status(502).json({ ok: false, error: "could not read flags" });
      }
    }

    if (req.method === "POST") {
      // isConfigured() BEFORE checkPasscode() - an unset FLAG_PASSCODE makes
      // checkPasscode(anything) false, so checking passcode first 401'd every
      // POST on an unconfigured deploy (Prat's actual state until he sets the
      // Vercel env vars) with a response carrying no `configured` field. The
      // client read that as "wrong passcode", prompted forever, and never
      // reached the graceful "saved locally, sync isn't set up" path at all.
      // Caught by Latte 2026-09-04.
      if (!isConfigured()) {
        return res.status(200).json({ ok: false, configured: false, error: "sync not set up" });
      }
      if (!checkPasscode(req.headers["x-passcode"])) {
        return res.status(401).json({ ok: false, error: "unauthorized" });
      }

      let body;
      try {
        body = req.body;
        if (typeof body === "string") body = JSON.parse(body);
      } catch (e) {
        return res.status(400).json({ ok: false, error: "bad json" });
      }
      const op = body && body.op === "delete" ? "delete" : "upsert";
      const flagIn = body && body.flag;
      if (!body || !flagIn || typeof flagIn !== "object") {
        return res.status(400).json({ ok: false, error: "bad payload" });
      }

      try {
        // Re-read-then-merge, retry once on a sha conflict (409) — the same
        // fix life-log's commitDay/commitWorkout apply, so two devices
        // flagging at the same moment never silently drop one of them.
        let attempt = 0, flags = null;
        while (attempt < 2) {
          attempt++;
          const read = await readFlagsRaw();
          const applied = applyOp(read.flags, op, flagIn);
          if (applied.error) {
            return res.status(200).json({ ok: false, error: applied.error });
          }
          if (!applied.changed) {
            flags = applied.next; // no-op, nothing to write
            break;
          }
          try {
            await writeFlagsRaw(applied.next, read.sha);
            flags = applied.next;
            break;
          } catch (err) {
            if (err.status === 409 && attempt < 2) continue;
            throw err;
          }
        }
        return res.status(200).json({ ok: true, flags: flags });
      } catch (err) {
        console.error("flags POST failed:", err);
        return res.status(502).json({ ok: false, error: "sync failed" });
      }
    }

    res.setHeader("Allow", "GET, POST");
    return res.status(405).json({ ok: false, error: "method not allowed" });
  } catch (err) {
    console.error("flags handler failed:", err);
    return res.status(502).json({ ok: false, error: "sync failed" });
  }
};
