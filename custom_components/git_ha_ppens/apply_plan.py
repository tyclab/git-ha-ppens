"""LOCAL PATCH (tycstation): decide restart, reloads or nothing from a pull's diff.

Pure function of (path, old text, new text) with no Home Assistant import, so
the lightweight pytest job runs it. Every reload below is proven against the
pinned HA image by tests/gitops_reload_runtime.py; anything else restarts.
A changed ESPHome node also names the node to flash (deploy.py, after the apply).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

# A reload never fires `homeassistant: start` (HA's trigger arms only before
# EVENT_HOMEASSISTANT_STARTED), so a new or changed item with one restarts.
START_TRIGGER_RESTARTS = True
_START_EVENTS = ("homeassistant_start", "homeassistant_started")
# A restart applies these to an existing helper; a reload keeps its value.
HELPER_RESTART_FIELDS = ("initial", "has_date", "has_time")

# Top-level YAML keys whose reload re-reads them from disk.
KEY_RELOADS: dict[str, tuple[str, str]] = {
    domain: (domain, "reload")
    for domain in (
        "automation", "script", "scene", "template", "timer", "intent_script",
        "input_boolean", "input_button", "input_datetime", "input_number",
        "input_select", "input_text", "rest", "rest_command", "command_line",
        "mqtt",
    )
}
# `<entity domain>: - platform: <integration>` entries, reloaded per integration.
PLATFORM_RELOADS: dict[tuple[str, str], tuple[str, str]] = {
    ("sensor", "statistics"): ("statistics", "reload"),
    ("sensor", "derivative"): ("derivative", "reload"),
    ("light", "group"): ("group", "reload"),
}
# Files configuration.yaml includes whole; a key diff is pointless for them.
FILE_RELOADS: dict[str, tuple[str, str]] = {
    "automations.yaml": ("automation", "reload"),
    "scripts.yaml": ("script", "reload"),
    "scenes.yaml": ("scene", "reload"),
}
PREFIX_RELOADS: dict[str, tuple[str, str]] = {
    "blueprints/automation/": ("automation", "reload"),
    "blueprints/script/": ("script", "reload"),
    "blueprints/template/": ("template", "reload"),
    "custom_sentences/": ("conversation", "reload"),
}
# HA writes these itself (UI editors reload them, bans live in memory), so a
# box-side commit of them is already live; a pulled one is not.
HA_WRITTEN = frozenset({"automations.yaml", "scripts.yaml", "scenes.yaml", "ip_bans.yaml"})
# HA never reads these, or reads them per request (/local). YAML dashboards
# also need their native refresh so an already-open tab learns about a pull.
NOTHING_PREFIXES = (
    "tools/", "tests/", "docs/", "esphome/", ".claude/", "dashboards/", "www/",
)
NOTHING_FILES = frozenset({
    ".gitattributes", ".gitignore", ".gitlab-ci.yml", ".gitleaks.toml",
    ".gitleaksignore", ".lintsync", ".markdownlint-cli2.yaml",
    ".pre-commit-config.yaml", ".yamllint", "Makefile", "renovate.json",
    "secrets.yaml.example", "blueprints/.yamllint",
})
# HA never reads esphome/; a pulled node config is compiled and flashed over the
# air instead (deploy.async_flash_esphome). ESPHOME_ALL: a shared include or
# local component changed, so every node the builder knows.
ESPHOME_DIR = "esphome/"
ESPHOME_ALL = "*"
_ESPHOME_REPO_ONLY = ("secrets.yaml",)


def esphome_node(path: str) -> str | None:
    """The node config (`<node>.yaml`) a changed path flashes, ESPHOME_ALL, or None."""
    if not path.startswith(ESPHOME_DIR):
        return None
    rest = path[len(ESPHOME_DIR):]
    name = rest.rsplit("/", 1)[-1]
    if any(part.startswith(".") for part in rest.split("/")):  # .gitignore, builder state
        return None
    if name.endswith(".md") or name.startswith("LICENSE"):
        return None
    if "/" in rest:
        return ESPHOME_ALL
    if rest.endswith((".yaml", ".yml")) and rest not in _ESPHOME_REPO_ONLY:
        return rest
    return None


# Inputs before their consumers, automations last so they see the rest.
ORDER = (
    "input_boolean", "input_button", "input_datetime", "input_number",
    "input_select", "input_text", "timer", "template", "statistics",
    "derivative", "group", "rest", "command_line", "mqtt", "rest_command",
    "intent_script", "conversation", "scene", "script", "automation",
)


@dataclass(frozen=True)
class Tagged:
    """An HA tag (`!secret`, `!include…`) compared by name, never resolved."""

    tag: str
    value: Any


class _Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):  # libyaml when present
    pass


def _tagged(loader: _Loader, suffix: str, node: yaml.Node) -> Tagged:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = tuple(loader.construct_sequence(node, deep=True))
    else:
        value = loader.construct_mapping(node, deep=True)
    return Tagged("!" + suffix, value)


_Loader.add_multi_constructor("!", _tagged)


@dataclass
class Plan:
    """What a pull needs: a restart, an ordered set of reloads, or nothing."""

    restart: bool = False
    reloads: list[tuple[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # Changed / removed `<domain>.<key>` helper items deploy.py checks before trusting a reload.
    helpers: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    # ESPHome node configs to compile and flash once the apply is done.
    esphome: list[str] = field(default_factory=list)
    # Changed YAML dashboard roots/fragments; refresh also notifies open tabs.
    dashboards: list[str] = field(default_factory=list)
    head: str = ""

    def add(self, path: str, what: str, action: tuple[str, str] | None) -> None:
        if action is None:
            self.restart = True
            self.reasons.append(f"{path}: {what} -> restart")
            return
        if action not in self.reloads:
            self.reloads.append(action)
        self.reasons.append(f"{path}: {what} -> {action[0]}.{action[1]}")

    def ordered_reloads(self) -> list[tuple[str, str]]:
        """In ORDER; a domain outside it never gets a reload."""
        rank = {domain: i for i, domain in enumerate(ORDER)}
        return sorted(self.reloads, key=lambda action: rank[action[0]])

    def as_event(self) -> dict[str, Any]:
        return {
            "restart": self.restart,
            "reloads": [f"{d}.{s}" for d, s in self.ordered_reloads()],
            "reasons": list(self.reasons),
            "esphome": list(self.esphome),
            "dashboards": list(self.dashboards),
            "head": self.head,
        }


def needs_content(path: str) -> bool:
    """Only these paths are decided from their old and new text."""
    return path in ("configuration.yaml", "automations.yaml") or (
        path.startswith(("packages/", "blueprints/")) and path.endswith(".yaml")
    )


def _starts(item: Any) -> bool:
    """A HA start trigger, or triggers this diff cannot judge (blueprint, !input)."""
    if not isinstance(item, dict):
        return False
    if "use_blueprint" in item:
        return True
    triggers = item.get("triggers", item.get("trigger", []))
    for trig in triggers if isinstance(triggers, list) else [triggers]:
        if isinstance(trig, Tagged):
            return True
        if not isinstance(trig, dict):
            continue
        kind = trig.get("trigger", trig.get("platform"))
        events = trig.get("event_type")
        events = events if isinstance(events, list) else [events]
        if any(isinstance(v, Tagged) for v in (kind, trig.get("event"), *events)):
            return True
        if kind == "homeassistant" and trig.get("event") == "start":
            return True
        if kind == "event" and any(e in _START_EVENTS for e in events):
            return True
    return False


def _new_starting_item(old: Any, new: Any) -> bool:
    olds = old if isinstance(old, list) else [old]
    news = new if isinstance(new, list) else [new]
    return START_TRIGGER_RESTARTS and any(
        _starts(item) for item in news if item not in olds
    )


def _load(text: str | None) -> dict[str, Any]:
    if text is None:
        return {}
    data = yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    if data is None:
        return {}
    if isinstance(data, list):
        return {"items": data}
    if not isinstance(data, dict):
        raise ValueError("top level is not a mapping or list")
    return data


def _platforms(value: Any) -> dict[str, list[Any]] | None:
    """Group a platform list by platform, or None if it is not one."""
    if value is None:
        return {}
    entries = value if isinstance(value, list) else [value]
    grouped: dict[str, list[Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("platform"), str):
            return None
        grouped.setdefault(entry["platform"], []).append(entry)
    return grouped


def _diff_helpers(plan: Plan, olds: dict[str, Any], news: dict[str, Any]) -> None:
    """Across all files: restart for a changed `initial` or date/time shape, queue
    the other changed items (`<domain>.<key>`) for deploy.py's value check."""
    plan.gone.extend(sorted(set(olds) - set(news)))
    for ref in sorted(set(olds) & set(news)):
        before, after = olds[ref] or {}, news[ref] or {}
        if before == after:
            continue
        if not isinstance(before, dict) or not isinstance(after, dict):
            plan.add(ref, "helper item is not a mapping", None)
        elif any(before.get(f) != after.get(f) for f in HELPER_RESTART_FIELDS):
            plan.add(ref, "helper initial or date/time shape", None)
        else:
            plan.helpers.append(ref)


