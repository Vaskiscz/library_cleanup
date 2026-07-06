"""Regression guards for the app's core promises (audit #16): the on-device
library core must never gain a network primitive, media responses must stay
no-store, and security headers must be present. These fail LOUD if a future
change quietly breaks a hard constraint."""
import glob
import os
import re

import pytest
from fastapi.testclient import TestClient

import photo_cleanup
import photocleanup.server as server_mod
from factories import make_stub_engine
from photocleanup.server import create_app
from photocleanup.store import Store

CORE_DIR = os.path.dirname(photo_cleanup.__file__)
SERVER_SRC = os.path.join(os.path.dirname(server_mod.__file__), "server.py")

# import socket / urllib / requests / httpx / http.client / smtplib / ftplib / urlopen
_NET = re.compile(
    r"\b(import\s+socket|socket\.(socket|create_connection)|urllib|urlopen|"
    r"import\s+requests|from\s+requests|httpx|http\.client|smtplib|ftplib)\b")


@pytest.fixture
def client():
    app = create_app(store=Store(":memory:"), engine=make_stub_engine())
    with TestClient(app) as c:
        yield c


def test_core_library_has_no_network_primitives():
    """The on-device photo core (photo_cleanup/) reads and derives photo data — it
    must never touch the network. (The sanctioned GitHub egress lives only in the
    app's updater.py, which is NOT in this package.)"""
    offenders = []
    for path in glob.glob(os.path.join(CORE_DIR, "*.py")):
        for i, line in enumerate(open(path).read().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _NET.search(line):
                offenders.append(f"{os.path.basename(path)}:{i}: {line.strip()}")
    assert not offenders, "network primitives in the on-device core:\n" + "\n".join(offenders)


def test_media_endpoints_are_no_store():
    """Thumbnails and video must be served no-store so the WebView can't persist
    photo pixels to disk. Guard the source so the header can't be dropped."""
    src = open(SERVER_SRC).read()
    # one for /api/thumb, one for /api/video (the constant appears twice in code)
    assert src.count('"Cache-Control": "no-store"') >= 2


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "default-src 'none'" in r.headers.get("Content-Security-Policy", "")
