"""Meta tools — TodoWrite for session task tracking."""

from __future__ import annotations

from matrx_tools.session import ToolSession
from matrx_tools.types import TodoItem, ToolResult, ToolResultType


async def tool_todo_write(
    session: ToolSession,
    todos: list[dict],
) -> ToolResult:
    validated: list[TodoItem] = []
    for i, item in enumerate(todos):
        content = item.get("content", "")
        status = item.get("status", "")
        active_form = item.get("activeForm", "")

        if not content or not content.strip():
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Todo item {i}: 'content' must be a non-empty string.",
            )
        if status not in ("pending", "in_progress", "completed"):
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Todo item {i}: 'status' must be 'pending', 'in_progress', or 'completed'. Got: '{status}'",
            )
        if not active_form or not active_form.strip():
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Todo item {i}: 'activeForm' must be a non-empty string.",
            )
        validated.append(TodoItem(content=content, status=status, activeForm=active_form))

    session.todos = validated

    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    for t in validated:
        counts[t.status] += 1

    return ToolResult(
        output=(
            f"Updated todo list: {len(validated)} items "
            f"({counts['pending']} pending, {counts['in_progress']} in_progress, "
            f"{counts['completed']} completed)"
        ),
    )
