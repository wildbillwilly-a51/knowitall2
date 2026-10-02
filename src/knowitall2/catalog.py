"""What KnowItAll2 knows, organized by system: the vocabulary, profiles, status, and the user's additions.

A system is anything agents work with or on: a service, a server, a device, a
piece of software, a project, or a practice. Each memory is filed under at
most one system, in one part of its profile (a facet). The background catalog
pass (``learning.cataloguer``) files memories and writes each system's plain
summary and what is missing. This module turns that into profiles, without
any model calls.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store

AREAS = (
    "Accounts and sign-in", "Servers and virtual machines", "Networking", "Storage and backups",
    "Home, cameras, and media", "Code and deployment", "AI and developer tools", "Apps and websites",
    "Projects and practices", "Other",
)
KINDS = ("service", "server", "device", "software", "project", "practice", "other")
INFRASTRUCTURE_KINDS = ("service", "server", "device", "software")
FACETS: dict[str, str] = {
    "about": "What it is",
    "where": "Where it is",
    "access": "How agents reach it",
    "signin": "Where the sign-in is kept",
    "can_do": "What agents can do with it",
    "howto": "How-tos",
    "rule": "Your rules",
    "decision": "Decisions",
    "lesson": "Lessons learned",
    "status": "Current state",
    "other": "Other notes",
}
# What a service, server, device, or software profile needs before it is complete.
ESSENTIAL_FACETS = ("where", "access", "signin")
# Evidence that agents really work with a system: an observed memory in one of these parts.
WORKING_FACETS = ("access", "can_do", "howto", "where")
READY_DAYS = 90
FACET_KINDS = {"howto": "procedure", "rule": "rule", "decision": "decision", "lesson": "lesson"}
_NON_WORD = re.compile(r"[\W_]+")


def area_for(kind: str, area: object) -> str:
    """The area a system is shown in: every project and practice together, others as filed."""

    if kind in ("project", "practice"):
        return "Projects and practices"
    return area if area in AREAS else "Other"


def normalize_name(name: str) -> str:
    return " ".join(_NON_WORD.split(name.casefold())).strip()


def system_id_for(name: str) -> str:
    return "sys-" + hashlib.sha256(normalize_name(name).encode("utf-8")).hexdigest()[:12]


def find_system(systems: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The system called ``name`` or known by it as an alias."""

    wanted = normalize_name(name)
    if not wanted:
        return None
    for system in systems:
        if wanted == normalize_name(system["name"]) or wanted in (normalize_name(alias) for alias in system["aliases"]):
            return system
    return None


def profile_fingerprint(records: list[dict[str, Any]]) -> str:
    """Identifies which memories a system's summary was written from, so it is rewritten only after changes."""

    body = "\n".join(sorted(f"{item['id']}:{item['facet']}" for item in records))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def profile(store: Store, system: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """One system's profile: its memories by part, what is missing, its status, and when it was last seen working."""

    moment = now or datetime.now(timezone.utc)
    records = store.system_records(system["id"])
    by_facet: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        by_facet.setdefault(item["facet"] if item["facet"] in FACETS else "other", []).append(item)
    infrastructure = system["kind"] in INFRASTRUCTURE_KINDS
    missing = [facet for facet in ESSENTIAL_FACETS if infrastructure and facet not in by_facet]
    seen = max(
        (item["confirmed_at"] or item["created_at"] for item in records
         if item["verification"] == "observed" and item["facet"] in WORKING_FACETS),
        default=None,
    )
    projects: dict[str, int] = {}
    for item in records:
        if item["project_id"]:
            projects[item["project_id"]] = projects.get(item["project_id"], 0) + 1
    return {
        **system,
        "memories": len(records),
        "facets": [
            {"facet": facet, "label": label, "memories": [_brief(item) for item in by_facet[facet]]}
            for facet, label in FACETS.items() if facet in by_facet
        ],
        "missing": [{"facet": facet, "label": FACETS[facet]} for facet in missing],
        "last_seen_working": seen,
        "status": status_of(system["kind"], missing, seen, len(records), moment),
        "main_project": max(projects, key=projects.get) if projects else None,
    }


def status_of(kind: str, missing: list[str], seen: str | None, count: int, now: datetime) -> dict[str, str]:
    """A plain status: ready to use, partly known, or only mentioned; projects and practices have none."""

    if kind not in INFRASTRUCTURE_KINDS:
        return {"key": kind, "label": "Project" if kind == "project" else "Practice" if kind == "practice" else "Notes"}
    recent = seen is not None and _parse(seen) >= now - timedelta(days=READY_DAYS)
    if not missing and recent:
        return {"key": "ready", "label": "Ready to use"}
    if count <= 2 and len(missing) == len(ESSENTIAL_FACETS):
        return {"key": "mentioned", "label": "Only mentioned"}
    return {"key": "partial", "label": "Partly known"}


def overview(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Every system, grouped by area, with its status and how much is known and missing."""

    counts = store.system_memory_counts()
    areas: dict[str, list[dict[str, Any]]] = {}
    for system in store.systems_list():
        if not counts.get(system["id"]):
            continue
        item = profile(store, system, now=now)
        areas.setdefault(area_for(system["kind"], system["area"]), []).append({
            "id": item["id"], "name": item["name"], "kind": item["kind"], "summary": item["summary"],
            "status": item["status"], "memories": item["memories"],
            "missing": len(item["missing"]) + len(item["gaps"]),
        })
    return {
        "areas": [{"area": area, "systems": sorted(areas[area], key=lambda entry: entry["name"].casefold())}
                  for area in AREAS if area in areas],
        "general": counts.get(None, 0),
        "unfiled": store.count_unnoted(),
    }


def tell(memory: Any, system: dict[str, Any], facet: str, text: str) -> Any:
    """The user fills in part of a system's profile, in their own words."""

    if facet not in FACETS:
        raise ValueError(f"unknown part {facet!r}")
    result = memory.remember(
        text, kind=FACET_KINDS.get(facet, "fact"), subjects=[system["name"]], scope="global", source="user",
        detect_project=False,
    )
    headline = " ".join(text.split())
    memory.store.set_note(
        result.record.id, headline=headline if len(headline) <= 120 else headline[:117].rstrip() + "...",
        system_id=system["id"], facet=facet, written_by="user", now=memory.now(),
    )
    return result


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in (
        "id", "headline", "text", "kind", "verification", "created_at", "confirmed_at", "source_agent",
        "source_kind", "scope", "project_name", "recall_count",
    )}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
