"""
Resilient HTTP session creation, retry policies, user-agent defaults, and rate limiting.
"""

import time
import threading
from typing import Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from musicscraper.config import Config


class RateLimiter:
    """Thread-safe rate limiter to enforce delays between API requests."""

    def __init__(self, min_interval: float = 0.2):
        self.min_interval = min_interval
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Blocks until the required interval has passed since the last request."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.time()


def create_resilient_session(
    user_agent: Optional[str] = None,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple = (429, 500, 502, 503, 504),
) -> requests.Session:
    """Creates a requests.Session with standard retry adapter and User-Agent."""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": user_agent or Config.USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9,ja;q=0.8"
    })
    return session
