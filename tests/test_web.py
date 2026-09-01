"""
Unit and integration tests for MusicScraper Web GUI server, tasks, and REST APIs.
"""

import json
import time
import urllib.request
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

import pytest

from musicscraper.web.tasks import BackgroundTask, TaskManager
from musicscraper.web.server import MusicScraperHTTPRequestHandler, start_server
from musicscraper.cli.main import build_parser


def test_task_manager_execution():
    """Tests BackgroundTask lifecycle, progress, and stdout/log capturing."""
    tm = TaskManager(max_workers=2)

    def dummy_task_fn(task: BackgroundTask):
        task.update_progress(25, "Step 1 in progress")
        print("Hello from stdout inside task!")
        time.sleep(0.05)
        task.update_progress(100, "Done")
        return {"items": 42}

    task = tm.submit(name="Test Task", task_type="audit", target_fn=dummy_task_fn, params={"artist": "TestArtist"})
    assert task.status in ("pending", "running")

    # Wait for completion
    for _ in range(50):
        if task.status in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert task.status == "completed"
    assert task.progress == 100.0
    assert task.result == {"items": 42}
    assert any("Hello from stdout" in log["message"] for log in task.logs)


def test_task_manager_rich_and_carriage_return_capture():
    """Tests Rich console log capture, soft wrap, and carriage return handling."""
    from musicscraper.core.report import console
    import sys

    tm = TaskManager(max_workers=2)

    def rich_task_fn(task: BackgroundTask):
        console.print("[cyan]Initiating Soulseek download for [bold]4[/bold] missing tracks...[/cyan]")
        sys.stdout.write("Overwritten line 1\rFinal line content\n")
        return "ok"

    original_soft_wrap = getattr(console, "soft_wrap", False)
    task = tm.submit(name="Rich Task", task_type="download", target_fn=rich_task_fn)

    for _ in range(50):
        if task.status in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert task.status == "completed"
    # Verify console.soft_wrap was restored
    assert getattr(console, "soft_wrap", False) == original_soft_wrap

    log_messages = [log["message"] for log in task.logs]
    assert any("Initiating Soulseek download" in msg for msg in log_messages)
    # Carriage return overwrite must leave only the final portion
    assert any("Final line content" in msg for msg in log_messages)
    assert not any("Overwritten line 1" in msg for msg in log_messages)
    assert not any("\r" in msg for msg in log_messages)


def test_task_manager_cancellation():
    """Tests cancelling a task in progress."""
    tm = TaskManager(max_workers=2)

    def slow_task_fn(task: BackgroundTask):
        for i in range(100):
            if task.cancel_requested:
                return "cancelled"
            time.sleep(0.05)
        return "done"

    task = tm.submit(name="Slow Task", task_type="clean_folders", target_fn=slow_task_fn)
    time.sleep(0.05)
    success = tm.cancel_task(task.id)
    assert success is True
    assert task.cancel_requested is True


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Prevents tests from mutating the local .env file."""
    from musicscraper.config import Config
    monkeypatch.setattr(Config, "save_to_env", lambda: True)


@pytest.fixture(scope="module")
def web_server():
    """Spins up a test HTTP server on an ephemeral port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MusicScraperHTTPRequestHandler)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://{host}:{port}"
    yield base_url

    server.shutdown()
    server.server_close()


def test_web_static_routes(web_server):
    """Verifies that static assets (HTML, CSS, JS) are properly served."""
    # Index HTML
    req = urllib.request.urlopen(f"{web_server}/")
    assert req.status == 200
    assert "text/html" in req.headers.get("Content-Type")
    content = req.read().decode("utf-8")
    assert "MusicScraper" in content
    assert "Discog Auditor" in content

    # CSS
    req_css = urllib.request.urlopen(f"{web_server}/app.css")
    assert req_css.status == 200
    assert "text/css" in req_css.headers.get("Content-Type")

    # JS
    req_js = urllib.request.urlopen(f"{web_server}/app.js")
    assert req_js.status == 200
    assert "application/javascript" in req_js.headers.get("Content-Type")


