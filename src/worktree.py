"""Git worktree lifecycle management.

Encapsulates all git operations so agents never call git directly.
"""

import asyncio
import os
import shutil
from pathlib import Path

from git import Repo, GitCommandError

WORKTREES_DIR = ".worktrees"
MAX_WORKTREES_PER_RUN = 10


def ensure_git_repo(workspace: str) -> Repo:
    """If workspace is not a git repo, initialise one with an empty commit."""
    try:
        return Repo(workspace)
    except Exception:
        repo = Repo.init(workspace)
        # Need at least one commit for worktrees to work
        repo.index.commit("initial commit")
        return repo


def create_worktree(repo: Repo, branch_name: str, worktree_id: str) -> str:
    """Create a git worktree with a new branch. Returns the worktree path."""
    workspace = repo.working_dir
    worktrees_root = os.path.join(workspace, WORKTREES_DIR)
    os.makedirs(worktrees_root, exist_ok=True)

    # Ensure .worktrees is gitignored
    gitignore = os.path.join(workspace, ".gitignore")
    _ensure_gitignore_entry(gitignore, WORKTREES_DIR)

    worktree_path = os.path.join(worktrees_root, worktree_id)

    # Clean up stale worktree at this path if it exists
    if os.path.exists(worktree_path):
        try:
            repo.git.worktree("remove", worktree_path, "--force")
        except GitCommandError:
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Delete stale branch if it exists from a previous run
    try:
        repo.git.branch("-D", branch_name)
    except GitCommandError:
        pass

    repo.git.worktree("prune")
    repo.git.worktree("add", worktree_path, "-b", branch_name)
    return worktree_path


def get_diff(worktree_path: str) -> str:
    """Return the full diff of changes in a worktree.

    Works both before and after commit:
    - Before commit: stages everything and shows staged diff
    - After commit: shows diff between parent and HEAD
    """
    repo = Repo(worktree_path)
    # Try uncommitted changes first
    repo.git.add("-A")
    diff = repo.git.diff("--cached")
    if diff:
        return diff
    # If already committed, show what the last commit changed
    try:
        diff = repo.git.diff("HEAD~1", "HEAD")
    except GitCommandError:
        diff = ""
    return diff or "(no changes)"


