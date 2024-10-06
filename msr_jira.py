#!/usr/bin/env python3
import json
import logging
from typing import Generator, Iterable

from requests.auth import AuthBase
from requests_ratelimiter import LimiterSession

from raw_issue_dict import RawJiraIssueDict
from worklog_issue import Issue

log = logging.getLogger(__name__)
session = LimiterSession(per_second=1)


class BearerAuth(AuthBase):
    """Attaches HTTP Bearer Authorization to the given Request object."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __eq__(self, other):
        return all(
            [
                self._token == getattr(other, "_token", None),
            ]
        )

    def __ne__(self, other):
        return not self == other

    def __call__(self, r):
        r.headers["Authorization"] = f"Bearer {self._token}"
        return r


def download_issues(
    jira_server_base: str, jira_token: str, jql: str, skip_vacation: bool = True
) -> dict[str, Issue]:
    """Download issues from Jira and return them as a dict of issue keys to
    issues.

    Assumes API version 2

    :param jql: The JQL query limiting the issues to download.
    :param jira_server_base: The base URL of the Jira server. (e.g.
        "https://jira.example.com")
    :param jira_token: The personal access token for the Jira server.
    :param skip_vacation: If True, skip issues with the "vacation" component.
    """
    log.debug(f"Downloading issues from Jira: {jql}")
    issues = (
        Issue.from_jira(raw_issue)
        for raw_issue in raw_issue_stream(
            jira_server_base,
            jira_token,
            jql,
            to_expand=(),
            fields=(
                "*all",
                "-comment",
                "-watches",
                "-votes",
            ),
        )
    )
    if skip_vacation:
        issues = (
            i
            for i in issues
            if "vacation" not in i.components and "Vacation" not in i.components
        )
    return {i.key: i for i in issues}


def raw_issue_stream(
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
    """  # noqa: E501
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
        response = session.post(url, headers=headers, data=payload, auth=auth)
        log.debug(f"Response encoding: {response.encoding}")
        log.debug(f"Response type: {response.headers['content-type']}")
        response.raise_for_status()
        response_json = response.json()
        if "names" in response_json:
            warn_about_changed_field_names(response_json["names"], field_set)
        if "issues" not in response_json:
            break
        response_issues = response_json["issues"]
        for issue in response_issues:
            yield issue
        index_of_first_result += len(response_issues)

        is_last_page = (
            response_json.get("isLast", False) or len(response_issues) == 0
        )


def _first_char(s: str) -> str | None:
    """Return the first character of the string or None if the string is empty.

    :param s: The string to get the first character of"""
    return s[0] if s else None


def warn_about_changed_field_names(
    response_names: dict[str, str], fields: set[str]
):
    """Warn about any changes in the field names from Jira.

    :param fields: The issue fields being requested from the Jira server
    :param response_names: The field names returned from the Jira server"""

    removed_names = {f[1:] for f in fields if _first_char(f) == "-"}
    added_names = {f[1:] for f in fields if _first_char(f) == "+"}
    if "*all" in fields:
        all_names = (
            _non_navigable_jira_issue_field_names
            | _navigable_jira_issue_field_names
        )
        expected = {
            k: v for k, v in all_names.items() if k not in removed_names
        }
    else:
        starting_names = added_names | _navigable_jira_issue_field_names.keys()
        remaining_names = starting_names - removed_names
        expected = {
            k: v
            for k, v in _navigable_jira_issue_field_names.items()
            if k in remaining_names
        }

    if response_names != expected:
        for key, value in response_names.items():
            # This format makes the output easy to copy into the code to update
            # the expected field names
            pair = f"{json.dumps(key)}: {json.dumps(value)},"
            if key not in expected:
                if "*all" in fields or key in added_names:
                    log.warning(f"New field: {pair}")
                else:
                    log.warning(f"New navigable field: {pair}")
                continue
            if value != expected[key]:
                log.warning(f"Field display name changed: {pair}")
                continue
        for key, value in expected.items():
            pair = f"{json.dumps(key)}: {json.dumps(value)},"
            if key not in response_names:
                log.warning(f"Removed field: {pair}")
                continue


