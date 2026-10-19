"""The dashboard picks up new code after a deploy (#12)."""

import re

from fastapi.testclient import TestClient

from fleet import app as app_module
from fleet.app import create_app


def test_static_files_are_revalidated(client):
    for path in ("/static/app.js", "/static/styles.css", "/static/heatmap.js", "/static/provision-flow.js"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["Cache-Control"] == "no-cache", path


def test_dashboard_references_versioned_assets(client):
    page = client.get("/")
    assert page.headers["Cache-Control"] == "no-cache"
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
    assert refs, "the page references static files"
    versions = {re.search(r"\?v=([0-9a-f]{10})$", ref).group(1) for ref in refs}
    assert len(versions) == 1  # one version for the whole set of files


def test_version_changes_when_a_static_file_changes(settings, engine, clock, tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text('<script src="/static/app.js"></script><link href="/static/styles.css">')
    (static / "app.js").write_text("console.log(1)")
    (static / "styles.css").write_text("body{}")
    monkeypatch.setattr(app_module, "STATIC_DIR", static)

    def version():
        with TestClient(create_app(settings, engine=engine, clock=clock, background=False)) as c:
            return re.search(r"app\.js\?v=([0-9a-f]+)", c.get("/").text).group(1)

    first = version()
    assert version() == first
    (static / "app.js").write_text("console.log(2)")
    assert version() != first
