from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_manifest_meets_the_android_install_criteria():
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.content)
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["name"] and manifest["short_name"]
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    # Chrome requires both a 192 and a 512 before it offers to install.
    assert {"192x192", "512x512"} <= sizes
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])


def test_manifest_shortcuts_point_at_pages_that_exist():
    manifest = json.loads(client.get("/manifest.webmanifest").content)
    for shortcut in manifest["shortcuts"]:
        assert client.get(shortcut["url"]).status_code == 200, shortcut["url"]


def test_worker_is_served_from_the_root_so_it_can_scope_the_whole_app():
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["Service-Worker-Allowed"] == "/"


def test_worker_never_caches_live_data():
    body = client.get("/sw.js").text
    # A cached quote is a wrong quote; the API must always hit the network.
    assert 'url.pathname.startsWith("/api/")' in body
    assert 'url.pathname.startsWith("/lock")' in body
    assert 'request.mode === "navigate"' in body
    assert 'const CACHE = "marsad-static-v2"' in body
    assert "cache.addAll" not in body


def test_icons_are_real_pngs_of_the_declared_size():
    import struct

    for name, expected in (("icon-192.png", 192), ("icon-512.png", 512)):
        content = client.get(f"/static/{name}").content
        assert content[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", content[16:24])
        assert (width, height) == (expected, expected)


def test_pages_link_the_manifest_so_any_entry_point_can_install():
    for path in ("/", "/spx", "/news", "/earnings"):
        assert 'rel="manifest"' in client.get(path).text, path


def test_install_files_stay_reachable_while_the_site_is_locked():
    from app.security.site_lock import OPEN_PATH_PREFIXES

    # Android fetches these before the user can enter a code.
    for path in ("/manifest.webmanifest", "/sw.js", "/static/"):
        assert path in OPEN_PATH_PREFIXES


def test_the_spx_internal_chart_has_a_visible_fixed_area():
    html = client.get("/spx").text
    assert 'id="syntheticChart"' in html
    assert ".chart-box{height:230px" in html
    assert "مباشر OPRA" in html
    assert "embed-widget-advanced-chart.js" not in html
