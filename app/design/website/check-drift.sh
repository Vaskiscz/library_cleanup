#!/usr/bin/env bash
# Guard against the failure mode that nearly reverted live copy: someone edits
# the deploy repo (or site/) directly, the two silently diverge, and the next
# copy from site/ overwrites newer live content.
# Run BEFORE copying site/ over the deploy repo.
set -uo pipefail
cd "$(dirname "$0")"
DEPLOY="${1:-$HOME/Projects/vaclavtrnka-web}"
drift=0
for f in index.html library-cleanup/index.html selects/index.html \
         privacy/index.html selects/privacy/index.html \
         sitemap.xml robots.txt 404.html netlify.toml; do
  if [ ! -f "$DEPLOY/$f" ]; then echo "  MISSING in deploy: $f"; drift=1
  elif ! diff -q "site/$f" "$DEPLOY/$f" >/dev/null; then
    echo "  DRIFT: $f"; drift=1
  fi
done
if [ "$drift" -eq 0 ]; then echo "  in sync: site/ matches $DEPLOY"; else
  echo
  echo "  Resolve before copying: inspect with"
  echo "    diff site/<file> $DEPLOY/<file>"
  echo "  The deploy repo may hold newer edits than site/."
fi
exit "$drift"
