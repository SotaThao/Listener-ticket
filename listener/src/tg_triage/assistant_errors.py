"""Errors raised by the assistant's read-only tools."""


class ToolError(Exception):
    """Base class; the message is safe to show to the model (no secrets, no paths)."""


class ToolDenied(ToolError):
    """The request is outside the tool's allowed scope (chat, repo, image, limit)."""


class ToolUnavailable(ToolError):
    """The tool cannot run right now (no token configured, network/API error)."""
