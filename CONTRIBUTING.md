# Contributing to Library Cleanup

Thanks for your interest! Before you send a change, please read this — it explains
the licensing terms your contribution is made under. **Opening a pull request (or
otherwise submitting a contribution) means you agree to these terms.**

## The project's license

Library Cleanup is released to the public under the
[PolyForm Noncommercial 1.0.0](LICENSE) license: free for noncommercial use,
no commercial use or selling without a separate license from the author.

The author, **Václav Trnka**, retains full ownership of the project and may also
offer it under other terms — including commercial or proprietary licenses. For
that to remain possible, contributions have to be made under the grant below.

## Contribution license (please read)

By submitting a contribution to this project (a "Contribution" — any code,
documentation, or other material you propose for inclusion, e.g. via a pull
request), you agree that:

1. **License grant.** You grant Václav Trnka a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable license to use, reproduce, modify,
   adapt, publish, distribute, publicly display and perform, sublicense, **and
   relicense** your Contribution and derivative works of it, **under any license
   terms — including proprietary or commercial terms** — without any further
   permission, notice, or compensation to you.

2. **You keep your copyright.** You are not assigning ownership; you retain
   copyright in your Contribution. This grant simply lets the project be
   relicensed and sold in the future without needing to track down every
   contributor for permission.

3. **You have the right to grant this.** You represent that each Contribution is
   your own original work (or you otherwise have the right to submit it under
   these terms), and that it does not knowingly infringe anyone else's rights.
   If your employer has rights to work you create, you confirm you have
   permission to make the Contribution.

4. **No warranty.** Contributions are provided "as is," without warranty of any
   kind.

If you do not agree to these terms, please don't submit a contribution — but
you're still very welcome to use the project under the PolyForm Noncommercial
license and to open issues.

## How to contribute

- **Issues / ideas:** open a GitHub issue — no agreement needed just to discuss.
- **Code:** fork, branch, and open a pull request. Please run the checks
  first: `uv run ruff check && uv run pytest -q` (one suite covers the CLI
  package and the app). CI runs the same two commands on every push/PR.
- Keep changes focused; match the surrounding code style.

*This document is a practical measure, not formal legal advice. For a large or
company contribution, a signed contributor agreement may be worth arranging.*
