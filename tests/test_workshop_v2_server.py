from functools import partial
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from harness_lab.auditor import AuditStore
from harness_lab.config import HarnessConfig
from harness_lab.settings import Settings
from harness_lab.workshop_server import WorkshopHandler


@pytest.fixture
def site(tmp_path):
    (tmp_path / "workshop-v2").mkdir()
    (tmp_path / "workshop-v2/index.html").write_text("Workshop", encoding="utf-8")
    (tmp_path / "workshop-v2/demo.html").write_text("Demo", encoding="utf-8")
    (tmp_path / "homework").mkdir()
    (tmp_path / "homework/index.html").write_text("Homework", encoding="utf-8")
    (tmp_path / "homework/etl_homework.ipynb").write_text("{}", encoding="utf-8")
    (tmp_path / "homework/.env").write_text("private", encoding="utf-8")
    (tmp_path / ".env").write_text("private", encoding="utf-8")
    (tmp_path / "workshop-v2/.env").write_text("private", encoding="utf-8")
    factory = partial(WorkshopHandler, workspace_root=tmp_path, directory=str(tmp_path),
                      settings=Settings("", "", "test-model", ""), cases={}, configs={"baseline": HarnessConfig()},
                      artifact_root=tmp_path / "artifacts", audit_store=AuditStore())
    server = ThreadingHTTPServer(("127.0.0.1", 0), factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", build_opener(ProxyHandler({})).open
    server.shutdown()
    server.server_close()
    thread.join()


def test_teaching_pages_and_live_status_share_origin(site):
    url, get = site
    assert get(url + "/").read() == b"Workshop"
    assert get(url + "/workshop-v2/demo.html?v=1").read() == b"Demo"
    assert get(url + "/demo.html").read() == b"Demo"
    assert get(url + "/homework/").read() == b"Homework"
    assert get(url + "/homework").read() == b"Homework"
    notebook = get(url + "/homework/etl_homework.ipynb")
    assert notebook.headers.get_content_type() == "application/octet-stream"
    status = json.loads(get(url + "/api/status").read())
    assert status["mode"] == "live" and status["model"] == "test-model"
    assert get(url + "/connection-settings.js").status == 200


@pytest.mark.parametrize("path", ["/.env", "/workshop-v2/.env", "/workshop-v2/../.env",
                                   "/workshop-v2/%2e%2e/.env", "/src/harness_lab/settings.py",
                                   "/homework/.env", "/homework/%2e%2e/.env"])
def test_workspace_and_dotfiles_are_not_public(site, path):
    url, get = site
    with pytest.raises(HTTPError) as exc:
        get(url + path)
    assert exc.value.code == 404


def test_live_api_keeps_origin_validation(site):
    url, get = site
    request = Request(url + "/api/chat", data=b"{}", headers={"Origin": "https://example.org"})
    with pytest.raises(HTTPError) as exc:
        get(request)
    assert exc.value.code == 403


def test_live_chat_uses_workshop_provider_budget(site, monkeypatch):
    from harness_lab import demo
    from harness_lab.agent import AgentResult

    seen = []

    def fake_run(settings, config, history, **options):
        seen.append(options)
        return AgentResult(case_id="free_chat", output="Ответ"), 67.0

    monkeypatch.setattr(demo, "run_free_chat", fake_run)
    url, get = site
    body = {"agent": "baseline", "messages": [{"role": "user", "content": "Привет"}]}
    request = Request(url + "/api/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    response = get(request)
    payload = json.loads(response.read())
    assert response.status == 200 and payload["result"]["output"] == "Ответ"
    assert seen == [{"model_timeout": 90.0, "total_timeout": 480.0, "max_tokens": 2048}]


def test_clean_checkout_uses_committed_optimized_config(tmp_path, monkeypatch):
    """Live arena must start without the presenter's local training artifacts."""
    from harness_lab import workshop_server

    (tmp_path / "configs").mkdir()
    HarnessConfig(name="control").to_yaml(tmp_path / "configs/baseline.yaml")
    HarnessConfig(name="measured").to_yaml(tmp_path / "configs/optimized.yaml")
    monkeypatch.setattr(workshop_server, "load_cases", lambda *args, **kwargs: [])
    monkeypatch.setattr(workshop_server, "CURATED_TASKS", {})
    server = workshop_server.create_server(tmp_path, "127.0.0.1", 0)
    try:
        assert server.RequestHandlerClass.keywords["configs"]["optimized"].name == "measured"
    finally:
        server.server_close()
