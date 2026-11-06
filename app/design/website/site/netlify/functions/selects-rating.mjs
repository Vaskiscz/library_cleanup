// /api/selects-rating: the Selects App Store rating, worldwide, plus the live
// version, so the site never needs a hand edit (or a deploy) to stay current.
//
// WHY THIS SHAPE
// - Apple's public lookup API answers per STOREFRONT: `userRatingCount` for
//   Germany is Germany's ratings only. App Store Connect's worldwide figure is
//   the sum over every storefront, which is what this adds up (checked
//   2026-11-06: 24 storefronts with ratings summed to exactly 107, matching
//   ASC). No credentials: it is the same public data the App Store shows.
// - It runs on Netlify, not in the visitor's browser, so a visitor's page
//   never talks to Apple, and Apple never sees who is reading.
// - Netlify's CDN keeps the answer for a day (durable, shared by every edge),
//   so Apple is asked about once a day however many people visit. Updates
//   need no deploy, which is the point: deploys are rationed.
// - Apple asks for roughly 20 lookups a minute and this makes ~175, so they
//   go out a few at a time, and a failed storefront is retried once.
//
// SAFETY: a storefront that still fails is left out, which can only make the
// count SMALLER. The pages therefore never show a number below the one baked
// into their HTML (see the script in partials/rating.html), and a partial
// answer is cached for an hour rather than a day so it heals quickly.

const APP_ID = "6790501172";

// Every App Store storefront (ISO 3166 alpha-2, lower case).
const STOREFRONTS = (
  "ae ag ai al am ao ar at au az ba bb be bf bg bh bj bm bn bo br bs bt bw by bz " +
  "ca cd cg ch ci cl cm cn co cr cv cy cz de dk dm do dz ec ee eg es fi fj fm fr " +
  "ga gb gd ge gh gm gr gt gw gy hk hn hr hu id ie il in iq is it jm jo jp ke kg " +
  "kh kn kr kw ky kz la lb lc lk lr lt lu lv ly ma md me mg mk ml mn mo mr ms mt " +
  "mu mv mw mx my mz na ne ng ni nl no np nr nz om pa pe pg ph pk pl pt pw py qa " +
  "ro rs ru rw sa sb sc se sg si sk sl sn sr st sv sz tc td th tj tm tn to tr tt " +
  "tw tz ua ug us uy uz vc ve vg vn vu xk ye za zm zw"
).split(" ");

// ~3.5 s for all storefronts at 10 at a time (6.3 s at 6, measured). Netlify
// stops a function at 10 s, so anything not asked by DEADLINE counts as
// failed and the answer is still returned in time.
const CONCURRENCY = 10;
const DEADLINE_MS = 7500;

async function lookup(country) {
  const url = `https://itunes.apple.com/lookup?id=${APP_ID}&country=${country}&entity=software`;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
      if (res.ok) {
        const app = (await res.json()).results?.[0];
        if (!app) return { country, count: 0 };   // not sold there
        return {
          country,
          count: app.userRatingCount || 0,
          average: app.averageUserRating || 0,
          version: app.version,
          released: app.currentVersionReleaseDate,
        };
      }
    } catch { /* timeout or network: retry once */ }
    await new Promise((r) => setTimeout(r, 400));
  }
  return { country, failed: true };
}

export default async () => {
  const start = Date.now();
  const results = [];
  let next = 0;
  await Promise.all(Array.from({ length: CONCURRENCY }, async () => {
    while (next < STOREFRONTS.length) {
      const country = STOREFRONTS[next++];
      results.push(Date.now() - start > DEADLINE_MS ? { country, failed: true } : await lookup(country));
    }
  }));

  const rated = results.filter((r) => r.count > 0);
  const failed = results.filter((r) => r.failed).map((r) => r.country);
  const count = rated.reduce((n, r) => n + r.count, 0);
  const average = count ? rated.reduce((s, r) => s + r.count * r.average, 0) / count : null;
  const us = results.find((r) => r.country === "us" && r.version) || results.find((r) => r.version);

  if (!count) {
    return Response.json({ ok: false }, {
      status: 503,
      headers: { "Cache-Control": "no-store", "Netlify-CDN-Cache-Control": "public, durable, s-maxage=300" },
    });
  }

  const day = 86400, hour = 3600;
  const keep = failed.length ? hour : day;
  return Response.json({
    ok: true,
    count,
    average: Math.round(average * 100) / 100,
    version: us?.version ?? null,
    released: us?.released ?? null,
    storefronts: rated.length,
    failed,
    checked: new Date().toISOString(),
  }, {
    headers: {
      // browsers: an hour; Netlify's CDN: a day (an hour if anything failed),
      // serving the old answer while it fetches a new one in the background
      "Cache-Control": `public, max-age=${hour}`,
      "Netlify-CDN-Cache-Control": `public, durable, s-maxage=${keep}, stale-while-revalidate=${day}`,
    },
  });
};

export const config = { path: "/api/selects-rating" };
