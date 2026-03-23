"""Scoped LangChain tools for worktree-isolated file operations."""

from langchain_community.tools.file_management import (
    ReadFileTool,
    WriteFileTool,
    ListDirectoryTool,
    CopyFileTool,
    MoveFileTool,
)


def make_file_tools(root_dir: str) -> list:
    """Return file management tools scoped to root_dir (prevents path traversal)."""
    return [
        ReadFileTool(root_dir=root_dir),
        WriteFileTool(root_dir=root_dir),
        ListDirectoryTool(root_dir=root_dir),
        CopyFileTool(root_dir=root_dir),
        MoveFileTool(root_dir=root_dir),
    ]