def _helper_items(data: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{key}.{ident}": item
        for key, items in data.items()
        if (str(key).startswith("input_") or key == "timer") and isinstance(items, dict)
        for ident, item in items.items()
    }


def _diff_key(plan: Plan, path: str, key: str, old: Any, new: Any) -> None:
    if key in KEY_RELOADS:
        if key in ("automation", "template") and _new_starting_item(old, new):
            plan.add(path, f"{key} item with a start trigger", None)
            return
        plan.add(path, key, KEY_RELOADS[key])
        return
    old_p, new_p = _platforms(old), _platforms(new)
    if old_p is None or new_p is None:
        plan.add(path, key, None)
        return
    for platform in sorted(set(old_p) | set(new_p)):
        if old_p.get(platform) != new_p.get(platform):
            what = f"{key} platform {platform}"
            plan.add(path, what, PLATFORM_RELOADS.get((key, platform)))


def _diff_yaml(
    plan: Plan, path: str, old: str | None, new: str | None, helpers: tuple[dict, dict]
) -> None:
    try:
        before, after = _load(old), _load(new)
    except (yaml.YAMLError, ValueError) as err:
        plan.add(path, f"unparsable YAML ({type(err).__name__})", None)
        return
    helpers[0].update(_helper_items(before))
    helpers[1].update(_helper_items(after))
    if path == "automations.yaml":
        if not {*before, *after} <= {"items"}:
            plan.add(path, "not a list of automations", None)
            return
        _diff_key(plan, path, "automation", before.get("items"), after.get("items"))
        return
    if path.startswith("blueprints/"):
        if before == after:
            return
        prefix = next((p for p in PREFIX_RELOADS if path.startswith(p)), None)
        starts = _new_starting_item(None, before) or _new_starting_item(None, after)
        action = None if starts or prefix is None else PREFIX_RELOADS[prefix]
        plan.add(path, "blueprint with a start trigger" if starts else "blueprint", action)
        return
    for key in sorted(set(before) | set(after), key=str):
        if before.get(key) != after.get(key):
            _diff_key(plan, path, str(key), before.get(key), after.get(key))


