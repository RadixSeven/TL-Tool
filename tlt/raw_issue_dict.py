"""TypedDicts for raw Jira issue data."""

from typing import Any, TypedDict

AvatarUrlsDict = TypedDict(
    "AvatarUrlsDict",
    {
        "16x16": str,
        "24x24": str,
        "32x32": str,
        "48x48": str,
    },
)


class AuthorDict(TypedDict):  # noqa: D101
    self: str
    name: str
    key: str
    emailAddress: str
    displayName: str
    avatarUrls: AvatarUrlsDict
    active: bool
    timeZone: str


class WorkLogDict(TypedDict):  # noqa: D101
    self: str
    author: AuthorDict
    updateAuthor: AuthorDict
    comment: str
    created: str
    updated: str
    started: str
    timeSpent: str
    timeSpentSeconds: int
    id: str
    issueId: str


class WorkLogsDict(TypedDict):  # noqa: D101
    startAt: int
    maxResults: int
    total: int
    worklogs: list[WorkLogDict]


class ComponentDict(TypedDict):  # noqa: D101
    self: str
    id: str
    name: str
    description: str


class IssueTypeDict(TypedDict):  # noqa: D101
    self: str
    id: str
    description: str
    iconUrl: str
    name: str
    subtask: bool


class RawJiraIssueFields(TypedDict):  # noqa: D101
    summary: str
    worklog: WorkLogsDict
    components: list[ComponentDict]
    # noinspection SpellCheckingInspection
    issuetype: dict[str, Any]
    updated: str  # This may be a hallucination


class RawJiraIssueDict(TypedDict):  # noqa: D101
    expand: str
    id: str
    self: str
    key: str
    fields: RawJiraIssueFields
