"""OpenAI-style tool (function) schemas exposed to the model.

These map 1:1 to the implementations in `tools.py`. Tools whose name appears in
`MUTATING_TOOLS` go through the approval gate before they execute.
"""
from __future__ import annotations

MUTATING_TOOLS = {
    "write_file",
    "edit_file",
    "multi_edit",
    "create_file",
    "delete_file",
    "run_command",
    "bash_background",
    "git_commit",
}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS = [
    _fn(
        "list_files",
        "List files in the workspace (recursive, ignores configured paths). "
        "Use to understand project structure.",
        {
            "path": {
                "type": "string",
                "description": "Directory relative to workspace root. Default '.'.",
            }
        },
        [],
    ),
    _fn(
        "read_file",
        "Read a UTF-8 text file from the workspace. Optionally a line range.",
        {
            "path": {"type": "string", "description": "File path relative to root."},
            "start_line": {"type": "integer", "description": "1-based start (optional)."},
            "end_line": {"type": "integer", "description": "Inclusive end (optional)."},
        },
        ["path"],
    ),
    _fn(
        "search_text",
        "Search file contents across the workspace (ripgrep if available, else "
        "a pure-Python fallback). Returns matching path:line: text rows.",
        {
            "query": {"type": "string", "description": "Substring or regex to find."},
            "glob": {
                "type": "string",
                "description": "Optional file glob filter, e.g. '*.py'.",
            },
        },
        ["query"],
    ),
    _fn(
        "write_file",
        "Create a new file or fully overwrite an existing one with `content`. "
        "Requires user approval. Prefer edit_file for changing existing files.",
        {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "Full file contents."},
        },
        ["path", "content"],
    ),
    _fn(
        "edit_file",
        "Make a surgical edit by replacing an exact substring. `old_string` must "
        "match the current file EXACTLY (incl. whitespace) and uniquely. Requires "
        "user approval.",
        {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence (default false).",
            },
        },
        ["path", "old_string", "new_string"],
    ),
    _fn(
        "create_file",
        "Create a new file with `content`. Fails if it already exists. Requires "
        "user approval.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _fn(
        "delete_file",
        "Delete a file from the workspace. Requires user approval.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _fn(
        "run_command",
        "Run a shell command in the workspace root (e.g. run tests, a script, a "
        "build). Requires user approval. Has a timeout; not for long-running "
        "servers — use bash_background for those.",
        {"command": {"type": "string", "description": "The shell command to run."}},
        ["command"],
    ),
    _fn(
        "glob",
        "Find files by glob pattern (supports ** for recursion), e.g. 'src/**/*.ts'.",
        {
            "pattern": {"type": "string", "description": "Glob pattern."},
            "path": {"type": "string", "description": "Base dir relative to root (default '.')."},
        },
        ["pattern"],
    ),
    _fn(
        "multi_edit",
        "Apply several exact search/replace edits to ONE file atomically (all or "
        "nothing). Each edit needs old_string (exact, unique unless replace_all) "
        "and new_string. Requires approval.",
        {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "description": "List of edits applied in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        ["path", "edits"],
    ),
    _fn(
        "web_search",
        "Search the web for up-to-date information. Returns titles, URLs, snippets.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    _fn(
        "web_fetch",
        "Fetch a URL and return its readable text content.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _fn(
        "bash_background",
        "Start a long-running command (dev server, watcher) in the background and "
        "return immediately with a process id. Requires approval. Poll with "
        "process_output, stop with process_kill.",
        {"command": {"type": "string"}},
        ["command"],
    ),
    _fn(
        "process_output",
        "Read recent output from a background process started with bash_background.",
        {"id": {"type": "string"}, "tail": {"type": "integer", "description": "Lines (default 100)."}},
        ["id"],
    ),
    _fn(
        "process_kill",
        "Stop a background process by id.",
        {"id": {"type": "string"}},
        ["id"],
    ),
    _fn("process_list", "List background processes and their status.", {}, []),
    _fn("git_status", "Show 'git status --short --branch' for the workspace.", {}, []),
    _fn(
        "git_diff",
        "Show 'git diff', optionally for one path.",
        {"path": {"type": "string", "description": "Optional path to diff."}},
        [],
    ),
    _fn(
        "git_commit",
        "Stage all changes and create a git commit. Requires approval.",
        {
            "message": {"type": "string", "description": "Commit message."},
            "all": {"type": "boolean", "description": "git add -A first (default true)."},
        },
        ["message"],
    ),
    _fn(
        "todo_write",
        "Create or update the visible task checklist for a multi-step job. Pass the "
        "FULL list each time. Mark exactly one item in_progress while working it.",
        {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        ["todos"],
    ),
    _fn(
        "spawn_agent",
        "Delegate a focused sub-task to a fresh sub-agent with its own context. Use "
        "for independent investigations or parallelizable work. Returns the "
        "sub-agent's final summary. The sub-agent can read/search/edit/run like you.",
        {
            "description": {"type": "string", "description": "Short label for the sub-task."},
            "prompt": {"type": "string", "description": "Full instructions for the sub-agent."},
        },
        ["description", "prompt"],
    ),
]

# Tools handled directly by the loop (not in tools.TOOL_FUNCS).
LOOP_TOOLS = {"todo_write", "spawn_agent", "update_plan"}

# In plan mode only these (read-only + planning) are offered.
PLAN_MODE_TOOLS = {
    "list_files",
    "read_file",
    "search_text",
    "glob",
    "web_search",
    "web_fetch",
    "process_list",
    "git_status",
    "git_diff",
    "update_plan",
    "todo_write",
}


def schemas_for(plan_mode: bool = False, extra: list | None = None) -> list:
    """Return the tool schemas to advertise this turn."""
    base = TOOL_SCHEMAS
    if plan_mode:
        base = [t for t in base if t["function"]["name"] in PLAN_MODE_TOOLS]
        base = base + [_PLAN_SCHEMA]
    return base + (extra or [])


_PLAN_SCHEMA = _fn(
    "update_plan",
    "Present your implementation plan for the user to approve before you make any "
    "changes. Call this when you've finished researching in plan mode.",
    {"plan": {"type": "string", "description": "The plan as markdown."}},
    ["plan"],
)
