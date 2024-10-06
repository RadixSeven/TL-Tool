"""Maintain a cache of Jira contents"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import cast

import requests
from requests.auth import AuthBase
from requests_ratelimiter import LimiterSession

log = logging.getLogger(__name__)

# Type alias for a function that returns a context manager for database connections
# it can be used in a with statement to manage a connection
ConnectionSupplier = Callable[[], AbstractContextManager[sqlite3.Connection]]


class BearerAuth(AuthBase):
    """Custom authentication class for adding a Bearer token to requests."""

    def __init__(self, token: str) -> None:
        """
        Initialize with the provided token.

        Args:
            token: The bearer token for authentication.
        """
        self._token = token

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        """
        Attach the Authorization header to the request.

        Returns:
            The modified request with the Authorization header.
        """
        if r.headers is not None:
            r.headers["Authorization"] = f"Bearer {self._token}"
        return r


class JiraCacheUpdater:
    """Class to manage Jira issue caching and periodic checks for updates."""

    def __init__(
        self,
        jira_server_base: str,
        jira_token: str,
        jql: str,
        connection_supplier: ConnectionSupplier,
        seconds_per_check: int = 5,
        requests_per_second: int = 1,
    ) -> None:
        """
        Initialize the JiraCacheUpdater with server details, token, JQL query,
        and a database connection manager.

        Also creates tables in the database if it does not have them.

        Args:
            jira_server_base: Base URL of the Jira server.
            jira_token: Authentication token for Jira.
            jql: JQL query to filter issues.
            connection_supplier: A callable that returns a context manager for database connections.
            seconds_per_check: Minimum number of seconds between checks.
            requests_per_second: Number of requests per second allowed for rate limiting.
        """
        self.jira_server_base = jira_server_base
        self.jira_token = jira_token
        self.jql = jql
        self.connection_supplier = connection_supplier
        self.seconds_per_check = seconds_per_check
        self.session = LimiterSession(per_second=requests_per_second)
        self.auth = BearerAuth(jira_token)

        self._init_db()

    def _init_db(self) -> None:
        """
        Initialize the SQLite database by creating necessary tables if they do
        not exist.
        """
        with self.connection_supplier() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    key TEXT PRIMARY KEY,
                    json_data TEXT,
                    last_updated TEXT
                )
            """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    last_check_time REAL
                )
            """
            )
            conn.commit()

    def _get_last_check_time(self) -> float | None:
        """
        Retrieve the time of the last check from the database.

        Returns:
            The timestamp of the last check, or None if no checks have been performed.
        """
        with self.connection_supplier() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_check_time FROM checks ORDER BY id DESC LIMIT 1"
            )
            result = cursor.fetchone()
            return result[0] if result else None

    def _set_last_check_time(self, check_time: float) -> None:
        """
        Record the time of the latest check in the database.

        Args:
            check_time: The timestamp of the latest check.
        """
        with self.connection_supplier() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO checks (last_check_time) VALUES (?)", (check_time,)
            )
            conn.commit()

    def _update_issue(self, issue: dict) -> None:
        """
        Insert or update an issue in the database.

        Args:
            issue: The issue data to be inserted or updated.
        """
        with self.connection_supplier() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO issues (key, json_data, last_updated) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    json_data=excluded.json_data,
                    last_updated=excluded.last_updated
            """,
                (issue["key"], json.dumps(issue), issue["fields"]["updated"]),
            )
            conn.commit()

    def run_check(self) -> None:
        """
        Run a check for updated issues since the last check and update the
        database accordingly.
        """
        last_check_time = self._get_last_check_time()
        jql = self.jql
        if last_check_time:
            # Prepend a condition to the JQL query to only fetch issues updated since the last check
            last_check_str = time.strftime(
                "%Y-%m-%d %H:%M", time.gmtime(last_check_time)
            )
            jql = f'updated >= "{last_check_str}" AND ({jql})'

        # Record the time of this check before starting the check
        self._set_last_check_time(time.time())

        log.debug(f"Running Jira check with JQL: {jql}")
        issues = self._download_issues(jql)
        for issue in issues:
            # Update each issue in the database
            self._update_issue(issue)

    def _download_issues(self, jql: str) -> Generator[dict, None, None]:
        """
        Download issues from Jira using the provided JQL query.

        Args:
            jql: The Jira Query Language (JQL) expression whose matches will
            be yielded.

        Yields:
            The data for the next issue returned by the query.
        """
        url = f"{self.jira_server_base}/rest/api/2/search"
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = json.dumps(
            {
                "jql": jql,
                "fields": ["*all"],  # Request all fields for each issue
                "maxResults": 100,  # Limit the number of results per request
            }
        )

        while True:
            # Make a POST request to the Jira API to fetch issues
            response = self.session.post(
                url, headers=headers, data=payload, auth=self.auth
            )
            response.raise_for_status()
            response_json = response.json()
            yield from response_json.get("issues", [])

            if response_json.get("isLast", True):
                # Break if this is the last page of results
                break

    def start(self) -> None:
        """
        Start the periodic check for Jira issues, respecting the interval
        between checks.
        """
        while True:
            start_time = time.time()
            self.run_check()
            elapsed_time = time.time() - start_time
            if elapsed_time < self.seconds_per_check:
                # Sleep to maintain the minimum interval between checks
                time.sleep(self.seconds_per_check - elapsed_time)


def create_file_db_connection_supplier(
    db_path: Path,
) -> ConnectionSupplier:
    """Return a function that can be called in a with statement to manage
    a SQLite connection.

    Args:
        db_path: The path to the SQLite database file.
    """

    @contextmanager
    def connection_manager() -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    return cast(
        ConnectionSupplier,
        connection_manager,
    )
