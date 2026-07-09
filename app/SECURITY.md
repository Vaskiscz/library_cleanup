# Security model — Library Cleanup

> This is the detailed technical security model. To **report a vulnerability**,
> or for supported versions and the reporting process, see the repository
> [security policy](../SECURITY.md).

Library Cleanup is a single-user, **on-device** macOS app. It reads the Photos
library locally (osxphotos + Full Disk Access), computes on-device (Apple
Vision), stores review state in a local SQLite file, and deletes via PhotoKit.
**Nothing is uploaded** — there is no network egress, no cloud, no telemetry,
no credentials.

## Trust boundaries & controls

- **Local API** — a FastAPI service on `127.0.0.1` is the only interface the
  WebView talks to.
  - Bound to loopback only (not the network).
  - **Host-header allowlist** (`TrustedHostMiddleware`: 127.0.0.1 / localhost) —
    rejects **DNS-rebinding** attempts where a malicious website resolves a
    domain to 127.0.0.1 to reach the API.
  - Default CORS (none) — browsers can't read cross-origin responses.
  - `/docs` and `/openapi.json` are disabled.
- **Deletion** — `/api/delete` goes through PhotoKit, so **macOS shows its own
  confirmation dialog** before anything is removed, and removed items go to
  Recently Deleted (recoverable ~30 days). There is no silent-delete path.
- **SQLite store** — all queries are parameterized (no SQL injection).
- **Thumbnails** — `/api/thumb/{uuid}` is a dict-key lookup; file paths come
  from indexed records, not the URL (no path traversal).
- **Diagnostic log** — `~/Library/Logs/Library Cleanup/` only; the home path is
  scrubbed to `~` before logging so a shared log doesn't leak the username.
- **Code signing** — the app is signed with a local self-signed cert that is
  **untrusted** (never a system trust root); it provides a stable identity for
  TCC, not chain trust.

## Known residual risks (accepted, documented)

1. **No per-request auth token.** A *malicious local process already running on
   the machine* could call the local API while the app is open and read photo
   metadata via the app's Full Disk Access. Accepted because: the remote (web)
   vector is closed by the Host allowlist; an attacker with local code execution
   has stronger avenues already; and the only destructive action is OS-confirmed.
   If the threat model changes, add a token minted by the Toga app and passed to
   the WebView via its load URL (`/?t=…`), required on `/api/*` (header for fetch,
   `?t=` for `<img>` thumbnails) — never served in the page so other processes
   can't scrape it.
2. **Unsigned / not notarized distribution.** Recipients must bypass Gatekeeper
   once (right-click → Open). Fix = Apple Developer ID + notarization.
3. **Image-decoder CVEs.** Pillow/Vision decode image files; keep Pillow current.
   Low risk — they're the user's own photos.
