"""LOCAL PATCH (tycstation): apply a pull — restart, reload, or nothing.

The decision needs each file's old text, which only git holds, and a failed
reload must fall back to a restart, which an automation cannot catch; hence
it is done here rather than in an automation on the pull event.

A pulled ESPHome node config is compiled and flashed over the air after the
apply, through HA's own ESPHome dashboard link (the firmware update entities'
path to the Device Builder), one node at a time; the first failure stops the
rest and raises a notification (async_flash_esphome).
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .apply_plan import ESPHOME_ALL, ESPHOME_DIR, HA_WRITTEN, Plan, build_plan, read_changes
from .const import DOMAIN
from .git_manager import GitManager

_LOGGER = logging.getLogger(__name__)
_LOCK = f"{DOMAIN}_apply_lock"
# The commit HA runs: HEAD at boot, then each applied plan's head; pulls diff from it.
APPLIED = f"{DOMAIN}_applied_head"
# Set when an apply ends without a restart it needed: the base..merged range then
# holds pulled changes too, so every plan restarts until HA has (boot clears it).
RESTART_OWED = f"{DOMAIN}_restart_owed"
REFUSED_ID = f"{DOMAIN}_restart_refused"
RELOAD_TIMEOUT = 300
# ESPHome nodes a pull changed and that are not flashed yet; a restart resumes them.
ESPHOME_STORE = f"{DOMAIN}.esphome_owed"
ESPHOME_LOCK = f"{DOMAIN}_esphome_lock"
ESPHOME_FAILED_ID = f"{DOMAIN}_esphome_failed"
EVENT_ESPHOME = f"{DOMAIN}_esphome"
COMPILE_TIMEOUT = 1800  # a first build after an ESPHome bump compiles the framework
UPLOAD_TIMEOUT = 600
ONLINE_TIMEOUT = 300  # the new firmware has to boot and reconnect to HA's API
ONLINE_POLL = 1
# Template, platform, rest and intent_script reloads report bad config only here.
WATCHED_LOGGERS = (
    "homeassistant.config",
    "homeassistant.helpers.reload",
    "homeassistant.setup",
)


async def async_init_applied(hass: HomeAssistant, git: GitManager) -> None:
    """Once per run, before the first pull: at boot HA has just loaded HEAD. ESPHome
    nodes a restart cut off (or a pull before it owed) are flashed once HA has started."""
    if not hass.data.get(APPLIED):
        hass.data[APPLIED] = await git.get_head_sha()
        async_schedule_esphome(hass)


async def async_plan_pull(hass: HomeAssistant, git: GitManager, pre_pull_head: str) -> Plan:
    """Plan box-side commits since the last apply (minus files HA wrote) and the
    pull itself; a plan that cannot be built restarts."""
    plan = Plan()
    if owed := hass.data.get(RESTART_OWED):
        plan.add("(pull)", f"a restart is still owed ({owed})", None)
    try:
        plan.head = await git.get_head_sha()
        base = hass.data.get(APPLIED) or pre_pull_head
        merged = getattr(git, "last_pre_merge_head", "") or pre_pull_head
        changes: list[tuple[str, str | None, str | None]] = []
        # Box-side commits: HA wrote some files itself, and an ESPHome node edited
        # box-side was edited (and installed) in the Device Builder; only a pull flashes.
        for start, end, skip, box in ((base, merged, HA_WRITTEN, True), (merged, plan.head, (), False)):
            if start != end:
                paths = await git.diff_name_only(start, end, strict=True)
                paths = [p for p in paths if p not in skip and not (box and p.startswith(ESPHOME_DIR))]
                changes += await read_changes(git, start, end, paths)
        return await hass.async_add_executor_job(build_plan, changes, plan)
    except Exception as err:  # noqa: BLE001 - fail closed on anything unexpected
        plan.add("(pull)", f"planning failed: {type(err).__name__}: {err}", None)
        return plan


class _Errors(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage()[:300])


def _outside_new_config(hass: HomeAssistant, before: dict[str, str]) -> str | None:
    """A changed helper whose old value its new config rejects: a reload keeps it
    (or clamps it, or leaves the select unknown) where a restart's restore resets it."""
    for entity_id, old in before.items():
        if (state := hass.states.get(entity_id)) is None:
            continue
        attrs, domain = state.attributes, entity_id.split(".", 1)[0]
        try:
            if domain == "input_select":
                valid = old in attrs.get("options", [])
            elif domain == "input_number":
                valid = attrs["min"] <= float(old) <= attrs["max"]
            elif domain == "input_text":
                valid = attrs.get("min", 0) <= len(old) <= attrs.get("max", 255)
            else:
                valid = True
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            return f"{entity_id}: {old!r} is outside its new config"
    return None


