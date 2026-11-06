#!/usr/bin/env bash
# Rebuild site/ from heads/ + fragments. site/ is what deploys (mirrored into
# the vaclavtrnka-web repo), so run this after editing any fragment.
#   built page                        =  heads/<frag>  +  <frag>  +  </body></html>
#
# A line that is exactly `<!--#include NAME-->` (leading spaces allowed), in a
# head or a fragment, is replaced by partials/NAME.html. One copy of the shared
# header, footer operator line and analytics, used by every page.
#
# Owner rule (2026-11-06): the projects are separate topics. Only the hub
# (vaclavtrnka.cz itself, and its site privacy notice) links to them; a project
# page never links to, or even mentions in its markup, another project. Partial
# lines enforce this at build time, so the markup is absent rather than hidden
# (a crawler reads hidden links too):
#   data-hub-only        kept on hub pages (home, privacy, notfound = 404) only
#   data-only="<page>"   kept on that page only (selects | library-cleanup)
set -euo pipefail
cd "$(dirname "$0")"
expand() { # $1=file  $2=page id: print it with include lines expanded
  awk -v page="$2" '
    function keep(l,   want) {
      if (l ~ /data-hub-only/ && page != "home" && page != "privacy" && page != "notfound") return 0
      if (match(l, /data-only="[a-z-]+"/)) { want = substr(l, RSTART + 11, RLENGTH - 12); if (want != page) return 0 }
      return 1
    }
    /^[ \t]*<!--#include [a-z0-9-]+-->[ \t]*$/ {
      name = $0; sub(/^[ \t]*<!--#include /, "", name); sub(/-->[ \t]*$/, "", name)
      file = "partials/" name ".html"
      if ((getline line < file) <= 0) { print "build.sh: missing " file > "/dev/stderr"; exit 1 }
      do { if (keep(line)) print line } while ((getline line < file) > 0)
      close(file); next
    }
    { print }' "$1"
}
build() { # $1=fragment  $2=output path  $3=page id (must match the .site data-page)
  mkdir -p "$(dirname "$2")"
  { expand "heads/$1" "$3"; expand "$1" "$3"; printf '</body>\n</html>\n'; } > "$2"
  grep -q "data-page=\"$3\"" "$2" || { echo "build.sh: $2 is not data-page=\"$3\"" >&2; exit 1; }
  echo "  built $2 ($3)"
}
build portfolio.html        site/index.html                  home
build privacy.html          site/privacy/index.html          privacy
build index.html            site/library-cleanup/index.html  library-cleanup
build selects.html          site/selects/index.html          selects
build selects-privacy.html  site/selects/privacy/index.html  selects
build selects-terms.html    site/selects/terms/index.html    selects
build selects-press.html    site/selects/press/index.html    selects
build 404.html              site/404.html                    notfound

# A project page must never link to, show or name another project. Checks
# what a reader or crawler meets: href/src targets and the visible text (CSS
# selectors and code comments naming page ids are not links). Fails the build.
python3 -I - <<'PY'
import html, re, sys
rules = {
  "site/library-cleanup/index.html": r"/selects|id6790501172|dumvporadku|selects|pořádku",
  "site/selects/index.html":         r"library[-_ ]cleanup|dumvporadku|pořádku",
  "site/selects/privacy/index.html": r"library[-_ ]cleanup|dumvporadku|pořádku",
  "site/selects/terms/index.html":   r"library[-_ ]cleanup|dumvporadku|pořádku",
  "site/selects/press/index.html":   r"library[-_ ]cleanup|dumvporadku|pořádku",
}
bad = []
for path, pat in rules.items():
    t = open(path, encoding="utf-8").read()
    targets = re.findall(r'(?:href|src)="([^"]*)"', t)
    text = re.sub(r"(?s)<(script|style)\b.*?</\1>|<!--.*?-->|<[^>]+>", " ", t)
    text = html.unescape(text)
    for x in targets + [text]:
        m = re.search(pat, x, re.I)
        if m: bad.append(f"{path}: {x[max(0, m.start()-40):m.end()+20]!r}")
if bad:
    print("build.sh: another project appears on a project page:", *bad, sep="\n  ", file=sys.stderr)
    sys.exit(1)
PY

# If the parked PostHog setup is switched back on with its placeholder key, the
# snippet skips init and nothing is sent. Say so rather than let it deploy
# unnoticed.
if grep -q 'phc_REPLACE' partials/analytics.html; then
  echo "  WARNING: partials/analytics.html still has the placeholder PostHog key. No visits are counted until it is replaced."
fi
