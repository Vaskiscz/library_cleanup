#!/usr/bin/env bash
# Rebuild site/ from heads/ + fragments. site/ is what deploys (mirrored into
# the vaclavtrnka-web repo), so run this after editing any fragment.
#   built page                        =  heads/<frag>  +  <frag>  +  </body></html>
set -euo pipefail
cd "$(dirname "$0")"
build() { # $1=fragment  $2=output path
  mkdir -p "$(dirname "$2")"
  { cat "heads/$1"; cat "$1"; printf '</body>\n</html>\n'; } > "$2"
  echo "  built $2"
}
build portfolio.html        site/index.html
build index.html            site/library-cleanup/index.html
build selects.html          site/selects/index.html
build privacy.html          site/privacy/index.html
build selects-privacy.html  site/selects/privacy/index.html
