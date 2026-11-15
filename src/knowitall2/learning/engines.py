"""Which engine learns a chat: the one that did the work, on a lighter model, or whichever one can.

The user's decisions: "keep the same design of a lesser/cheaper model doing the learning, but it happens with the
app/agent doing the work. So codex work learns via a codex model. Claude work learns via a claude model."
(2026-11-13); and no engine may be a requirement: a user may have only Claude or only Codex, so each engine is a
learner in its own right, never gated on matching the other (2026-11-15).

``Engines`` is built once per learner run. ``for_source`` gives a transcript's engine with the reason for the
choice, which the call ledger records (``preferred_engine``, ``actual_engine``, ``route_reason``):

- Each chat learns on the engine of its source: Claude with ``claude_model``, Codex with ``codex_model`` at
  ``codex_effort``. When Codex no longer lists that model, its everyday model is used and the reason says so.
- When that engine is not installed, paused (signed out, a usage limit, until its reset), or became unusable in
  this run, the chat learns on the other engine: failover, both ways. A chat from an unknown source learns on the
  primary engine, or the other one when the primary cannot.
- With ``route_by_source`` off (the user's choice) every chat learns on the primary engine, with the same
  failover.
- Upkeep (maintenance, the catalog) runs on the primary engine, or the other one when the primary cannot
  (``upkeep``).
- Only when no engine can learn does a chat wait, with its place kept.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

CLAUDE, CODEX = "claude-cli", "codex-cli"
# The engine of each transcript source, as the transcript readers name sources (``Session.agent``).
SOURCE_ENGINES = {"claude-code": CLAUDE, "codex": CODEX}


def qualification_id(model: str, effort: str) -> str:
    """What the quality gate qualifies: the Codex model, its effort, and the prompts and schemas it was judged on."""

    return f"{model}@{effort}#{prompts_fingerprint()}"


def prompts_fingerprint() -> str:
    from . import cataloguer, extractor, prompts

    parts = [prompts.QUICK_PROMPT, prompts.QUICK_SCHEMA, prompts.FINAL_PROMPT, prompts.FINAL_SCHEMA,
             prompts.RECONCILE_PROMPT, prompts.RECONCILE_SCHEMA, extractor.CODEX_PREAMBLE, extractor.OUTPUT_SCHEMA,
             # the filing that follows learning from work runs on the chat's engine too
             cataloguer.FILE_PROMPT, cataloguer.FILE_SCHEMA, cataloguer.PROFILE_PROMPT, cataloguer.PROFILE_SCHEMA,
             cataloguer.REVIEW_PROMPT, cataloguer.REVIEW_SCHEMA,
             # how a Codex learning call is set up: the learning prompt as its base instructions (lean)
             "codex-context: lean"]
    text = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class Route:
    engine: Any            # the extractor to use; None when the chat must wait (its engine is unusable now)
    preferred: str | None  # the engine of the chat's source; None when the source is unknown
    reason: str


class Engines:
    """The engines one learner run may use, built when first needed."""

    def __init__(self, settings: Any, primary: Any, *, build: Callable[[str, str | None, str | None], Any] | None = None,
                 paused: Callable[[str], dict | None] | None = None) -> None:
        self.settings, self.primary = settings, primary
        self._build = build or _build
        self._paused = paused or _paused
        # ``learn --retry-backend``: the user asked to try a paused engine once; its pause is not a reason to route
        # around it (``availability.before_call`` spends the one try).
        self.retry = bool(getattr(primary, "retry_backend_once", False))
        self._engines: dict[str, Any] = {}
        self._eligible: dict[str, tuple[bool, str]] = {}
        self.models: dict[str, str] = {}   # how each engine's learning model was chosen, for the ledger and status
        self.blocked: dict[str, str] = {}

    # --- what one chat learns on

    def for_source(self, agent: str | None) -> Route:
        preferred = SOURCE_ENGINES.get(agent or "")
        primary = getattr(self.primary, "name", None)
        if preferred is None:
            first, reason = primary, "source unknown: primary engine"
        elif not getattr(self.settings, "route_by_source", True):
            first, reason = primary, "routing off: primary engine"
        else:
            first, reason = preferred, "routed to the chat's engine"
        # The primary as configured (its own model) when the chat goes to it; the source's learning model otherwise
        # (Codex's review of 372b11b: routing off must not swap a chosen model for ``claude_model``).
        as_primary = first == primary and (preferred is None or reason.startswith("routing off"))
        order = [name for name in (first, _other(first)) if name in (CLAUDE, CODEX)]
        problems: list[str] = []
        for position, backend in enumerate(order):
            route = self._try(backend, preferred, reason if position == 0 else f"{'; '.join(problems)}; on the other engine",
                              as_primary=as_primary and position == 0)
            if route.engine is not None:
                return route
            problems.append(route.reason)
        return Route(None, preferred, f"{'; '.join(problems) or 'no engine'}; no engine can learn it now")

    def upkeep(self) -> Any:
        """The engine for maintenance and the catalog: the primary as configured, or the other engine at its
        learning model when the primary cannot run now; None when neither can."""

        primary = getattr(self.primary, "name", None)
        if primary in (CLAUDE, CODEX) and self._unusable(primary) is None:
            return self.primary
        other = _other(primary) or CLAUDE
        return self._try(other, None, "upkeep").engine

    def _unusable(self, backend: str) -> str | None:
        """Why ``backend`` cannot run now, read before it is built (no engine process for a paused engine)."""

        if backend in self.blocked:
            return f"{_NAMES[backend]} unusable in this run ({self.blocked[backend]})"
        paused = None if self.retry else self._paused(backend)
        if paused:
            return f"{_NAMES[backend]} paused until {paused.get('retry_at')}"
        return None

    def _try(self, backend: str, preferred: str | None, reason: str, *, as_primary: bool = False) -> Route:
        problem = self._unusable(backend)
        if problem:
            return Route(None, preferred, problem)
        if as_primary and self.primary is not None:
            return Route(self.primary, preferred, reason)
        ok, why = self.eligible(backend)
        if not ok:
            return Route(None, preferred, why)
        note = self.models.get(backend)
        return Route(self.engine(backend), preferred, f"{reason} ({note})" if note else reason)

    # --- the engines

    def engine(self, backend: str) -> Any:
        """The extractor for ``backend`` at its learning model; the primary when it is the same engine and model."""

        if backend not in self._engines:
            model, effort = self._model(backend)
            primary = self.primary
            if (getattr(primary, "name", None) == backend and getattr(primary, "model", None) == model
                    and (backend != CODEX or getattr(primary, "effort", None) == effort)):
                self._engines[backend] = primary
            else:
                engine = self._build(backend, model, effort)
                if engine is not None and self.retry:
                    engine.retry_backend_once = True
                self._engines[backend] = engine
        return self._engines[backend]

    def _model(self, backend: str) -> tuple[str | None, str | None]:
        if backend == CODEX:
            return self.settings.codex_model or None, self.settings.codex_effort
        return self.settings.claude_model or None, None

    def eligible(self, backend: str) -> tuple[bool, str]:
        """Whether ``backend`` can learn chats on this computer, and why not; each engine on its own."""

        if backend not in self._eligible:
            self._eligible[backend] = self._check(backend)
        return self._eligible[backend]

    def _check(self, backend: str) -> tuple[bool, str]:
        if backend == CLAUDE:
            return (True, "") if self.engine(CLAUDE) is not None else (False, "Claude Code is not installed")
        engine = self.engine(CODEX)
        if engine is None:
            return False, "Codex is not installed"
        model = self.settings.codex_model
        # ``command.build_engine`` reads Codex's model list once and puts its everyday model in place of a renamed
        # or retired one, so learning never stops on a model name; the reason travels with every route.
        if model and getattr(engine, "model", model) != model:
            self.models[CODEX] = f"Codex no longer lists {model}; its default model {engine.model or 'is used'}"
        return True, ""

    def built(self, name: str) -> Any:
        """The engine already built for ``name`` in this run, if any."""

        return self._engines.get(name)

    def block(self, name: str, reason: str) -> None:
        """``name`` turned out unusable (signed out, a usage limit): its chats move to the other engine."""

        self.blocked[name] = str(reason)[:200]

    def all_blocked(self) -> bool:
        """Whether no engine is left that could learn: every installed engine and the primary blocked or paused
        (a blocked first engine must not stop chats a healthy one has not reached)."""

        primary = getattr(self.primary, "name", None)
        if primary is not None and primary not in (CLAUDE, CODEX) and primary not in self.blocked:
            return False   # an engine of another kind (a stand-in) is judged by this run's blocks only
        for backend in (CLAUDE, CODEX):
            # The same retry-aware reading as routing, and before anything is built: a paused engine is not built.
            if self._unusable(backend) is None and (backend == primary or self.eligible(backend)[0]):
                return False
        return True


_NAMES = {CLAUDE: "Claude Code", CODEX: "Codex"}


def _other(backend: str | None) -> str | None:
    return {CLAUDE: CODEX, CODEX: CLAUDE}.get(backend or "")


def _build(backend: str, model: str | None, effort: str | None) -> Any:
    # Through the one place engines are made (``command.build_engine``), so every route builds them alike.
    from .command import build_engine

    engine = build_engine(backend, model)
    if engine is not None and backend == CODEX and effort:
        engine.effort = effort
    return engine


def _paused(backend: str) -> dict | None:
    from . import availability

    return availability.active(backend)
