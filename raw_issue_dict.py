from typing import TypedDict, Any


AvatarUrlsDict = TypedDict(
    "AvatarUrlsDict",
    {
        "16x16": str,
        "24x24": str,
        "32x32": str,
        "48x48": str,
    },
)


class AuthorDict(TypedDict):
    self: str
    name: str
    key: str
    emailAddress: str
    displayName: str
    avatarUrls: AvatarUrlsDict
    active: bool
    timeZone: str


class WorkLogDict(TypedDict):
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


class WorkLogsDict(TypedDict):
    startAt: int
    maxResults: int
    total: int
    worklogs: list[WorkLogDict]


class ComponentDict(TypedDict):
    self: str
    id: str
    name: str
    description: str


class IssueTypeDict(TypedDict):
    self: str
    id: str
    description: str
    iconUrl: str
    name: str
    subtask: bool


class RawJiraIssueFields(TypedDict):
    summary: str
    worklog: WorkLogsDict
    components: list[ComponentDict]
    # noinspection SpellCheckingInspection
    issuetype: dict[str, Any]


class RawJiraIssueDict(TypedDict):
    expand: str
    id: str
    self: str
    key: str
    fields: RawJiraIssueFields
