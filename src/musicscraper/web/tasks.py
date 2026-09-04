"""
Background task manager with thread-safe execution, stdout/Rich log capturing,
and real-time status tracking for the MusicScraper Web GUI.
"""

import io
import re
import sys
import uuid
import collections
import threading
from typing import Dict, List, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone


_task_log_context = threading.local()
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class TaskCancelledException(Exception):
    """Raised when a task is cooperatively cancelled during execution."""
    pass


class ThreadLocalStreamProxy(io.TextIOBase):
    """
    Transparent stream proxy that routes write/flush calls to the current
    thread's TaskLogCapture stream if active, or falls back to the original stream.
    """

    def __init__(self, fallback_stream: io.TextIOBase):
        self._fallback = fallback_stream

    @property
    def fallback_stream(self) -> io.TextIOBase:
        return self._fallback

    def write(self, s: str) -> int:
        stream = getattr(_task_log_context, "capture_stream", None)
        if stream is not None:
            return stream.write(s)
        try:
            return self._fallback.write(s)
        except Exception:
            return len(s)

    def flush(self) -> None:
        stream = getattr(_task_log_context, "capture_stream", None)
        if stream is not None:
            stream.flush()
            return
        try:
            self._fallback.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        stream = getattr(_task_log_context, "capture_stream", None)
        if stream is not None:
            return False
        try:
            return self._fallback.isatty()
        except Exception:
            return False

    def fileno(self) -> int:
        try:
            return self._fallback.fileno()
        except Exception:
            raise io.UnsupportedOperation("fileno")

    def writable(self) -> bool:
        return True


class TaskLogCapture(io.StringIO):
    """Custom stream that captures written text line-by-line into task logs."""

    def __init__(self, task: "BackgroundTask", original_stream: Optional[io.TextIOBase] = None):
        super().__init__()
        self.task = task
        self.original_stream = original_stream
        self._line_buffer = ""
        self._lock = threading.RLock()

    def write(self, s: str) -> int:
        if self.original_stream:
            try:
                self.original_stream.write(s)
                self.original_stream.flush()
            except Exception:
                pass

        with self._lock:
            self._line_buffer += s
            # Safety bound line buffer to prevent unbounded memory spike on huge single-line streams
            while len(self._line_buffer) > 65536 and "\n" not in self._line_buffer:
                chunk = self._line_buffer[:65536]
                self._line_buffer = self._line_buffer[65536:]
                self._process_line(chunk)

            while "\n" in self._line_buffer:
                line, self._line_buffer = self._line_buffer.split("\n", 1)
                self._process_line(line)
        return len(s)

    def _process_line(self, line: str) -> None:
        clean_line = line.rstrip("\r")
        if "\r" in clean_line:
            clean_line = clean_line.split("\r")[-1]
        clean_line = _ANSI_ESCAPE_RE.sub("", clean_line)
        if clean_line.strip():
            self.task.append_log(clean_line)

    def flush(self) -> None:
        with self._lock:
            if self._line_buffer:
                clean_line = self._line_buffer.rstrip("\r\n")
                if "\r" in clean_line:
                    clean_line = clean_line.split("\r")[-1]
                clean_line = _ANSI_ESCAPE_RE.sub("", clean_line)
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

    def __init__(
        self,
        task_id: str,
        name: str,
        task_type: str,
        params: Dict[str, Any],
        max_logs: int = 1000
    ):
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
        self._max_logs = max_logs
        self.logs: collections.deque = collections.deque(maxlen=max_logs)
        self._total_log_count = 0
        self.cancel_requested = False
        self._lock = threading.RLock()
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
            self._total_log_count += 1
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(entry)
            except Exception:
                pass

    def update_progress(self, progress: float, stage: Optional[str] = None) -> None:
        """Updates numeric progress (0-100) and current stage description."""
        with self._lock:
            if self.status == "cancelled" or self.cancel_requested:
                return
            try:
                prog_val = float(progress)
            except (ValueError, TypeError):
                prog_val = self.progress
            self.progress = max(0.0, min(100.0, prog_val))
            if stage is not None:
                self.stage = str(stage)
        if stage is not None:
            self.append_log(f"[{int(self.progress)}%] {stage}")

    def check_cancelled(self) -> None:
        """Raises TaskCancelledException if cancellation has been requested."""
        with self._lock:
            if self.cancel_requested or self.status == "cancelled":
                raise TaskCancelledException(f"Task '{self.id}' ({self.name}) was cancelled.")

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self.cancel_requested or self.status == "cancelled"

    def cancel(self, future: Optional[Future] = None) -> bool:
        """Atomically requests cancellation and transitions status to cancelled."""
        with self._lock:
            if self.status in ("completed", "failed", "cancelled"):
                return False
            self.cancel_requested = True
            self.status = "cancelled"
            self.stage = "Cancelled"
            if self.ended_at is None:
                self.ended_at = datetime.now(timezone.utc).isoformat()
            entry = {
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "level": "WARNING",
                "message": "Cancellation requested by user."
            }
            self.logs.append(entry)
            self._total_log_count += 1
            listeners = list(self._listeners)

        if future is not None and not future.done():
            try:
                future.cancel()
            except Exception:
                pass

        for listener in listeners:
            try:
                listener(entry)
            except Exception:
                pass

        return True

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Registers a callback for incoming log events."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Unregisters a log callback."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def to_dict(self, include_logs: bool = False, log_limit: int = 200) -> Dict[str, Any]:
        """Serializes task state for JSON API in a thread-safe snapshot."""
        with self._lock:
            data = {
                "id": self.id,
                "name": self.name,
                "type": self.task_type,
                "status": self.status,
                "progress": self.progress,
                "stage": self.stage,
                "params": dict(self.params) if self.params else {},
                "result": self.result,
                "error": self.error,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "log_count": self._total_log_count,
            }
            if include_logs:
                all_logs = list(self.logs)
                if log_limit > 0:
                    data["logs"] = all_logs[-log_limit:]
                else:
                    data["logs"] = all_logs
            return data


