/* share-it hosted uploader — R2-backed, zero-setup backend for the packaged app.
 *
 * POST /up            body = bytes; headers: X-Share-Token, X-Expiry-Hours (0=∞),
 *                     Content-Type, X-Name (optional filename hint)
 * GET  /p/<key>       public read (expired objects 404 and are deleted lazily)
 * DELETE /p/<key>     requires X-Share-Token
 * cron                daily sweep of expired objects
 */

const MAX_BYTES = 25 * 1024 * 1024;
const ALLOWED_HOURS = new Set([0, 24, 72, 168]);

const token = () => {
  const a = new Uint8Array(6);  // 48-bit unguessable, ~8 chars
  crypto.getRandomValues(a);
  return btoa(String.fromCharCode(...a)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
};

const sanitize = (name) =>
  (name || "").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 80);

export default {
  async fetch(req, env) {
    const url = new URL(req.url);

    if (req.method === "POST" && url.pathname === "/up") {
      if (req.headers.get("X-Share-Token") !== env.SHARE_TOKEN)
        return new Response("unauthorized", { status: 401 });
      const len = Number(req.headers.get("Content-Length") || 0);
      if (!len || len > MAX_BYTES)
        return new Response("bad size", { status: 413 });
      let hours = Number(req.headers.get("X-Expiry-Hours") || 0);
      if (!ALLOWED_HOURS.has(hours)) hours = 168;
      const name = sanitize(req.headers.get("X-Name"));
      const key = `p/${token()}${name ? "-" + name : ""}`;
      const meta = {};
      if (hours > 0)
        meta.expiresAt = String(Date.now() + hours * 3600 * 1000);
      await env.LINKS.put(key, req.body, {
        httpMetadata: { contentType: req.headers.get("Content-Type") || "text/markdown" },
        customMetadata: meta,
      });
      return Response.json({ url: `${url.origin}/${key}`, key, hours: hours || null });
    }

    if (url.pathname.startsWith("/p/")) {
      const key = url.pathname.slice(1);
      if (req.method === "DELETE") {
        if (req.headers.get("X-Share-Token") !== env.SHARE_TOKEN)
          return new Response("unauthorized", { status: 401 });
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
        return new Response(req.method === "HEAD" ? null : obj.body, {
          headers: {
            "Content-Type": obj.httpMetadata?.contentType || "text/plain",
            "Cache-Control": "no-transform",
            "X-Robots-Tag": "noindex",
          },
        });
      }
    }
    return new Response("share-it links", { status: 404 });
  },

  async scheduled(_evt, env) {
    let cursor;
    do {
      const page = await env.LINKS.list({ cursor, include: ["customMetadata"] });
      for (const o of page.objects) {
        const exp = o.customMetadata?.expiresAt;
        if (exp && Number(exp) < Date.now()) await env.LINKS.delete(o.key);
      }
      cursor = page.truncated ? page.cursor : undefined;
    } while (cursor);
  },
};
