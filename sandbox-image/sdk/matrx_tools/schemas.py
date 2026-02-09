"""JSON schema definitions for all Claude Code-compatible tools.

These schemas are passed directly to the Claude API as the `tools` parameter
in messages.create(). Names and parameter shapes match the official Claude Code
tool definitions for maximum model compatibility.
"""

from __future__ import annotations

TOOL_DEFINITIONS: list[dict] = [
    # ── File Operations ────────────────────────────────────────────────────
    {
        "name": "Read",
        "description": (
            "Reads a file from the local filesystem. You can access any file "
            "directly by using this tool.\n\n"
            "Usage:\n"
            "- The file_path parameter must be an absolute path, not a relative path\n"
            "- By default, it reads up to 2000 lines starting from the beginning of the file\n"
            "- You can optionally specify a line offset and limit (especially handy for long files)\n"
            "- Any lines longer than 2000 characters will be truncated\n"
            "- Results are returned using cat -n format, with line numbers starting at 1\n"
            "- This tool allows reading images (eg PNG, JPG). When reading an image file "
            "the contents are presented visually.\n"
            "- This tool can read PDF files (.pdf). For large PDFs (more than 10 pages), "
            "you MUST provide the pages parameter to read specific page ranges.\n"
            "- This tool can read Jupyter notebooks (.ipynb files) and returns all cells "
            "with their outputs.\n"
            "- This tool can only read files, not directories."
        ),
        "input_schema": {
            "type": "object",
            "required": ["file_path"],
            "additionalProperties": False,
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to read",
                },
                "offset": {
                    "type": "number",
                    "description": (
                        "The line number to start reading from. "
                        "Only provide if the file is too large to read at once"
                    ),
                },
                "limit": {
                    "type": "number",
                    "description": (
                        "The number of lines to read. "
                        "Only provide if the file is too large to read at once."
                    ),
                },
                "pages": {
                    "type": "string",
                    "description": (
                        "Page range for PDF files (e.g., '1-5', '3', '10-20'). "
                        "Only applicable to PDF files. Maximum 20 pages per request."
                    ),
                },
            },
        },
    },
    {
        "name": "Write",
        "description": (
            "Writes a file to the local filesystem.\n\n"
            "Usage:\n"
            "- This tool will overwrite the existing file if there is one at the provided path.\n"
            "- If this is an existing file, you MUST use the Read tool first to read the file's "
            "contents. This tool will fail if you did not read the file first.\n"
            "- ALWAYS prefer editing existing files in the codebase. NEVER write new files "
            "unless explicitly required."
        ),
        "input_schema": {
            "type": "object",
            "required": ["file_path", "content"],
            "additionalProperties": False,
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to write (must be absolute, not relative)",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
        },
    },
    {
        "name": "Edit",
        "description": (
            "Performs exact string replacements in files.\n\n"
            "Usage:\n"
            "- You must use your Read tool at least once in the conversation before editing.\n"
            "- The edit will FAIL if old_string is not unique in the file. Either provide a "
            "larger string with more surrounding context to make it unique or use replace_all "
            "to change every instance of old_string.\n"
            "- Use replace_all for replacing and renaming strings across the file."
        ),
        "input_schema": {
            "type": "object",
            "required": ["file_path", "old_string", "new_string"],
            "additionalProperties": False,
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to modify",
                },
                "old_string": {
                    "type": "string",
                    "description": "The text to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must be different from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace all occurrences of old_string (default false)",
                },
            },
        },
    },
    {
        "name": "MultiEdit",
        "description": (
            "Performs multiple exact string replacements in a single file atomically.\n\n"
            "Usage:\n"
            "- All edits are applied sequentially within the file.\n"
            "- If any edit fails validation, none of the edits are applied.\n"
            "- You must use the Read tool at least once before editing."
        ),
        "input_schema": {
            "type": "object",
            "required": ["file_path", "edits"],
            "additionalProperties": False,
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to modify",
                },
                "edits": {
                    "type": "array",
                    "description": "List of edit operations to apply sequentially",
                    "items": {
                        "type": "object",
                        "required": ["old_string", "new_string"],
                        "additionalProperties": False,
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": "The text to replace",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "The text to replace it with",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "default": False,
                                "description": "Replace all occurrences (default false)",
                            },
                        },
                    },
                },
            },
        },
    },
    {
        "name": "Glob",
        "description": (
            "Fast file pattern matching tool that works with any codebase size.\n"
            "Supports glob patterns like '**/*.js' or 'src/**/*.ts'.\n"
            "Returns matching file paths sorted by modification time."
        ),
        "input_schema": {
            "type": "object",
            "required": ["pattern"],
            "additionalProperties": False,
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "The directory to search in. If not specified, the current "
                        "working directory will be used. IMPORTANT: Omit this field "
                        "to use the default directory."
                    ),
                },
            },
        },
    },
    {
        "name": "Grep",
        "description": (
            "A powerful search tool built on ripgrep.\n\n"
            "Usage:\n"
            "- ALWAYS use Grep for search tasks. NEVER invoke grep or rg as a Bash command.\n"
            "- Supports full regex syntax (e.g., 'log.*Error', 'function\\s+\\w+')\n"
            "- Filter files with glob parameter (e.g., '*.js') or type parameter (e.g., 'js', 'py')\n"
            "- Output modes: 'content' shows matching lines, 'files_with_matches' shows only "
            "file paths (default), 'count' shows match counts\n"
            "- Pattern syntax: Uses ripgrep (not grep) — literal braces need escaping\n"
            "- Multiline matching: By default patterns match within single lines only. "
            "For cross-line patterns, use multiline: true"
        ),
        "input_schema": {
            "type": "object",
            "required": ["pattern"],
            "additionalProperties": False,
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regular expression pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in. Defaults to current working directory.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "Output mode: 'content' shows matching lines, "
                        "'files_with_matches' shows file paths (default), "
                        "'count' shows match counts."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.js', '*.{ts,tsx}')",
                },
                "type": {
                    "type": "string",
                    "description": "File type to search (e.g., js, py, rust, go, java)",
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case insensitive search",
                },
                "-n": {
                    "type": "boolean",
                    "description": "Show line numbers in output. Requires output_mode: 'content'.",
                },
                "-A": {
                    "type": "number",
                    "description": "Number of lines to show after each match. Requires output_mode: 'content'.",
                },
                "-B": {
                    "type": "number",
                    "description": "Number of lines to show before each match. Requires output_mode: 'content'.",
                },
                "-C": {
                    "type": "number",
                    "description": "Number of lines of context (before and after). Alias for context.",
                },
                "context": {
                    "type": "number",
                    "description": "Number of lines to show before and after each match. Alias for -C.",
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Enable multiline mode where . matches newlines. Default: false.",
                },
                "head_limit": {
                    "type": "number",
                    "description": "Limit output to first N lines/entries. Defaults to 0 (unlimited).",
                },
                "offset": {
                    "type": "number",
                    "description": "Skip first N lines/entries before applying head_limit. Defaults to 0.",
                },
            },
        },
    },
    {
        "name": "NotebookEdit",
        "description": (
            "Completely replaces the contents of a specific cell in a Jupyter notebook "
            "(.ipynb file) with new source. The notebook_path parameter must be an absolute "
            "path. Use edit_mode=insert to add a new cell. Use edit_mode=delete to delete a cell."
        ),
        "input_schema": {
            "type": "object",
            "required": ["notebook_path", "new_source"],
            "additionalProperties": False,
            "properties": {
                "notebook_path": {
                    "type": "string",
                    "description": "The absolute path to the Jupyter notebook file to edit",
                },
                "new_source": {
                    "type": "string",
                    "description": "The new source for the cell",
                },
                "cell_id": {
                    "type": "string",
                    "description": (
                        "The ID of the cell to edit. When inserting a new cell, "
                        "the new cell will be inserted after the cell with this ID."
                    ),
                },
                "cell_type": {
                    "type": "string",
                    "enum": ["code", "markdown"],
                    "description": "The type of the cell. Required for edit_mode=insert.",
                },
                "edit_mode": {
                    "type": "string",
                    "enum": ["replace", "insert", "delete"],
                    "description": "The type of edit to make. Defaults to replace.",
                },
            },
        },
    },
    # ── Execution Tools ────────────────────────────────────────────────────
    {
        "name": "Bash",
        "description": (
            "Executes a given bash command with optional timeout. "
            "Working directory persists between commands; shell state does not.\n\n"
            "Usage:\n"
            "- The command argument is required.\n"
            "- You can specify an optional timeout in milliseconds (up to 600000ms / 10 minutes). "
            "If not specified, commands will timeout after 120000ms (2 minutes).\n"
            "- If the output exceeds 30000 characters, output will be truncated.\n"
            "- You can use run_in_background to run the command in the background.\n"
            "- Try to maintain your current working directory by using absolute paths.\n"
            "- When issuing multiple commands: if independent, make multiple Bash tool calls. "
            "If dependent, chain with &&."
        ),
        "input_schema": {
            "type": "object",
            "required": ["command"],
            "additionalProperties": False,
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute",
                },
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does (5-10 words)",
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional timeout in milliseconds (max 600000)",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Set to true to run this command in the background.",
                },
            },
        },
    },
    {
        "name": "BashOutput",
        "description": (
            "Retrieves output from a running or completed background bash shell.\n"
            "Always returns only new output since the last check.\n"
            "Supports optional regex filtering to show only lines matching a pattern."
        ),
        "input_schema": {
            "type": "object",
            "required": ["bash_id"],
            "additionalProperties": False,
            "properties": {
                "bash_id": {
                    "type": "string",
                    "description": "The ID of the background shell to retrieve output from",
                },
                "filter": {
                    "type": "string",
                    "description": (
                        "Optional regular expression to filter output lines. "
                        "Non-matching lines will no longer be available to read."
                    ),
                },
            },
        },
    },
    {
        "name": "TaskStop",
        "description": (
            "Stops a running background task by its ID.\n"
            "Returns a success or failure status."
        ),
        "input_schema": {
            "type": "object",
            "required": ["task_id"],
            "additionalProperties": False,
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ID of the background task to stop",
                },
            },
        },
    },
    # ── Web Tools (placeholders) ──────────────────────────────────────────
    {
        "name": "WebFetch",
        "description": (
            "Fetches content from a specified URL and processes it using an AI model.\n"
            "Takes a URL and a prompt as input. Fetches the URL content, converts HTML "
            "to markdown, and processes the content with the prompt."
        ),
        "input_schema": {
            "type": "object",
            "required": ["url", "prompt"],
            "additionalProperties": False,
            "properties": {
                "url": {
                    "type": "string",
                    "format": "uri",
                    "description": "The URL to fetch content from",
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to run on the fetched content",
                },
            },
        },
    },
    {
        "name": "WebSearch",
        "description": (
            "Search the web and use the results to inform responses.\n"
            "Provides up-to-date information for current events and recent data."
        ),
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "additionalProperties": False,
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 2,
                    "description": "The search query to use",
                },
                "allowed_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only include search results from these domains",
                },
                "blocked_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Never include search results from these domains",
                },
            },
        },
    },
    # ── Meta Tools ─────────────────────────────────────────────────────────
    {
        "name": "TodoWrite",
        "description": (
            "Use this tool to create and manage a structured task list for "
            "your current coding session. This helps you track progress, "
            "organize complex tasks, and demonstrate thoroughness to the user."
        ),
        "input_schema": {
            "type": "object",
            "required": ["todos"],
            "additionalProperties": False,
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The updated todo list",
                    "items": {
                        "type": "object",
                        "required": ["content", "status", "activeForm"],
                        "additionalProperties": False,
                        "properties": {
                            "content": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Imperative form: what needs to be done",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Task status",
                            },
                            "activeForm": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Present continuous form: what's being done",
                            },
                        },
                    },
                },
            },
        },
    },
]

TOOL_NAMES: set[str] = {t["name"] for t in TOOL_DEFINITIONS}
TOOL_SCHEMA_MAP: dict[str, dict] = {t["name"]: t for t in TOOL_DEFINITIONS}
