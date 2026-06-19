#!/usr/bin/env python3
"""Bump the patch (3rd) digit of the app version, in lockstep across:
  - src/photocleanup/__init__.py  (__version__  -> shown in the app footer/health)
  - pyproject.toml                ([tool.briefcase] version -> the bundle version)
Prints the new version. Run from anywhere (paths are resolved from this file).
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent      # -> app/
INIT = ROOT / "src" / "photocleanup" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"

text = INIT.read_text()
m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
if not m:
    sys.exit("could not find __version__ in __init__.py")
major, minor, patch = (int(x) for x in m.groups())
new = f"{major}.{minor}.{patch + 1}"

INIT.write_text(re.sub(r'(__version__\s*=\s*")\d+\.\d+\.\d+(")', rf"\g<1>{new}\g<2>", text))

pt = PYPROJECT.read_text()
pt, n = re.subn(r'(\[tool\.briefcase\][^\[]*?\bversion\s*=\s*")\d+\.\d+\.\d+(")',
                rf"\g<1>{new}\g<2>", pt, count=1, flags=re.S)
if n != 1:
    sys.exit("could not find [tool.briefcase] version in pyproject.toml")
PYPROJECT.write_text(pt)

print(new)
