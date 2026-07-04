# Deploying vaclavtrnka.cz

This folder is the complete, ready-to-publish website. It's static HTML — no build step.

```
site/
├── index.html                  → vaclavtrnka.cz            (portfolio / directory)
├── favicon.svg
├── netlify.toml                → Netlify config (publish this folder as-is)
└── library-cleanup/
    ├── index.html              → vaclavtrnka.cz/library-cleanup/   (app page)
    └── favicon.svg
```

## Before you publish

The Library Cleanup page has a download button pointing at the placeholder
`DOWNLOAD_URL` (3 places in `library-cleanup/index.html`). Replace it with the
real `.dmg` link — the simplest host is a **GitHub Release asset**:

1. In a GitHub repo, create a Release and attach `Library Cleanup-0.1.20.dmg`.
2. Copy the asset URL (looks like
   `https://github.com/<you>/<repo>/releases/download/v0.1.20/Library.Cleanup-0.1.20.dmg`).
3. Find/replace `DOWNLOAD_URL` with it in `library-cleanup/index.html`.

## Option A — Git + Netlify (recommended: auto-deploys on every push)

1. Put this `site/` folder in its own GitHub repo (e.g. `vaclavtrnka-web`).
2. In Netlify → **Add new site → Import from Git** → pick the repo.
3. Build command: *(leave empty)*. Publish directory: `.` (already set by `netlify.toml`).
4. Deploy. Every `git push` now redeploys automatically.

## Option B — Drag & drop (fastest, no Git)

1. Netlify → **Add new site → Deploy manually**.
2. Drag this `site/` folder onto the drop zone. Done.
   (To update later, drag the folder again — no version history, though.)

## Point vaclavtrnka.cz at it

1. Netlify site → **Domain management → Add a domain** → `vaclavtrnka.cz`.
2. Netlify shows the DNS records. At your domain registrar set either:
   - **Netlify DNS** (easiest): change the domain's nameservers to the ones
     Netlify lists; or
   - **External DNS**: add an `A` record for `@` → `75.2.60.5` and a `CNAME`
     for `www` → `<your-site>.netlify.app`.
3. Netlify provisions a free HTTPS certificate automatically (a few minutes).
