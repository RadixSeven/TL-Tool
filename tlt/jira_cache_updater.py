"""Maintain a cache of Jira contents"""

import json
import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import requests
from requests.auth import AuthBase
from requests_ratelimiter import LimiterSession

from tlt.raw_issue_dict import (
    AssigneeDict,
    RawJiraIssueDict,
    TimeTrackingDict,
    WorkLogDict,
)

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


# noinspection SpellCheckingInspection
TIME_TRACKING = "timetracking"
# noinspection SpellCheckingInspection
WORK_LOGS = "worklogs"


@dataclass
class Issue:
    """A Jira issue with some clean-up and aggregation

    Attributes:
        key: The issue key.
        json_data: The JSON data for the issue.
        assignee_name: The name of the assignee or None if unassigned.
        last_updated: The last time the issue was updated.
        seconds_spent: A dictionary of user keys to the number of seconds spent.
        original_seconds_estimated: The original estimate in seconds.
    """

    key: str
    json_data: str
    assignee_name: str | None
    last_updated: str
    seconds_spent: dict[str, int]
    original_seconds_estimated: int | None

    @property
    def total_seconds_spent(self) -> int:
        """
        Calculate the total number of seconds spent on the issue.

        Returns:
            The total number of seconds spent on the issue.
        """
        return sum(self.seconds_spent.values())

    @staticmethod
    def from_raw(raw_issue: RawJiraIssueDict) -> "Issue":
        """
        Convert a RawJiraIssueDict to an Issue.

        Args:
            raw_issue: The raw issue data to convert.

        Returns:
            The Issue object created from the raw issue data.
        """
        fields = raw_issue["fields"]
        original_estimate_seconds: int | None = cast(
            TimeTrackingDict, fields.get(TIME_TRACKING, {})
        ).get("originalEstimateSeconds", None)
        work_logs = cast(
            list[WorkLogDict],
            raw_issue["fields"].get("worklog", {}).get(WORK_LOGS, []),
        )

        assignee_field = cast(AssigneeDict, fields.get("assignee", {}))
        assignee = assignee_field.get("name", None) if assignee_field else None
        seconds_spent: dict[str, int] = defaultdict(int)
        for work_log in work_logs:
            seconds_spent[work_log["author"]["key"]] += work_log[
                "timeSpentSeconds"
            ]
        return Issue(
            key=raw_issue["key"],
            json_data=json.dumps(raw_issue),
            last_updated=raw_issue["fields"]["updated"],
            seconds_spent=seconds_spent,
            original_seconds_estimated=original_estimate_seconds,
            assignee_name=assignee,
        )


