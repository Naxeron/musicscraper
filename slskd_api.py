#!/usr/bin/env python3
"""
slskd API Client
================
A robust Python client for interacting with slskd's REST API (v0).

Features:
- Programmatic JWT session authentication and automatic token refresh.
- Soulseek search submission and response polling.
- Peer remote directory browsing.
- Download enqueuing for individual files and complete album directories.
- Transfer queue inspection and search cleanup.
"""

import os
import time
from typing import Dict, List, Optional, Any, Union
from pathlib import Path
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class SlskdAPIError(Exception):
    """Base exception for slskd API errors."""
    pass


class SlskdClient:
    """Client for communicating with the slskd REST API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.base_url = (base_url or os.environ.get("SLSKD_URL") or "http://localhost:5030").rstrip("/")
        self.username = username or os.environ.get("SLSKD_USERNAME")
        self.password = password or os.environ.get("SLSKD_PASSWORD")
        self.api_key = api_key or os.environ.get("SLSKD_API_KEY")
        self.timeout = timeout

        self.session = requests.Session()
        self.token: Optional[str] = None
        self.token_expiry: float = 0

        # Memory cache for directory browsing to reduce redundant network roundtrips
        self._directory_cache: Dict[str, List[Dict[str, Any]]] = {}

        self._ensure_authenticated()

    def _ensure_authenticated(self, force_refresh: bool = False) -> None:
        """Ensures an active, valid authentication token or API key header."""
        if self.api_key:
            self.session.headers.update({"X-API-Key": self.api_key})
            return

        now = time.time()
        if not force_refresh and self.token and now < self.token_expiry - 60:
            return

        if not self.username or not self.password:
            # Check if server allows unauthenticated requests
            try:
                resp = self.session.get(f"{self.base_url}/api/v0/application", timeout=self.timeout)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            raise SlskdAPIError("No slskd credentials found. Please set SLSKD_USERNAME and SLSKD_PASSWORD or SLSKD_API_KEY in .env.")

        # Authenticate via /api/v0/session
        session_url = f"{self.base_url}/api/v0/session"
        try:
            resp = self.session.post(
                session_url,
                json={"username": self.username, "password": self.password},
                timeout=self.timeout
            )
            if resp.status_code != 200:
                raise SlskdAPIError(f"Authentication failed (HTTP {resp.status_code}): {resp.text}")

            data = resp.json()
            self.token = data.get("token")
            self.token_expiry = data.get("expires", now + 3600)
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        except requests.RequestException as e:
            raise SlskdAPIError(f"Failed to connect to slskd at {self.base_url}: {e}")

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Performs an authenticated request with automatic token retry on 401."""
        self._ensure_authenticated()
        url = f"{self.base_url}{path}"
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout

        try:
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 401 and not self.api_key:
                # Token may have expired; refresh once and retry
                self._ensure_authenticated(force_refresh=True)
                resp = self.session.request(method, url, **kwargs)
            return resp
        except requests.RequestException as e:
            raise SlskdAPIError(f"slskd request error [{method} {path}]: {e}")

    def get_application(self) -> Dict[str, Any]:
        """Gets slskd system state, connected user, and Soulseek server status."""
        resp = self._request("GET", "/api/v0/application")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get application info (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def get_options(self) -> Dict[str, Any]:
        """Gets slskd configuration and storage directories."""
        resp = self._request("GET", "/api/v0/options")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get options (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def list_searches(self) -> List[Dict[str, Any]]:
        """Lists all searches stored in slskd history."""
        resp = self._request("GET", "/api/v0/searches")
        if resp.status_code != 200:
            return []
        return resp.json()

    def search(
        self,
        query: str,
        timeout: float = 10.0,
        poll_interval: float = 1.0,
        min_responses: int = 5,
        use_existing: bool = True,
        delete_after: bool = False
    ) -> Dict[str, Any]:
        """
        Initiates a search on the Soulseek network and polls until complete,
        sufficient responses are gathered, or timeout occurs.
        If use_existing is True, reuses existing completed search results if available.
        """
        clean_q = query.strip()
        if not clean_q:
            return {"responses": [], "responseCount": 0, "fileCount": 0}

        # Check existing searches in slskd history
        if use_existing:
            existing = self.list_searches()
            for s in reversed(existing):
                if s.get("searchText", "").strip().lower() == clean_q.lower() and s.get("fileCount", 0) > 0:
                    sid = s.get("id")
                    if sid:
                        try:
                            res = self.get_search_results(sid)
                            if res.get("responses"):
                                return res
                        except Exception:
                            pass

        init_resp = self._request("POST", "/api/v0/searches", json={"searchText": clean_q})
        if init_resp.status_code not in (200, 201):
            raise SlskdAPIError(f"Failed to initiate search for '{clean_q}' (HTTP {init_resp.status_code}): {init_resp.text}")

        search_data = init_resp.json()
        search_id = search_data.get("id")

        start_time = time.time()
        last_data = search_data

        while time.time() - start_time < timeout:
            time.sleep(poll_interval)
            poll_resp = self._request("GET", f"/api/v0/searches/{search_id}?includeResponses=true")
            if poll_resp.status_code != 200:
                continue

            last_data = poll_resp.json()
            is_complete = last_data.get("isComplete", False)
            state = last_data.get("state", "")
            response_count = last_data.get("responseCount", 0)

            if is_complete or "Completed" in state:
                break
            if response_count >= min_responses and (time.time() - start_time) >= 4.0:
                break

        if delete_after and search_id:
            try:
                self._request("DELETE", f"/api/v0/searches/{search_id}")
            except Exception:
                pass

        return last_data

    def get_search_results(self, search_id: str) -> Dict[str, Any]:
        """Fetches full search object with responses for a given search ID."""
        resp = self._request("GET", f"/api/v0/searches/{search_id}?includeResponses=true")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get search results for {search_id}: {resp.text}")
        return resp.json()

    def delete_search(self, search_id: str) -> bool:
        """Deletes a search from slskd memory."""
        resp = self._request("DELETE", f"/api/v0/searches/{search_id}")
        return resp.status_code in (200, 204)

    def browse_directory(self, username: str, directory: str, use_cache: bool = True) -> List[Dict[str, Any]]:
        """
        Fetches the complete remote file listing of a peer's directory.
        Returns a list of directory nodes containing file metadata.
        """
        cache_key = f"{username}:{directory}"
        if use_cache and cache_key in self._directory_cache:
            return self._directory_cache[cache_key]

        resp = self._request("POST", f"/api/v0/users/{username}/directory", json={"directory": directory})
        if resp.status_code != 200:
            return []

        data = resp.json()
        if use_cache and data:
            self._directory_cache[cache_key] = data
        return data

    def enqueue_download(self, username: str, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Enqueues a list of files from a peer for download.
        Each file item must contain at least 'filename' (full path) and 'size'.
        """
        if not files:
            return {"status": "skipped", "count": 0}

        payload = [
            {"filename": f.get("filename"), "size": f.get("size", 0)}
            for f in files
            if f.get("filename")
        ]

        if not payload:
            return {"status": "skipped", "count": 0}

        resp = self._request("POST", f"/api/v0/transfers/downloads/{username}", json=payload)
        if resp.status_code not in (200, 201, 202):
            raise SlskdAPIError(f"Failed to enqueue download from {username} (HTTP {resp.status_code}): {resp.text}")

        return {"status": "enqueued", "username": username, "files_count": len(payload)}

    def get_downloads(self) -> List[Dict[str, Any]]:
        """Gets all download transfer states and queues."""
        resp = self._request("GET", "/api/v0/transfers/downloads")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get downloads (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def get_queued_filenames(self) -> set:
        """Returns a set of all currently active, queued, or completed filenames in slskd."""
        downloads = self.get_downloads()
        queued = set()
        for user_transfers in downloads:
            for d in user_transfers.get("directories", []):
                for f in d.get("files", []):
                    fn = f.get("filename")
                    if fn:
                        queued.add(fn)
        return queued
