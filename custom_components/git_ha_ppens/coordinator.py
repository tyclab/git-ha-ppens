"""DataUpdateCoordinator for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .checks import (
    async_run_pre_deploy_check,
    notify_check_failed,
    notify_secret_blocked,
    notify_sync_blocked,
)
from .const import (
    DEFAULT_FETCH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_CHECK_FAILED,
    EVENT_ERROR,
    EVENT_FETCH,
    EVENT_PULL,
    EVENT_PUSH,
)
from .deploy import async_plan_pull, async_schedule_apply
from .git_manager import (
    GitConflictError,
    GitError,
    GitManager,
    GitStatus,
    PreDeployCheckError,
    SecretPathBlockedError,
)

_LOGGER = logging.getLogger(__name__)


class GitHaPpensCoordinator(DataUpdateCoordinator[GitStatus]):
    """Coordinator to poll git repository status."""

    def __init__(
        self,
        hass: HomeAssistant,
        git_manager: GitManager,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        auto_pull: bool = False,
        auto_push: bool = False,
        remote_configured: bool = False,
        fetch_interval: int = DEFAULT_FETCH_INTERVAL,
        pre_deploy_check: bool = False,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.git_manager = git_manager
        self._auto_pull = auto_pull
        self._auto_push = auto_push
        self._remote_configured = remote_configured
        self._fetch_interval = fetch_interval
        self._pre_deploy_check = pre_deploy_check
        self.git_lock = asyncio.Lock()

        self._last_fetch_time: datetime | None = None
        self._last_pull_time: datetime | None = None
        self._last_push_time: datetime | None = None
        # Remote SHA whose auto-pull was blocked (failed pre-deploy check, or a
        # conflicting merge); skip re-attempting it until the remote advances.
        self._blocked_remote_sha: str | None = None

    @property
    def last_fetch_time(self) -> datetime | None:
        """Return the last fetch timestamp."""
        return self._last_fetch_time

    @property
    def last_pull_time(self) -> datetime | None:
        """Return the last pull timestamp."""
        return self._last_pull_time

    @property
    def last_push_time(self) -> datetime | None:
        """Return the last push timestamp."""
        return self._last_push_time

    def record_push_time(self) -> None:
        """Record that a push just happened."""
        self._last_push_time = datetime.now(tz=timezone.utc)

    def record_pull_time(self) -> None:
        """Record that a pull just happened."""
        self._last_pull_time = datetime.now(tz=timezone.utc)

    def pre_deploy_validator(
        self,
    ) -> Callable[[], Awaitable[list[str]]] | None:
        """Return a validation callback for pull(), or None if disabled."""
        if not self._pre_deploy_check:
            return None

        async def _validate() -> list[str]:
            return await async_run_pre_deploy_check(
                self.hass, self.git_manager.repo_path
            )

        return _validate

    async def _async_update_data(self) -> GitStatus:
        """Fetch git status from the repository."""
        # Fetch from remote if enough time has passed
        fetched = False
        if self._remote_configured:
            fetched = await self._maybe_fetch()

        try:
            status = await self.git_manager.get_status()
        except GitError as err:
            raise UpdateFailed(f"Error fetching git status: {err}") from err

        # LOCAL PATCH (tycstation): publish a box that is only ahead. The push
        # after a healing pull runs while that pull's restart stops HA and can
        # be lost there; afterwards only another pull or a file event pushed,
        # so the box stayed ahead until a merge that did not restart. Once per
        # fetch, so a failing remote is retried at the fetch interval, not
        # every poll.
        if (
            fetched
            and self._auto_push
            and status.ahead > 0
            and status.behind == 0
            and not self.git_lock.locked()
        ):
            _LOGGER.warning(
                "Box is %d commit(s) ahead and not behind after a fetch: "
                "pushing what an earlier push did not land",
                status.ahead,
            )
            async with self.git_lock:
                status = await self._async_push_ahead(
                    status, "Auto-push of an ahead-only box"
                )

        # Auto-pull if enabled and remote has new commits
        if self._auto_pull and self._remote_configured and status.behind > 0:
            if self.git_lock.locked():
                _LOGGER.debug(
                    "Skipping auto-pull: another git operation in progress"
                )
                return status

            # Skip remote commits already blocked, until the remote advances, to
            # avoid re-running the heavy check (or re-conflicting) every poll.
            upstream_sha = await self.git_manager.get_upstream_sha()
            if (
                self._blocked_remote_sha is not None
                and upstream_sha == self._blocked_remote_sha
            ):
                _LOGGER.debug(
                    "Skipping auto-pull: remote %s previously blocked by "
                    "pre-deploy check or conflict",
                    upstream_sha[:8],
                )
                return status

            async with self.git_lock:
                try:
                    # LOCAL PATCH (tycstation): capture HEAD before the pull so
                    # deploy.py can plan restart, reloads or nothing from the
                    # diff, and the event carries it.
                    pre_pull_head = await self.git_manager.get_head_sha()
                    commits_pulled = await self.git_manager.pull(
                        backup=True, validate=self.pre_deploy_validator()
                    )
                    self._last_pull_time = datetime.now(tz=timezone.utc)
                    self._blocked_remote_sha = None
                    changed_files = await self.git_manager.diff_name_only(
                        pre_pull_head
                    )
                    plan = await async_plan_pull(
                        self.hass, self.git_manager, pre_pull_head
                    )
                    self.hass.bus.async_fire(
                        EVENT_PULL,
                        {
                            "commits_pulled": commits_pulled,
                            "auto": True,
                            "changed_files": changed_files,
                            "apply": plan.as_event(),
                        },
                    )
                    async_schedule_apply(self.hass, plan)
                    _LOGGER.info(
                        "Auto-pull: %d commit(s) pulled from remote",
                        commits_pulled,
                    )

                    # Re-fetch status so sensors reflect the post-pull state
                    try:
                        status = await self.git_manager.get_status()
                    except GitError:
                        pass

                    # LOCAL PATCH (tycstation): publish what the pull just
                    # healed. A rejected push leaves the box ahead of the
                    # remote, and upstream only retries on the next
                    # file-watcher event — so a box edit stayed unpushed for
                    # hours after the divergence that blocked it was resolved.
                    if self._auto_push and status.ahead > 0:
                        status = await self._async_push_ahead(
                            status, "Auto-push after pull"
                        )

                except PreDeployCheckError as err:
                    _LOGGER.warning(
                        "Auto-pull blocked by pre-deploy check: %s", err
                    )
                    self._blocked_remote_sha = upstream_sha or None
                    self.hass.bus.async_fire(
                        EVENT_CHECK_FAILED,
                        {"errors": err.errors, "auto": True},
                    )
                    notify_check_failed(self.hass, err.errors)
                    # Re-fetch status so sensors reflect the rolled-back state
                    try:
                        status = await self.git_manager.get_status()
                    except GitError:
                        pass

                except GitConflictError as err:
                    _LOGGER.error("Auto-pull aborted: %s", err)
                    # Same suppression as a failed pre-deploy check: without it
                    # the conflicting remote would be re-pulled and re-aborted
                    # every poll.
                    self._blocked_remote_sha = upstream_sha or None
                    self.hass.bus.async_fire(
                        EVENT_ERROR,
                        {"operation": "auto_pull", "error": str(err)},
                    )
                    notify_sync_blocked(self.hass, "auto-pull", str(err))

                # Ahead of the GitError arm it subclasses; the pull is aborted
                # with the index reset, so nothing was committed or merged.
                except SecretPathBlockedError as err:
                    _LOGGER.error("Auto-pull refused: %s", err)
                    self.hass.bus.async_fire(
                        EVENT_ERROR,
                        {
                            "operation": "auto_pull",
                            "error": str(err),
                            "secret_paths": err.paths,
                        },
                    )
                    notify_secret_blocked(self.hass, "auto-pull backup", err.paths)

                except GitError as err:
                    _LOGGER.warning("Auto-pull failed: %s", err)
                    self.hass.bus.async_fire(
                        EVENT_ERROR,
                        {"operation": "auto_pull", "error": str(err)},
                    )

        return status

    async def _async_push_ahead(self, status: GitStatus, label: str) -> GitStatus:
        """Push the commits the box is ahead by; the caller holds git_lock."""
        try:
            commits_pushed = await self.git_manager.push()
            self.record_push_time()
            self.hass.bus.async_fire(
                EVENT_PUSH,
                {
                    "commits_pushed": commits_pushed,
                    "auto": True,
                },
            )
            _LOGGER.info("%s: %d commit(s) pushed", label, commits_pushed)
            try:
                status = await self.git_manager.get_status()
            except GitError:
                pass
        except GitConflictError as push_err:
            _LOGGER.error("Auto-push aborted: %s", push_err)
            self.hass.bus.async_fire(
                EVENT_ERROR,
                {
                    "operation": "auto_push",
                    "error": str(push_err),
                },
            )
            notify_sync_blocked(self.hass, "auto-push", str(push_err))
        except GitError as push_err:
            _LOGGER.error("Auto-push failed: %s", push_err)
            self.hass.bus.async_fire(
                EVENT_ERROR,
                {
                    "operation": "auto_push",
                    "error": str(push_err),
                },
            )
        return status

    async def _maybe_fetch(self) -> bool:
        """Fetch from remote if the fetch interval has elapsed; True if it did."""
        now = datetime.now(tz=timezone.utc)

        if self._last_fetch_time is not None:
            elapsed = (now - self._last_fetch_time).total_seconds()
            if elapsed < self._fetch_interval:
                return False

        if self.git_lock.locked():
            _LOGGER.debug("Skipping fetch: another git operation in progress")
            return False

        async with self.git_lock:
            try:
                await self.git_manager.fetch()
                self._last_fetch_time = datetime.now(tz=timezone.utc)
                self.hass.bus.async_fire(
                    EVENT_FETCH,
                    {"auto": True},
                )
                _LOGGER.debug("Auto-fetch completed")
                return True
            except GitError as err:
                _LOGGER.warning("Auto-fetch failed: %s", err)
                # Record time even on failure to avoid hammering a broken remote
                self._last_fetch_time = datetime.now(tz=timezone.utc)
                self.hass.bus.async_fire(
                    EVENT_ERROR,
                    {"operation": "auto_fetch", "error": str(err)},
                )
                return False