def _helper_entities(hass: HomeAssistant, refs: list[str]) -> dict[str, str | None]:
    """Entity id per `<domain>.<yaml key>`; YAML helpers register unique_id = key."""
    registry = er.async_get(hass)
    entities: dict[str, str | None] = {}
    for ref in refs:
        domain, key = ref.split(".", 1)
        entities[ref] = registry.async_get_entity_id(domain, domain, key)
    return entities


def _unreachable_helper(hass: HomeAssistant, plan: Plan) -> str | None:
    """A changed helper without an entity, or a changed/removed one renamed in the registry:
    a rename since boot unlinks it from its collection (helpers/collection.py), so the
    reload never updates or removes it."""
    entities = _helper_entities(hass, [*plan.helpers, *plan.gone])
    for ref, entity_id in entities.items():
        if entity_id is None and ref in plan.helpers:
            return f"{ref} has no registered entity to check"
        if entity_id not in (None, ref):
            return f"{ref} is renamed to {entity_id}, which a reload may not reach"
    return None


async def _reload(hass: HomeAssistant, plan: Plan) -> str | None:
    """Run reloads in order; return why the first one failed, or None."""
    if unreachable := _unreachable_helper(hass, plan):
        return unreachable
    entities = _helper_entities(hass, plan.helpers)
    before = {
        entity_id: state.state
        for entity_id in entities.values()
        if (state := hass.states.get(entity_id))
        and state.state not in ("unknown", "unavailable")
    }
    errors = _Errors()
    loggers = [logging.getLogger(name) for name in WATCHED_LOGGERS]
    for watched in loggers:
        watched.addHandler(errors)
    try:
        for domain, service in plan.ordered_reloads():
            try:
                async with asyncio.timeout(RELOAD_TIMEOUT):
                    await hass.services.async_call(domain, service, blocking=True)
            except Exception as err:  # noqa: BLE001 - any failure, timeout included, restarts
                return f"{domain}.{service} raised {type(err).__name__}: {err}"
            if errors.messages:
                return f"{domain}.{service} logged: {errors.messages[0]}"
        if plan.dashboards:
            # This is the same force-load used by Lovelace's Refresh action. It
            # replaces the server cache before emitting lovelace_updated, which
            # makes existing non-editing browser tabs fetch the new configuration.
            # Match dashboard roots when possible; an included fragment has no
            # exposed dependency list, so it refreshes every YAML dashboard.
            # An !include change need not alter the root file's mtime. Storage
            # dashboards and frontend JS/CSS resources are untouched.
            from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415
            from homeassistant.components.lovelace.dashboard import LovelaceYAML  # noqa: PLC0415

            if (data := hass.data.get(LOVELACE_DATA)) is None:
                return "YAML dashboard refresh: lovelace is not loaded"
            dashboards = [d for d in data.dashboards.values() if isinstance(d, LovelaceYAML)]
            changed = {hass.config.path(path) for path in plan.dashboards}
            includes = bool(changed - {d.path for d in dashboards})
            try:
                async with asyncio.timeout(RELOAD_TIMEOUT):
                    for dashboard in dashboards:
                        if includes or dashboard.path in changed:
                            await dashboard.async_load(force=True)
            except Exception as err:  # noqa: BLE001 - same fallback as service reloads
                return f"YAML dashboard refresh raised {type(err).__name__}: {err}"
            if errors.messages:
                return f"YAML dashboard refresh logged: {errors.messages[0]}"
        return _outside_new_config(hass, before)
    finally:
        for watched in loggers:
            watched.removeHandler(errors)


async def _restart(hass: HomeAssistant, why: list[str]) -> None:
    _LOGGER.warning("Pull apply: restarting Home Assistant (%s)", "; ".join(why))
    try:
        await hass.services.async_call("homeassistant", "restart", blocking=True)
    except Exception as err:  # noqa: BLE001 - the core refuses on invalid config
        hass.data[RESTART_OWED] = str(err)[:200] or type(err).__name__  # '' would read as none owed
        _LOGGER.error("Pull apply: restart refused, the pull is not live: %s", err)
        persistent_notification.async_create(
            hass,
            f"Home Assistant refused the restart a pulled change needs, so it is "
            f"not live; every later pull restarts until one succeeds.\n\n{err}\n\n"
            + "\n".join(f"- {reason}" for reason in why),
            title="git-ha-ppens: Restart refused",
            notification_id=REFUSED_ID,
        )