def count_worktree_lines(worktree_path: str) -> tuple[int, int]:
    """Count total text files and total lines in the worktree (excluding .git).

    Returns (file_count, line_count).
    """
    root = Path(worktree_path)
    file_count = 0
    line_count = 0
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    for path in root.rglob("*"):
        if any(part in skip for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        file_count += 1
        try:
            line_count += len(path.read_text(errors="replace").splitlines())
        except Exception:
            pass
    return file_count, line_count


def validate_diff_safety(worktree_path: str,
                         pre_files: int = 0,
                         pre_lines: int = 0) -> tuple[bool, str]:
    """Check for signs of mass code destruction.

    Uses two strategies:
    1. Compare current file/line counts against pre-worker snapshot (if provided)
    2. Analyse git diff stats for mass deletions

    Returns (safe, reason). If unsafe, the commit should be aborted.
    """
    # Strategy 1: snapshot comparison (most reliable for rewrites)
    if pre_files > 0 and pre_lines > 0:
        cur_files, cur_lines = count_worktree_lines(worktree_path)
        if cur_files < pre_files * 0.5:
            return False, (
                f"File count dropped from {pre_files} to {cur_files} "
                f"({cur_files/pre_files:.0%} remaining)"
            )
        if cur_lines < pre_lines * 0.5:
            return False, (
                f"Total lines dropped from {pre_lines} to {cur_lines} "
                f"({cur_lines/pre_lines:.0%} remaining)"
            )

    # Strategy 2: git diff stats (catches pure deletions)
    repo = Repo(worktree_path)
    repo.git.add("-A")

    try:
        stat = repo.git.diff("--cached", "--numstat")
    except GitCommandError:
        return True, "ok"

    if not stat.strip():
        return True, "no changes"

    total_added = 0
    total_deleted = 0
    deleted_files = 0
    total_files = 0

    for line in stat.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        total_files += 1
        added_str, deleted_str = parts[0], parts[1]
        if added_str == "-" or deleted_str == "-":
            continue
        added = int(added_str)
        deleted = int(deleted_str)
        total_added += added
        total_deleted += deleted
        if added == 0 and deleted > 0:
            deleted_files += 1

    # If more than half the files are pure deletions
    if total_files > 0 and deleted_files > total_files * 0.5:
        return False, (
            f"Mass deletion detected: {deleted_files}/{total_files} files are pure deletions "
            f"(+{total_added} -{total_deleted} lines)"
        )

    # If net deletion is >80% of changes and >50 lines removed
    if total_deleted > 50 and total_added > 0:
        deletion_ratio = total_deleted / (total_added + total_deleted)
        if deletion_ratio > 0.8:
            return False, (
                f"Excessive deletion: {deletion_ratio:.0%} of changes are deletions "
                f"(+{total_added} -{total_deleted} lines)"
            )

    # If >100 lines deleted with zero additions
    if total_deleted > 100 and total_added == 0:
        return False, (
            f"All code deleted with nothing added (+0 -{total_deleted} lines)"
        )

    return True, "ok"


def reset_worktree(worktree_path: str):
    """Hard-reset worktree to the last commit, discarding all uncommitted changes."""
    repo = Repo(worktree_path)
    repo.git.reset("--hard", "HEAD")
    repo.git.clean("-fd")


def commit_worktree(worktree_path: str, message: str) -> str:
    """Stage all changes and commit in the worktree. Returns the commit hash."""
    repo = Repo(worktree_path)
    repo.git.add("-A")
    if not repo.is_dirty(untracked_files=True):
        return ""
    commit = repo.index.commit(message)
    return str(commit.hexsha)


def merge_worktree(repo: Repo, branch_name: str) -> tuple[bool, str]:
    """Merge a worktree branch into the current branch (main). Returns (success, message)."""
    try:
        result = repo.git.merge(branch_name, "--no-ff", "-m", f"merge: {branch_name}")
        return True, result
    except GitCommandError as e:
        # Abort the failed merge to leave repo in clean state
        try:
            repo.git.merge("--abort")
        except GitCommandError:
            pass
        return False, str(e)


def cleanup_worktree(repo: Repo, worktree_path: str, branch_name: str):
    """Remove a worktree and delete its branch."""
    try:
        repo.git.worktree("remove", worktree_path, "--force")
    except GitCommandError:
        shutil.rmtree(worktree_path, ignore_errors=True)
    try:
        repo.git.worktree("prune")
    except GitCommandError:
        pass
    try:
        repo.git.branch("-D", branch_name)
    except GitCommandError:
        pass


def cleanup_all_worktrees(workspace: str):
    """Safety net: remove all worktrees created by this system and their branches."""
    worktrees_root = os.path.join(workspace, WORKTREES_DIR)
    try:
        repo = Repo(workspace)
        # Remove worktree directories first
        if os.path.exists(worktrees_root):
            shutil.rmtree(worktrees_root, ignore_errors=True)
        repo.git.worktree("prune")
        # Delete all wt/* branches left over from previous runs
        try:
            branches = repo.git.branch("--list", "wt/*").strip()
            if branches:
                for branch in branches.splitlines():
                    branch = branch.strip().lstrip("* ")
                    if branch:
                        try:
                            repo.git.branch("-D", branch)
                        except GitCommandError:
                            pass
        except GitCommandError:
            pass
    except Exception:
        # Workspace may not be a git repo yet — nothing to clean
        if os.path.exists(worktrees_root):
            shutil.rmtree(worktrees_root, ignore_errors=True)


def _ensure_gitignore_entry(gitignore_path: str, entry: str):
    """Add entry to .gitignore if not already present."""
    existing = ""
    if os.path.exists(gitignore_path):
        existing = Path(gitignore_path).read_text()
    if entry not in existing.splitlines():
        with open(gitignore_path, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{entry}\n")


# Async wrappers for use in agent nodes
async def async_create_worktree(repo: Repo, branch_name: str, worktree_id: str) -> str:
    return await asyncio.to_thread(create_worktree, repo, branch_name, worktree_id)


async def async_get_diff(worktree_path: str) -> str:
    return await asyncio.to_thread(get_diff, worktree_path)


async def async_commit_worktree(worktree_path: str, message: str) -> str:
    return await asyncio.to_thread(commit_worktree, worktree_path, message)


async def async_count_worktree_lines(worktree_path: str) -> tuple[int, int]:
    return await asyncio.to_thread(count_worktree_lines, worktree_path)


async def async_validate_diff_safety(worktree_path: str,
                                     pre_files: int = 0,
                                     pre_lines: int = 0) -> tuple[bool, str]:
    return await asyncio.to_thread(validate_diff_safety, worktree_path, pre_files, pre_lines)


async def async_reset_worktree(worktree_path: str):
    return await asyncio.to_thread(reset_worktree, worktree_path)


async def async_merge_worktree(repo: Repo, branch_name: str) -> tuple[bool, str]:
    return await asyncio.to_thread(merge_worktree, repo, branch_name)


async def async_cleanup_worktree(repo: Repo, worktree_path: str, branch_name: str):
    return await asyncio.to_thread(cleanup_worktree, repo, worktree_path, branch_name)
