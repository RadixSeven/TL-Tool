import json
import sqlite3
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, Mock, patch

import pytest

from tlt.jira_cache_updater import ConnectionSupplier, JiraCacheUpdater

if TYPE_CHECKING:
    from tlt.raw_issue_dict import RawJiraIssueDict


@pytest.fixture
def in_memory_db():
    # The connection supplier always returns the SAME connection
    # this keeps the in-memory database alive for the whole test
    conn = sqlite3.connect(":memory:")

    def connection_supplier():
        return conn

    yield connection_supplier
    # Tear down - close the connection after the test
    conn.close()


@pytest.fixture
def mock_session():
    with patch("tlt.jira_cache_updater.LimiterSession") as mock:
        yield mock.return_value


@pytest.fixture
def jira_cache_updater(
    in_memory_db: ConnectionSupplier, mock_session: MagicMock
):
    updater = JiraCacheUpdater(
        jira_server_base="https://jira.example.com",
        jira_token="test_token",  # noqa: S106
        jql="project = TEST",
        connection_supplier=in_memory_db,
        seconds_per_check=1,
        requests_per_second=1,
    )
    updater.session = mock_session
    return updater


def test_init_db(
    jira_cache_updater: JiraCacheUpdater,  # noqa: ARG001
    in_memory_db: ConnectionSupplier,
):
    with in_memory_db() as conn:
        cursor = conn.cursor()

        # Check if issues table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='issues'"
        )
        assert cursor.fetchone() is not None

        # Check if checks table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='checks'"
        )
        assert cursor.fetchone() is not None


def test_get_set_last_check_time(jira_cache_updater: JiraCacheUpdater):
    # Test setting and getting last check time
    current_time = time.time()
    jira_cache_updater._set_last_check_time(current_time)

    retrieved_time = jira_cache_updater._get_last_check_time()
    assert retrieved_time == current_time


def test_update_issue(
    jira_cache_updater: JiraCacheUpdater, in_memory_db: ConnectionSupplier
):
    test_issue: RawJiraIssueDict = {
        "key": "TEST-1",
        "fields": {
            "summary": "Test issue",
            "updated": "2023-01-01T12:00:00.000+0000",
            "components": [],
            "issuetype": {
                "self": "dummy_self",
                "id": "dummy_id",
                "description": "dummy_description",
                "iconUrl": "dummy_iconUrl",
                "name": "dummy_name",
                "subtask": False,
            },
            "worklog": {
                "startAt": 0,
                "maxResults": 0,
                "total": 0,
                "worklogs": [],
            },
        },
        "expand": "dummy_expand",
        "id": "dummy_id",
        "self": "dummy_self",
    }

    jira_cache_updater._update_issue(test_issue)

    with in_memory_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM issues WHERE key=?", ("TEST-1",))
        result = cursor.fetchone()

        assert result is not None
        assert result[0] == "TEST-1"
        assert json.loads(result[1]) == test_issue
        assert result[2] == "2023-01-01T12:00:00.000+0000"


def test_run_check(
    jira_cache_updater: JiraCacheUpdater, mock_session: MagicMock
):
    # Mock the API response
    mock_response = Mock()
    mock_response.json.return_value = {
        "issues": [
            {
                "key": "TEST-1",
                "fields": {"updated": "2023-01-01T12:00:00.000+0000"},
                "summary": "Test issue",
            }
        ],
        "isLast": True,
    }
    mock_session.post.return_value = mock_response

    # Run the check
    jira_cache_updater.run_check()

    # Verify that the issue was updated in the database
    with jira_cache_updater.connection_supplier() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM issues WHERE key=?", ("TEST-1",))
        result = cursor.fetchone()

        assert result is not None
        assert result[0] == "TEST-1"
        assert "Test issue" in result[1]


def test_download_issues(
    jira_cache_updater: JiraCacheUpdater, mock_session: MagicMock
):
    # Mock the API response
    mock_response = Mock()
    mock_response.json.return_value = {
        "issues": [
            {"key": "TEST-1", "fields": {"summary": "Issue 1"}},
            {"key": "TEST-2", "fields": {"summary": "Issue 2"}},
        ],
        "isLast": True,
    }
    mock_session.post.return_value = mock_response

    issues = list(jira_cache_updater._download_issues("project = TEST"))

    assert len(issues) == 2
    assert issues[0]["key"] == "TEST-1"
    assert issues[1]["key"] == "TEST-2"


def test_start(jira_cache_updater: JiraCacheUpdater):
    with patch.object(jira_cache_updater, "run_check") as mock_run_check, patch(
        "time.sleep"
    ) as mock_sleep:
        # Make start() run only twice
        mock_run_check.side_effect = [None, None, Exception("Stop")]

        with pytest.raises(Exception, match="Stop"):
            jira_cache_updater.start()

        assert mock_run_check.call_count == 3
        assert mock_sleep.call_count == 2


if __name__ == "__main__":
    pytest.main()
