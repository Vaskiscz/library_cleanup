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
                       analytics (empty for now: the site has no analytics)
    site/              built output, what actually deploys

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

The site currently has **no analytics at all**. A cookieless PostHog setup is
parked in `partials/analytics-posthog.parked.html`. To switch it on:
1. It needs its own PostHog project (EU), never the Selects app's. The current
   PostHog organization is on the free plan, which allows one project, and
   Selects uses it: a second free organization or a paid plan is needed first.
2. In that project, turn cookieless mode on (or its events are dropped) and
   "Discard client IP data" on.
3. Put the project key in the parked file, rename it to `analytics.html`.
4. Rewrite the "Visit statistics" part of `privacy.html` to describe it. The
   notice currently says the site collects none, and it must stay true.
    build.sh           heads/ + fragments -> site/   (byte-reproducible)
    check-drift.sh     compares site/ against the deploy repo before you copy

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
