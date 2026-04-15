"""Runner — main execution loop for tero2."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import signal
import sys
from contextlib import suppress
from pathlib import Path

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.checkpoint import CheckpointManager
from tero2.config import Config, load_config
from tero2.constants import EXIT_LOCK_HELD, HARD_TIMEOUT_S
from tero2.disk_layer import DiskLayer
from tero2.errors import LockHeldError, RateLimitError
from tero2.escalation import (
    EscalationLevel,
    decide_escalation,
    execute_escalation,
)
from tero2.lock import FileLock
from tero2.notifier import Notifier, NotifyLevel
from tero2.providers.chain import ProviderChain
from tero2.providers.registry import create_provider
from tero2.reflexion import ReflexionContext, add_attempt
from tero2.state import AgentState, Phase
from tero2.stuck_detection import StuckSignal, check_stuck, update_tool_hash

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        project_path: Path,
        plan_file: Path,
        config: Config | None = None,
    ) -> None:
        self.project_path = project_path
        self.plan_file = plan_file
        self.config = config or load_config(project_path)
        self.disk = DiskLayer(project_path)
        self.checkpoint = CheckpointManager(
            self.disk, max_steps_per_task=self.config.retry.max_steps_per_task
        )
        self.notifier = Notifier(self.config.telegram)
        self.lock = FileLock(self.disk.lock_path)
        self.cb_registry = CircuitBreakerRegistry(
            failure_threshold=self.config.retry.cb_failure_threshold,
            recovery_timeout_s=self.config.retry.cb_recovery_timeout_s,
        )
        self._current_state: AgentState | None = None
        # Escalation state (instance-level — persists across _execute_plan calls)
        self._escalation_level = EscalationLevel.NONE
        self._div_steps = 0
        self._escalation_history: list[EscalationLevel] = []

    async def run(self) -> None:
        self.disk.init()
        _shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_signal() -> None:
            log.info("shutdown signal received")
            _shutdown_event.set()

        loop.add_signal_handler(signal.SIGTERM, _on_signal)
        loop.add_signal_handler(signal.SIGINT, _on_signal)

        try:
            self.lock.acquire()
            state = self.checkpoint.restore()
            self._current_state = state

            if state.phase == Phase.COMPLETED:
                return

            if state.phase in (Phase.IDLE, Phase.FAILED):
                state = self.checkpoint.mark_started(str(self.plan_file))
                self._current_state = state

            await self.notifier.notify("started", NotifyLevel.PROGRESS)
            await self._execute_plan(state, _shutdown_event)

        except LockHeldError:
            print("another tero2 instance is running")
            sys.exit(EXIT_LOCK_HELD)
        except Exception:
            if self._current_state and self._current_state.phase == Phase.RUNNING:
                with suppress(Exception):
                    self.checkpoint.mark_failed(self._current_state, "unexpected error")
            raise
        finally:
            with suppress(ValueError):
                loop.remove_signal_handler(signal.SIGTERM)
            with suppress(ValueError):
                loop.remove_signal_handler(signal.SIGINT)
            self.lock.release()

    async def _execute_plan(
        self, state: AgentState, shutdown_event: asyncio.Event | None = None
    ) -> None:
        plan_content = self.disk.read_plan(str(self.plan_file))
        if not plan_content or not plan_content.strip():
            state = self.checkpoint.mark_failed(state, "plan file is empty or missing")
            self._current_state = state
            await self.notifier.notify("failed — empty plan", NotifyLevel.ERROR)
            return
        retry_cfg = self.config.retry
        reflexion_ctx = ReflexionContext()
        max_attempts = min(
            self.config.retry.max_retries,
            self.config.reflexion.max_cycles + 1,
        )

        for attempt in range(state.retry_count, max_attempts):
            # ── MVP2: stuck detection + escalation ──────────────────
            inject_prompt = ""
            if attempt > 0:
                stuck = check_stuck(state, self.config.stuck_detection)
                if stuck.signal != StuckSignal.NONE:
                    action = decide_escalation(
                        stuck, self._escalation_level, self._div_steps, self.config.escalation
                    )
                    if action.level != EscalationLevel.NONE:
                        self._escalation_history.append(action.level)
                    state = await execute_escalation(
                        action,
                        state,
                        self.disk,
                        self.notifier,
                        self.checkpoint,
                        stuck_result=stuck,
                        escalation_history=self._escalation_history,
                    )
                    self._current_state = state
                    if action.should_pause:
                        return  # Level 3: paused, waiting for human
                    self._escalation_level = action.level
                    if action.level == EscalationLevel.DIVERSIFICATION:
                        self._div_steps += 1
                    elif action.level == EscalationLevel.BACKTRACK_COACH:
                        self._div_steps = 0
                    inject_prompt = action.inject_prompt

            if attempt > 0:
                wait = min(
                    retry_cfg.chain_retry_wait_s * retry_cfg.backoff_base ** (attempt - 1),
                    300,
                )
                jitter = random.uniform(0, retry_cfg.chain_retry_wait_s * 0.1)
                await asyncio.sleep(wait + jitter)

            override = await self._check_override()
            if override:
                self._handle_override(override, state)
                if state.phase == Phase.FAILED:
                    self._current_state = state
                    await self.notifier.notify("stopped by OVERRIDE.md", NotifyLevel.ERROR)
                    return
                if state.phase == Phase.PAUSED:
                    await self.notifier.notify(
                        "paused — remove PAUSE from OVERRIDE.md to resume",
                        NotifyLevel.STUCK,
                    )
                    while await self._override_contains_pause():
                        if shutdown_event and shutdown_event.is_set():
                            log.info("shutdown requested during PAUSE — exiting")
                            return
                        await asyncio.sleep(60)
                    log.info("PAUSE cleared — resuming")
                    state = self.checkpoint.mark_running(state)
                    self._current_state = state

            effective_plan = plan_content
            # ── MVP1: reflexion — inject failure context before plan ──
            reflexion_section = reflexion_ctx.to_prompt()
            if reflexion_section:
                effective_plan = f"{reflexion_section}\n\n---\n\n{plan_content}"
            if inject_prompt:
                effective_plan = f"## Notice\n{inject_prompt}\n\n---\n\n{effective_plan}"
            steer = await self._check_steer()
            if steer:
                log.info("STEER.md present — prepending to plan")
                effective_plan = f"## Steering\n{steer}\n\n---\n\n{effective_plan}"

            chain = self._build_chain(start_index=state.provider_index)
            success, captured_output = await self._run_agent(chain, effective_plan, state)

            if success:
                state = self.checkpoint.mark_completed(state)
                self._current_state = state
                await self.notifier.notify("done", NotifyLevel.DONE)
                return

            # ── MVP1: record failure in reflexion context ──
            reflexion_ctx = add_attempt(
                reflexion_ctx,
                builder_output=captured_output,
                verifier_feedback=f"Attempt {attempt + 1} did not succeed (no explicit verifier)",
            )

            state = self.checkpoint.increment_retry(state)
            self._current_state = state
            log.warning(f"attempt {attempt + 1} failed, retrying...")

        state = self.checkpoint.mark_failed(state, "all retries exhausted")
        self._current_state = state
        await self.notifier.notify(
            f"failed after {self.config.retry.max_retries} attempts", NotifyLevel.ERROR
        )

    def _build_chain(self, start_index: int = 0) -> ProviderChain:
        role = self.config.roles.get("executor")
        if role is None:
            from tero2.errors import ConfigError

            raise ConfigError("no executor role configured")
        all_names = [role.provider] + role.fallback
        names = all_names[start_index:]
        providers = []
        for i, n in enumerate(names):
            override = role.model if start_index + i == 0 else ""
            providers.append(
                create_provider(
                    n, self.config, model_override=override, working_dir=str(self.project_path)
                )
            )
        return ProviderChain(
            providers,
            cb_registry=self.cb_registry,
            rate_limit_max_retries=self.config.retry.rate_limit_max_retries,
            rate_limit_wait_s=self.config.retry.rate_limit_wait_s,
        )

    async def _run_agent(
        self,
        chain: ProviderChain,
        plan_content: str,
        state: AgentState,
    ) -> tuple[bool, str]:
        """Run the agent and return (success, captured_output).

        captured_output contains text collected from provider messages,
        used by reflexion to inform the next retry attempt.
        """
        base_provider_index = state.provider_index
        state_ref = [state]
        captured_parts: list[str] = []
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(state_ref))
        try:
            timeout = self.config.roles.get("executor", None)
            timeout_s = timeout.timeout_s if timeout else HARD_TIMEOUT_S
            async with asyncio.timeout(timeout_s):
                async for message in chain.run_prompt(plan_content):
                    # Capture text content for reflexion
                    text_content = (
                        getattr(message, "text", None)
                        or (message.get("text") if isinstance(message, dict) else None)
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
                            state, _ = update_tool_hash(state, str(message))
                        state = self.checkpoint.increment_step(state)
                        state_ref[0] = state
                        self._current_state = state
                        if msg_type == "tool_result":
                            if state.steps_in_task >= self.checkpoint.max_steps_per_task:
                                log.warning("STEP_LIMIT reached — aborting attempt")
                                return False, "\n".join(captured_parts)
                            mid_stuck = check_stuck(state, self.config.stuck_detection)
                            if mid_stuck.signal == StuckSignal.TOOL_REPEAT:
                                log.warning("TOOL_REPEAT detected — aborting attempt")
                                return False, "\n".join(captured_parts)
            return True, "\n".join(captured_parts)
        except TimeoutError:
            log.error("hard timeout reached")
            return False, "\n".join(captured_parts)
        except RateLimitError:
            log.error("all providers exhausted")
            return False, "\n".join(captured_parts)
        except Exception as exc:
            from tero2.providers.chain import _is_recoverable_error

            if not _is_recoverable_error(exc):
                raise
            log.error(f"agent error: {exc}")
            return False, "\n".join(captured_parts)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self, state_ref: list[AgentState]) -> None:
        interval = self.config.telegram.heartbeat_interval_s
        while True:
            await asyncio.sleep(interval)
            state = state_ref[0]
            await self.notifier.notify(
                f"still working — step {state.steps_in_task}, retry {state.retry_count}",
                NotifyLevel.HEARTBEAT,
            )

    async def _check_override(self) -> str | None:
        content = await asyncio.to_thread(self.disk.read_override)
        return content if content else None

    async def _check_steer(self) -> str | None:
        content = await asyncio.to_thread(self.disk.read_steer)
        return content if content else None

    async def _override_contains_pause(self) -> bool:
        content = await asyncio.to_thread(self.disk.read_override)
        return bool(self._RE_PAUSE.search(content)) if content else False

    _RE_STOP = re.compile(r"^\s*STOP\s*$", re.MULTILINE)
    _RE_PAUSE = re.compile(r"^\s*PAUSE\s*$", re.MULTILINE)

    def _handle_override(self, content: str, state: AgentState) -> None:
        if self._RE_STOP.search(content):
            self.checkpoint.mark_failed(state, "STOP directive in OVERRIDE.md")
            return
        if self._RE_PAUSE.search(content) and state.phase != Phase.PAUSED:
            self.checkpoint.mark_paused(state, "PAUSE directive in OVERRIDE.md")
