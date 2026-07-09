# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue with
exploit details.

- Preferred: GitHub's **[Report a vulnerability](https://github.com/Vaskiscz/library_cleanup/security/advisories/new)**
  (repo **Security** tab → *Advisories* → *Report a vulnerability*). This opens a
  private channel visible only to the maintainer.
- If that isn't available to you, open a normal issue that simply asks the
  maintainer to open a private channel — **without** any exploit details.

Please include, where possible: the affected version, the platform, and steps to
reproduce. You'll get an acknowledgement within about **5 business days**, and a
fix or mitigation plan once the report is confirmed.

## Supported versions

Only the latest released version receives security fixes. The app auto-updates
from GitHub Releases, so keeping it current is the supported path.

| Version | Supported |
| ------- | --------- |
| Latest release | ✅ |
| Older releases | ❌ |

## Scope & threat model

Library Cleanup runs **entirely on your Mac**. It reads your Photos library
locally and never uploads photos or metadata anywhere.

- The **only** outbound network request is an anonymous update check to the
  public GitHub Releases API (and downloading a release when you choose to
  update). No account, no analytics, no telemetry.
- There is no backend server and no remote storage; the local service binds to
  `127.0.0.1` only.

Security reports of most interest include: anything that could exfiltrate photo
data or metadata off-device, tamper with the auto-update path, or let a local/
network attacker reach the loopback service or the app's file handling.
