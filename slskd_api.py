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
import re
import time
import urllib.parse
from typing import Dict, List, Optional, Any, Union, Tuple, Set
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
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
        self._lock = threading.Lock()

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

        with self._lock:
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
        timeout: float = 12.0,
        poll_interval: float = 1.0,
        min_responses: int = 10,
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

        search_id = None
        # Check existing searches in slskd history
        if use_existing:
            existing = self.list_searches()
            for s in reversed(existing):
                if s.get("searchText", "").strip().lower() == clean_q.lower():
                    sid = s.get("id")
                    st = s.get("state", "")
                    is_done = s.get("isComplete", False) or "Completed" in st
                    if sid and (is_done and s.get("fileCount", 0) > 0):
                        try:
                            res = self.get_search_results(sid)
                            if res.get("responses"):
                                return res
                        except Exception:
                            pass
                    elif sid and not is_done:
                        # An active search is already running in slskd, track it
                        search_id = sid
                        break

        if not search_id:
            init_resp = self._request("POST", "/api/v0/searches", json={"searchText": clean_q})
            if init_resp.status_code not in (200, 201):
                raise SlskdAPIError(f"Failed to initiate search for '{clean_q}' (HTTP {init_resp.status_code}): {init_resp.text}")

            search_data = init_resp.json()
            search_id = search_data.get("id")
            last_data = search_data
        else:
            last_data = {"id": search_id, "searchText": clean_q, "responses": []}

        start_time = time.time()

        while time.time() - start_time < timeout:
            time.sleep(poll_interval)
            try:
                poll_resp = self._request("GET", f"/api/v0/searches/{search_id}?includeResponses=true")
                if poll_resp.status_code != 200:
                    continue

                last_data = poll_resp.json()
                is_complete = last_data.get("isComplete", False)
                state = last_data.get("state", "")
                response_count = last_data.get("responseCount", 0)
                file_count = last_data.get("fileCount", 0)

                if is_complete or "Completed" in state:
                    break
                # Only break early if substantial responses are already gathered and enough time has elapsed
                if response_count >= min_responses and file_count > 0 and (time.time() - start_time) >= 8.0:
                    break
            except Exception:
                pass

        if delete_after and search_id:
            try:
                self._request("DELETE", f"/api/v0/searches/{search_id}")
            except Exception:
                pass

        return last_data

    def batch_search(
        self,
        queries: List[str],
        timeout: float = 14.0,
        poll_interval: float = 1.0,
        max_concurrent: int = 8,
        use_existing: bool = True
    ) -> Dict[str, Dict[str, Any]]:
        """
        Executes multiple Soulseek searches concurrently across the Soulseek network.
        Dispatches all queries in parallel, tracks active search IDs, and polls
        until completion or timeout.
        Returns a dictionary mapping: { clean_query: search_result_dict }.
        """
        clean_queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not clean_queries:
            return {}

        results: Dict[str, Dict[str, Any]] = {}
        pending_queries: List[str] = []
        query_to_search_id: Dict[str, str] = {}

        # 1. Check existing searches in slskd history
        if use_existing:
            existing = self.list_searches()
            existing_by_text: Dict[str, List[Dict[str, Any]]] = {}
            for s in existing:
                st = s.get("searchText", "").strip().lower()
                if st:
                    if st not in existing_by_text:
                        existing_by_text[st] = []
                    existing_by_text[st].append(s)

            for q in clean_queries:
                q_lower = q.lower()
                if q_lower in existing_by_text:
                    matches = existing_by_text[q_lower]
                    best_s = max(matches, key=lambda x: (x.get("fileCount", 0), 1 if "Completed" in x.get("state", "") else 0))
                    sid = best_s.get("id")
                    st = best_s.get("state", "")
                    is_done = best_s.get("isComplete", False) or "Completed" in st
                    if best_s.get("fileCount", 0) > 0:
                        try:
                            full_res = self.get_search_results(sid)
                            if full_res.get("responses"):
                                results[q] = full_res
                                continue
                        except Exception:
                            pass
                    elif is_done and best_s.get("fileCount", 0) == 0:
                        # Stale 0-file search, delete it to allow fresh search
                        try:
                            self.delete_search(sid)
                        except Exception:
                            pass
                    elif not is_done and sid:
                        query_to_search_id[q] = sid

                pending_queries.append(q)
        else:
            pending_queries = list(clean_queries)

        # 2. Dispatch remaining searches in controlled chunks
        chunk_size = max(1, min(max_concurrent, 4))
        for i in range(0, len(pending_queries), chunk_size):
            chunk = pending_queries[i:i + chunk_size]
            chunk_query_ids: Dict[str, str] = {}

            def _init_single(q_str: str) -> Tuple[str, Optional[str]]:
                # Reuse in-progress search ID if available
                if q_str in query_to_search_id:
                    return q_str, query_to_search_id[q_str]
                try:
                    resp = self._request("POST", "/api/v0/searches", json={"searchText": q_str})
                    if resp.status_code in (200, 201):
                        return q_str, resp.json().get("id")
                except Exception:
                    pass
                return q_str, None

            with ThreadPoolExecutor(max_workers=len(chunk)) as executor:
                futures = [executor.submit(_init_single, q) for q in chunk]
                for fut in as_completed(futures):
                    q_str, sid = fut.result()
                    if sid:
                        chunk_query_ids[q_str] = sid

            # Poll chunk searches concurrently
            active_chunk = dict(chunk_query_ids)
            start_time = time.time()

            while active_chunk and (time.time() - start_time < timeout):
                time.sleep(poll_interval)
                for q_str, sid in list(active_chunk.items()):
                    try:
                        poll_resp = self._request("GET", f"/api/v0/searches/{sid}?includeResponses=true")
                        if poll_resp.status_code == 200:
                            s_data = poll_resp.json()
                            is_complete = s_data.get("isComplete", False)
                            state = s_data.get("state", "")
                            resp_count = s_data.get("responseCount", 0)
                            file_count = s_data.get("fileCount", 0)

                            if file_count > 0 and len(s_data.get("responses", [])) > 0:
                                results[q_str] = s_data

                            if is_complete or "Completed" in state:
                                if q_str not in results or results[q_str].get("fileCount", 0) == 0:
                                    results[q_str] = s_data
                                del active_chunk[q_str]
                            elif resp_count >= 8 and file_count > 0 and (time.time() - start_time) >= 6.0:
                                results[q_str] = s_data
                                del active_chunk[q_str]
                    except Exception:
                        pass

            # Final pass for chunk
            for q_str, sid in active_chunk.items():
                if q_str not in results or results[q_str].get("fileCount", 0) == 0:
                    try:
                        poll_resp = self._request("GET", f"/api/v0/searches/{sid}?includeResponses=true")
                        if poll_resp.status_code == 200:
                            results[q_str] = poll_resp.json()
                    except Exception:
                        pass

        return results

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
        with self._lock:
            if use_cache and cache_key in self._directory_cache:
                return self._directory_cache[cache_key]

        try:
            resp = self._request("POST", f"/api/v0/users/{username}/directory", json={"directory": directory})
            if resp.status_code != 200:
                return []

            data = resp.json()
            if use_cache and data:
                with self._lock:
                    self._directory_cache[cache_key] = data
            return data
        except Exception:
            return []

    def browse_directories_batch(
        self,
        requests_list: List[Tuple[str, str]],
        use_cache: bool = True,
        max_workers: int = 6
    ) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        """
        Concurrently browses multiple peer remote directories.
        Returns mapping: { (username, directory): directory_nodes_list }
        """
        unique_reqs = list(dict.fromkeys(requests_list))
        results: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        to_fetch: List[Tuple[str, str]] = []

        for user, dir_path in unique_reqs:
            cache_key = f"{user}:{dir_path}"
            with self._lock:
                if use_cache and cache_key in self._directory_cache:
                    results[(user, dir_path)] = self._directory_cache[cache_key]
                    continue
            to_fetch.append((user, dir_path))

        if not to_fetch:
            return results

        def _fetch_one(u: str, d: str) -> Tuple[Tuple[str, str], List[Dict[str, Any]]]:
            res = self.browse_directory(u, d, use_cache=use_cache)
            return (u, d), res

        with ThreadPoolExecutor(max_workers=min(len(to_fetch), max_workers)) as executor:
            futures = [executor.submit(_fetch_one, u, d) for u, d in to_fetch]
            for fut in as_completed(futures):
                key, nodes = fut.result()
                results[key] = nodes

        return results

    def enqueue_download(self, username: str, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Enqueues a list of files from a peer for download.
        Each file item must contain at least 'filename' (full path) and 'size'.
        """
        if not files:
            return {"status": "skipped", "count": 0}

        import urllib.parse
        # Clean username from any trailing IP/port info (e.g. "NxHx]||||[xNxP (178.234.127.195:51726)")
        clean_user = re.sub(r"\s*\(.*?\)$", "", username).strip()
        encoded_user = urllib.parse.quote(clean_user, safe="")

        payload = [
            {"filename": f.get("filename"), "size": f.get("size", 0)}
            for f in files
            if f.get("filename")
        ]

        if not payload:
            return {"status": "skipped", "count": 0}

        # Chunk large lists (max 50 files per call) with extended timeout
        chunk_size = 50
        for i in range(0, len(payload), chunk_size):
            chunk = payload[i:i + chunk_size]
            resp = self._request("POST", f"/api/v0/transfers/downloads/{encoded_user}", json=chunk, timeout=30.0)
            if resp.status_code not in (200, 201, 202):
                raise SlskdAPIError(f"Failed to enqueue download from {clean_user} (HTTP {resp.status_code}): {resp.text}")

        return {"status": "enqueued", "username": clean_user, "files_count": len(payload)}

    def get_downloads(self) -> List[Dict[str, Any]]:
        """Gets all download transfer states and queues."""
        resp = self._request("GET", "/api/v0/transfers/downloads")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get downloads (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def get_queued_filenames(self) -> Set[str]:
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

    def get_queued_track_fingerprints(self) -> Dict[str, Set[str]]:
        """
        Returns multiple fingerprint sets of active, queued, and completed downloads in slskd:
        - 'full_paths': exact remote filenames/paths
        - 'base_filenames': lowercase base filenames (e.g. '01 - track.flac')
        - 'clean_titles': normalized track title tokens stripped of numbers/artists
        """
        downloads = self.get_downloads()
        full_paths = set()
        base_filenames = set()
        clean_titles = set()

        for user_transfers in downloads:
            for d in user_transfers.get("directories", []):
                for f in d.get("files", []):
                    fn = f.get("filename")
                    if not fn:
                        continue
                    full_paths.add(fn)
                    clean_p = fn.replace("/", "\\").split("\\")[-1]
                    if clean_p:
                        base_filenames.add(clean_p.lower())
                        no_ext = os.path.splitext(clean_p)[0]
                        # Clean leading track numbers
                        clean_t = re.sub(r"^(\d+[\-_.]|\d+[\-_.]\d+|\d+)\s*[-_.]*\s*", "", no_ext).strip().lower()
                        if " - " in clean_t:
                            clean_t = clean_t.split(" - ", 1)[1].strip()
                        if clean_t:
                            clean_titles.add(clean_t)

        return {
            "full_paths": full_paths,
            "base_filenames": base_filenames,
            "clean_titles": clean_titles,
        }

    def get_transfers_summary(self) -> Dict[str, Any]:
        """Returns aggregated summary of download queues and active transfer speeds."""
        downloads = self.get_downloads()
        total_files = 0
        total_bytes = 0
        active_count = 0
        queued_count = 0
        completed_count = 0
        errored_count = 0

        for user_transfers in downloads:
            for d in user_transfers.get("directories", []):
                for f in d.get("files", []):
                    total_files += 1
                    total_bytes += f.get("size", 0)
                    st = f.get("state", "").lower()
                    if "downloading" in st or "active" in st:
                        active_count += 1
                    elif "queued" in st or "requested" in st:
                        queued_count += 1
                    elif "completed" in st or "succeeded" in st:
                        completed_count += 1
                    elif "errored" in st or "cancelled" in st or "aborted" in st:
                        errored_count += 1

        return {
            "total_files": total_files,
            "total_bytes": total_bytes,
            "active_transfers": active_count,
            "queued_transfers": queued_count,
            "completed_transfers": completed_count,
            "errored_transfers": errored_count,
        }

    def delete_download(self, username: str, transfer_id: Optional[str] = None) -> bool:
        """Cancels and removes download(s) from a user."""
        path = f"/api/v0/transfers/downloads/{username}"
        if transfer_id:
            path += f"/{transfer_id}"
        resp = self._request("DELETE", path)
        return resp.status_code in (200, 204)

