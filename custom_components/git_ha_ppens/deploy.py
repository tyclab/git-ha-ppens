"""LOCAL PATCH (tycstation): apply a pull — restart, reload, or nothing.

The decision needs each file's old text, which only git holds, and a failed
reload must fall back to a restart, which an automation cannot catch; hence
this replaces the former packages/gitops.yaml automation.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import persistent_notification
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.start import async_at_started

from .apply_plan import HA_WRITTEN, Plan, build_plan, read_changes
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
# Template, platform, rest and intent_script reloads report bad config only here.
WATCHED_LOGGERS = (
    "homeassistant.config",
    "homeassistant.helpers.reload",
    "homeassistant.setup",
)


async def async_init_applied(hass: HomeAssistant, git: GitManager) -> None:
    """Once per run, before the first pull: at boot HA has just loaded HEAD."""
    if not hass.data.get(APPLIED):
        hass.data[APPLIED] = await git.get_head_sha()


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
        for start, end, skip in ((base, merged, HA_WRITTEN), (merged, plan.head, ())):
            if start != end:
                paths = await git.diff_name_only(start, end, strict=True)
                changes += await read_changes(git, start, end, [p for p in paths if p not in skip])
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
    """Apply a plan, serialised; returns nothing, reloaded, restart or skipped."""
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
            failure = await _reload(hass, plan) if plan.reloads else None
        if failure is None:
            if plan.head:
                hass.data[APPLIED] = plan.head
            outcome = "reloaded" if plan.reloads else "nothing"
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
