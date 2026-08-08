/* share-it hosted uploader — R2-backed, zero-setup backend for the packaged app.
 *
 * Bundles (v3): a share is N objects under one prefix, committed by writing the
 * manifest LAST (R2 is strongly consistent, so a visible manifest guarantees
 * every referenced object exists — partial uploads are invisible orphans).
 *
 * POST   /bundle/new                 → { id }                      (X-Share-Token)
 * POST   /bundle/<id>/<name>        body = bytes                   (X-Share-Token)
 * POST   /bundle/<id>/commit       body = manifest json            (X-Share-Token)
 * GET    /b/<id>                   → serves the bundle's index doc
 * GET    /b/<id>/<name>            → serves a bundle object (manifest-gated)
 * DELETE /b/<id>                   → cascade-delete whole bundle   (X-Share-Token)
 *
 * Legacy single-object routes stay for old links:
 * POST /up · GET|DELETE /p/<key>
 * cron: daily sweep of expired manifests (delete manifest first, then sweep).
 */

const MAX_BYTES = 25 * 1024 * 1024;
const MAX_BUNDLE_BYTES = 100 * 1024 * 1024;
const MAX_BUNDLE_OBJECTS = 64;
const ALLOWED_HOURS = new Set([0, 24, 72, 168]);

const token = () => {
  const a = new Uint8Array(16);  // 128-bit unguessable
  crypto.getRandomValues(a);
  return btoa(String.fromCharCode(...a)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
};

const sanitize = (name) =>
  (name || "").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 80);

const authed = (req, env) => req.headers.get("X-Share-Token") === env.SHARE_TOKEN;

// user-supplied content must never run script on this origin
const SAFE_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "X-Robots-Tag": "noindex",
  "Cache-Control": "no-transform",
};
const ACTIVE = /text\/html|image\/svg|application\/xhtml/i;

async function getManifest(env, id) {
  const obj = await env.LINKS.get(`b/${id}/manifest.json`);
  if (!obj) return null;
  try { return await obj.json(); } catch { return null; }
}

