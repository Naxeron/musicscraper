"""
MusicScraper Web GUI package.
"""

from musicscraper.web.server import start_server, MusicScraperHTTPRequestHandler
from musicscraper.web.tasks import BackgroundTask, TaskManager, global_task_manager

__all__ = [
    "start_server",
    "MusicScraperHTTPRequestHandler",
    "BackgroundTask",
    "TaskManager",
    "global_task_manager",
]
