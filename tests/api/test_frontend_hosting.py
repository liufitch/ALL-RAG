from fastapi.testclient import TestClient

import main


def test_built_frontend_and_assets_are_served_without_shadowing_api(tmp_path, monkeypatch):
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<html><script src="/assets/app.js"></script></html>')
    (dist / "assets" / "app.js").write_text('console.log("console")')
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))

    with TestClient(main.create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert '/assets/app.js' in response.text
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == 'console.log("console")'
        assert "/api/knowledge_base/list" in client.get("/openapi.json").json()["paths"]
        assert client.get("/api/nonexistent").status_code == 404
        assert client.get("/assets/missing.js").status_code == 404


def test_api_starts_without_a_frontend_build(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    with TestClient(main.create_app()) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/").status_code == 404