async def read_changes(
    git: Any, base: str, head: str, paths: Iterable[str]
) -> list[tuple[str, str | None, str | None]]:
    """Each path with its text at base and head where needs_content, via git.show_file."""
    changes: list[tuple[str, str | None, str | None]] = []
    for path in paths:
        if needs_content(path):
            changes.append((path, await git.show_file(base, path), await git.show_file(head, path)))
        else:
            changes.append((path, None, None))
    return changes


def build_plan(
    changes: Iterable[tuple[str, str | None, str | None]], plan: Plan | None = None
) -> Plan:
    """Plan for (path, old text, new text); text is only read for needs_content."""
    plan = plan if plan is not None else Plan()
    spans: dict[str, list[str | None]] = {}
    for path, old, new in changes:  # a path in both ranges: its first old, last new
        spans.setdefault(path, [old, new])[1] = new
    helpers: tuple[dict[str, Any], dict[str, Any]] = ({}, {})
    for path, (old, new) in spans.items():
        if (node := esphome_node(path)) and node not in plan.esphome:
            plan.esphome.append(node)
            what = "every node" if node == ESPHOME_ALL else node
            plan.reasons.append(f"{path}: ESPHome -> flash {what}")
        if path.endswith(".md"):
            continue
        if path.startswith("dashboards/") and path.endswith((".yaml", ".yml")):
            plan.dashboards.append(path)
            plan.reasons.append(f"{path}: YAML dashboards -> refresh")
            continue
        if path in NOTHING_FILES or path.startswith(NOTHING_PREFIXES):
            continue
        if needs_content(path):
            _diff_yaml(plan, path, old, new, helpers)
        elif path in FILE_RELOADS:
            plan.add(path, "file", FILE_RELOADS[path])
        else:
            prefix = next((p for p in PREFIX_RELOADS if path.startswith(p)), None)
            what = "included file" if prefix else "unclassified path"
            plan.add(path, what, PREFIX_RELOADS.get(prefix) if prefix else None)
    _diff_helpers(plan, *helpers)
    return plan