async def async_apply(hass: HomeAssistant, plan: Plan) -> str:
    """Apply a plan, serialised; returns nothing, reloaded, restart or skipped.
    Its ESPHome nodes are owed first, then flashed now or after the restart."""
    await async_queue_esphome(hass, plan.esphome)
    outcome = await _apply(hass, plan)
    if plan.esphome and (outcome != "restart" or hass.data.get(RESTART_OWED)):
        async_schedule_esphome(hass)
    return outcome


async def _apply(hass: HomeAssistant, plan: Plan) -> str:
    async with hass.data.setdefault(_LOCK, asyncio.Lock()):
        if hass.state is not CoreState.running:
            return "skipped"
        why = plan.reasons
        if plan.restart:
            failure: str | None = "restart planned"
        elif owed := hass.data.get(RESTART_OWED):  # a plan made before the refusal
            failure = f"a restart is still owed ({owed})"
        elif missing := [a for a in plan.ordered_reloads() if not hass.services.has_service(*a)]:
            failure = f"{missing[0][0]}.{missing[0][1]} is not loaded"
        else:
            failure = await _reload(hass, plan) if plan.reloads or plan.dashboards else None
        if failure is None:
            if plan.head:
                hass.data[APPLIED] = plan.head
            outcome = "reloaded" if plan.reloads or plan.dashboards else "nothing"
            _LOGGER.info("Pull apply: %s (%s)", outcome, "; ".join(why) or "no change")
            return outcome
        if not plan.restart:
            why = [*why, failure]
        await _restart(hass, why)
        return "restart"


@callback
def async_schedule_apply(hass: HomeAssistant, plan: Plan) -> None:
    """Apply now, or once started: a pull during startup lands after YAML loaded."""

    async def _apply(hass: HomeAssistant) -> None:
        await async_apply(hass, plan)

    async_at_started(hass, _apply)


def _esphome_store(hass: HomeAssistant) -> Store[dict[str, list[str]]]:
    return Store(hass, 1, ESPHOME_STORE)


async def async_queue_esphome(hass: HomeAssistant, configurations: list[str]) -> None:
    """Owe these node configs (or ESPHOME_ALL) a flash; persisted across restarts."""
    if not configurations:
        return
    store = _esphome_store(hass)
    owed = (await store.async_load() or {}).get("owed", [])
    await store.async_save({"owed": sorted({*owed, *configurations})})


@callback
def async_schedule_esphome(hass: HomeAssistant) -> None:
    """Flash whatever is owed once HA has started (at boot: what a restart cut off)."""

    async def _start(hass: HomeAssistant) -> None:
        hass.async_create_background_task(
            async_flash_esphome(hass), f"{DOMAIN} ESPHome flash", eager_start=False
        )

    async_at_started(hass, _start)


def _dashboard(hass: HomeAssistant) -> Any:
    """HA's ESPHome dashboard link (esphome-ha-link to the Device Builder), or None."""
    from homeassistant.components.esphome.dashboard import (  # noqa: PLC0415 - only when flashing
        async_get_dashboard,
    )

    return async_get_dashboard(hass)


def _firmware(hass: HomeAssistant, name: str) -> tuple[bool, str] | None:
    """(connected, compilation time) of the HA ESPHome entry for node `name`, or None."""
    for entry in hass.config_entries.async_entries("esphome"):
        data = getattr(entry, "runtime_data", None)
        info = getattr(data, "device_info", None)
        if info is not None and info.name == name:
            return bool(data.available), info.compilation_time
    return None


async def _came_back(hass: HomeAssistant, name: str, before: tuple[bool, str]) -> bool:
    """The flashed node reconnected to HA: a new build, or connected again after a drop."""
    dropped = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ONLINE_TIMEOUT
    while loop.time() < deadline:
        now = _firmware(hass, name)
        if now is not None:
            dropped = dropped or not now[0]
            if now[0] and (dropped or now[1] != before[1]):
                return True
        await asyncio.sleep(ONLINE_POLL)
    return False