class TaskManager:
    """Thread-safe manager for background task execution and querying."""

    def __init__(self, max_workers: int = 4, max_retained_tasks: int = 200, max_logs_per_task: int = 1000):
        self._tasks: Dict[str, BackgroundTask] = {}
        self._futures: Dict[str, Future] = {}
        self._max_retained_tasks = max_retained_tasks
        self._max_logs_per_task = max_logs_per_task
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="MusicScraperTask")
        self._lock = threading.RLock()
        self._is_shutdown = False
        self._install_stream_proxies()

    def _install_stream_proxies(self) -> None:
        """Installs thread-local stream proxies on sys.stdout and sys.stderr if not already installed."""
        if not isinstance(sys.stdout, ThreadLocalStreamProxy):
            sys.stdout = ThreadLocalStreamProxy(sys.__stdout__ or sys.stdout)
        if not isinstance(sys.stderr, ThreadLocalStreamProxy):
            sys.stderr = ThreadLocalStreamProxy(sys.__stderr__ or sys.stderr)
        try:
            from musicscraper.core.report import console
            console.file = sys.stdout
        except Exception:
            pass

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
        task = BackgroundTask(
            task_id=task_id,
            name=name,
            task_type=task_type,
            params=params or {},
            max_logs=self._max_logs_per_task
        )

        with self._lock:
            if self._is_shutdown:
                raise RuntimeError("Cannot submit task: TaskManager is shut down.")
            self._tasks[task_id] = task
            future = self._executor.submit(self._run_task, task, target_fn, *args, **kwargs)
            self._futures[task_id] = future
            self._prune_old_tasks_unlocked()

        return task

    def _prune_old_tasks_unlocked(self) -> None:
        """Prunes oldest terminal tasks when capacity exceeds max_retained_tasks."""
        if len(self._tasks) <= self._max_retained_tasks:
            return

        terminal_tasks = [
            (tid, t) for tid, t in self._tasks.items()
            if t.status in ("completed", "failed", "cancelled")
        ]
        terminal_tasks.sort(key=lambda item: item[1].created_at)

        excess = len(self._tasks) - self._max_retained_tasks
        for tid, _ in terminal_tasks[:excess]:
            self._tasks.pop(tid, None)
            self._futures.pop(tid, None)

    def _run_task(self, task: BackgroundTask, target_fn: Callable[..., Any], *args, **kwargs) -> None:
        """Executes task function within captured output context."""
        cancelled_before_run = False
        with task._lock:
            if task.status == "cancelled" or task.cancel_requested:
                task.append_log("Task cancelled before execution started.", level="WARNING")
                cancelled_before_run = True
            else:
                task.status = "running"
                task.started_at = datetime.now(timezone.utc).isoformat()
                task.stage = "Running"
                task.append_log(f"Starting task: {task.name} ({task.task_type})")

        if cancelled_before_run:
            with self._lock:
                self._prune_old_tasks_unlocked()
            return

        capture_stream = TaskLogCapture(task, original_stream=None)
        _task_log_context.capture_stream = capture_stream

        try:
            try:
                res = target_fn(task, *args, **kwargs)
            except TypeError:
                try:
                    res = target_fn(*args, task=task, **kwargs)
                except TypeError:
                    res = target_fn(*args, **kwargs)
            with task._lock:
                if task.status != "cancelled" and not task.cancel_requested:
                    task.result = res
                    task.status = "completed"
                    task.progress = 100.0
                    task.stage = "Completed successfully"
                    task.append_log("Task finished successfully.")
                else:
                    task.append_log("Task execution terminated due to cancellation.", level="WARNING")
        except (TaskCancelledException, InterruptedError) as exc:
            with task._lock:
                task.status = "cancelled"
                task.stage = "Cancelled"
                task.append_log(str(exc), level="WARNING")
        except BaseException as exc:
            with task._lock:
                if task.status != "cancelled" and not task.cancel_requested:
                    task.status = "failed"
                    task.error = str(exc) if str(exc) else exc.__class__.__name__
                    task.stage = f"Error: {task.error}"
                    task.append_log(f"Task failed with error: {task.error}", level="ERROR")
                else:
                    task.append_log(f"Task cancelled; suppressed error: {exc}", level="WARNING")
        finally:
            try:
                capture_stream.flush()
            finally:
                _task_log_context.capture_stream = None

            with task._lock:
                if task.ended_at is None:
                    task.ended_at = datetime.now(timezone.utc).isoformat()

            with self._lock:
                self._prune_old_tasks_unlocked()

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
        """Marks a task as cancellation requested and attempts to cancel queued future."""
        with self._lock:
            task = self._tasks.get(task_id)
            future = self._futures.get(task_id)
            if not task:
                return False
            cancelled = task.cancel(future=future)
            self._prune_old_tasks_unlocked()
            return cancelled

    def delete_task(self, task_id: str) -> bool:
        """Removes a finished task from the registry."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in ("completed", "failed", "cancelled"):
                return False
            self._tasks.pop(task_id, None)
            self._futures.pop(task_id, None)
            return True

    def shutdown(self, wait: bool = True, cancel_pending: bool = True) -> None:
        """Shuts down the worker pool, optionally cancelling pending/running tasks."""
        with self._lock:
            self._is_shutdown = True
            tasks_and_futures = list(zip(
                [self._tasks.get(tid) for tid in self._futures],
                self._futures.values()
            ))

        if cancel_pending:
            for task, future in tasks_and_futures:
                if task and task.status in ("pending", "running"):
                    task.cancel(future=future)

        try:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_pending)
        except TypeError:
            self._executor.shutdown(wait=wait)


# Global TaskManager singleton
global_task_manager = TaskManager()
