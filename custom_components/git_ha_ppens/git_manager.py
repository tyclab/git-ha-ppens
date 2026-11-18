"""Git operations manager for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .const import (
    DEFAULT_GITIGNORE_ENTRIES,
    SECRET_PATH_PATTERNS,
    SECRET_PATTERNS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class GitStatus:
    """Represents the current git repository status."""

    branch: str = "unknown"
    dirty: bool = False
    changed_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    staged_files: list[str] = field(default_factory=list)
    last_commit_hash: str = ""
    last_commit_hash_short: str = ""
    last_commit_message: str = ""
    last_commit_author: str = ""
    last_commit_time: datetime | None = None
    ahead: int = 0
    behind: int = 0
    remote_configured: bool = False
    total_commits: int = 0
    has_upstream: bool = False


@dataclass
class CommitInfo:
    """Represents a single commit."""

    hash: str
    hash_short: str
    message: str
    author: str
    timestamp: datetime


class GitError(Exception):
    """Raised when a git operation fails."""


class PreDeployCheckError(GitError):
    """Raised when a pre-deploy check fails and the pull was rolled back.

    Subclasses GitError so existing ``except GitError`` paths still handle it,
    while callers that want to react specifically can catch this first.
    """

    def __init__(self, errors: list[str]) -> None:
        """Store the individual check error messages."""
        self.errors = errors
        joined = "; ".join(errors) if errors else "unknown error"
        super().__init__(f"Pre-deploy check failed: {joined}")


class GitConflictError(GitError):
    """LOCAL PATCH (tycstation): sync aborted on conflict or stale divergence.

    Raised instead of resolving the situation automatically. The working tree is
    left on its pre-sync commit — no half-merged tree, no force-push.
    """


class SecretPathBlockedError(GitError):
    """LOCAL PATCH (tycstation): commit refused, a staged path looks like a secret.

    Its own type rather than GitConflictError because the remedy differs: a
    conflict means /config and `main` diverged and need a merge, while this
    means a file must never reach `main` at all and needs ignoring or deleting. Callers must catch this BEFORE their ``except
    GitError`` arm, or the refusal degrades into a generic "commit failed".
    """

    def __init__(self, paths: list[str]) -> None:
        """Store the offending staged paths."""
        self.paths = paths
        joined = ", ".join(paths)
        super().__init__(
            "Commit refused: the following staged paths look like secrets and "
            f"must never reach `main`: {joined}. Untracked: add them to "
            ".gitignore or delete them. Already tracked: on the box, "
            "`git rm --cached <path>`, add it to .gitignore and commit that "
            "removal by hand — this guard only gates the integration's own "
            "commits, and the sync stays stopped until the path is gone."
        )


class GitManager:
    """Manages git operations for the Home Assistant config directory."""

    def __init__(
        self,
        repo_path: str,
        git_user: str = "",
        git_email: str = "",
    ) -> None:
        """Initialize the git manager."""
        self._repo_path = repo_path
        self._git_user = git_user
        self._git_email = git_email

    @property
    def repo_path(self) -> str:
        """Return the repository path."""
        return self._repo_path

    async def _run_git(
        self,
        *args: str,
        check: bool = True,
        capture_stderr: bool = True,
    ) -> str:
        """Run a git command and return stdout.

        Args:
            *args: Git command arguments (without 'git' prefix).
            check: If True, raise GitError on non-zero exit code.
            capture_stderr: If True, capture stderr for error messages.

        Returns:
            The stdout output of the command.

        Raises:
            GitError: If the command fails and check is True.
        """
        cmd = ["git", "-C", self._repo_path, *args]
        _LOGGER.debug("Running git command: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if capture_stderr else None,
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "LC_ALL": "C",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": os.path.realpath(self._repo_path),
                },
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace").rstrip()
            stderr = (
                stderr_bytes.decode("utf-8", errors="replace").strip()
                if stderr_bytes
                else ""
            )
        except FileNotFoundError as err:
            raise GitError("Git is not installed or not found in PATH") from err
        except OSError as err:
            raise GitError(f"Failed to run git: {err}") from err

        if check and process.returncode != 0:
            error_msg = stderr or stdout or f"Git command failed with code {process.returncode}"
            _LOGGER.error("Git command failed: %s -> %s", " ".join(cmd), error_msg)
            raise GitError(error_msg)

        return stdout

    async def is_git_installed(self) -> bool:
        """Check if git is installed and available."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            return process.returncode == 0 and b"git version" in stdout
        except (FileNotFoundError, OSError):
            return False

    async def get_git_version(self) -> str:
        """Return the installed git version string."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            return stdout.decode("utf-8", errors="replace").strip()
        except (FileNotFoundError, OSError):
            return "not installed"

    async def is_repo_initialized(self) -> bool:
        """Check if the path is an initialized git repository."""
        try:
            result = await self._run_git(
                "rev-parse", "--is-inside-work-tree", check=False
            )
            return result == "true"
        except GitError:
            return False

    async def init_repo(self) -> None:
        """Initialize a git repository and configure user settings."""
        repo_path = Path(self._repo_path)
        if not repo_path.is_dir():
            raise GitError(f"Repository path does not exist: {self._repo_path}")

        if not await self.is_repo_initialized():
            await self._run_git("init")
            _LOGGER.info("Initialized git repository at %s", self._repo_path)

            # Verify .git was actually created
            git_dir = repo_path / ".git"
            if not git_dir.exists():
                raise GitError(
                    f"git init succeeded but .git directory was not created at "
                    f"{self._repo_path}. Check directory permissions and ownership."
                )

        # Configure user
        if self._git_user:
            await self._run_git("config", "user.name", self._git_user)
        if self._git_email:
            await self._run_git("config", "user.email", self._git_email)

        # Set default branch name for future inits
        await self._run_git("config", "init.defaultBranch", "main", check=False)

        # Normalize branch to "main" if repo has no commits yet (unborn HEAD)
        if not await self.has_commits():
            current_branch = await self._run_git(
                "rev-parse", "--abbrev-ref", "HEAD", check=False
            )
            if current_branch and current_branch != "main" and "fatal" not in current_branch:
                await self._run_git("branch", "-M", "main", check=False)
                _LOGGER.info("Renamed branch from %s to main", current_branch)

    async def has_commits(self) -> bool:
        """Check if the repository has any commits."""
        try:
            result = await self._run_git(
                "rev-parse", "--verify", "HEAD", check=False
            )
            return bool(result) and len(result) >= 40 and "fatal" not in result.lower()
        except GitError:
            return False

    async def get_head_sha(self) -> str:
        """Return the current HEAD commit SHA (empty string if unborn)."""
        result = await self._run_git(
            "rev-parse", "--verify", "HEAD", check=False
        )
        if result and len(result) >= 40 and "fatal" not in result.lower():
            return result
        return ""

    # LOCAL PATCH (tycstation): changed paths for EVENT_PULL ([] on error) and for
    # deploy.py (strict). --no-renames: a rename must list the old path too.
    async def diff_name_only(
        self, ref_a: str, ref_b: str = "HEAD", *, strict: bool = False
    ) -> list[str]:
        """Return file paths changed between two refs (empty on error unless strict)."""
        try:
            if not ref_a:
                raise GitError("no base ref")
            out = await self._run_git("diff", "--no-renames", "--name-only", ref_a, ref_b)
        except GitError:
            if strict:
                raise
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def show_file(self, ref: str, path: str) -> str | None:
        """Return a file's text at a ref, or None if it does not exist there."""
        if not await self._run_git("ls-tree", "--name-only", ref, "--", path):
            return None
        return await self._run_git("show", f"{ref}:{path}")

    async def reset_hard(self, ref: str) -> None:
        """Hard-reset the working tree to the given ref (rollback)."""
        await self._run_git("reset", "--hard", ref)
        _LOGGER.info("Rolled back working tree to %s", ref[:8])

    async def _merge_in_progress(self) -> bool:
        """Return True while an unresolved merge is in progress."""
        result = await self._run_git(
            "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False
        )
        return bool(result.strip())

    async def get_upstream_sha(self) -> str:
        """Return the upstream tracking branch SHA (empty string if none)."""
        result = await self._run_git(
            "rev-parse", "--verify", "@{u}", check=False
        )
        if result and len(result) >= 40 and "fatal" not in result.lower():
            return result
        return ""

    async def _has_upstream(self) -> bool:
        """Check if the current branch has an upstream tracking branch."""
        try:
            result = await self._run_git(
                "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                check=False,
            )
            return bool(result) and "fatal" not in result.lower() and "error" not in result.lower()
        except GitError:
            return False

    async def is_remote_configured(self) -> bool:
        """Check if the remote 'origin' is configured with a valid URL."""
        try:
            result = await self._run_git(
                "remote", "get-url", "origin", check=False
            )
            return bool(result) and "fatal" not in result.lower() and "error" not in result.lower()
        except GitError:
            return False

    async def get_remote_url(self) -> str:
        """Return the configured remote URL (empty string if not set)."""
        try:
            result = await self._run_git(
                "remote", "get-url", "origin", check=False
            )
            if result and "fatal" not in result.lower() and "error" not in result.lower():
                return result
        except GitError:
            pass
        return ""

    async def get_status(self) -> GitStatus:
        """Get the complete repository status."""
        status = GitStatus()

        if not await self.is_repo_initialized():
            return status

        try:
            branch = await self._run_git(
                "rev-parse", "--abbrev-ref", "HEAD", check=False
            )
            status.branch = branch if branch and "fatal" not in branch else "unknown"
        except GitError:
            status.branch = "unknown"

        # Get changed/staged/untracked files
        try:
            porcelain = await self._run_git("status", "--porcelain")
            if porcelain:
                for line in porcelain.splitlines():
                    if len(line) < 4:
                        continue
                    index_status = line[0]
                    worktree_status = line[1]
                    filepath = line[3:]

                    if index_status == "?" and worktree_status == "?":
                        status.untracked_files.append(filepath)
                    elif index_status in "MADRC":
                        status.staged_files.append(filepath)
                    if worktree_status in "MD":
                        status.changed_files.append(filepath)

                status.dirty = True
            else:
                status.dirty = False
        except GitError:
            pass

        try:
            log_format = await self._run_git(
                "log", "-1", "--format=%H%n%h%n%s%n%an%n%aI", check=False
            )
            if log_format and "fatal" not in log_format.lower():
                parts = log_format.splitlines()
                if len(parts) >= 5:
                    status.last_commit_hash = parts[0]
                    status.last_commit_hash_short = parts[1]
                    status.last_commit_message = parts[2]
                    status.last_commit_author = parts[3]
                    try:
                        status.last_commit_time = datetime.fromisoformat(parts[4])
                    except ValueError:
                        status.last_commit_time = None
        except GitError:
            pass

        try:
            count_str = await self._run_git("rev-list", "--count", "HEAD", check=False)
            if count_str and "fatal" not in count_str.lower():
                status.total_commits = int(count_str)
        except (GitError, ValueError):
            status.total_commits = 0

        # Get ahead/behind remote
        try:
            status.has_upstream = await self._has_upstream()
            if status.has_upstream:
                status.remote_configured = True
                ab_output = await self._run_git(
                    "rev-list", "--left-right", "--count", "HEAD...@{u}",
                    check=False,
                )
                if ab_output and "fatal" not in ab_output.lower():
                    parts = ab_output.split()
                    if len(parts) == 2:
                        status.ahead = int(parts[0])
                        status.behind = int(parts[1])
            else:
                # No tracking branch — check if remote origin exists
                remotes = await self._run_git("remote", check=False)
                status.remote_configured = bool(remotes.strip())
                # Signal "not pushed" when remote exists but no tracking
                if status.remote_configured and status.total_commits > 0:
                    status.ahead = -1  # Convention: -1 = never pushed
        except (GitError, ValueError):
            pass

        return status

    async def commit(self, message: str | None = None) -> CommitInfo | None:
        """Stage all changes and create a commit.

        Args:
            message: Commit message. If None, auto-generates one.

        Returns:
            CommitInfo for the new commit, or None if nothing to commit.
        """
        porcelain = await self._run_git("status", "--porcelain")
        if not porcelain:
            _LOGGER.debug("Nothing to commit")
            return None

        # Auto-generate message if not provided
        if not message:
            message = self._generate_commit_message(porcelain)

        # Stage all changes (respecting .gitignore)
        await self._run_git("add", "-A")

        # Refuse BEFORE the commit exists: auto_push puts it on protected
        # `main`, where history cannot be rewritten to drop a leaked secret.
        await self._assert_no_secret_paths_staged()

        await self._run_git("commit", "-m", message)

        log_output = await self._run_git(
            "log", "-1", "--format=%H%n%h%n%s%n%an%n%aI"
        )
        parts = log_output.splitlines()
        if len(parts) >= 5:
            try:
                timestamp = datetime.fromisoformat(parts[4])
            except ValueError:
                timestamp = datetime.now(tz=timezone.utc)

            commit_info = CommitInfo(
                hash=parts[0],
                hash_short=parts[1],
                message=parts[2],
                author=parts[3],
                timestamp=timestamp,
            )
            _LOGGER.info("Created commit %s: %s", commit_info.hash_short, message)
            return commit_info

        return None

    async def _assert_no_secret_paths_staged(self) -> None:
        """Unstage and refuse if any staged path is secret-shaped.

        Raises:
            SecretPathBlockedError: If a staged path matches
                SECRET_PATH_PATTERNS. The index is reset first, so the caller
                that handles this leaves no commit and no staged secret behind.
        """
        # quotePath=false: git C-quotes any path with a non-ASCII byte
        # ("k\303\274che/secrets.yaml", trailing quote included), which defeats
        # every anchored pattern. check stays on — a guard that reads the index
        # must fail closed, not treat an unreadable index as clean.
        staged = await self._run_git(
            "-c", "core.quotePath=false", "diff", "--cached", "--name-only"
        )
        if not staged:
            return

        patterns = [re.compile(p, re.IGNORECASE) for p in SECRET_PATH_PATTERNS]
        offenders = [
            path
            for path in (line.strip() for line in staged.splitlines())
            if path and any(p.search(path) for p in patterns)
        ]
        if not offenders:
            return

        # Leave the index clean so the next cycle re-evaluates from scratch
        # instead of inheriting a poisoned staging area.
        await self._run_git("reset", check=False)

        _LOGGER.error(
            "Commit refused: %d secret-shaped path(s) staged: %s",
            len(offenders),
            ", ".join(offenders),
        )
        raise SecretPathBlockedError(offenders)

    def _generate_commit_message(self, porcelain_output: str) -> str:
        """Generate a commit message from porcelain status output."""
        files: list[str] = []
        for line in porcelain_output.splitlines():
            if len(line) >= 4:
                filepath = line[3:].strip()
                filename = Path(filepath).name
                if filename and filename not in files:
                    files.append(filename)

        if not files:
            return "Auto: configuration updated"

        if len(files) <= 3:
            return f"Auto: {', '.join(files)} changed"

        return f"Auto: {len(files)} files changed"

    async def push(self) -> int:
        """Push commits to the configured remote.

        Returns:
            Number of commits that were pushed.

        Raises:
            GitConflictError: If the remote moved and the local branch is stale
                or diverged — nothing is merged or force-pushed.
            GitError: If the push fails for any other reason.
        """
        if not await self.is_remote_configured():
            raise GitError(
                "Remote 'origin' is not configured. "
                "Please set a valid remote URL in the integration options "
                "(Settings → Devices & Services → git-ha-ppens → Configure)."
            )

        branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        has_upstream = await self._has_upstream()

        ahead = 0
        if has_upstream:
            try:
                ab_output = await self._run_git(
                    "rev-list", "--left-right", "--count", "HEAD...@{u}",
                    check=False,
                )
                if ab_output and "fatal" not in ab_output.lower():
                    ahead = int(ab_output.split()[0])
            except (GitError, ValueError, IndexError):
                pass
        else:
            # No upstream — all local commits need pushing
            try:
                count_str = await self._run_git(
                    "rev-list", "--count", "HEAD", check=False
                )
                if count_str and "fatal" not in count_str.lower():
                    ahead = int(count_str)
            except (GitError, ValueError):
                ahead = 0

        try:
            await self._run_git("push", "-u", "origin", branch)
            _LOGGER.info("Push successful: %d commit(s) to origin/%s", ahead, branch)
            return ahead
        except GitError as err:
            error_lower = str(err).lower()
            _LOGGER.warning("Push rejected: %s", err)

            # Auth/config errors say nothing about divergence — surface as-is.
            if any(keyword in error_lower for keyword in [
                "permission denied",
                "403",
                "401",
                "authentication",
                "could not read username",
                "invalid credentials",
                "authorization failed",
                "does not appear to be a git repository",
                "could not read from remote repository",
                "repository not found",
            ]):
                _LOGGER.error(
                    "Push failed due to authentication/permission/configuration error. "
                    "Check: 1) Remote URL is correct, "
                    "2) Repository exists on GitHub, "
                    "3) PAT has 'repo' scope. Error: %s",
                    err,
                )
                raise

            push_err = err

        # LOCAL PATCH (tycstation): abort and alert on conflict or stale
        # divergence. Upstream escalated a rejected push to an unrelated-histories
        # merge and then to a leased force-push; this box pushes straight to a
        # protected `main`, so both would publish an unreviewed — possibly
        # conflicted — tree. The pre-deploy-checked pull path heals divergence
        # here.
        await self._run_git("fetch", "origin", branch, check=False)
        behind = 0
        ab_output = await self._run_git(
            "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}",
            check=False,
        )
        parts = ab_output.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            ahead, behind = int(parts[0]), int(parts[1])

        if behind > 0:
            raise GitConflictError(
                f"Push to origin/{branch} rejected: local is {ahead} ahead and "
                f"{behind} behind. Not merging and not force-pushing — pull "
                f"(pre-deploy checked) or resolve by hand."
            ) from push_err

        raise push_err

    async def fetch(self) -> None:
        """Fetch from the configured remote without merging.

        Raises:
            GitError: If remote is not configured or fetch fails.
        """
        if not await self.is_remote_configured():
            raise GitError(
                "Remote 'origin' is not configured."
            )

        branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        await self._run_git("fetch", "origin", branch)
        _LOGGER.debug("Fetched from origin/%s", branch)

    async def pull(
        self,
        backup: bool = True,
        validate: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> int:
        """Pull from the configured remote.

        Args:
            backup: If True, create a backup commit of uncommitted changes first.
            validate: Optional async callback run after merging new commits. It
                returns a list of error messages; if non-empty, the pull is
                rolled back to the pre-pull state and PreDeployCheckError is
                raised.

        Returns:
            Number of commits pulled.

        Raises:
            PreDeployCheckError: If validation fails (after rolling back).
            GitConflictError: If the merge conflicts (after aborting it).
            SecretPathBlockedError: If the backup commit would stage a
                secret-shaped path (index reset, nothing committed or pulled).
            GitError: If the pull itself fails.
        """
        if not await self.is_remote_configured():
            raise GitError(
                "Remote 'origin' is not configured. "
                "Please set a valid remote URL in the integration options "
                "(Settings → Devices & Services → git-ha-ppens → Configure)."
            )

        if backup:
            porcelain = await self._run_git("status", "--porcelain")
            if porcelain:
                await self._run_git("add", "-A")
                # Same refusal as commit(): a path the auto-commit guard already
                # rejected is still on disk, and `add -A` would sweep it into
                # this commit, which auto_push then puts on protected `main`.
                await self._assert_no_secret_paths_staged()
                await self._run_git(
                    "commit", "-m",
                    "Backup: auto-saved before pull"
                )
                _LOGGER.info("Created backup commit before pull")

        try:
            count_before = int(
                await self._run_git("rev-list", "--count", "HEAD")
            )
        except (GitError, ValueError):
            count_before = 0

        # Capture HEAD right before the merge so a rollback restores this exact
        # state (including the backup commit above) and only discards the merge.
        pre_pull_head = await self.get_head_sha()
        # LOCAL PATCH (tycstation): deploy.py splits box-side from pulled changes here.
        self.last_pre_merge_head = pre_pull_head

        branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        try:
            # --no-rebase: without a pull strategy configured, git 2.33+ refuses
            # divergent branches outright, so the merge below never starts.
            await self._run_git("pull", "--no-rebase", "origin", branch)
        except GitError as err:
            # LOCAL PATCH (tycstation): a conflicted merge leaves conflict
            # markers in the live config, and the next auto-commit would
            # `git add -A` them straight onto protected `main`. Abort the merge
            # so /config stays on its last-good commit, and alert.
            if await self._merge_in_progress():
                # Name the files before aborting — afterwards there is nothing
                # left to read them from.
                unmerged = await self._run_git(
                    "diff", "--name-only", "--diff-filter=U", check=False
                )
                files = ", ".join(unmerged.split()) or "unknown files"
                await self._run_git("merge", "--abort", check=False)
                raise GitConflictError(
                    f"Pull from origin/{branch} conflicted on {files}; the "
                    f"merge was aborted and the working tree stays on "
                    f"{pre_pull_head[:8]}. Resolve by hand."
                ) from err
            raise

        try:
            count_after = int(
                await self._run_git("rev-list", "--count", "HEAD")
            )
            pulled = count_after - count_before
        except (GitError, ValueError):
            pulled = 0

        # Pre-deploy gate: validate the merged result before keeping it.
        if validate is not None and pulled > 0 and pre_pull_head:
            errors = await validate()
            if errors:
                _LOGGER.warning(
                    "Pre-deploy check failed after pull; rolling back to %s",
                    pre_pull_head[:8],
                )
                await self.reset_hard(pre_pull_head)
                raise PreDeployCheckError(errors)

        _LOGGER.info("Pulled %d commit(s) from origin/%s", pulled, branch)
        return pulled

    async def get_log(self, count: int = 10) -> list[CommitInfo]:
        """Get recent commit history."""
        commits: list[CommitInfo] = []
        try:
            log_output = await self._run_git(
                "log", f"-{count}", "--format=%H%n%h%n%s%n%an%n%aI%n---"
            )
            if not log_output:
                return commits

            entries = log_output.split("---\n")
            for entry in entries:
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.splitlines()
                if len(parts) >= 5:
                    try:
                        timestamp = datetime.fromisoformat(parts[4])
                    except ValueError:
                        timestamp = datetime.now(tz=timezone.utc)

                    commits.append(
                        CommitInfo(
                            hash=parts[0],
                            hash_short=parts[1],
                            message=parts[2],
                            author=parts[3],
                            timestamp=timestamp,
                        )
                    )
        except GitError:
            pass

        return commits

    async def get_diff(self) -> str:
        """Get the current diff of uncommitted changes."""
        try:
            diff = await self._run_git("diff")
            staged_diff = await self._run_git("diff", "--cached")
            combined = ""
            if diff:
                combined += diff
            if staged_diff:
                if combined:
                    combined += "\n"
                combined += staged_diff
            return combined
        except GitError:
            return ""

    async def setup_gitignore(self, skip_defaults: bool = False) -> bool:
        """Create or update .gitignore with security defaults."""
        return await asyncio.to_thread(self._setup_gitignore_sync, skip_defaults)

    def _setup_gitignore_sync(self, skip_defaults: bool = False) -> bool:
        """Synchronous implementation of setup_gitignore."""
        if skip_defaults:
            # User manages .gitignore via UI; don't append defaults
            return False

        gitignore_path = Path(self._repo_path) / ".gitignore"
        existing_entries: set[str] = set()
        modified = False

        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding="utf-8")
            existing_entries = {
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
        else:
            content = ""

        entries_to_add: list[str] = []
        for entry in DEFAULT_GITIGNORE_ENTRIES:
            clean = entry.strip()
            if not clean or clean.startswith("#"):
                if not existing_entries:
                    entries_to_add.append(entry)
                continue
            if clean not in existing_entries:
                entries_to_add.append(entry)
                modified = True

        if modified or not gitignore_path.exists():
            if content and not content.endswith("\n"):
                content += "\n"
            if content and entries_to_add:
                content += "\n# Added by git-ha-ppens\n"

            content += "\n".join(entries_to_add) + "\n"
            gitignore_path.write_text(content, encoding="utf-8")
            _LOGGER.info("Updated .gitignore at %s", gitignore_path)
            return True

        return False

    async def apply_gitignore(self) -> None:
        """Remove tracked files that are now covered by .gitignore.

        Runs 'git rm -r --cached .' followed by 'git add -A'
        to re-apply .gitignore rules to the index.
        """
        await self._run_git("rm", "-r", "--cached", ".", check=False)
        await self._run_git("add", "-A")
        _LOGGER.info("Re-applied .gitignore rules to tracked files")

    async def scan_for_secrets(self) -> list[dict[str, str]]:
        """Scan staged files for potential secrets."""
        findings: list[dict[str, str]] = []

        try:
            staged = await self._run_git("diff", "--cached", "--name-only", check=False)
            if not staged:
                staged = await self._run_git(
                    "ls-files", "--modified", check=False
                )
            if not staged:
                return findings

            compiled_patterns = [re.compile(p) for p in SECRET_PATTERNS]

            filepaths = [
                fp.strip()
                for fp in staged.splitlines()
                if fp.strip()
                and fp.strip().endswith(
                    (".yaml", ".yml", ".json", ".conf", ".cfg", ".ini", ".txt", ".env")
                )
            ]

            if filepaths:
                findings = await asyncio.to_thread(
                    self._scan_files_for_secrets_sync, filepaths, compiled_patterns
                )

        except GitError:
            pass

        if findings:
            _LOGGER.warning(
                "Secret scan found %d potential secret(s) in tracked files",
                len(findings),
            )

        return findings

    def _scan_files_for_secrets_sync(
        self,
        filepaths: list[str],
        compiled_patterns: list[re.Pattern[str]],
    ) -> list[dict[str, str]]:
        """Synchronous implementation of file scanning for secrets."""
        findings: list[dict[str, str]] = []

        for filepath in filepaths:
            full_path = Path(self._repo_path) / filepath
            if not full_path.is_file():
                continue

            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
                for line_num, line in enumerate(content.splitlines(), 1):
                    for pattern in compiled_patterns:
                        if pattern.search(line):
                            findings.append(
                                {
                                    "file": filepath,
                                    "line": str(line_num),
                                    "pattern": pattern.pattern[:50],
                                }
                            )
                            break
            except OSError:
                continue

        return findings

    async def set_remote(self, url: str) -> None:
        """Configure the remote origin URL."""
        remotes = await self._run_git("remote", check=False)
        if "origin" in remotes.splitlines():
            await self._run_git("remote", "set-url", "origin", url)
        else:
            await self._run_git("remote", "add", "origin", url)
        _LOGGER.info("Remote origin set to %s", self._redact_url(url))

    async def configure_token_auth(self, url: str, token: str) -> None:
        """Configure token-based authentication by embedding in the remote URL."""
        if url.startswith("https://"):
            authed_url = url.replace("https://", f"https://oauth2:{token}@")
            await self.set_remote(authed_url)
        else:
            _LOGGER.warning("Token auth only works with HTTPS URLs")

    async def configure_ssh_auth(self, ssh_key_path: str) -> None:
        """Configure SSH-based authentication."""
        key_path = Path(ssh_key_path)
        if not key_path.is_file():
            raise GitError(f"SSH key not found: {ssh_key_path}")

        ssh_command = f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=accept-new"
        await self._run_git(
            "config", "core.sshCommand", ssh_command
        )
        _LOGGER.info("SSH authentication configured with key: %s", ssh_key_path)

    @staticmethod
    def _redact_url(url: str) -> str:
        """Redact sensitive parts of a URL for logging."""
        redacted = re.sub(r"://[^@]+@", "://***@", url)
        return redacted