async function deleteBundle(env, id) {
  // manifest first: the bundle vanishes atomically, then sweep data objects
  await env.LINKS.delete(`b/${id}/manifest.json`);
  let cursor;
  do {
    const page = await env.LINKS.list({ prefix: `b/${id}/`, cursor });
    if (page.objects.length)
      await env.LINKS.delete(page.objects.map((o) => o.key));
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
}

function serveObject(obj, name, manifest, head) {
  const ct = obj.httpMetadata?.contentType || "application/octet-stream";
  const headers = { ...SAFE_HEADERS, "Content-Type": ct };
  if (ACTIVE.test(ct) && !(manifest?.trusted || []).includes(name)) {
    // user artifact HTML/SVG: render in an opaque origin, no scripts reach ours
    headers["Content-Security-Policy"] = "sandbox allow-scripts";
  }
  return new Response(head ? null : obj.body, { headers });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;

    // ---------- bundle write plane ----------
    if (req.method === "POST" && path === "/bundle/new") {
      if (!authed(req, env)) return new Response("unauthorized", { status: 401 });
      return Response.json({ id: token() });
    }

    let m = path.match(/^\/bundle\/([A-Za-z0-9_-]{16,32})\/commit$/);
    if (req.method === "POST" && m) {
      if (!authed(req, env)) return new Response("unauthorized", { status: 401 });
      const id = m[1];
      let manifest;
      try { manifest = await req.json(); } catch { return new Response("bad manifest", { status: 400 }); }
      if (!manifest || typeof manifest.index !== "string" || !Array.isArray(manifest.objects))
        return new Response("bad manifest", { status: 400 });
      let hours = Number(manifest.hours ?? 0);
      if (!ALLOWED_HOURS.has(hours)) hours = 168;
      if (!manifest.objects.some((o) => sanitize(o.name) === sanitize(manifest.index)))
        return new Response("index not in objects", { status: 400 });
      if (manifest.objects.length > MAX_BUNDLE_OBJECTS)
        return Response.json({ error: `too many objects (max ${MAX_BUNDLE_OBJECTS})` }, { status: 413 });
      // verify EVERY referenced object landed with the promised size + total cap
      let total = 0;
      for (const o of manifest.objects) {
        const headObj = await env.LINKS.head(`b/${id}/${sanitize(o.name)}`);
        if (!headObj || (o.size != null && headObj.size !== o.size))
          return Response.json({ error: `missing or size-mismatched: ${o.name}` }, { status: 409 });
        total += headObj.size;
      }
      if (total > MAX_BUNDLE_BYTES)
        return Response.json({ error: `bundle exceeds ${MAX_BUNDLE_BYTES} bytes` }, { status: 413 });
      manifest.hours = hours;
      manifest.committedAt = Date.now();
      const meta = {};
      if (hours > 0) meta.expiresAt = String(Date.now() + hours * 3600 * 1000);
      // create-once: a bundle id can never be re-committed to different content
      const put = await env.LINKS.put(`b/${id}/manifest.json`, JSON.stringify(manifest), {
        httpMetadata: { contentType: "application/json" },
        customMetadata: meta,
        onlyIf: { etagDoesNotMatch: "*" },
      });
      if (!put) return new Response("already committed", { status: 409 });
      return Response.json({ url: `${url.origin}/b/${id}`, id, hours: hours || null });
    }

    m = path.match(/^\/bundle\/([A-Za-z0-9_-]{16,32})\/([A-Za-z0-9._-]{1,80})$/);
    if (req.method === "POST" && m) {
      if (!authed(req, env)) return new Response("unauthorized", { status: 401 });
      const [, id, name] = m;
      if (name === "manifest.json") return new Response("reserved", { status: 400 });
      if (await env.LINKS.head(`b/${id}/manifest.json`))
        return new Response("bundle already committed", { status: 409 });
      const claimed = Number(req.headers.get("Content-Length") || 0);
      if (claimed > MAX_BYTES) return new Response("too large", { status: 413 });
      const body = await req.arrayBuffer();
      if (!body.byteLength || body.byteLength > MAX_BYTES)
        return new Response("bad size", { status: 413 });
      await env.LINKS.put(`b/${id}/${name}`, body, {
        httpMetadata: { contentType: req.headers.get("Content-Type") || "application/octet-stream" },
      });
      return Response.json({ ok: true, name, size: body.byteLength });
    }

    // ---------- bundle read/delete plane ----------
    m = path.match(/^\/b\/([A-Za-z0-9_-]{16,32})(?:\/([A-Za-z0-9._-]{1,80}))?$/);
    if (m) {
      const [, id, name] = m;
      if (req.method === "DELETE" && !name) {
        if (!authed(req, env)) return new Response("unauthorized", { status: 401 });
        await deleteBundle(env, id);
        return new Response("deleted");
      }
      if (req.method === "GET" || req.method === "HEAD") {
        const manifest = await getManifest(env, id);   // binding read — never cached
        if (!manifest) return new Response("not found", { status: 404 });
        if (manifest.hours > 0 &&
            manifest.committedAt + manifest.hours * 3600 * 1000 < Date.now()) {
          await deleteBundle(env, id);
          return new Response("expired", { status: 404 });
        }
        const want = sanitize(name || manifest.index);
        // the manifest is the allowlist — uncommitted strays never serve
        const listed = (manifest.objects || []).some((o) => sanitize(o.name) === want);
        if (!listed) return new Response("not found", { status: 404 });
        const obj = await env.LINKS.get(`b/${id}/${want}`);
        if (!obj) return new Response("not found", { status: 404 });
        return serveObject(obj, want, manifest, req.method === "HEAD");
      }
    }

    // ---------- legacy single-object plane ----------
    if (req.method === "POST" && path === "/up") {
      if (!authed(req, env)) return new Response("unauthorized", { status: 401 });
      const claimed = Number(req.headers.get("Content-Length") || 0);
      if (claimed > MAX_BYTES) return new Response("too large", { status: 413 });
      const body = await req.arrayBuffer();
      if (!body.byteLength || body.byteLength > MAX_BYTES)
        return new Response("bad size", { status: 413 });
      let hours = Number(req.headers.get("X-Expiry-Hours") || 0);
      if (!ALLOWED_HOURS.has(hours)) hours = 168;
      const name = sanitize(req.headers.get("X-Name"));
      const key = `p/${token()}${name ? "-" + name : ""}`;
      const meta = {};
      if (hours > 0) meta.expiresAt = String(Date.now() + hours * 3600 * 1000);
      await env.LINKS.put(key, body, {
        httpMetadata: { contentType: req.headers.get("Content-Type") || "text/markdown" },
        customMetadata: meta,
      });
      return Response.json({ url: `${url.origin}/${key}`, key, hours: hours || null });
    }

    if (path.startsWith("/p/")) {
      const key = path.slice(1);
      if (req.method === "DELETE") {
        if (!authed(req, env)) return new Response("unauthorized", { status: 401 });
        await env.LINKS.delete(key);
        return new Response("deleted");
      }
      if (req.method === "GET" || req.method === "HEAD") {
        const obj = await env.LINKS.get(key);
        if (!obj) return new Response("not found", { status: 404 });
        const exp = obj.customMetadata?.expiresAt;
        if (exp && Number(exp) < Date.now()) {
          await env.LINKS.delete(key);
          return new Response("expired", { status: 404 });
        }
        return serveObject(obj, key, null, req.method === "HEAD");
      }
    }
    return new Response("share-it links", { status: 404 });
  },

  async scheduled(_evt, env) {
    const now = Date.now();
    let cursor;
    do {
      const page = await env.LINKS.list({ cursor, include: ["customMetadata"] });
      for (const o of page.objects) {
        const exp = o.customMetadata?.expiresAt;
        if (!(exp && Number(exp) < now)) continue;
        const bm = o.key.match(/^b\/([A-Za-z0-9_-]{16,32})\/manifest\.json$/);
        if (bm) await deleteBundle(env, bm[1]);
        else if (!o.key.startsWith("b/")) await env.LINKS.delete(o.key);
      }
      cursor = page.truncated ? page.cursor : undefined;
    } while (cursor);
  },
};
