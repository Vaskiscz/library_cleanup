# vaclavtrnka.cz website

`site/` is the deploy artifact: its contents are mirrored into the
[vaclavtrnka-web](https://github.com/Vaskiscz/vaclavtrnka-web) repo, which
Netlify publishes. Everything else here is source.

## Layout

    heads/<fragment>   per-page <head> (meta, OG tags, Clarity snippet, ... <body>)
    <fragment>.html    the page body: portfolio, index (= Library Cleanup),
                       selects, privacy, selects-privacy
    site/              built output — what actually deploys
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
- Support address: `vaclavtrnkaproductsupport@gmail.com`.
- Brand green is `--brand` for fills and icons; use `--brand-text` (`#147966`)
  for text, which clears WCAG AA on the light background.
- Factual claims (download size, OS requirements, what the app sends over the
  network) must match the shipped build. Verify against the release, not memory.