async def _flash(hass: HomeAssistant, dashboard: Any, device: dict[str, Any]) -> str | None:
    """Compile, upload over the air, wait for the node; why it failed, or None."""
    configuration, name = device["configuration"], device["name"]
    lines: deque[str] = deque(maxlen=15)
    try:
        if dashboard.supports_build_queue:
            async with asyncio.timeout(COMPILE_TIMEOUT):
                compiled = await dashboard.api.compile(configuration, lines.append)
        else:  # the HA update entities' lock: one compile at a time on an old builder
            async with hass.data.setdefault("esphome_update_lock", asyncio.Lock()):
                async with asyncio.timeout(COMPILE_TIMEOUT):
                    compiled = await dashboard.api.compile(configuration, lines.append)
        if not compiled:
            return "compile failed:\n" + "\n".join(lines)
        before = _firmware(hass, name)
        lines.clear()
        async with asyncio.timeout(UPLOAD_TIMEOUT):
            uploaded = await dashboard.api.upload(configuration, "OTA", lines.append)
        if not uploaded:
            return "upload failed:\n" + "\n".join(lines)
    except Exception as err:  # noqa: BLE001 - timeouts and dashboard errors fail the node
        return f"{type(err).__name__}: {err}"
    if before is None:
        _LOGGER.warning("ESPHome apply: %s has no HA entry, so its boot is not checked", name)
    elif not await _came_back(hass, name, before):
        return f"flashed, but {name} did not reconnect to HA within {ONLINE_TIMEOUT} s"
    return None


def _report(hass: HomeAssistant, configuration: str, result: str, reason: str = "") -> None:
    hass.bus.async_fire(
        EVENT_ESPHOME, {"configuration": configuration, "result": result, "reason": reason[:500]}
    )


async def async_flash_esphome(hass: HomeAssistant) -> None:
    """Flash the owed nodes one at a time; a node leaves the list once it is back
    online. The first failure stops the rest and clears the list (retry with the
    flash_esphome service or a new change); a restart mid-run resumes at boot."""
    async with hass.data.setdefault(ESPHOME_LOCK, asyncio.Lock()):
        store = _esphome_store(hass)
        owed = (await store.async_load() or {}).get("owed", [])
        if not owed:
            return
        dashboard = _dashboard(hass)
        try:
            if dashboard is None:
                raise RuntimeError("HA has no ESPHome dashboard link")
            devices = {d["configuration"]: d for d in (await dashboard.api.get_devices())["configured"]}
        except Exception as err:  # noqa: BLE001 - keep the list for the next pull or boot
            _LOGGER.error("ESPHome apply: dashboard unavailable, %s still owed: %s", owed, err)
            persistent_notification.async_create(
                hass,
                f"git-ha-ppens could not reach the ESPHome Device Builder, so these nodes "
                f"are not flashed yet; the next pull or restart tries again.\n\n{err}\n\n"
                + "\n".join(f"- {c}" for c in owed),
                title="git-ha-ppens: ESPHome flash waiting",
                notification_id=ESPHOME_FAILED_ID,
            )
            return
        queue = sorted(devices) if ESPHOME_ALL in owed else [c for c in owed if c != ESPHOME_ALL]
        await store.async_save({"owed": queue})
        for index, configuration in enumerate(queue):
            device = devices.get(configuration)
            if device is None:
                _LOGGER.warning("ESPHome apply: %s is not a Device Builder node, skipped", configuration)
                _report(hass, configuration, "skipped", "not a Device Builder node")
            else:
                _LOGGER.info("ESPHome apply: flashing %s", configuration)
                if failure := await _flash(hass, dashboard, device):
                    rest = queue[index + 1:]
                    await store.async_save({"owed": []})
                    _LOGGER.error("ESPHome apply: %s failed, not flashed: %s; %s",
                                  configuration, rest or "none", failure)
                    _report(hass, configuration, "failed", failure)
                    persistent_notification.async_create(
                        hass,
                        f"{configuration} was not flashed: {failure}\n\n"
                        + (f"Not flashed either: {', '.join(rest)}\n\n" if rest else "")
                        + "Fix and merge, or retry with the git_ha_ppens.flash_esphome service.",
                        title="git-ha-ppens: ESPHome flash failed",
                        notification_id=ESPHOME_FAILED_ID,
                    )
                    return
                _LOGGER.info("ESPHome apply: %s flashed and back online", configuration)
                _report(hass, configuration, "flashed")
            await store.async_save({"owed": queue[index + 1:]})
        persistent_notification.async_dismiss(hass, ESPHOME_FAILED_ID)