def test_web_api_status_and_config(web_server):
    """Tests GET /api/status, GET /api/config, and POST /api/config."""
    # Status
    req = urllib.request.urlopen(f"{web_server}/api/status")
    assert req.status == 200
    status_data = json.loads(req.read().decode("utf-8"))
    assert "paths" in status_data
    assert "services" in status_data
    assert "musicbrainz" in status_data["services"]

    # Config GET
    req_cfg = urllib.request.urlopen(f"{web_server}/api/config")
    assert req_cfg.status == 200
    cfg_data = json.loads(req_cfg.read().decode("utf-8"))
    assert "DEFAULT_LIBRARY_DIR" in cfg_data

    # Config POST
    post_data = json.dumps({"BANDCAMP_EMAIL": "test@example.com"}).encode("utf-8")
    req_post = urllib.request.Request(
        f"{web_server}/api/config",
        data=post_data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp = urllib.request.urlopen(req_post)
    assert resp.status == 200
    resp_data = json.loads(resp.read().decode("utf-8"))
    assert resp_data.get("success") is True
    assert resp_data["config"]["BANDCAMP_EMAIL"] == "test@example.com"


def test_web_api_task_lifecycle(web_server):
    """Tests task submission, polling, log retrieval, and task listing."""
    # Submit a dry-run clean_folders task
    payload = json.dumps({
        "type": "clean_folders",
        "name": "Test Folder Clean",
        "params": {"path": "/tmp", "execute": False}
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{web_server}/api/tasks/run",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 202
    data = json.loads(resp.read().decode("utf-8"))
    assert data.get("success") is True
    task_id = data["task"]["id"]

    # List tasks
    req_list = urllib.request.urlopen(f"{web_server}/api/tasks")
    assert req_list.status == 200
    list_data = json.loads(req_list.read().decode("utf-8"))
    assert any(t["id"] == task_id for t in list_data["tasks"])

    # Query task details
    time.sleep(0.2)
    req_detail = urllib.request.urlopen(f"{web_server}/api/tasks/{task_id}")
    assert req_detail.status == 200
    detail_data = json.loads(req_detail.read().decode("utf-8"))
    assert detail_data["id"] == task_id
    assert detail_data["name"] == "Test Folder Clean"


def test_web_api_library_releases(web_server):
    """Tests GET /api/library/releases and release missing download task dispatch."""
    req = urllib.request.urlopen(f"{web_server}/api/library/releases")
    assert req.status == 200
    data = json.loads(req.read().decode("utf-8"))
    assert "summary" in data
    assert "releases" in data
    assert "count" in data

    # Test dispatching a release missing download task
    payload = json.dumps({
        "type": "release_missing_download",
        "name": "Download Missing: Test Album",
        "params": {
            "artist": "Test Artist",
            "release_title": "Test Album",
            "missing_tracks": [{"title": "Track 1"}],
            "dry_run": True
        }
    }).encode("utf-8")

    req_task = urllib.request.Request(
        f"{web_server}/api/tasks/run",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp = urllib.request.urlopen(req_task)
    assert resp.status == 202
    res_data = json.loads(resp.read().decode("utf-8"))
    assert res_data.get("success") is True


def test_cli_web_subcommand_parser():
    """Verifies that the CLI parser correctly parses web and gui subcommands."""
    parser = build_parser()
    args_web = parser.parse_args(["web", "--host", "0.0.0.0", "-p", "9090", "--open"])
    assert args_web.command == "web"
    assert args_web.host == "0.0.0.0"
    assert args_web.port == 9090
    assert args_web.open is True

    args_gui = parser.parse_args(["gui"])
    assert args_gui.command == "gui"
    assert args_gui.host == "127.0.0.1"
    assert args_gui.port == 8080


def test_web_api_navidrome_credentials(web_server, monkeypatch):
    """Verifies that Navidrome credentials and status are properly exposed and handled."""
    from musicscraper.config import Config

    # Check status endpoint exposes navidrome configured status
    req = urllib.request.urlopen(f"{web_server}/api/status")
    status_data = json.loads(req.read().decode("utf-8"))
    assert "navidrome" in status_data["services"]
    assert "configured" in status_data["services"]["navidrome"]

    # Check config endpoint includes Navidrome user and token flags
    req_cfg = urllib.request.urlopen(f"{web_server}/api/config")
    cfg_data = json.loads(req_cfg.read().decode("utf-8"))
    assert "NAVIDROME_URL" in cfg_data
    assert "NAVIDROME_USER" in cfg_data
    assert "NAVIDROME_USERNAME" in cfg_data
    assert "has_navidrome_token" in cfg_data
    assert "has_slskd_password" in cfg_data

    # Test updating Navidrome settings without clearing password
    saved_url = Config.NAVIDROME_URL
    saved_user = Config.NAVIDROME_USER
    saved_token = Config.NAVIDROME_TOKEN
    try:
        post_data = json.dumps({
            "NAVIDROME_URL": "http://subsonic.local:4533",
            "NAVIDROME_USERNAME": "testuser"
        }).encode("utf-8")
        req_post = urllib.request.Request(
            f"{web_server}/api/config",
            data=post_data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req_post)
        assert resp.status == 200
        res = json.loads(resp.read().decode("utf-8"))
        assert res["config"]["NAVIDROME_URL"] == "http://subsonic.local:4533"
        assert res["config"]["NAVIDROME_USER"] == "testuser"
        # Verify password was preserved (not blanked)
        assert Config.NAVIDROME_TOKEN == saved_token
    finally:
        Config.NAVIDROME_URL = saved_url
        Config.NAVIDROME_USER = saved_user
        Config.NAVIDROME_USERNAME = saved_user
        Config.NAVIDROME_TOKEN = saved_token
        Config.NAVIDROME_PASSWORD = saved_token


