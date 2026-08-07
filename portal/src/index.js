/**
 * gh-fleet-analytics — serves the private portal page from a Cloudflare Worker.
 *
 * HTTP Basic auth over TLS, against a single shared password held as a Worker secret.
 * Not Cloudflare Access: Access is the better answer for a team, but it needs dashboard
 * configuration and an identity provider, and a personal portal has one reader. Basic
 * auth costs one secret, works in every browser without a login page to build, and can
 * be swapped for Access later without touching the page itself.
 *
 * The page is baked into the Worker at deploy time rather than fetched from anywhere, so
 * there is no origin to secure separately and no way for the HTML to leak through a
 * misconfigured bucket. Re-render and re-deploy to update it.
 *
 * The portal is the surface that shows private repo names, so it fails CLOSED: with no
 * password configured it serves 503 rather than the data.
 */

import PORTAL_HTML from "../page.html";

/** Shown in the browser's own sign-in dialog, which is the only branding this page has
 *  before you are through the door. Set PORTAL_REALM in wrangler.jsonc `vars` to change
 *  it — it is not a secret. */
const DEFAULT_REALM = "Fleet analytics";

/** Constant-time-ish comparison. A plain === on a secret leaks its length and, in
 *  principle, its prefix through timing. A portal is a low-value target and Workers
 *  add far more jitter than this measures, but the correct comparison is three lines. */
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const ab = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

function unauthorised(realm) {
  return new Response("Authentication required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": `Basic realm="${realm}", charset="UTF-8"`,
      "Content-Type": "text/plain; charset=utf-8",
    },
  });
}

export default {
  async fetch(request, env) {
    const realm = env.PORTAL_REALM || DEFAULT_REALM;

    if (!env.PORTAL_PASSWORD) {
      // Fail closed, and say why. A portal that serves the whole fleet's numbers
      // because a secret was missing is the one failure mode worth being loud about.
      return new Response(
        "Portal is not configured: the PORTAL_PASSWORD secret is not set.",
        { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } },
      );
    }

    const header = request.headers.get("Authorization") || "";
    if (!header.startsWith("Basic ")) return unauthorised(realm);

    let decoded;
    try {
      decoded = atob(header.slice(6));
    } catch {
      return unauthorised(realm);
    }
    // "user:password" — the username is ignored, only the password is checked.
    const password = decoded.slice(decoded.indexOf(":") + 1);
    if (!safeEqual(password, env.PORTAL_PASSWORD)) return unauthorised(realm);

    return new Response(PORTAL_HTML, {
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        // Never cached anywhere but the reader's own tab, and never indexed.
        "Cache-Control": "private, no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
      },
    });
  },
};
