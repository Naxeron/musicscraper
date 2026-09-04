"""
Lightweight, zero-bloat HTTP server and REST API for MusicScraper Web GUI.
Built directly on Python standard library's http.server with multi-threading.
"""

import os
import sys
import json
import time
import urllib.parse
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, Any

from musicscraper.web.tasks import global_task_manager
from musicscraper.web import api


import logging

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class MusicScraperHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler routing static assets and REST API endpoints."""

    server_version = "MusicScraperWeb/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        """Suppresses default noisy stderr request logging."""
        pass

    def handle(self) -> None:
        """Processes incoming requests, suppressing client disconnect errors gracefully."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Overrides default HTML error responses to emit structured JSON."""
        msg = message or "HTTP error"
        self._send_error_json(msg, status_code=code)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE, PATCH")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json_response(self, data: Any, status_code: int = 200) -> None:
        try:
            payload = json.dumps(data, default=str).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_error_json(
        self,
        message: Any = "Bad Request",
        status_code: int = 400,
        error_message: Optional[str] = None
    ) -> None:
        """Sends a uniform JSON error response: {'error': ..., 'success': False}."""
        if error_message is not None:
            message = error_message
        if isinstance(message, int):
            status_code, message = message, status_code
            if not isinstance(message, str):
                message = "Error"
        self._send_json_response({"error": str(message), "success": False}, status_code=status_code)

    def _parse_query_int(
        self,
        query: Dict[str, list],
        key: str,
        default: int,
        min_val: Optional[int] = None,
        max_val: Optional[int] = None
    ) -> int:
        """Safely parses integer query parameters, raising ValueError on failure."""
        vals = query.get(key)
        if not vals or not vals[0].strip():
            return default
        raw_val = vals[0].strip()
        try:
            val = int(raw_val)
        except ValueError:
            raise ValueError(f"Invalid integer value for parameter '{key}': '{raw_val}'")

        if min_val is not None and val < min_val:
            raise ValueError(f"Parameter '{key}' must be >= {min_val}")
        if max_val is not None and val > max_val:
            raise ValueError(f"Parameter '{key}' must be <= {max_val}")

        return val

    def _parse_json_body(self) -> Optional[Dict[str, Any]]:
        """Safely extracts and validates JSON dictionary request body."""
        raw_cl = self.headers.get("Content-Length")
        if raw_cl is None or raw_cl.strip() == "":
            return {}

        try:
            content_length = int(raw_cl.strip())
            if content_length < 0:
                self._send_error_json("Invalid Content-Length header", status_code=400)
                return None
        except ValueError:
            self._send_error_json("Malformed Content-Length header", status_code=400)
            return None

        if content_length == 0:
            return {}

        try:
            raw_bytes = self.rfile.read(content_length)
            raw_body = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            self._send_error_json("Request body contains invalid UTF-8 encoding", status_code=400)
            return None
        except Exception as e:
            self._send_error_json(f"Error reading request body: {e}", status_code=400)
            return None

        try:
            parsed = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            self._send_error_json(f"Malformed JSON payload: {exc.msg}", status_code=400)
            return None
        except Exception as exc:
            self._send_error_json(f"Malformed JSON payload: {exc}", status_code=400)
            return None

        if not isinstance(parsed, dict):
            self._send_error_json("Request body must be a JSON object", status_code=400)
            return None

        return parsed

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_PUT(self) -> None:
        self._send_error_json(f"Method PUT not allowed on {self.path}", status_code=405)

    def do_DELETE(self) -> None:
        self._send_error_json(f"Method DELETE not allowed on {self.path}", status_code=405)

    def do_PATCH(self) -> None:
        self._send_error_json(f"Method PATCH not allowed on {self.path}", status_code=405)

    def do_HEAD(self) -> None:
        self._send_error_json(f"Method HEAD not allowed on {self.path}", status_code=405)

    def do_GET(self) -> None:
        try:
            self._dispatch_get()
        except ValueError as e:
            self._send_error_json(str(e), status_code=400)
        except Exception as e:
            logger.exception("Unhandled exception in GET %s: %s", self.path, e)
            self._send_error_json("Internal server error", status_code=500)

    def _dispatch_get(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed_url.query)

        # 1. Static Assets
        if path == "" or path == "/index.html":
            return self._serve_static_file("index.html", "text/html; charset=utf-8")
        elif path == "/app.css" or path == "/static/app.css":
            return self._serve_static_file("app.css", "text/css; charset=utf-8")
        elif path == "/app.js" or path == "/static/app.js":
            return self._serve_static_file("app.js", "application/javascript; charset=utf-8")

        # 2. REST API: System Status & Config
        if path == "/api/status":
            force_refresh = query.get("refresh", ["false"])[0].lower() in ("true", "1", "yes")
            return self._send_json_response(api.get_system_status(force=force_refresh))
        elif path == "/api/config":
            return self._send_json_response(api.get_system_config())
        elif path == "/api/slskd/transfers":
            return self._send_json_response(api.get_slskd_transfers())
        elif path == "/api/library/browse":
            subpath = query.get("path", [""])[0]
            return self._send_json_response(api.browse_library(subpath))

        elif path == "/api/library/releases":
            refresh = query.get("refresh", ["false"])[0].lower() in ("true", "1", "yes")
            search = query.get("search", [""])[0]
            filter_mode = query.get("filter", ["all"])[0].lower()
            return self._send_json_response(api.get_library_releases(refresh=refresh, search=search, filter_mode=filter_mode))

        elif path.startswith("/api/library/releases/"):
            rel_id = path[len("/api/library/releases/"):].strip("/")
            if not rel_id:
                return self._send_error_json("Release ID is required", status_code=400)
            audit = query.get("audit", ["true"])[0].lower() not in ("false", "0", "no")
            refresh = query.get("refresh", ["false"])[0].lower() in ("true", "1", "yes")
            try:
                details = api.get_library_release_details(rel_id, audit=audit, force_refresh=refresh)
                return self._send_json_response(details)
            except ValueError as e:
                return self._send_error_json(str(e), status_code=404)

        # 3. REST API: Task Management
        elif path == "/api/tasks":
            limit = self._parse_query_int(query, "limit", default=50, min_val=1)
            tasks = [t.to_dict(include_logs=False) for t in global_task_manager.list_tasks(limit=limit)]
            return self._send_json_response({"tasks": tasks})

        elif path.startswith("/api/tasks/"):
            sub_parts = path[len("/api/tasks/"):].split("/")
            task_id = sub_parts[0].strip()
            if not task_id:
                return self._send_error_json("Task ID is required", status_code=400)

            # Server-Sent Events (SSE) log stream: /api/tasks/<id>/events
            if len(sub_parts) > 1 and sub_parts[1] == "events":
                return self._handle_sse_task_stream(task_id)

            task = global_task_manager.get_task(task_id)
            if not task:
                return self._send_error_json(f"Task '{task_id}' not found", status_code=404)

            logs_val = query.get("logs", ["true"])[0].lower()
            include_logs = logs_val not in ("false", "0", "no")
            log_limit = self._parse_query_int(query, "log_limit", default=500, min_val=1)
            return self._send_json_response(task.to_dict(include_logs=include_logs, log_limit=log_limit))

        # 4. Unknown endpoint
        return self._send_error_json(f"Endpoint not found: {self.path}", status_code=404)

    def do_POST(self) -> None:
        try:
            self._dispatch_post()
        except ValueError as e:
            self._send_error_json(str(e), status_code=400)
        except Exception as e:
            logger.exception("Unhandled exception in POST %s: %s", self.path, e)
            self._send_error_json("Internal server error", status_code=500)

    def _dispatch_post(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        # Safely extract and validate JSON payload
        body = self._parse_json_body()
        if body is None:
            return

        # 1. Update Config
        if path == "/api/config":
            updated = api.update_system_config(body)
            return self._send_json_response({"success": True, "config": updated})

        # 2. Re-audit library release
        elif path.startswith("/api/library/releases/") and path.endswith("/audit"):
            rel_id = path[len("/api/library/releases/"): -len("/audit")].strip("/")
            if not rel_id:
                return self._send_error_json("Release ID is required", status_code=400)
            try:
                details = api.get_library_release_details(rel_id, audit=True, force_refresh=True)
                return self._send_json_response({"success": True, "release": details})
            except ValueError as e:
                return self._send_error_json(str(e), status_code=404)

        # 3. Run Background Task
        elif path == "/api/tasks/run":
            task_type = body.get("type")
            if not task_type or not isinstance(task_type, str) or not task_type.strip():
                return self._send_error_json("Missing required parameter: 'type'", status_code=400)
            task_type = task_type.strip()
            if task_type not in api.TASK_DISPATCHER:
                return self._send_error_json(f"Unknown task type: '{task_type}'", status_code=400)

            params = body.get("params", {})
            if params is not None and not isinstance(params, dict):
                return self._send_error_json("Parameter 'params' must be a JSON object", status_code=400)
            if params is None:
                params = {}

            name = body.get("name")
            if name is not None and not isinstance(name, str):
                return self._send_error_json("Parameter 'name' must be a string", status_code=400)

            task = api.launch_task(task_type=task_type, params=params, name=name)
            return self._send_json_response({
                "success": True,
                "task": task.to_dict(include_logs=True)
            }, status_code=202)

        # 4. Cancel Task
        elif path.startswith("/api/tasks/") and path.endswith("/cancel"):
            task_id = path[len("/api/tasks/"): -len("/cancel")].strip("/")
            if not task_id:
                return self._send_error_json("Task ID is required", status_code=400)
            task = global_task_manager.get_task(task_id)
            if not task:
                return self._send_error_json(f"Task '{task_id}' not found", status_code=404)
            success = global_task_manager.cancel_task(task_id)
            if success:
                return self._send_json_response({"success": True, "message": f"Task {task_id} cancellation requested"})
            else:
                return self._send_error_json(f"Could not cancel task {task_id}", status_code=400)

        return self._send_error_json(f"Endpoint not found: {self.path}", status_code=404)


    def _serve_static_file(self, filename: str, content_type: str) -> None:
        file_path = (STATIC_DIR / filename).resolve()
        if STATIC_DIR.resolve() not in file_path.parents and file_path != STATIC_DIR.resolve():
            return self._send_error_json("Access denied", status_code=403)
        if not file_path.exists() or not file_path.is_file():
            return self._send_error_json(f"Static file '{filename}' not found", status_code=404)

        try:
            content = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            self._send_error_json(f"Error reading file: {e}", status_code=500)

    def _handle_sse_task_stream(self, task_id: str) -> None:
        task = global_task_manager.get_task(task_id)
        if not task:
            return self._send_error_json(f"Task '{task_id}' not found", status_code=404)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
        self.end_headers()

        log_queue = []
        lock = threading.Lock()

        def on_log(entry: Dict[str, Any]) -> None:
            with lock:
                log_queue.append(entry)

        task.add_listener(on_log)

        try:
            # First send existing logs
            with task._lock:
                init_data = json.dumps({"type": "init", "task": task.to_dict(include_logs=True)}, default=str)
            self.wfile.write(f"data: {init_data}\n\n".encode("utf-8"))
            self.wfile.flush()

            while True:
                with lock:
                    entries = list(log_queue)
                    log_queue.clear()

                for entry in entries:
                    msg = json.dumps({"type": "log", "entry": entry, "status": task.status, "progress": task.progress, "stage": task.stage}, default=str)
                    self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))

                if task.status in ("completed", "failed", "cancelled"):
                    final_msg = json.dumps({"type": "done", "status": task.status, "result": task.result, "error": task.error}, default=str)
                    self.wfile.write(f"data: {final_msg}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break

                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            task.remove_listener(on_log)


class MusicScraperServer(ThreadingHTTPServer):
    """Threading HTTP server that suppresses noisy broken pipe disconnect errors."""
    daemon_threads = True
    request_queue_size = 128

    def handle_error(self, request: Any, client_address: Any) -> None:
        ex_type, _, _ = sys.exc_info()
        if ex_type in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)


def start_server(host: str = "127.0.0.1", port: int = 8080) -> MusicScraperServer:
    """Starts the threaded HTTP web server."""
    server_address = (host, port)
    httpd = MusicScraperServer(server_address, MusicScraperHTTPRequestHandler)
    return httpd
