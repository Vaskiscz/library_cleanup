#!/usr/bin/env python3
"""Bump the app version in lockstep across:
  - src/photocleanup/__init__.py  (__version__  -> shown in the app footer/health)
  - pyproject.toml                ([tool.briefcase] version -> the bundle version)

Default (no args): bump the patch (3rd) digit — this is every local build.
  --minor:         bump the minor (2nd) digit and reset patch to 0 — this is a
                   PUBLIC GitHub release, done only when explicitly requested.
Prints the new version. Run from anywhere (paths are resolved from this file).
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent      # -> app/
INIT = ROOT / "src" / "photocleanup" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"

minor_release = "--minor" in sys.argv[1:]
show_only = "--show" in sys.argv[1:]   # print the current version, change nothing

text = INIT.read_text()
m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
if not m:
    sys.exit("could not find __version__ in __init__.py")
major, minor, patch = (int(x) for x in m.groups())
if show_only:
    print(f"{major}.{minor}.{patch}")
    sys.exit(0)
new = f"{major}.{minor + 1}.0" if minor_release else f"{major}.{minor}.{patch + 1}"

# Run BOTH substitutions before writing ANYTHING: if either regex fails to
# match, no file is touched, so the two versions can never fall out of lockstep.
new_init, n_init = re.subn(r'(__version__\s*=\s*")\d+\.\d+\.\d+(")',
                           rf"\g<1>{new}\g<2>", text, count=1)
if n_init != 1:
    sys.exit("could not find __version__ in __init__.py")

pt = PYPROJECT.read_text()
new_pt, n_pt = re.subn(r'(\[tool\.briefcase\][^\[]*?\bversion\s*=\s*")\d+\.\d+\.\d+(")',
                       rf"\g<1>{new}\g<2>", pt, count=1, flags=re.S)
if n_pt != 1:
    sys.exit("could not find [tool.briefcase] version in pyproject.toml")

INIT.write_text(new_init)
PYPROJECT.write_text(new_pt)

print(new)
