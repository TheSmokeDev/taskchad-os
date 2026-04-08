"""
Google Drive Direct Integration for The Homie.

Read-only access to Google Drive for finding files by name/type.
Shares OAuth token with Gmail, Calendar, Sheets, and Docs.

Usage:
    uv run python -m integrations.drive_api find "Content Calendar"
    uv run python -m integrations.drive_api find "Content Calendar" --type spreadsheet
    uv run python -m integrations.drive_api list --type document --max 10
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import with_retry  # noqa: E402

# Maps friendly type names to Drive MIME types
MIME_TYPES: dict[str, str] = {
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "document": "application/vnd.google-apps.document",
    "folder": "application/vnd.google-apps.folder",
    "presentation": "application/vnd.google-apps.presentation",
    "pdf": "application/pdf",
}


@dataclass
class DriveFile:
    """Represents a Google Drive file."""

    id: str
    name: str
    mime_type: str
    url: str
    modified_time: datetime | None = None
    size: int | None = None
    parent_id: str | None = None

    @property
    def friendly_type(self) -> str:
        """Return human-readable file type."""
        for name, mime in MIME_TYPES.items():
            if self.mime_type == mime:
                return name
        return self.mime_type.split("/")[-1]


def get_drive_service() -> Any:
    """Build authenticated Drive API service."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from integrations.auth import get_google_credentials

    creds = get_google_credentials()
    service: Any = build("drive", "v3", credentials=creds)
    return service


def _parse_file(item: dict[str, Any]) -> DriveFile:
    """Parse a Drive API file resource into a DriveFile."""
    modified = None
    if "modifiedTime" in item:
        modified = datetime.fromisoformat(item["modifiedTime"].replace("Z", "+00:00"))

    size = None
    if "size" in item:
        size = int(item["size"])

    parents = item.get("parents", [])

    return DriveFile(
        id=item["id"],
        name=item.get("name", "(untitled)"),
        mime_type=item.get("mimeType", ""),
        url=item.get("webViewLink", f"https://drive.google.com/file/d/{item['id']}"),
        modified_time=modified,
        size=size,
        parent_id=parents[0] if parents else None,
    )


DRIVE_FIELDS = "files(id,name,mimeType,webViewLink,modifiedTime,size,parents)"


def find_files(
    query: str,
    file_type: str | None = None,
    max_results: int = 10,
) -> list[DriveFile]:
    """
    Search for files by name.

    Args:
        query: Search term (matched against file name)
        file_type: Filter by type (spreadsheet, document, folder, presentation, pdf)
        max_results: Maximum files to return
    """
    service = get_drive_service()

    q_parts: list[str] = [f"name contains '{query}'", "trashed = false"]

    if file_type and file_type in MIME_TYPES:
        q_parts.append(f"mimeType = '{MIME_TYPES[file_type]}'")

    drive_query = " and ".join(q_parts)

    result: dict[str, Any] = with_retry(
        lambda: service.files()
        .list(
            q=drive_query,
            pageSize=max_results,
            fields=DRIVE_FIELDS,
            orderBy="modifiedTime desc",
        )
        .execute()
    )

    return [_parse_file(item) for item in result.get("files", [])]


def list_files(
    file_type: str | None = None,
    max_results: int = 10,
    folder_id: str | None = None,
) -> list[DriveFile]:
    """
    List recent files, optionally filtered by type or folder.

    Args:
        file_type: Filter by type (spreadsheet, document, folder, presentation, pdf)
        max_results: Maximum files to return
        folder_id: Only list files in this folder
    """
    service = get_drive_service()

    q_parts: list[str] = ["trashed = false"]

    if file_type and file_type in MIME_TYPES:
        q_parts.append(f"mimeType = '{MIME_TYPES[file_type]}'")

    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")

    drive_query = " and ".join(q_parts)

    result: dict[str, Any] = with_retry(
        lambda: service.files()
        .list(
            q=drive_query,
            pageSize=max_results,
            fields=DRIVE_FIELDS,
            orderBy="modifiedTime desc",
        )
        .execute()
    )

    return [_parse_file(item) for item in result.get("files", [])]


def get_file_by_id(file_id: str) -> DriveFile | None:
    """Get a single file's metadata by ID."""
    service = get_drive_service()

    try:
        item: dict[str, Any] = with_retry(
            lambda: service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,webViewLink,modifiedTime,size,parents",
            )
            .execute()
        )
        return _parse_file(item)
    except Exception as e:
        print(f"Error getting file {file_id}: {e}")
        return None


def format_files_for_context(files: list[DriveFile]) -> str:
    """Format files for inclusion in Claude's context prompt."""
    if not files:
        return "No files found."

    output: list[str] = []
    for f in files:
        modified = f.modified_time.strftime("%Y-%m-%d %H:%M") if f.modified_time else "unknown"
        entry = (
            f"- **{f.name}** ({f.friendly_type})\n"
            f"  ID: `{f.id}`\n"
            f"  Modified: {modified}\n"
            f"  URL: {f.url}"
        )
        output.append(entry)

    return "\n\n".join(output)


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Google Drive integration")
    parser.add_argument("command", choices=["find", "list", "get"])
    parser.add_argument("query", nargs="?", default=None)
    parser.add_argument("--type", dest="file_type", default=None,
                        choices=list(MIME_TYPES.keys()))
    parser.add_argument("--max", type=int, default=10)

    args = parser.parse_args()

    if args.command == "find":
        if not args.query:
            print("Error: search query required for find command")
            sys.exit(1)
        results = find_files(args.query, file_type=args.file_type, max_results=args.max)
        print(format_files_for_context(results))

    elif args.command == "list":
        results = list_files(file_type=args.file_type, max_results=args.max)
        print(format_files_for_context(results))

    elif args.command == "get":
        if not args.query:
            print("Error: file ID required for get command")
            sys.exit(1)
        file = get_file_by_id(args.query)
        if file:
            print(format_files_for_context([file]))
        else:
            print("File not found")