# Maps the navigable Jira field names to their display names
# noinspection SpellCheckingInspection
_navigable_jira_issue_field_names = {
    "customfield_19200": "Team",
    "customfield_19201": "Parent Link",
    "customfield_19202": "Target start",
    "customfield_19203": "Target end",
    "customfield_17300": "External Sender Email Addresses",
    "customfield_10470": "Flagged",
    "customfield_18911": "Why",
    "customfield_10471": "Epic/Theme",
    "customfield_18912": "Who",
    "fixVersions": "Fix Version/s",
    "customfield_10110": "% Complete",
    "customfield_10473": "Story Points",
    "customfield_17301": "External Sender Names",
    "customfield_10111": "Start Date",
    "customfield_13500": "Last Public Comment",
    "customfield_19204": "Original story points",
    "resolution": "Resolution",
    "customfield_10112": "URL",
    "customfield_10113": "Watchers (custom)",
    "customfield_13501": "issueFunction",
    "customfield_18910": "What",
    "lastViewed": "Last Viewed",
    "customfield_16600": "Team Role",
    "customfield_18903": "Acceptance Criteria",
    "priority": "Priority",
    "labels": "Labels",
    "customfield_11303": "JIRA Capture URL",
    "customfield_11304": "JIRA Capture Screen Resolution",
    "customfield_11305": "JIRA Capture jQuery Version",
    "aggregatetimeoriginalestimate": "\u03a3 Original Estimate",
    "timeestimate": "Remaining Estimate",
    "versions": "Affects Version/s",
    "issuelinks": "Linked Issues",
    "assignee": "Assignee",
    "status": "Status",
    "components": "Component/s",
    "customfield_17003": "Organizations",
    "customfield_17002": "Customer Request Type",
    "customfield_17001": "Request participants",
    "customfield_17000": "Approvals",
    "customfield_10052": "Date of First Response",
    "customfield_17005": "Satisfaction date",
    "customfield_17004": "Satisfaction",
    "customfield_11300": "JIRA Capture User Agent",
    "customfield_11301": "JIRA Capture Browser",
    "customfield_17801": "LastCreatedComment",
    "customfield_11302": "JIRA Capture Operating System",
    "customfield_17800": "LastUpdatedComment",
    "customfield_10600": "Rank (Obsolete)",
    "aggregatetimeestimate": "\u03a3 Remaining Estimate",
    "customfield_20000": "gitBranch",
    "creator": "Creator",
    "customfield_20001": "gitCommitsReferenced",
    "subtasks": "Sub-Tasks",
    "customfield_10163": "Participants",
    "reporter": "Reporter",
    "aggregateprogress": "\u03a3 Progress",
    "customfield_13312": "jqltField",
    "customfield_16700": "Development",
    "customfield_17903": "Link Dependencies",
    "progress": "Progress",
    "issuetype": "Issue Type",
    "customfield_17101": "Approvers",
    "customfield_17100": "Time to resolution",
    "customfield_19000": "Linked major incidents",
    "timespent": "Time Spent",
    "project": "Project",
    "aggregatetimespent": "\u03a3 Time Spent",
    "customfield_11400": "Sprint",
    "resolutiondate": "Resolved",
    "workratio": "Work Ratio",
    "customfield_20500": "Groups",
    "customfield_17211": "Change managers",
    "customfield_17210": "Source",
    "created": "Created",
    "customfield_10260": "Send reminder on",
    "customfield_10140": "Watchers (group)",
    "customfield_17216": "Time to first response",
    "customfield_17215": "Workaround",
    "customfield_12200": "JIRA Capture Document Mode",
    "customfield_17213": "Investigation reason",
    "customfield_12202": "Epic Link",
    "customfield_12201": "Testing Status",
    "customfield_17218": "Time to approve normal change",
    "customfield_17217": "Time to close after resolution",
    "customfield_11504": "PercentDone",
    "customfield_11506": "Planned Start",
    "customfield_11507": "Planned End",
    "updated": "Updated",
    "customfield_17201": "Impact",
    "customfield_17200": "Pending reason",
    "timeoriginalestimate": "Original Estimate",
    "customfield_17205": "Change start date",
    "description": "Description",
    "customfield_17204": "Change reason",
    "customfield_10010": "Category",
    "customfield_17203": "Change risk",
    "customfield_17202": "Change type",
    "customfield_17209": "Operational categorization",
    "customfield_17208": "Product categorization",
    "customfield_17207": "Urgency",
    "customfield_12305": "Memory",
    "customfield_12304": "Model",
    "customfield_14603": "Iteration",
    "customfield_12306": "Disk",
    "summary": "Summary",
    "customfield_10240": "Last Updated By",
    "customfield_10120": "Time In Status",
    "customfield_14601": "Account",
    "customfield_18401": "Issues in Epic",
    "customfield_14602": "Team",
    "customfield_10003": "OS",
    "customfield_14600": "Rank",
    "environment": "Environment",
    "duedate": "Due Date",
    "votes": "Votes",
    "watches": "Watchers",
    "customfield_10226": "Test Type",
    "customfield_15900": "MGG Business Line",
    "customfield_12205": "Epic Colour",
    "customfield_12204": "Epic Name",
    "customfield_12203": "Epic Status",
    "customfield_18801": "MFG",
    "customfield_16500": "Days Since Creation",
    "customfield_18800": "RAM Size",
}

# Maps the non-navigable Jira field names to their display names
# noinspection SpellCheckingInspection
_non_navigable_jira_issue_field_names = {
    "archiveddate": "Archived",
    "worklog": "Log Work",
    "archivedby": "Archiver",
    "timetracking": "Time Tracking",
    "attachment": "Attachment",
    "comment": "Comment",
}
