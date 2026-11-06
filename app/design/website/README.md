# vaclavtrnka.cz website

`site/` is the deploy artifact: its contents are mirrored into the
[vaclavtrnka-web](https://github.com/Vaskiscz/vaclavtrnka-web) repo, which
Netlify publishes. Everything else here is source.

## Layout

    heads/<fragment>   per-page <head> (meta, OG tags, ... <body>)
    <fragment>.html    the page body: portfolio, index (= Library Cleanup),
                       selects, privacy, selects-privacy, selects-terms,
                       selects-press, 404
    partials/          shared pieces, pulled in by `<!--#include NAME-->`:
                       header (top bar, phone menu, bottom download bar),
                       operator (the legal line in every footer),
                       analytics (cookieless PostHog visit statistics),
                       asc-sync (fills the App Store numbers, see below)
    site/              built output, what actually deploys. Like netlify.toml,
                       the images and the sitemap, the one Netlify function
                       is edited in place here:
                       site/netlify/functions/selects-rating.mjs

Each page's `.site` root carries `data-theme` (dark | light) and `data-page`
(home | selects | library-cleanup | privacy); the shared header reads both.
**Projects are separate topics, by the owner's rule (2026-11-06).** Only the
hub links to them: the home page and the site privacy notice. A project page
(Selects and its privacy, terms and press pages; Library Cleanup) never links
to, shows or names another project: no companion sections, no "how is it
different from" FAQ, no sibling links in the header. `build.sh` enforces it
(partial lines marked `data-hub-only` / `data-only="<page>"` are dropped, and
the build fails if another project appears in a link or in visible text). The
page id passed to `build` must match the page's `data-page`.

**No consent banner, by the owner's rule.** So nothing may set cookies or use
browser storage for tracking: no session replay, no embedded widget that sets
cookies. Microsoft Clarity was removed for this reason.

**Visit statistics: PostHog, cookieless** (switched on 2026-11-06), in
`partials/analytics.html`, included on every page. Page views plus the named
clicks: any element with `data-evt="name"` sends that name. No autocapture,
no session recording, no heatmaps, no surveys, and it runs only on the live
domain, so previews send nothing.
- It is the website's **own PostHog project, EU Cloud, in a separate PostHog
  account** from the Selects app's (the free plan allows one project per
  organization). Never point it at the app's project.
- That project must keep **"Enable cookieless tracking" ON** (Settings →
  Project → Web analytics), or PostHog drops every event, and **"Discard
  client IP data" ON**, which the privacy notice promises.
- **No country.** Cookieless mode strips the IP before PostHog's GeoIP step
  (posthog/posthog#48660), so `$geoip_*` stays empty; `$timezone` is the
  rough stand-in. The privacy notice says no location is recorded, so a fix
  on PostHog's side that starts adding it means updating the notice.
- What it records is described in the "Visit statistics, without cookies"
  section of `privacy.html`. Change the two together.
    build.sh           heads/ + fragments -> site/   (byte-reproducible)
    check-drift.sh     compares site/ against the deploy repo before you copy

## App Store numbers update themselves

The Selects rating (average, star row, count) on the home page, the Selects
page and the press kit, and the press kit's "Current version" line, are
filled in by `partials/asc-sync.html` from `/api/selects-rating`, a Netlify
function that adds up Apple's public per-storefront ratings (the sum matches
App Store Connect's worldwide figure). Netlify's CDN keeps the answer a day,
so Apple is asked about once a day and an update never needs a deploy.

Mark a number with `data-asc="average" | "stars" | "count" | "version" |
"released"` and include `asc-sync` at the end of the page. The figures written
in the HTML are the fallback (no JS, a failed call, the local preview, which
has no functions), so refresh them by hand when you are editing the page
anyway. A count lower than the written one is ignored, since it can only mean
some storefronts did not answer.

Not synced, on purpose: the usage totals under the Selects hero and in the
press kit (from PostHog, refreshed by hand with a date), and the review
quotes (picked by hand).

To test the function locally: `node` can import it and call its default
export with a `Request`; it needs no Netlify runtime.

## Editing

1. Edit the fragment (or its head), never `site/` directly.
2. `./build.sh`
3. `./check-drift.sh` and resolve anything it reports.
4. Copy `site/` into the deploy repo, commit, push. Netlify does the rest.

## Why check-drift.sh exists

The head templates used to live only in a temp directory, so the build was not
reproducible, and copy edits were twice applied straight to the deploy repo.
`site/` then held *older* content than live, and copying it over would have
silently reverted live copy. Run the check before every copy.

## Conventions

- **No em dashes** in any user-facing copy. Use a period, comma, colon,
  parentheses, or the middle dot `·`.
- Addresses: Selects pages use `selects@vaclavtrnka.cz`; everything else,
  Library Cleanup included (header Contact, operator line, privacy notice),
  uses `info@vaclavtrnka.cz`. The old product-support gmail address is retired.
- Brand green is `--brand` for fills and icons; use `--brand-text` (`#147966`)
  for text, which clears WCAG AA on the light background.
- Factual claims (download size, OS requirements, what the app sends over the
  network) must match the shipped build. Verify against the release, not memory.