class JiraCacheUpdater:
    """Class to manage Jira issue caching and periodic checks for updates."""

    def __init__(
        self,
        jira_server_base: str,
        jira_token: str,
        jql: str,
        connection_supplier: ConnectionSupplier,
        seconds_per_check: float = 5,
        requests_per_second: float = 1,
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
                    -- The name of the assignee or None if unassigned
                    -- For the same individual, this should be the
                    -- same as the key in the seconds_spent table
                    assignee_name TEXT,
                    last_updated TEXT,
                    original_seconds_estimated INTEGER,
                    -- Record the time the issue was last updated in the database
                    cache_time TEXT NOT NULL DEFAULT current_timestamp
                )
            """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS seconds_spent (
                    issue_key TEXT NOT NULL,
                    user_key TEXT NOT NULL,
                    seconds INTEGER NOT NULL,
                    PRIMARY KEY (issue_key, user_key)
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
                "SELECT last_check_time FROM checks "
                "ORDER BY last_check_time DESC LIMIT 1"
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

    def _update_issue(self, raw_issue: RawJiraIssueDict) -> None:
        """
        Insert or update an issue in the database.

        Args:
            raw_issue: The issue data to be inserted or updated.
        """
        with self.connection_supplier() as conn:
            log.debug(f"Updating issue {raw_issue['key']}")
            issue = Issue.from_raw(raw_issue)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO issues (
                    key,
                    json_data,
                    assignee_name,
                    last_updated,
                    original_seconds_estimated
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    json_data=excluded.json_data,
                    assignee_name=excluded.assignee_name
                    last_updated=excluded.last_updated
                    original_seconds_estimated=excluded.original_seconds_estimated
            """,
                (
                    issue.key,
                    issue.json_data,
                    issue.assignee_name,
                    issue.last_updated,
                    issue.original_seconds_estimated,
                ),
            )
            for user_name, seconds_spent in issue.seconds_spent.items():
                cursor.execute(
                    """
                    INSERT INTO seconds_spent (issue_key, user_key, seconds)
                    VALUES (?, ?, ?)
                    ON CONFLICT(issue_key, user_key) DO UPDATE SET
                        seconds=excluded.seconds
                    """,
                    (issue.key, user_name, seconds_spent),
                )
            conn.commit()

    def run_check(self) -> None:
        """
        Run a check for updated issues since the last check and update the
        database accordingly.
        """
        time_started = time.time()
        last_check_time = self._get_last_check_time()
        jql = self.jql
        if last_check_time:
            # Prepend a condition to the JQL query to only fetch issues updated since the last check
            last_check_str = time.strftime(
                "%Y-%m-%d %H:%M", time.gmtime(last_check_time)
            )
            jql = f'updated >= "{last_check_str}" AND ({jql})'

        log.debug(f"Running Jira check with JQL: {jql}")
        for issue in self._download_issues(jql):
            # Update each issue in the database
            self._update_issue(issue)

        # Record the time of this check started once it is finished.
        # This ensures that if the check did not complete, the next check will
        # not miss any issues it should have gotten. And if issues came after
        # the check started, they will be fetched in the next check.
        self._set_last_check_time(time_started)

    def _download_issues(
        self, jql: str
    ) -> Generator[RawJiraIssueDict, None, None]:
        """
        Download issues from Jira using the provided JQL query.

        Args:
            jql: The Jira Query Language (JQL) expression whose matches will
            be yielded.

        Yields:
            The data for the next issue returned by the query.
        """
        yield from self._raw_issue_stream(
            self.jira_server_base,
            self.jira_token,
            jql,
            fields=("*all",),
            to_expand=("names",),
        )

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

    def _raw_issue_stream(
        self,
        jira_server_base: str,
        jira_token: str,
        jql: str,
        to_expand: Iterable[str] = (),
        fields: Iterable[str] = (),
        max_results_per_page: int = 100,
    ) -> Generator[RawJiraIssueDict, None, None]:
        """Download issues from Jira and return them one by one

        Assumes API version 2

        Handles pagination

        :param jql: The JQL query limiting the issues to download.
        :param jira_server_base: The base URL of the Jira server. (e.g.
            "https://jira.example.com")
        :param jira_token: The personal access token for the Jira server.
        :param to_expand: The fields to expand in the response. See
            https://developer.atlassian.com/cloud/jira/platform/rest/v2/intro/#expansion
        :param fields: The fields to return in the response. "*all" starts with all
            instead of navigable fields. "+field" adds a field to the list of fields
            to return. "-field" removes a field from the list of fields to return.
            For more details, see
            https://developer.atlassian.com/cloud/jira/platform/rest/v2/api-group-issue-search/#api-rest-api-2-search-post
        :param max_results_per_page: The maximum number of issues to return in a
            single page of results. The actual number of issues returned may be
            less than this number if there are fewer issues matching the query.
            This is internal to this method (since it returns all issues), but may
            be important for performance tuning.
        """
        url = f"{jira_server_base}/rest/api/2/search"
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/json; charset=utf-8",
        }
        auth = BearerAuth(jira_token)

        expand_set = set(to_expand)
        field_set = set(fields)
        is_last_page = False
        first_time = True
        index_of_first_result = 0
        while not is_last_page:
            to_expand = expand_set | {"names"} if first_time else expand_set
            payload = json.dumps(
                {
                    "expand": list(to_expand),
                    "fields": list(field_set),
                    "jql": jql,
                    "maxResults": max_results_per_page,
                    "startAt": index_of_first_result,
                }
            )
            response = self.session.post(
                url, headers=headers, data=payload, auth=auth
            )
            log.debug(f"Response encoding: {response.encoding}")
            log.debug(f"Response type: {response.headers['content-type']}")
            response.raise_for_status()
            response_json = response.json()
            # Do not check for unexpected or missing names in the response.
            # This might be a later feature. (See ManagementJira msr_jira.py
            # for an example implementation.)
            if "issues" not in response_json:
                break
            response_issues = response_json["issues"]
            yield from response_issues
            index_of_first_result += len(response_issues)

            is_last_page = (
                response_json.get("isLast", False) or len(response_issues) == 0
            )


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
