"""Main module for the TL Tool."""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from multiprocessing import Process
from pathlib import Path

from jira_cache_updater import (
    ConnectionSupplier,
    JiraCacheUpdater,
    create_file_db_connection_supplier,
)


def token_path_error_msg(token_path: Path) -> str | None:
    """
    Return the error message to print if the token file has errors.

    Args:
        token_path: The path to the token file.

    Returns:
        The error message if the token file has incorrect permissions or does
        not exist or None if the token file is OK.
    """
    try:
        stat = os.stat(token_path)
        if sys.platform == "win32":
            return None
        mode = stat.st_mode & 0o777
        if mode in [0o600, 0o400]:
            return None
        return (
            f"Error: The token file {token_path} should have 0600 "
            "or 0400 permissions, that is only the owner can read"
            "or write it."
        )
    except FileNotFoundError:
        return f"Error: The token file {token_path} does not exist."
    except PermissionError:
        return (
            f"Error: The token file {token_path} is not visible "
            "(it may be in a directory this user cannot read)."
        )


def read_token(token_path: Path) -> str:
    """
    Read the Jira token from the specified file.

    Args:
        token_path: The path to the token file.

    Returns:
        The Jira token read from the file.
    """
    with token_path.open("r") as f:
        return f.read().strip()


def get_sorted_update_times(conn_supplier: ConnectionSupplier) -> list[str]:
    """
    Get a sorted list of issue update times from the cache database.

    Args:
        conn_supplier: Supplier for database connections.

    Returns:
        List[str]: A sorted list of issue update times.
    """
    with conn_supplier() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_updated FROM issues ORDER BY last_updated")
        return [row[0] for row in cursor.fetchall()]


@dataclass
class Args:
    """Parsed command line arguments. See _parse_args()."""

    url: str
    token_path: Path
    projects: list[str]
    cache_db: Path
    seconds_between_checks: int
    rate_limit: int
    operation: str


def _parse_args() -> Args:
    parser = argparse.ArgumentParser(description="Jira Cache Updater CLI")
    parser.add_argument(
        "--url",
        default="https://jira.ncbi.nlm.nih.gov/",
        help="Jira server URL",
    )
    parser.add_argument(
        "--token-path",
        default="~/.config/tl-tool/jira-auth-token",
        help="Path to Jira auth token",
    )
    parser.add_argument(
        "--projects",
        nargs="+",
        required=True,
        type=jira_project_argument,
        help="List of Jira projects",
    )
    parser.add_argument(
        "--cache-db",
        default="~/.cache/tl-tool/jira-cache.sqlite",
        help="Path to cache database",
    )
    parser.add_argument(
        "--seconds-between-checks",
        type=int,
        default=5,
        help="Seconds between checks",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=1,
        help="Rate limit for requests per second",
    )
    parser.add_argument(
        "operation", choices=["update-cache"], help="Operation to perform"
    )
    args = parser.parse_args()
    return Args(
        url=args.url,
        token_path=Path(args.token_path).expanduser(),
        projects=args.projects,
        cache_db=Path(args.cache_db).expanduser(),
        seconds_between_checks=args.seconds_between_checks,
        rate_limit=args.rate_limit,
        operation=args.operation,
    )


JIRA_PROJECT_REGEX = r"^[A-Z][A-Z0-9_]*$"
JIRA_PROJECT_PATTERN = re.compile(JIRA_PROJECT_REGEX)


def jira_project_argument(name: str) -> str:
    """
    Throw on an invalid Jira project argument.

    Args:
        name: The requested project name.

    Returns:
        The project name if it is valid.

    Raises:
        argparse.ArgumentTypeError: If the project is invalid
    """
    if not JIRA_PROJECT_PATTERN.match(name):
        raise argparse.ArgumentTypeError(
            f"Invalid Jira project name: {name}. Must match pattern: {JIRA_PROJECT_REGEX}"
        )
    return name


def main() -> int:
    """Run the TL Tool."""
    args = _parse_args()

    # Check token file
    err = token_path_error_msg(args.token_path)
    if err:
        print(err, file=sys.stderr)  # noqa: T201
        sys.exit(1)

    # Read the token
    jira_token = read_token(args.token_path)

    # Create JQL query from projects
    jql = " OR ".join(f"project = {project}" for project in args.projects)

    # Create connection supplier
    connection_supplier = create_file_db_connection_supplier(args.cache_db)

    # Create JiraCacheUpdater
    updater = JiraCacheUpdater(
        args.url,
        jira_token,
        jql,
        connection_supplier,
        args.seconds_between_checks,
        args.rate_limit,
    )

    # Start the updater in a separate process
    p = Process(target=updater.start)
    p.start()

    if args.operation == "update-cache":
        wait_for_cache_update(connection_supplier, args.seconds_between_checks)
    p.terminate()  # Stop the updater process
    return 0


def wait_for_cache_update(
    connection_supplier: ConnectionSupplier, seconds_between_checks: float
) -> None:
    """
    Wait for the cache to update.

    Args:
        connection_supplier: Supplier for database connections.
        seconds_between_checks: Seconds between checks.
    """
    print("Waiting for cache to update ...")  # noqa: T201
    prev_update_times: list[str] = []
    while True:
        # The delay between checking the db also includes the time to read
        # the db, not just the time to sleep
        time.sleep(seconds_between_checks * 1.5)
        current_update_times = get_sorted_update_times(connection_supplier)
        print(f"{len(current_update_times)} issues ...")  # noqa: T201

        if current_update_times == prev_update_times and current_update_times:
            break
        prev_update_times = current_update_times
    print("Finished.")  # noqa: T201


if __name__ == "__main__":
    sys.exit(main())
