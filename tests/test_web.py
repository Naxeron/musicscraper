"""
Comprehensive 4-tier test suite for MusicScraper Web API, Background Tasks, and HTTP Server.

Tier 1: Feature & Endpoint Coverage (All REST routes, status, config, library, tasks, static assets)
Tier 2: Boundary & Corner Cases (Malformed JSON, non-object roots, query bounds, path traversal, 405, 404, 500 boundary)
Tier 3: Concurrency & Stream Isolation (Thread safety, pending/active cancellation, log isolation, FIFO eviction, memory bounds)
Tier 4: Real-World Scenarios & Live Server Lifecycle (End-to-end task runs, SSE stream consumption, high-concurrency burst)
"""

import io
import json
import time
import socket
import urllib.request
import urllib.error
import urllib.parse
import http.client
import threading
from pathlib import Path
from typing import Dict, Any, Generator
from unittest.mock import patch, MagicMock

import pytest

from musicscraper.config import Config
from musicscraper.web.tasks import (
    BackgroundTask,
    TaskManager,
    TaskCancelledException,
    ThreadLocalStreamProxy,
    TaskLogCapture,
    global_task_manager,
)
from musicscraper.web.server import (
    MusicScraperHTTPRequestHandler,
    MusicScraperServer,
    start_server,
)
from musicscraper.web import api
from musicscraper.cli.main import build_parser


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Prevents tests from mutating the local .env file."""
    monkeypatch.setattr(Config, "save_to_env", lambda: True)


@pytest.fixture(scope="module")
def web_server() -> Generator[str, None, None]:
    """Spins up a live test HTTP server on an ephemeral port."""
    server = start_server("127.0.0.1", 0)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://{host}:{port}"
    yield base_url

    server.shutdown()
    server.server_close()
    for task in global_task_manager.list_tasks(limit=100):
        if task.status in ("pending", "running"):
            global_task_manager.cancel_task(task.id)


# ==============================================================================
# Tier 1: Feature & Endpoint Coverage
# ==============================================================================

class TestTier1FeatureAndEndpointCoverage:
    """Verifies all functional routes, controllers, and core features."""

    def test_static_asset_serving_html_css_js(self, web_server: str):
        """Verifies that static assets (HTML, CSS, JS) are served with correct headers."""
        # 1. Root and /index.html
        for route in ("/", "/index.html"):
            req = urllib.request.urlopen(f"{web_server}{route}")
            assert req.status == 200
            assert "text/html" in req.headers.get("Content-Type", "")
            content = req.read().decode("utf-8")
            assert "MusicScraper" in content
            assert "Discog Auditor" in content

        # 2. CSS routes
        for route in ("/app.css", "/static/app.css"):
            req_css = urllib.request.urlopen(f"{web_server}{route}")
            assert req_css.status == 200
            assert "text/css" in req_css.headers.get("Content-Type", "")
            css_content = req_css.read().decode("utf-8")
            assert len(css_content) > 0

        # 3. JS routes
        for route in ("/app.js", "/static/app.js"):
            req_js = urllib.request.urlopen(f"{web_server}{route}")
            assert req_js.status == 200
            assert "application/javascript" in req_js.headers.get("Content-Type", "")
            js_content = req_js.read().decode("utf-8")
            assert len(js_content) > 0

    def test_system_status_endpoint(self, web_server: str):
        """Tests GET /api/status returns system health, services, and paths."""
        req = urllib.request.urlopen(f"{web_server}/api/status")
        assert req.status == 200
        data = json.loads(req.read().decode("utf-8"))
        assert "paths" in data
        assert "library_dir" in data["paths"]
        assert "output_dir" in data["paths"]
        assert "services" in data
        assert "slskd" in data["services"]
        assert "navidrome" in data["services"]
        assert "musicbrainz" in data["services"]

    def test_system_status_force_refresh(self, web_server: str):
        """Tests GET /api/status?refresh=true forces a cache refresh."""
        req = urllib.request.urlopen(f"{web_server}/api/status?refresh=true")
        assert req.status == 200
        data = json.loads(req.read().decode("utf-8"))
        assert "timestamp" in data

    def test_system_config_read_and_update(self, web_server: str):
        """Tests GET /api/config and POST /api/config updates."""
        # Read config
        req_cfg = urllib.request.urlopen(f"{web_server}/api/config")
        assert req_cfg.status == 200
        cfg = json.loads(req_cfg.read().decode("utf-8"))
        assert "DEFAULT_LIBRARY_DIR" in cfg
        assert "SLSKD_URL" in cfg

        # Update config
        post_data = json.dumps({"BANDCAMP_EMAIL": "user@musicscraper.org"}).encode("utf-8")
        req_post = urllib.request.Request(
            f"{web_server}/api/config",
            data=post_data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req_post)
        assert resp.status == 200
        res = json.loads(resp.read().decode("utf-8"))
        assert res.get("success") is True
        assert res["config"]["BANDCAMP_EMAIL"] == "user@musicscraper.org"

    def test_navidrome_and_slskd_credential_handling(self, web_server: str):
        """Verifies credentials flags and password preservation during config updates."""
        saved_url = Config.NAVIDROME_URL
        saved_user = Config.NAVIDROME_USER
        saved_token = Config.NAVIDROME_TOKEN
        saved_slskd_pass = Config.SLSKD_PASSWORD
        try:
            post_data = json.dumps({
                "NAVIDROME_URL": "http://subsonic.local:4533",
                "NAVIDROME_USERNAME": "test_nav_user"
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
            assert res["config"]["NAVIDROME_USER"] == "test_nav_user"
            # Verify password/token was preserved
            assert Config.NAVIDROME_TOKEN == saved_token
            assert Config.SLSKD_PASSWORD == saved_slskd_pass
        finally:
            Config.NAVIDROME_URL = saved_url
            Config.NAVIDROME_USER = saved_user
            Config.NAVIDROME_USERNAME = saved_user
            Config.NAVIDROME_TOKEN = saved_token
            Config.NAVIDROME_PASSWORD = saved_token
            Config.SLSKD_PASSWORD = saved_slskd_pass

    def test_slskd_transfers_endpoint(self, web_server: str):
        """Tests GET /api/slskd/transfers response structure."""
        req = urllib.request.urlopen(f"{web_server}/api/slskd/transfers")
        assert req.status == 200
        data = json.loads(req.read().decode("utf-8"))
        assert isinstance(data, dict)
        assert "connected" in data
        assert "downloads" in data

    def test_library_browse_endpoint(self, web_server: str):
        """Tests GET /api/library/browse directory browsing."""
        req = urllib.request.urlopen(f"{web_server}/api/library/browse")
        assert req.status == 200
        data = json.loads(req.read().decode("utf-8"))
        assert "current_path" in data
        assert "directories" in data
        assert "files" in data
        assert isinstance(data["directories"], list)
        assert isinstance(data["files"], list)

    def test_library_releases_list_and_filters(self, web_server: str):
        """Tests GET /api/library/releases with summary and search/filter query parameters."""
        # 1. Base list
        req = urllib.request.urlopen(f"{web_server}/api/library/releases")
        assert req.status == 200
        data = json.loads(req.read().decode("utf-8"))
        assert "summary" in data
        assert "releases" in data
        assert "count" in data
        assert isinstance(data["releases"], list)

        # 2. Filter query parameters
        req_filt = urllib.request.urlopen(f"{web_server}/api/library/releases?refresh=false&search=test&filter=missing")
        assert req_filt.status == 200
        data_filt = json.loads(req_filt.read().decode("utf-8"))
        assert "releases" in data_filt
        assert isinstance(data_filt["releases"], list)

    def test_library_release_details_and_audit(self, web_server: str):
        """Tests GET and POST /api/library/releases/<id> detail and audit endpoints."""
        mock_release = {
            "id": "rel_mock_tier1",
            "artist": "Tier1 Artist",
            "title": "Tier1 Album",
            "status": "complete",
            "missing_count": 0,
            "folder_path": str(Config.DEFAULT_LIBRARY_DIR),
            "tracks": [{"title": "Track 1", "duration": 180}],
        }
        with patch("musicscraper.services.library.LibraryReleaseService.scan_library_releases", return_value=[mock_release]):
            with patch("musicscraper.services.library.LibraryReleaseService.audit_release", return_value=mock_release):
                # GET detail
                req_get = urllib.request.urlopen(f"{web_server}/api/library/releases/rel_mock_tier1?audit=false&refresh=true")
                assert req_get.status == 200
                det = json.loads(req_get.read().decode("utf-8"))
                assert det["id"] == "rel_mock_tier1"
                assert det["artist"] == "Tier1 Artist"

                # POST re-audit
                req_post = urllib.request.Request(
                    f"{web_server}/api/library/releases/rel_mock_tier1/audit",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                resp_post = urllib.request.urlopen(req_post)
                assert resp_post.status == 200
                audit_res = json.loads(resp_post.read().decode("utf-8"))
                assert audit_res.get("success") is True
                assert audit_res["release"]["id"] == "rel_mock_tier1"

    def test_tasks_list_and_details_endpoints(self, web_server: str):
        """Tests GET /api/tasks and GET /api/tasks/<id>."""
        # Submit a quick task
        task = global_task_manager.submit(
            name="Tier 1 List Test Task",
            task_type="clean_folders",
            target_fn=lambda t: "done",
            params={"path": "/tmp", "execute": False}
        )
        time.sleep(0.05)

        # GET /api/tasks
        req_list = urllib.request.urlopen(f"{web_server}/api/tasks?limit=10")
        assert req_list.status == 200
        list_data = json.loads(req_list.read().decode("utf-8"))
        assert "tasks" in list_data
        assert any(t["id"] == task.id for t in list_data["tasks"])

        # GET /api/tasks/<id>
        req_det = urllib.request.urlopen(f"{web_server}/api/tasks/{task.id}?logs=true&log_limit=50")
        assert req_det.status == 200
        det_data = json.loads(req_det.read().decode("utf-8"))
        assert det_data["id"] == task.id
        assert det_data["name"] == "Tier 1 List Test Task"

    def test_task_run_clean_folders(self, web_server: str):
        """Tests POST /api/tasks/run with clean_folders task type."""
        payload = json.dumps({
            "type": "clean_folders",
            "name": "Clean Folders Run",
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
        assert "task" in data
        assert data["task"]["type"] == "clean_folders"

    def test_task_run_release_missing_download(self, web_server: str):
        """Tests POST /api/tasks/run with release_missing_download task type."""
        with patch("musicscraper.services.library.LibraryReleaseService.download_missing_tracks", return_value={"status": "skipped", "queued_count": 0}):
            payload = json.dumps({
                "type": "release_missing_download",
                "name": "Download Missing Test",
                "params": {
                    "artist": "Test Artist",
                    "release_title": "Test Album",
                    "missing_tracks": [{"title": "Track 1"}],
                    "dry_run": True
                }
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
            for _ in range(50):
                poll_resp = urllib.request.urlopen(f"{web_server}/api/tasks/{task_id}")
                poll_data = json.loads(poll_resp.read().decode("utf-8"))
                if poll_data["status"] in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.05)
            assert poll_data["status"] == "completed"

    def test_task_cancel_endpoint(self, web_server: str):
        """Tests POST /api/tasks/<id>/cancel."""
        def slow_fn(t: BackgroundTask):
            for _ in range(50):
                if t.is_cancelled:
                    raise TaskCancelledException("Cancelled")
                time.sleep(0.05)
            return "ok"

        task = global_task_manager.submit(
            name="Cancel Target Task",
            task_type="clean_folders",
            target_fn=slow_fn
        )
        time.sleep(0.05)

        req_cancel = urllib.request.Request(
            f"{web_server}/api/tasks/{task.id}/cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req_cancel)
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("success") is True
        assert task.is_cancelled is True

    def test_cli_web_subcommand_parser(self):
        """Verifies that the CLI argument parser parses web and gui subcommands properly."""
        parser = build_parser()

        args_web = parser.parse_args(["web", "--host", "0.0.0.0", "-p", "9090", "--open"])
        assert args_web.command == "web"
        assert args_web.host == "0.0.0.0"
        assert args_web.port == 9090
        assert args_web.open is True

        args_gui = parser.parse_args(["gui", "-p", "8888"])
        assert args_gui.command == "gui"
        assert args_gui.host == "127.0.0.1"
        assert args_gui.port == 8888


# ==============================================================================
# Tier 2: Boundary & Corner Cases
# ==============================================================================

class TestTier2BoundaryAndCornerCases:
    """Verifies input validation, boundary values, error formatting, and security controls."""

    @pytest.mark.parametrize("bad_payload", [
        b"{unclosed json",
        b'{"key": undefined}',
        b'{"incomplete": ',
        b"   ",
    ])
    def test_malformed_json_body_returns_400(self, web_server: str, bad_payload: bytes):
        """Verifies that malformed JSON syntax in POST request body returns 400."""
        req = urllib.request.Request(
            f"{web_server}/api/config",
            data=bad_payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert res.get("success") is False
        assert "error" in res

    @pytest.mark.parametrize("non_dict_payload", [
        b"[1, 2, 3]",
        b'"just a string"',
        b"12345",
        b"-99.5",
        b"true",
        b"false",
        b"null",
    ])
    def test_non_dict_json_payloads_return_400(self, web_server: str, non_dict_payload: bytes):
        """Verifies that JSON arrays, primitives, and null roots are rejected with 400."""
        req = urllib.request.Request(
            f"{web_server}/api/config",
            data=non_dict_payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert res.get("success") is False
        assert "Request body must be a JSON object" in res["error"]

    def test_invalid_utf8_body_returns_400(self, web_server: str):
        """Verifies that invalid UTF-8 byte sequences in request body return 400."""
        req = urllib.request.Request(
            f"{web_server}/api/config",
            data=b"\x80\xFF\xFE",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert res.get("success") is False
        assert "invalid UTF-8 encoding" in res["error"]

    def test_malformed_content_length_headers(self, web_server: str):
        """Verifies negative or non-numeric Content-Length headers return 400."""
        host, port = urllib.parse.urlparse(web_server).netloc.split(":")
        conn = http.client.HTTPConnection(host, int(port))

        # Negative Content-Length
        conn.request("POST", "/api/config", body=b"{}", headers={"Content-Length": "-10", "Content-Type": "application/json"})
        res = conn.getresponse()
        assert res.status == 400
        data = json.loads(res.read().decode("utf-8"))
        assert data.get("success") is False

        # Non-numeric Content-Length
        conn.request("POST", "/api/config", body=b"{}", headers={"Content-Length": "not_a_number", "Content-Type": "application/json"})
        res2 = conn.getresponse()
        assert res2.status == 400
        data2 = json.loads(res2.read().decode("utf-8"))
        assert data2.get("success") is False

    @pytest.mark.parametrize("bad_query", [
        "/api/tasks?limit=not_a_number",
        "/api/tasks?limit=12.34",
        "/api/tasks?limit=abc",
    ])
    def test_query_param_non_integer_rejected(self, web_server: str, bad_query: str):
        """Verifies that non-integer query parameters return 400."""
        req = urllib.request.Request(f"{web_server}{bad_query}")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert res.get("success") is False
        assert "Invalid integer value" in res["error"]

    @pytest.mark.parametrize("negative_query", [
        "/api/tasks?limit=-5",
        "/api/tasks?limit=0",
    ])
    def test_query_param_negative_bounds_rejected(self, web_server: str, negative_query: str):
        """Verifies that negative or zero values below min_val return 400."""
        req = urllib.request.Request(f"{web_server}{negative_query}")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert res.get("success") is False
        assert "must be >=" in res["error"]

    def test_task_detail_query_param_validation(self, web_server: str):
        """Verifies query param integer parsing and bounds validation on /api/tasks/<id>."""
        task = global_task_manager.submit("QueryParamTask", "clean_folders", lambda t: "ok")
        time.sleep(0.05)

        # Non-integer log_limit on existing task
        req_bad = urllib.request.Request(f"{web_server}/api/tasks/{task.id}?log_limit=abc")
        with pytest.raises(urllib.error.HTTPError) as exc_bad:
            urllib.request.urlopen(req_bad)
        assert exc_bad.value.code == 400

        # Negative log_limit on existing task
        req_neg = urllib.request.Request(f"{web_server}/api/tasks/{task.id}?log_limit=-10")
        with pytest.raises(urllib.error.HTTPError) as exc_neg:
            urllib.request.urlopen(req_neg)
        assert exc_neg.value.code == 400

    def test_library_browse_path_traversal_confinement(self, web_server: str):
        """Verifies that path traversal attempts via ../ cannot escape library directory."""
        req = urllib.request.urlopen(f"{web_server}/api/library/browse?path=../../../../etc")
        assert req.status == 200
        res = json.loads(req.read().decode("utf-8"))
        assert "current_path" in res
        # Path must resolve strictly inside configured library root
        assert str(Config.DEFAULT_LIBRARY_DIR.resolve()) in res["current_path"]

    def test_static_file_path_traversal_denied(self, web_server: str):
        """Verifies that static file path traversal attempts return 403 or 404."""
        host, port = urllib.parse.urlparse(web_server).netloc.split(":")
        conn = http.client.HTTPConnection(host, int(port))

        for attack_path in ["/static/../../etc/passwd", "/static/../../../server.py"]:
            conn.request("GET", attack_path)
            res = conn.getresponse()
            assert res.status in (403, 404)
            data = json.loads(res.read().decode("utf-8"))
            assert data.get("success") is False

    def test_unsupported_http_methods_return_405(self, web_server: str):
        """Verifies that PUT, DELETE, PATCH, HEAD return 405 with structured JSON."""
        for method in ("PUT", "DELETE", "PATCH"):
            for route in ("/api/config", "/api/status", "/api/tasks"):
                req = urllib.request.Request(f"{web_server}{route}", method=method)
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    urllib.request.urlopen(req)
                assert exc_info.value.code == 405
                assert "application/json" in exc_info.value.headers.get("Content-Type", "")
                res = json.loads(exc_info.value.read().decode("utf-8"))
                assert res.get("success") is False
                assert f"Method {method} not allowed" in res["error"]

        # HEAD method returns 405 with Content-Type header and no body (standard HTTP)
        for route in ("/api/config", "/api/status", "/api/tasks"):
            req_head = urllib.request.Request(f"{web_server}{route}", method="HEAD")
            with pytest.raises(urllib.error.HTTPError) as exc_head:
                urllib.request.urlopen(req_head)
            assert exc_head.value.code == 405
            assert "application/json" in exc_head.value.headers.get("Content-Type", "")

    def test_options_cors_preflight_returns_204(self, web_server: str):
        """Verifies OPTIONS preflight requests return HTTP 204 with CORS headers and empty body."""
        host, port = urllib.parse.urlparse(web_server).netloc.split(":")
        conn = http.client.HTTPConnection(host, int(port))

        for route in ("/api/config", "/api/status", "/api/tasks/run", "/api/library/browse"):
            conn.request("OPTIONS", route)
            res = conn.getresponse()
            body = res.read()
            assert res.status == 204
            assert res.getheader("Access-Control-Allow-Origin") == "*"
            assert "GET" in res.getheader("Access-Control-Allow-Methods", "")
            assert "POST" in res.getheader("Access-Control-Allow-Methods", "")
            assert len(body) == 0

    def test_unknown_endpoints_return_404(self, web_server: str):
        """Verifies that unknown routes return 404 with structured JSON."""
        for bad_route in ("/api/unknown_route", "/api/foo/bar", "/random_page"):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(f"{web_server}{bad_route}")
            assert exc_info.value.code == 404
            res = json.loads(exc_info.value.read().decode("utf-8"))
            assert res.get("success") is False
            assert "Endpoint not found" in res["error"]

    def test_nonexistent_task_and_release_return_404(self, web_server: str):
        """Verifies that non-existent task IDs or release IDs return 404."""
        # Non-existent task ID
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{web_server}/api/tasks/task_does_not_exist_999")
        assert exc_info.value.code == 404
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert res.get("success") is False

        # Non-existent release ID
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{web_server}/api/library/releases/nonexistent_release_id_xyz")
        assert exc_info.value.code == 404
        res2 = json.loads(exc_info.value.read().decode("utf-8"))
        assert res2.get("success") is False

    def test_missing_id_in_subroutes_returns_400(self, web_server: str):
        """Verifies that omitting IDs in action routes returns 400 Bad Request."""
        # Missing task ID on cancel
        req_cancel = urllib.request.Request(
            f"{web_server}/api/tasks//cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_cancel)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert "Task ID is required" in res["error"]

        # Missing release ID on audit
        req_audit = urllib.request.Request(
            f"{web_server}/api/library/releases//audit",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_audit)
        assert exc_info.value.code == 400
        res2 = json.loads(exc_info.value.read().decode("utf-8"))
        assert "Release ID is required" in res2["error"]

    def test_task_run_missing_or_unknown_type(self, web_server: str):
        """Verifies POST /api/tasks/run rejects missing or unknown 'type' field."""
        # Missing type
        req_missing = urllib.request.Request(
            f"{web_server}/api/tasks/run",
            data=json.dumps({"name": "No Type"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_missing)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert "Missing required parameter: 'type'" in res["error"]

        # Unknown type
        req_unknown = urllib.request.Request(
            f"{web_server}/api/tasks/run",
            data=json.dumps({"type": "completely_fictitious_task"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_unknown)
        assert exc_info.value.code == 400
        res2 = json.loads(exc_info.value.read().decode("utf-8"))
        assert "Unknown task type" in res2["error"]

    def test_task_run_invalid_params_or_name(self, web_server: str):
        """Verifies that non-dict params or non-string name in task run returns 400."""
        # Non-dict params
        req_bad_params = urllib.request.Request(
            f"{web_server}/api/tasks/run",
            data=json.dumps({"type": "clean_folders", "params": "not_a_dict"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_bad_params)
        assert exc_info.value.code == 400
        res = json.loads(exc_info.value.read().decode("utf-8"))
        assert "params' must be a JSON object" in res["error"]

        # Non-string name
        req_bad_name = urllib.request.Request(
            f"{web_server}/api/tasks/run",
            data=json.dumps({"type": "clean_folders", "name": 12345}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_bad_name)
        assert exc_info.value.code == 400
        res2 = json.loads(exc_info.value.read().decode("utf-8"))
        assert "name' must be a string" in res2["error"]

    def test_top_level_exception_interception_500(self, web_server: str):
        """Verifies unhandled controller exceptions return 500 JSON without crashing the server."""
        with patch("musicscraper.web.api.get_system_status", side_effect=RuntimeError("Simulated unhandled crash")):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(f"{web_server}/api/status")
            assert exc_info.value.code == 500
            res = json.loads(exc_info.value.read().decode("utf-8"))
            assert res.get("success") is False
            assert res["error"] == "Internal server error"

        # Verify server remains fully functional on subsequent requests
        req_ok = urllib.request.urlopen(f"{web_server}/api/config")
        assert req_ok.status == 200


# ==============================================================================
# Tier 3: Concurrency, Task Subsystem & Stream Isolation
# ==============================================================================

class CustomFatalBaseError(BaseException):
    """Custom BaseException subclass not inheriting from Exception."""
    pass


class CustomDomainError(Exception):
    """Custom domain Exception."""
    pass


class TestTier3ConcurrencyAndTaskSubsystem:
    """Verifies task lifecycle transitions, log stream isolation, memory bounding, and concurrency."""

    def test_basic_task_execution_lifecycle_and_logs(self):
        """Tests task execution, progress updates, and stdout capture."""
        tm = TaskManager(max_workers=2)

        def worker_fn(task: BackgroundTask):
            task.update_progress(30, "Phase 1 running")
            print("Message from stdout worker")
            time.sleep(0.02)
            task.update_progress(100, "Done")
            return {"count": 99}

        task = tm.submit(name="Basic Task", task_type="audit", target_fn=worker_fn)
        assert task.status in ("pending", "running")

        for _ in range(50):
            if task.status in ("completed", "failed"):
                break
            time.sleep(0.02)

        assert task.status == "completed"
        assert task.progress == 100.0
        assert task.result == {"count": 99}
        assert any("Message from stdout worker" in log["message"] for log in task.logs)
        tm.shutdown(wait=True)

    def test_task_log_capture_rich_and_carriage_return(self):
        """Tests Rich console output, soft wrap preservation, and \\r carriage return overwrites."""
        from musicscraper.core.report import console
        import sys

        tm = TaskManager(max_workers=2)

        def rich_task_fn(task: BackgroundTask):
            console.print("[green]Processing album download...[/green]")
            sys.stdout.write("Temp Status 1\rFinal Status Message\n")
            return "ok"

        orig_soft_wrap = getattr(console, "soft_wrap", False)
        task = tm.submit(name="Rich Test", task_type="download", target_fn=rich_task_fn)

        for _ in range(50):
            if task.status in ("completed", "failed"):
                break
            time.sleep(0.02)

        assert task.status == "completed"
        assert getattr(console, "soft_wrap", False) == orig_soft_wrap

        log_messages = [log["message"] for log in task.logs]
        assert any("Processing album download" in m for m in log_messages)
        assert any("Final Status Message" in m for m in log_messages)
        assert not any("Temp Status 1" in m for m in log_messages)
        assert not any("\r" in m for m in log_messages)
        tm.shutdown(wait=True)

    def test_task_log_capture_large_line_chunking(self):
        """Verifies TaskLogCapture chunks very long lines (>64KB) without unbounded memory growth."""
        tm = TaskManager(max_workers=2)

        def huge_line_fn(task: BackgroundTask):
            # Emit a 130KB single line with no newline
            huge_line = "A" * (130 * 1024)
            print(huge_line, end="")
            return "done"

        task = tm.submit(name="Huge Line Task", task_type="audit", target_fn=huge_line_fn)

        for _ in range(50):
            if task.status in ("completed", "failed"):
                break
            time.sleep(0.02)

        assert task.status == "completed"
        assert len(task.logs) >= 2
        tm.shutdown(wait=True)

    def test_task_exception_handling_base_and_custom(self):
        """Verifies BaseException, custom domain errors, and empty exception messages."""
        tm = TaskManager(max_workers=2)

        # 1. BaseException
        def fatal_fn(task: BackgroundTask):
            raise CustomFatalBaseError("Fatal system failure")

        t1 = tm.submit(name="Fatal Task", task_type="audit", target_fn=fatal_fn)
        for _ in range(50):
            if t1.status in ("completed", "failed"):
                break
            time.sleep(0.02)
        assert t1.status == "failed"
        assert "Fatal system failure" in str(t1.error)

        # 2. Custom Domain Exception
        def custom_fn(task: BackgroundTask):
            raise CustomDomainError("Custom domain error msg")

        t2 = tm.submit(name="Custom Task", task_type="audit", target_fn=custom_fn)
        for _ in range(50):
            if t2.status in ("completed", "failed"):
                break
            time.sleep(0.02)
        assert t2.status == "failed"
        assert "Custom domain error msg" in str(t2.error)

        # 3. Empty message Exception fallback
        def empty_fn(task: BackgroundTask):
            raise ValueError()

        t3 = tm.submit(name="Empty Task", task_type="audit", target_fn=empty_fn)
        for _ in range(50):
            if t3.status in ("completed", "failed"):
                break
            time.sleep(0.02)
        assert t3.status == "failed"
        assert t3.error == "ValueError"

        tm.shutdown(wait=True)

    def test_pending_task_cancellation_never_executes(self):
        """Verifies that cancelling a queued pending task prevents it from executing or resurrecting."""
        tm = TaskManager(max_workers=1)
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        executed_flag = []

        def blocker_fn(task: BackgroundTask):
            blocker_started.set()
            release_blocker.wait(timeout=5.0)
            return "blocker_done"

        def queued_fn(task: BackgroundTask):
            executed_flag.append(True)
            return "should_not_run"

        blocker_task = tm.submit(name="Blocker", task_type="clean_folders", target_fn=blocker_fn)
        assert blocker_started.wait(timeout=3.0)

        queued_task = tm.submit(name="Queued", task_type="clean_folders", target_fn=queued_fn)
        assert queued_task.status == "pending"

        assert tm.cancel_task(queued_task.id) is True
        assert queued_task.status == "cancelled"

        release_blocker.set()

        for _ in range(50):
            if blocker_task.status in ("completed", "failed"):
                break
            time.sleep(0.02)

        assert blocker_task.status == "completed"
        time.sleep(0.1)

        assert queued_task.status == "cancelled"
        assert len(executed_flag) == 0
        assert queued_task.result is None
        tm.shutdown(wait=True)

    def test_running_task_cooperative_cancellation_preserves_status(self):
        """Verifies that cancelling an active running task preserves cancelled status on worker exit."""
        tm = TaskManager(max_workers=2)
        step_reached = threading.Event()

        def running_fn(task: BackgroundTask):
            step_reached.set()
            for _ in range(100):
                if task.is_cancelled:
                    raise TaskCancelledException("Aborted by test")
                time.sleep(0.02)
            return "completed_result"

        task = tm.submit(name="Running Cancel Task", task_type="clean_folders", target_fn=running_fn)
        assert step_reached.wait(timeout=3.0)
        assert task.status == "running"

        assert tm.cancel_task(task.id) is True
        assert task.status == "cancelled"

        for _ in range(50):
            if not task.is_cancelled:
                break
            time.sleep(0.02)

        assert task.status == "cancelled"
        assert task.result is None
        assert task.error is None
        tm.shutdown(wait=True)

    def test_cancel_already_terminal_tasks(self):
        """Verifies that cancel_task returns False on finished tasks and leaves their state intact."""
        tm = TaskManager(max_workers=2)

        # 1. Completed
        t_comp = tm.submit(name="Comp", task_type="clean_folders", target_fn=lambda t: "done_val")
        for _ in range(50):
            if t_comp.status == "completed":
                break
            time.sleep(0.02)
        assert t_comp.status == "completed"
        assert tm.cancel_task(t_comp.id) is False
        assert t_comp.status == "completed"
        assert t_comp.result == "done_val"

        # 2. Failed
        def fail_fn(t):
            raise ValueError("Intentional error")
        t_fail = tm.submit(name="Fail", task_type="clean_folders", target_fn=fail_fn)
        for _ in range(50):
            if t_fail.status == "failed":
                break
            time.sleep(0.02)
        assert t_fail.status == "failed"
        assert tm.cancel_task(t_fail.id) is False
        assert t_fail.status == "failed"
        tm.shutdown(wait=True)

    def test_high_frequency_concurrent_submissions(self):
        """Submits dozens of concurrent tasks from parallel threads without races or deadlocks."""
        tm = TaskManager(max_workers=4, max_retained_tasks=100)
        errors = []

        def worker_fn(task: BackgroundTask, idx: int):
            for _ in range(10):
                if task.is_cancelled:
                    raise TaskCancelledException("Cancelled")
                time.sleep(0.001)
            return f"res_{idx}"

        def submitter(client_id: int):
            try:
                for i in range(10):
                    task = tm.submit(
                        name=f"Task_{client_id}_{i}",
                        task_type="audit",
                        target_fn=worker_fn,
                        idx=i
                    )
                    if i % 3 == 0:
                        time.sleep(0.002)
                        tm.cancel_task(task.id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=submitter, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(errors) == 0
        tm.shutdown(wait=True)
        for t in tm.list_tasks(limit=100):
            assert t.status in ("completed", "failed", "cancelled")

    def test_per_thread_log_stream_isolation(self):
        """Verifies that concurrent tasks on separate threads isolate their captured logs."""
        from musicscraper.core.report import console
        tm = TaskManager(max_workers=4)
        start_barrier = threading.Barrier(4)

        def worker_fn(task: BackgroundTask, task_num: int):
            start_barrier.wait(timeout=5.0)
            for i in range(5):
                print(f"[TASK_{task_num}_STDOUT_LINE_{i}]")
                console.print(f"[TASK_{task_num}_RICH_LINE_{i}]")
                time.sleep(0.01)
            return f"task_{task_num}_done"

        tasks = [
            tm.submit(name=f"Worker {i}", task_type="audit", target_fn=worker_fn, task_num=i)
            for i in range(1, 5)
        ]

        for t in tasks:
            for _ in range(100):
                if t.status in ("completed", "failed"):
                    break
                time.sleep(0.02)
            assert t.status == "completed"

        for i, t in enumerate(tasks, 1):
            task_logs = " ".join(log["message"] for log in t.logs)
            assert f"TASK_{i}_STDOUT_LINE_0" in task_logs
            assert f"TASK_{i}_RICH_LINE_0" in task_logs

            # Verify no cross-talk from other tasks
            for other_i in range(1, 5):
                if other_i != i:
                    assert f"TASK_{other_i}_STDOUT" not in task_logs
                    assert f"TASK_{other_i}_RICH" not in task_logs

        tm.shutdown(wait=True)

    def test_task_memory_bounding(self):
        """Verifies task log buffer maintains max_logs bounding while tracking total_log_count."""
        tm = TaskManager(max_workers=2, max_logs_per_task=50)

        def heavy_logger_fn(task: BackgroundTask):
            for i in range(250):
                print(f"Log line message {i}")
            return "done"

        task = tm.submit(name="Heavy Logger", task_type="clean_folders", target_fn=heavy_logger_fn)

        for _ in range(50):
            if task.status in ("completed", "failed"):
                break
            time.sleep(0.02)

        assert task.status == "completed"
        assert len(task.logs) == 50
        snapshot = task.to_dict(include_logs=True, log_limit=25)
        assert snapshot["log_count"] >= 250
        assert len(snapshot["logs"]) == 25
        tm.shutdown(wait=True)

    def test_task_manager_fifo_eviction(self):
        """Verifies FIFO task pruning of oldest terminal tasks when capacity is exceeded."""
        tm = TaskManager(max_workers=2, max_retained_tasks=5)
        tasks = []

        for i in range(12):
            t = tm.submit(name=f"Task {i}", task_type="clean_folders", target_fn=lambda t: "ok")
            tasks.append(t)
            time.sleep(0.01)

        for t in tasks:
            for _ in range(50):
                if t.status in ("completed", "failed"):
                    break
                time.sleep(0.01)
            assert t.status == "completed"

        with tm._lock:
            assert len(tm._tasks) <= 5
            # Earliest tasks must have been evicted
            assert tasks[0].id not in tm._tasks
            assert tasks[1].id not in tm._tasks
            assert tasks[-1].id in tm._tasks

        tm.shutdown(wait=True)

    def test_task_manager_delete_task(self):
        """Verifies manual task deletion via delete_task method."""
        tm = TaskManager(max_workers=2)
        t = tm.submit(name="Delete Target", task_type="clean_folders", target_fn=lambda t: "done")
        for _ in range(50):
            if t.status == "completed":
                break
            time.sleep(0.02)

        assert tm.delete_task(t.id) is True
        assert tm.get_task(t.id) is None
        # Deleting already deleted task returns False
        assert tm.delete_task(t.id) is False
        tm.shutdown(wait=True)

    def test_task_manager_shutdown_cancels_pending(self):
        """Verifies TaskManager.shutdown cancels pending tasks and rejects new submissions."""
        tm = TaskManager(max_workers=1)
        blocker_started = threading.Event()
        release_blocker = threading.Event()

        def blocker_fn(task: BackgroundTask):
            blocker_started.set()
            release_blocker.wait(timeout=5.0)
            return "done"

        t1 = tm.submit(name="Blocker", task_type="clean_folders", target_fn=blocker_fn)
        assert blocker_started.wait(timeout=3.0)

        t2 = tm.submit(name="Pending 1", task_type="clean_folders", target_fn=lambda t: "p1")
        t3 = tm.submit(name="Pending 2", task_type="clean_folders", target_fn=lambda t: "p2")

        tm.shutdown(wait=False, cancel_pending=True)
        release_blocker.set()

        assert t2.status == "cancelled"
        assert t3.status == "cancelled"

        with pytest.raises(RuntimeError):
            tm.submit(name="Post Shutdown", task_type="clean_folders", target_fn=lambda t: "nope")


# ==============================================================================
# Tier 4: Real-World Scenarios & Live Server Lifecycle
# ==============================================================================

class TestTier4RealWorldScenariosAndE2E:
    """Verifies complete end-to-end client flows, SSE streaming, and high-concurrency bursts."""

    def test_e2e_task_lifecycle_with_sse_event_stream(self, web_server: str):
        """Tests end-to-end flow: submit task -> stream SSE logs -> receive completion event."""
        # 1. Submit task
        payload = json.dumps({
            "type": "clean_folders",
            "name": "E2E SSE Flow Task",
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
        task_id = data["task"]["id"]

        # 2. Connect to SSE event stream
        sse_resp = urllib.request.urlopen(f"{web_server}/api/tasks/{task_id}/events", timeout=5.0)
        assert sse_resp.status == 200
        assert "text/event-stream" in sse_resp.headers.get("Content-Type", "")

        events_received = []
        start_time = time.time()
        while time.time() - start_time < 5.0:
            raw_line = sse_resp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8").strip()
            if line.startswith("data: "):
                event_data = json.loads(line[len("data: "):])
                events_received.append(event_data)
                if event_data.get("type") == "done":
                    break

        assert len(events_received) >= 1
        assert events_received[0]["type"] == "init"
        assert any(e.get("type") == "done" for e in events_received)

        # 3. Verify final state via GET /api/tasks/<id>
        final_req = urllib.request.urlopen(f"{web_server}/api/tasks/{task_id}")
        assert final_req.status == 200
        final_data = json.loads(final_req.read().decode("utf-8"))
        assert final_data["status"] == "completed"
        assert final_data["progress"] == 100.0

    def test_e2e_task_cancellation_flow(self, web_server: str):
        """Tests end-to-end task cancellation flow via HTTP API."""
        def slow_worker(task: BackgroundTask):
            for _ in range(100):
                if task.is_cancelled:
                    raise TaskCancelledException("Aborted by E2E test")
                time.sleep(0.05)
            return "done"

        task = global_task_manager.submit(
            name="E2E Cancel Flow",
            task_type="clean_folders",
            target_fn=slow_worker
        )
        time.sleep(0.05)

        # Send cancellation
        req_cancel = urllib.request.Request(
            f"{web_server}/api/tasks/{task.id}/cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req_cancel)
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("success") is True

        # Poll status until cancelled
        for _ in range(50):
            req_poll = urllib.request.urlopen(f"{web_server}/api/tasks/{task.id}")
            poll_data = json.loads(req_poll.read().decode("utf-8"))
            if poll_data["status"] == "cancelled":
                break
            time.sleep(0.05)

        assert poll_data["status"] == "cancelled"
        assert poll_data["result"] is None

    def test_e2e_high_concurrency_mixed_traffic_storm(self, web_server: str):
        """Fires 30 concurrent mixed requests (GET, invalid query 400, malformed POST 400, OPTIONS 204)."""
        results = []
        errors = []

        def worker(idx: int):
            try:
                if idx % 4 == 0:
                    # Valid GET /api/status
                    resp = urllib.request.urlopen(f"{web_server}/api/status", timeout=5.0)
                    assert resp.status == 200
                    results.append(200)
                elif idx % 4 == 1:
                    # Invalid query parameter 400
                    req = urllib.request.Request(f"{web_server}/api/tasks?limit=bad_{idx}")
                    try:
                        urllib.request.urlopen(req, timeout=5.0)
                    except urllib.error.HTTPError as e:
                        assert e.code == 400
                        results.append(400)
                elif idx % 4 == 2:
                    # Malformed JSON POST 400
                    req = urllib.request.Request(
                        f"{web_server}/api/tasks/run",
                        data=b"not json",
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    try:
                        urllib.request.urlopen(req, timeout=5.0)
                    except urllib.error.HTTPError as e:
                        assert e.code == 400
                        results.append(400)
                else:
                    # OPTIONS preflight 204
                    host, port = urllib.parse.urlparse(web_server).netloc.split(":")
                    conn = http.client.HTTPConnection(host, int(port), timeout=5.0)
                    conn.request("OPTIONS", "/api/config")
                    res = conn.getresponse()
                    res.read()
                    assert res.status == 204
                    results.append(204)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(errors) == 0, f"Encountered errors during traffic storm: {errors}"
        assert len(results) == 30

    def test_e2e_server_lifecycle_start_and_shutdown(self):
        """Tests live server ephemeral instantiation, request handling, and clean shutdown."""
        server = start_server("127.0.0.1", 0)
        host, port = server.server_address
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        # Send request
        resp = urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=5.0)
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "paths" in data

        # Shutdown
        server.shutdown()
        server.server_close()
        t.join(timeout=3.0)

    def test_library_release_audit_async_lifecycle(self, web_server: str):
        """
        Tests POST /api/library/releases/<id>/audit lifecycle.
        Supports async task execution returning 202 Accepted with task dictionary,
        or synchronous fallback with release details.
        """
        mock_release = {
            "id": "rel_async_audit_test",
            "artist": "Async Artist",
            "title": "Async Album",
            "status": "complete",
            "missing_count": 0,
            "tracks": [],
        }

        with patch("musicscraper.services.library.LibraryReleaseService.scan_library_releases", return_value=[mock_release]):
            with patch("musicscraper.services.library.LibraryReleaseService.audit_release", return_value=mock_release):
                # 1. Test POST /api/library/releases/<id>/audit with async parameter
                req = urllib.request.Request(
                    f"{web_server}/api/library/releases/rel_async_audit_test/audit?async=true",
                    data=json.dumps({"async": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                resp = urllib.request.urlopen(req, timeout=5.0)
                assert resp.status in (200, 202)
                data = json.loads(resp.read().decode("utf-8"))
                assert data.get("success") is True
                if resp.status == 202:
                    assert "task" in data
                    task_id = data["task"]["id"]
                    # Poll task until complete
                    for _ in range(50):
                        poll_req = urllib.request.urlopen(f"{web_server}/api/tasks/{task_id}")
                        poll_data = json.loads(poll_req.read().decode("utf-8"))
                        if poll_data["status"] in ("completed", "failed", "cancelled"):
                            break
                        time.sleep(0.05)
                    assert poll_data["status"] == "completed"
                else:
                    assert "release" in data

    def test_tasks_run_library_audit(self, web_server: str):
        """
        Tests POST /api/tasks/run with type 'library_audit'.
        Verifies task is dispatched, queued, and transitions through states.
        """
        with patch("musicscraper.services.library.LibraryReleaseService.audit_all_releases", return_value=[]):
            req = urllib.request.Request(
                f"{web_server}/api/tasks/run",
                data=json.dumps({
                    "type": "library_audit",
                    "params": {"library_dir": str(Config.DEFAULT_LIBRARY_DIR)},
                    "name": "Audit Entire Library Test",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=5.0)
            assert resp.status == 202
            data = json.loads(resp.read().decode("utf-8"))
            assert data.get("success") is True
            assert "task" in data
            assert data["task"]["type"] == "library_audit"
            task_id = data["task"]["id"]
            for _ in range(50):
                poll_resp = urllib.request.urlopen(f"{web_server}/api/tasks/{task_id}")
                poll_data = json.loads(poll_resp.read().decode("utf-8"))
                if poll_data["status"] in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.05)
            assert poll_data["status"] == "completed"

    def test_tasks_run_soulseek_download(self, web_server: str):
        """
        Tests POST /api/tasks/run with type 'soulseek_download'.
        Verifies valid parameters enqueue the task, and missing parameters transition to failed.
        """
        # 1. Valid dispatch with mock client
        with patch("musicscraper.clients.slskd.SlskdClient.enqueue_download", return_value={"status": "enqueued", "files_count": 1}):
            req = urllib.request.Request(
                f"{web_server}/api/tasks/run",
                data=json.dumps({
                    "type": "soulseek_download",
                    "params": {
                        "username": "peer_test",
                        "files": [{"filename": "Music\\Album\\01 Song.flac", "size": 1000}],
                    },
                    "name": "Soulseek Download Test",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=5.0)
            assert resp.status == 202
            data = json.loads(resp.read().decode("utf-8"))
            assert data.get("success") is True
            assert data["task"]["type"] == "soulseek_download"
            task_id = data["task"]["id"]
            for _ in range(50):
                poll_resp = urllib.request.urlopen(f"{web_server}/api/tasks/{task_id}")
                poll_data = json.loads(poll_resp.read().decode("utf-8"))
                if poll_data["status"] in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.05)
            assert poll_data["status"] == "completed"

        # 2. Missing required parameters inside worker raises ValueError -> status: failed
        req_bad = urllib.request.Request(
            f"{web_server}/api/tasks/run",
            data=json.dumps({
                "type": "soulseek_download",
                "params": {},
                "name": "Bad Soulseek Download",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp_bad = urllib.request.urlopen(req_bad, timeout=5.0)
        assert resp_bad.status == 202
        data_bad = json.loads(resp_bad.read().decode("utf-8"))
        bad_task_id = data_bad["task"]["id"]

        for _ in range(50):
            poll_resp = urllib.request.urlopen(f"{web_server}/api/tasks/{bad_task_id}")
            poll_data = json.loads(poll_resp.read().decode("utf-8"))
            if poll_data["status"] == "failed":
                break
            time.sleep(0.05)

        assert poll_data["status"] == "failed"
        assert "required" in (poll_data.get("error") or "").lower()

    def test_sse_non_primitive_serialization_safety(self, web_server: str):
        """
        Verifies that SSE /api/tasks/<id>/events serializes non-primitive JSON objects
        (e.g. pathlib.Path, set) in task.result via default=str without raising TypeError.
        """
        def non_primitive_worker(task: BackgroundTask):
            time.sleep(0.05)
            # Return non-JSON-serializable objects (Path, set)
            return {
                "file_path": Path("/music/test.flac"),
                "aliases": {"alias1", "alias2"},
            }

        task = global_task_manager.submit(
            name="SSE Non-Primitive Test",
            task_type="clean_folders",
            target_fn=non_primitive_worker
        )

        sse_resp = urllib.request.urlopen(f"{web_server}/api/tasks/{task.id}/events", timeout=5.0)
        assert sse_resp.status == 200
        assert "text/event-stream" in sse_resp.headers.get("Content-Type", "")

        events_received = []
        start_time = time.time()
        while time.time() - start_time < 5.0:
            raw_line = sse_resp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8").strip()
            if line.startswith("data: "):
                event_data = json.loads(line[len("data: "):])
                events_received.append(event_data)
                if event_data.get("type") == "done":
                    break

        assert any(e.get("type") == "done" for e in events_received), "SSE stream must emit 'done' event"
        done_event = next(e for e in events_received if e.get("type") == "done")
        assert done_event["status"] == "completed"

    def test_e2e_inflight_task_cancellation_cooperative(self, web_server: str):
        """
        Verifies that in-flight task cancellation stops active workers and
        transitions status cleanly to 'cancelled' without resurrection or deadlock.
        """
        def long_running_worker(task: BackgroundTask):
            for i in range(200):
                if task.is_cancelled:
                    raise TaskCancelledException("Cooperative stop")
                time.sleep(0.05)
            return "finished"

        task = global_task_manager.submit(
            name="Long Running Inflight Task",
            task_type="clean_folders",
            target_fn=long_running_worker
        )
        time.sleep(0.05)

        # Cancel while actively running
        req_cancel = urllib.request.Request(
            f"{web_server}/api/tasks/{task.id}/cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req_cancel, timeout=5.0)
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("success") is True

        # Poll status until cancelled
        for _ in range(50):
            req_poll = urllib.request.urlopen(f"{web_server}/api/tasks/{task.id}")
            poll_data = json.loads(req_poll.read().decode("utf-8"))
            if poll_data["status"] == "cancelled":
                break
            time.sleep(0.05)

        assert poll_data["status"] == "cancelled"
        assert poll_data["result"] is None

