"""
Google Sheets Direct Integration for The Homie.

Read/write access to Google Sheets. Shares OAuth token with Gmail, Calendar, Drive, and Docs.

Usage:
    uv run python -m integrations.sheets_api read <spreadsheet_id>
    uv run python -m integrations.sheets_api read <spreadsheet_id> --range "Sheet1!A1:Z100"
    uv run python -m integrations.sheets_api info <spreadsheet_id>
    uv run python -m integrations.sheets_api write <spreadsheet_id> --range "A1" --values '[["a","b"],["c","d"]]'
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import with_retry  # noqa: E402


@dataclass
class SheetInfo:
    """Metadata about a single sheet (tab) within a spreadsheet."""

    title: str
    sheet_id: int
    row_count: int
    col_count: int


@dataclass
class SpreadsheetData:
    """Represents data read from a spreadsheet."""

    id: str
    title: str
    url: str
    sheets: list[SheetInfo] = field(default_factory=list)
    values: list[list[str]] = field(default_factory=list)
    range: str = ""


def get_sheets_service() -> Any:
    """Build authenticated Sheets API service."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from integrations.auth import get_google_credentials

    creds = get_google_credentials()
    service: Any = build("sheets", "v4", credentials=creds)
    return service


def get_spreadsheet_info(spreadsheet_id: str) -> SpreadsheetData:
    """
    Get spreadsheet metadata (title, sheet names, dimensions).

    Args:
        spreadsheet_id: The spreadsheet ID from the URL
    """
    service = get_sheets_service()

    result: dict[str, Any] = with_retry(
        lambda: service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id)
        .execute()
    )

    sheets: list[SheetInfo] = []
    for sheet in result.get("sheets", []):
        props = sheet.get("properties", {})
        grid = props.get("gridProperties", {})
        sheets.append(
            SheetInfo(
                title=props.get("title", ""),
                sheet_id=props.get("sheetId", 0),
                row_count=grid.get("rowCount", 0),
                col_count=grid.get("columnCount", 0),
            )
        )

    return SpreadsheetData(
        id=spreadsheet_id,
        title=result.get("properties", {}).get("title", "(untitled)"),
        url=result.get("spreadsheetUrl", ""),
        sheets=sheets,
    )


def read_spreadsheet(
    spreadsheet_id: str,
    range_notation: str = "",
    max_rows: int = 500,
) -> SpreadsheetData:
    """
    Read data from a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID
        range_notation: A1 notation range (e.g., "Sheet1!A1:Z100"). If empty, reads the first sheet.
        max_rows: Safety limit on rows returned
    """
    service = get_sheets_service()

    # Get metadata first
    info = get_spreadsheet_info(spreadsheet_id)

    # Default to first sheet if no range specified
    if not range_notation and info.sheets:
        range_notation = info.sheets[0].title

    result: dict[str, Any] = with_retry(
        lambda: service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_notation)
        .execute()
    )

    values: list[list[str]] = result.get("values", [])

    # Apply row limit
    if len(values) > max_rows:
        values = values[:max_rows]

    info.values = values
    info.range = result.get("range", range_notation)

    return info


def write_spreadsheet(
    spreadsheet_id: str,
    range_notation: str,
    values: list[list[str]],
    input_option: str = "USER_ENTERED",
) -> dict[str, Any]:
    """
    Write data to a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID
        range_notation: A1 notation for where to write (e.g., "Sheet1!A1")
        values: 2D list of values to write
        input_option: How to interpret input (USER_ENTERED or RAW)
    """
    service = get_sheets_service()

    body = {"values": values}

    result: dict[str, Any] = with_retry(
        lambda: service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_notation,
            valueInputOption=input_option,
            body=body,
        )
        .execute()
    )

    return result


def append_to_spreadsheet(
    spreadsheet_id: str,
    range_notation: str,
    values: list[list[str]],
    input_option: str = "USER_ENTERED",
) -> dict[str, Any]:
    """
    Append rows to a spreadsheet (adds after existing data).

    Args:
        spreadsheet_id: The spreadsheet ID
        range_notation: A1 notation for the target range (e.g., "Sheet1!A:Z")
        values: 2D list of row values to append
        input_option: How to interpret input (USER_ENTERED or RAW)
    """
    service = get_sheets_service()

    body = {"values": values}

    result: dict[str, Any] = with_retry(
        lambda: service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range_notation,
            valueInputOption=input_option,
            body=body,
        )
        .execute()
    )

    return result


def format_spreadsheet_for_context(data: SpreadsheetData, max_chars: int = 4000) -> str:
    """Format spreadsheet data for inclusion in Claude's context prompt."""
    output: list[str] = []

    # Header
    output.append(f"**{data.title}**")
    output.append(f"ID: `{data.id}`")
    if data.url:
        output.append(f"URL: {data.url}")

    # Sheet tabs
    if data.sheets:
        tabs = ", ".join(
            f"{s.title} ({s.row_count}x{s.col_count})" for s in data.sheets
        )
        output.append(f"Sheets: {tabs}")

    # Values as markdown table
    if data.values:
        output.append(f"\nRange: {data.range}")
        output.append(f"Rows: {len(data.values)}")

        chars = sum(len(line) for line in output)

        # Use first row as header
        header = data.values[0]
        separator = ["---"] * len(header)
        output.append("")
        output.append("| " + " | ".join(str(h) for h in header) + " |")
        output.append("| " + " | ".join(separator) + " |")
        chars += len(output[-1]) * 2

        for row in data.values[1:]:
            # Pad row to match header length
            padded = row + [""] * (len(header) - len(row))
            line = "| " + " | ".join(str(c) for c in padded[:len(header)]) + " |"
            if chars + len(line) > max_chars:
                remaining = len(data.values) - len(output) + 3  # account for header lines
                output.append(f"\n... {remaining} more rows truncated")
                break
            output.append(line)
            chars += len(line)

    return "\n".join(output)


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Google Sheets integration")
    parser.add_argument("command", choices=["read", "info", "write", "append"])
    parser.add_argument("spreadsheet_id")
    parser.add_argument("--range", dest="range_notation", default="")
    parser.add_argument("--values", default=None, help="JSON 2D array for write/append")
    parser.add_argument("--max-rows", type=int, default=500)

    args = parser.parse_args()

    if args.command == "info":
        sheet_info = get_spreadsheet_info(args.spreadsheet_id)
        print(format_spreadsheet_for_context(sheet_info))

    elif args.command == "read":
        sheet_data = read_spreadsheet(
            args.spreadsheet_id,
            range_notation=args.range_notation,
            max_rows=args.max_rows,
        )
        print(format_spreadsheet_for_context(sheet_data))

    elif args.command in ("write", "append"):
        if not args.values:
            print("Error: --values required for write/append command")
            sys.exit(1)
        if not args.range_notation:
            print("Error: --range required for write/append command")
            sys.exit(1)
        parsed_values = json.loads(args.values)
        if args.command == "write":
            write_result = write_spreadsheet(
                args.spreadsheet_id, args.range_notation, parsed_values,
            )
        else:
            write_result = append_to_spreadsheet(
                args.spreadsheet_id, args.range_notation, parsed_values,
            )
        print(json.dumps(write_result, indent=2))
