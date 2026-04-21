"""Phase execution context — shared state for all SORA phase handlers.

RunnerContext carries every infrastructure reference needed by phase
handlers so that each handler receives a single ``ctx`` argument instead
of a long parameter list.

Helper functions:
    _read_next_slice(ctx)          — claim next unclaimed slice from TASK_QUEUE.md
    _load_slice_plan_from_disk(ctx, slice_id) — crash-recovery SlicePlan loader
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from tero2.checkpoint import CheckpointManager
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config
from tero2.constants import HARD_TIMEOUT_S
from tero2.disk_layer import DiskLayer
from tero2.errors import RateLimitError
from tero2.escalation import EscalationLevel
from tero2.events import EventDispatcher
from tero2.notifier import Notifier, NotifyLevel
from tero2.persona import PersonaRegistry
from tero2.players.architect import SlicePlan, _parse_slice_plan
from tero2.providers.chain import ProviderChain, _is_recoverable_error
from tero2.providers.registry import create_provider
from tero2.state import AgentState
from tero2.stuck_detection import StuckSignal, check_stuck, update_tool_hash

log = logging.getLogger(__name__)


# ── Phase result ──────────────────────────────────────────────────────────


@dataclass
class PhaseResult:
    """Common result returned by every SORA phase handler.

    ``data`` holds phase-specific payloads — e.g. :class:`~tero2.players.architect.SlicePlan`
    for :func:`~tero2.phases.architect_phase.run_architect`, or the hardened
    plan string for :func:`~tero2.phases.harden_phase.run_harden`.
    """

    success: bool
    error: str = ""
    data: Any = None


# ── Runner context ────────────────────────────────────────────────────────


@dataclass
class RunnerContext:
    """Shared execution context for all SORA phase handlers.

    Carries all runner state, infrastructure references, and escalation
    tracking.  Phase handlers receive this as their sole argument.

    Construct via ``Runner._build_runner_context()`` — do not call
    directly from production code.
    """

    config: Config = field(default_factory=Config)
    disk: DiskLayer | None = None
    checkpoint: CheckpointManager | None = None
    notifier: Notifier | None = None
    state: AgentState = field(default_factory=AgentState)
    cb_registry: CircuitBreakerRegistry = field(default_factory=CircuitBreakerRegistry)
    project_path: str = ""
    personas: PersonaRegistry = field(default_factory=PersonaRegistry)
    assembler: Any = None
    milestone_path: str = "milestones/M001"
    shutdown_event: asyncio.Event | None = None
    dispatcher: EventDispatcher | None = None
    command_queue: asyncio.Queue | None = None
    escalation_level: EscalationLevel = EscalationLevel.NONE
    div_steps: int = 0
    escalation_history: list[EscalationLevel] = field(default_factory=list)

    def reset(self) -> None:
        self.escalation_level = EscalationLevel.NONE
        self.div_steps = 0
        self.escalation_history = []

    # ── Chain builder ─────────────────────────────────────────────────────

    def build_chain(self, role_name: str) -> ProviderChain:
        """Build a :class:`~tero2.providers.chain.ProviderChain` for *role_name*.

        Replaces the hardcoded ``"executor"`` lookup in ``Runner._build_chain()``.
        Uses the role's configured provider + fallbacks, applying the role's
        model override only on the primary provider.

        Raises:
            :class:`~tero2.errors.ConfigError`: when *role_name* is not in config.
        """
        from tero2.errors import ConfigError

        role_cfg = self.config.roles.get(role_name)
        if role_cfg is None:
            raise ConfigError(f"no {role_name!r} role configured")

        all_names = [role_cfg.provider] + role_cfg.fallback
        providers = [
            create_provider(
                name,
                self.config,
                model_override=role_cfg.model if i == 0 else "",
                working_dir=str(self.disk.project_path) if self.disk is not None else "",
            )
            for i, name in enumerate(all_names)
        ]
        return ProviderChain(
            providers,
            cb_registry=self.cb_registry,
            rate_limit_max_retries=self.config.retry.rate_limit_max_retries,
            rate_limit_wait_s=self.config.retry.rate_limit_wait_s,
        )

    # ── Agent runner ──────────────────────────────────────────────────────

    async def run_agent(
        self,
        chain: ProviderChain,
        prompt_text: str,
        *,
        role: str = "executor",
    ) -> tuple[bool, str]:
        """Run the agent and return ``(success, captured_output)``.

        Extracted from ``Runner._run_agent()``.  Handles per-step stuck
        detection, step tracking, and heartbeat.  Updates ``self.state``
        in place after each step.

        Args:
            chain:       Pre-built :class:`~tero2.providers.chain.ProviderChain`.
            prompt_text: Full prompt to send.
            role:        Role name used to look up the timeout from config.

        Returns:
            ``(True, output)`` on success, ``(False, partial_output)`` on failure.
        """
        state = self.state
        base_provider_index = state.provider_index
        state_ref: list[AgentState] = [state]
        captured_parts: list[str] = []

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(state_ref))
        try:
            role_cfg = self.config.roles.get(role)
            timeout_s = role_cfg.timeout_s if role_cfg else HARD_TIMEOUT_S

            async with asyncio.timeout(timeout_s):
                async for message in chain.run_prompt(prompt_text):
                    # Extract text from three provider output shapes:
                    #   1. plain str
                    #   2. dict with "text"/"content" key
                    #   3. object with .text / .content attribute
                    if isinstance(message, str):
                        text_content = message
                    elif isinstance(message, dict):
                        text_content = message.get("text") or message.get("content") or ""
                    else:
                        text_content = (
                            getattr(message, "text", None)
                            or getattr(message, "content", None)
                            or ""
                        )
                    if text_content:
                        captured_parts.append(text_content)

                    msg_type = getattr(message, "type", None) or (
                        message.get("type") if isinstance(message, dict) else None
                    )
                    if msg_type in ("tool_result", "turn_end"):
                        state.provider_index = base_provider_index + chain.current_provider_index
                        if msg_type == "tool_result":
                            state, _ = update_tool_hash(state, text_content or str(message))
                        if self.checkpoint is not None:
                            state = self.checkpoint.increment_step(state)
                        state_ref[0] = state
                        self.state = state

                        if msg_type == "tool_result":
                            mid_stuck = check_stuck(state, self.config.stuck_detection)
                            if mid_stuck.signal == StuckSignal.STEP_LIMIT:
                                log.warning("STEP_LIMIT reached — aborting attempt")
                                return False, "\n".join(captured_parts)
                            if self.checkpoint is not None and state.steps_in_task >= self.checkpoint.max_steps_per_task:
                                log.warning("STEP_LIMIT (max_steps) reached — aborting")
                                return False, "\n".join(captured_parts)
                            if mid_stuck.signal == StuckSignal.TOOL_REPEAT:
                                log.warning("TOOL_REPEAT detected — aborting attempt")
                                return False, "\n".join(captured_parts)

            return True, "\n".join(captured_parts)

        except TimeoutError:
            log.error("hard timeout reached after %ds", timeout_s)
            return False, "\n".join(captured_parts)
        except RateLimitError:
            log.error("all providers in chain exhausted")
            return False, "\n".join(captured_parts)
        except Exception as exc:
            if not _is_recoverable_error(exc):
                raise
            log.error("agent error: %s", exc)
            return False, "\n".join(captured_parts)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _heartbeat_loop(self, state_ref: list[AgentState]) -> None:
        """Send Telegram heartbeat on the configured interval."""
        interval = self.config.telegram.heartbeat_interval_s
        while True:
            await asyncio.sleep(interval)
            s = state_ref[0]
            if self.notifier is not None:
                await self.notifier.notify(
                    f"still working — step {s.steps_in_task}, retry {s.retry_count}",
                    NotifyLevel.HEARTBEAT,
                )


# ── Slice helpers (used by runner SORA loop) ──────────────────────────────


def _read_next_slice(ctx: RunnerContext) -> str | None:
    """Claim the first unclaimed slice from ``strategic/TASK_QUEUE.md``.

    Finds the first ``[ ]`` item, rewrites it as ``[~]`` (in-progress),
    extracts the slice ID (e.g. ``S02``), and returns it.  Returns
    ``None`` when there are no remaining unclaimed slices.

    The ``[~]`` marker prevents the same slice from being double-claimed
    on crash recovery or concurrent reads.
    """
    import re

    content = ctx.disk.read_file("strategic/TASK_QUEUE.md")
    if not content:
        return None

    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if "[ ]" not in line:
            continue
        m = re.search(r"\bS\d+\b", line)
        if not m:
            continue
        slice_id = m.group(0)
        lines[i] = line.replace("[ ]", "[~]", 1)
        ctx.disk.write_file("strategic/TASK_QUEUE.md", "".join(lines))
        return slice_id

    return None


def _load_slice_plan_from_disk(ctx: RunnerContext, slice_id: str) -> SlicePlan:
    """Load a :class:`~tero2.players.architect.SlicePlan` from disk for crash recovery.

    Reads ``{milestone_path}/{slice_id}/{slice_id}-PLAN.md`` and parses it.

    Raises:
        ValueError: When the plan file is missing or empty (A2 — Architect may have
            crashed before writing the plan; the caller must re-run Architect rather
            than proceeding to Execute with an empty plan).
    """
    plan_path = f"{ctx.milestone_path}/{slice_id}/{slice_id}-PLAN.md"
    content = ctx.disk.read_file(plan_path)
    if not content:
        raise ValueError(
            f"plan file missing: {plan_path} — Architect may have crashed before "
            f"writing the plan. Re-run Architect before proceeding to Execute."
        )
    return _parse_slice_plan(content, slice_id, ctx.milestone_path)


# Alias kept for backward compatibility — callers that expect the old silent
# empty-SlicePlan behavior should migrate to _load_slice_plan_from_disk.
def _load_slice_plan_from_disk_safe(ctx: RunnerContext, slice_id: str) -> SlicePlan:
    """Like _load_slice_plan_from_disk but returns empty SlicePlan instead of raising."""
    try:
        return _load_slice_plan_from_disk(ctx, slice_id)
    except ValueError:
        return SlicePlan(
            slice_id=slice_id,
            slice_dir=f"{ctx.milestone_path}/{slice_id}",
        )
