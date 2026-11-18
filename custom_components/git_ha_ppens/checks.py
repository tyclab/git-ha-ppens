"""Validate on-disk HA config before keeping a pull; callers roll back on failure."""

from __future__ import annotations

import logging
import os

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID = f"{DOMAIN}_pre_deploy_check_failed"
_SYNC_BLOCKED_NOTIFICATION_ID = f"{DOMAIN}_sync_blocked"


async def async_run_pre_deploy_check(
    hass: HomeAssistant, repo_path: str
) -> list[str]:
    """Return config errors, or [] when valid or outside HA's config directory."""
    # The HA config check validates hass.config.config_dir. It only makes sense
    # when the repository IS the Home Assistant config directory. If it points
    # somewhere else, skip the check (treat as "pass") instead of blocking.
    if os.path.realpath(repo_path) != os.path.realpath(hass.config.config_dir):
        _LOGGER.warning(
            "Pre-deploy check skipped: repository path %s is not the Home "
            "Assistant config directory %s",
            repo_path,
            hass.config.config_dir,
        )
        return []

    try:
        # Lazy import tolerates HA cores with a different helper signature.
        from homeassistant.helpers.check_config import async_check_ha_config_file

        result = await async_check_ha_config_file(hass)
    except Exception as err:  # noqa: BLE001 - fail closed: a gate that cannot run must block
        # Fail CLOSED: if the validator itself cannot run we cannot prove the
        # config is good, so block the pull (and roll back) rather than waving
        # unvalidated config through to the live config dir. The error is
        # surfaced via the persistent notification so the cause is visible.
        _LOGGER.error(
            "Pre-deploy check could not run (%s); blocking pull (fail-closed)",
            err,
        )
        return [f"Pre-deploy config check could not run: {err}"]

    errors = [err.message for err in result.errors]
    if errors:
        _LOGGER.warning(
            "Pre-deploy check found %d configuration error(s)", len(errors)
        )
    return errors


def notify_check_failed(hass: HomeAssistant, errors: list[str]) -> None:
    """Surface a blocked pull in the UI via a persistent notification."""
    bullet_list = "\n".join(f"- {err}" for err in errors) or "- (no details)"
    persistent_notification.async_create(
        hass,
        (
            "A pull from the remote repository was **blocked** because the "
            "Home Assistant configuration check failed. The repository was "
            "rolled back to the last working state.\n\n"
            f"Errors:\n{bullet_list}"
        ),
        title="git-ha-ppens: Pull blocked",
        notification_id=_NOTIFICATION_ID,
    )


def notify_sync_blocked(hass: HomeAssistant, operation: str, reason: str) -> None:
    """Surface an aborted pull/push in the UI via a persistent notification.

    LOCAL PATCH (tycstation): conflict and stale divergence used to be resolved
    silently (auto-merge, force-push). They now stop the sync, so they need to
    be visible — a fixed notification id keeps a stuck box to one notification.
    """
    persistent_notification.async_create(
        hass,
        (
            f"git-ha-ppens aborted the **{operation}**: `/config` and `main` "
            "are out of sync. Nothing was merged and nothing was force-pushed, "
            "so the working tree is unchanged — but the box is no longer "
            "syncing until this is resolved.\n\n"
            f"{reason}"
        ),
        title="git-ha-ppens: Sync blocked",
        notification_id=_SYNC_BLOCKED_NOTIFICATION_ID,
    )
