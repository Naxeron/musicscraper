"""
Background task manager with thread-safe execution, stdout/Rich log capturing,
and real-time status tracking for the MusicScraper Web GUI.
"""

import io
import sys
import time
import uuid
import logging
import threading
from typing import Dict, List, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


class TaskLogCapture(io.StringIO):
    """Custom stream that captures written text line-by-line into task logs."""

    def __init__(self, task: "BackgroundTask", original_stream: Optional[io.TextIOBase] = None):
        super().__init__()
        self.task = task
        self.original_stream = original_stream
        self._line_buffer = ""

    def write(self, s: str) -> int:
        if self.original_stream:
            try:
                self.original_stream.write(s)
                self.original_stream.flush()
            except Exception:
                pass

        self._line_buffer += s
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            clean_line = line.rstrip("\r")
            if "\r" in clean_line:
                clean_line = clean_line.split("\r")[-1]
            if clean_line:
                self.task.append_log(clean_line)
        return len(s)

    def flush(self) -> None:
        clean_line = self._line_buffer.rstrip("\r\n")
        if "\r" in clean_line:
            clean_line = clean_line.split("\r")[-1]
        if clean_line.strip():
            self.task.append_log(clean_line)
        self._line_buffer = ""
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass


class BackgroundTask:
    """Represents an asynchronous operation run by MusicScraper."""

    def __init__(self, task_id: str, name: str, task_type: str, params: Dict[str, Any]):
        self.id = task_id
        self.name = name
        self.task_type = task_type
        self.params = params
        self.status = "pending"  # pending, running, completed, failed, cancelled
        self.progress = 0.0      # 0 to 100
        self.stage = "Queued"
        self.result: Optional[Any] = None
        self.error: Optional[str] = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None
        self.logs: List[Dict[str, Any]] = []
        self.cancel_requested = False
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def append_log(self, message: str, level: str = "INFO") -> None:
        """Appends a log line to task history and notifies active listeners."""
        entry = {
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "level": level,
            "message": message
        }
        with self._lock:
            self.logs.append(entry)
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(entry)
            except Exception:
                pass

    def update_progress(self, progress: float, stage: Optional[str] = None) -> None:
        """Updates numeric progress (0-100) and current stage description."""
        with self._lock:
            self.progress = max(0.0, min(100.0, float(progress)))
            if stage:
                self.stage = stage
        if stage:
            self.append_log(f"[{int(self.progress)}%] {stage}")

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Registers a callback for incoming log events."""
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Unregisters a log callback."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def to_dict(self, include_logs: bool = False, log_limit: int = 200) -> Dict[str, Any]:
        """Serializes task state for JSON API."""
        with self._lock:
            data = {
                "id": self.id,
                "name": self.name,
                "type": self.task_type,
                "status": self.status,
                "progress": self.progress,
                "stage": self.stage,
                "params": self.params,
                "result": self.result,
                "error": self.error,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "log_count": len(self.logs),
            }
            if include_logs:
                data["logs"] = self.logs[-log_limit:] if log_limit > 0 else self.logs
            return data


class TaskManager:
    """Thread-safe manager for background task execution and querying."""

    def __init__(self, max_workers: int = 4):
        self._tasks: Dict[str, BackgroundTask] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="MusicScraperTask")
        self._lock = threading.Lock()

    def submit(
        self,
        name: str,
        task_type: str,
        target_fn: Callable[..., Any],
        params: Optional[Dict[str, Any]] = None,
        *args,
        **kwargs
    ) -> BackgroundTask:
        """Creates and enqueues a new background task."""
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        task = BackgroundTask(task_id=task_id, name=name, task_type=task_type, params=params or {})

        with self._lock:
            self._tasks[task_id] = task

        self._executor.submit(self._run_task, task, target_fn, *args, **kwargs)
        return task

    def _run_task(self, task: BackgroundTask, target_fn: Callable[..., Any], *args, **kwargs) -> None:
        """Executes task function within captured output context."""
        task.status = "running"
        task.started_at = datetime.now(timezone.utc).isoformat()
        task.append_log(f"Starting task: {task.name} ({task.task_type})")

        # Capture sys.stdout / sys.stderr and Rich console
        from musicscraper.core.report import console
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        capture_stream = TaskLogCapture(task, original_stream=old_stdout)
        sys.stdout = capture_stream
        sys.stderr = capture_stream

        # Temporarily redirect Rich console output
        old_console_file = console.file
        old_soft_wrap = getattr(console, "soft_wrap", False)
        console.file = capture_stream
        console.soft_wrap = True

        try:
            # Provide task as first argument if expected or via kwargs
            res = target_fn(task=task, *args, **kwargs)
            task.result = res
            task.status = "completed"
            task.progress = 100.0
            task.stage = "Completed successfully"
            task.append_log("Task finished successfully.")
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.stage = f"Error: {exc}"
            task.append_log(f"Task failed with error: {exc}", level="ERROR")
        finally:
            capture_stream.flush()
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            console.file = old_console_file
            console.soft_wrap = old_soft_wrap
            task.ended_at = datetime.now(timezone.utc).isoformat()

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        """Retrieves a task by ID."""
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 50) -> List[BackgroundTask]:
        """Returns list of tasks sorted newest first."""
        with self._lock:
            all_tasks = list(self._tasks.values())
        all_tasks.sort(key=lambda t: t.created_at, reverse=True)
        return all_tasks[:limit]

    def cancel_task(self, task_id: str) -> bool:
        """Marks a task as cancellation requested."""
        task = self.get_task(task_id)
        if task and task.status in ("pending", "running"):
            task.cancel_requested = True
            task.append_log("Cancellation requested by user.", level="WARNING")
            task.status = "cancelled"
            task.stage = "Cancelled"
            task.ended_at = datetime.now(timezone.utc).isoformat()
            return True
        return False


# Global TaskManager singleton
global_task_manager = TaskManager()
