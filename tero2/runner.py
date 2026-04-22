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
from tero2.constants import EXIT_LOCK_HELD
from tero2.disk_layer import DiskLayer
from tero2.errors import LockHeldError
from tero2.escalation import (
    EscalationAction,
    EscalationLevel,
    decide_escalation,
    execute_escalation,
)
from tero2.events import Command, EventDispatcher, make_event
from tero2.notifier import Notifier, NotifyLevel
from tero2.phases import run_architect, run_coach, run_execute, run_harden, run_scout
from tero2.phases.context import (
    RunnerContext,
    _load_slice_plan_from_disk,
    _load_slice_plan_from_disk_safe,
    _read_next_slice,
)
from tero2.project_lock import ProjectLock
from tero2.providers.chain import ProviderChain
from tero2.providers.registry import create_provider
from tero2.reflexion import MAX_BUILDER_OUTPUT_CHARS, ReflexionContext, add_attempt
from tero2.state import SORA_PHASE_ORDER, AgentState, Phase, SoraPhase
from tero2.stuck_detection import StuckSignal, check_stuck
from tero2.triggers import CoachTrigger

log = logging.getLogger(__name__)

_IDLE_POLL_S = 60.0


_PHASE_ORDER = SORA_PHASE_ORDER


def _phase_already_done(current: SoraPhase, candidate: SoraPhase) -> bool:
    try:
        return _PHASE_ORDER.index(candidate) < _PHASE_ORDER.index(current)
    except ValueError:
        return False


class Runner:
    def __init__(
        self,
        project_path: Path,
        plan_file: Path | None = None,
        config: Config | None = None,
        *,
        dispatcher: EventDispatcher | None = None,
        command_queue: asyncio.Queue[Command] | None = None,
    ) -> None:
        self.project_path = project_path
        self.plan_file = plan_file
        self.config = config or load_config(project_path)
        self.disk = DiskLayer(project_path)
        self.checkpoint = CheckpointManager(
            self.disk, max_steps_per_task=self.config.retry.max_steps_per_task
        )
        self.notifier = Notifier(self.config.telegram)
        self.lock = ProjectLock(self.disk.lock_path)
        self.cb_registry = CircuitBreakerRegistry(
            failure_threshold=self.config.retry.cb_failure_threshold,
            recovery_timeout_s=self.config.retry.cb_recovery_timeout_s,
        )
        self._current_state: AgentState | None = None
        self._dispatcher = dispatcher
        self._command_queue = command_queue
        self._ctx: RunnerContext | None = None
        # Set True by a `skip_task` TUI command; consumed (and cleared) by
        # execute_phase at the next attempt boundary to advance past the
        # currently-running task without waiting for its natural completion.
        self._skip_current_task: bool = False

    async def run(self) -> None:
        _shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_signal() -> None:
            log.info("shutdown signal received")
            _shutdown_event.set()

        loop.add_signal_handler(signal.SIGTERM, _on_signal)
        loop.add_signal_handler(signal.SIGINT, _on_signal)

        self.disk.init()

        try:
            self.lock.acquire()
            state = self.checkpoint.restore()
            self._current_state = state

            if state.phase == Phase.COMPLETED:
                await self._idle_loop(_shutdown_event)
                return

            if state.phase in (Phase.IDLE, Phase.FAILED):
                if self.plan_file is None:
                    await self._idle_loop(_shutdown_event)
                    return
                if not self.plan_file.is_file():
                    log.error("plan file not found: %s", self.plan_file)
                    await self._idle_loop(_shutdown_event)
                    return
                state = self.checkpoint.mark_started(str(self.plan_file))
                self._current_state = state
                await self.notifier.notify("started", NotifyLevel.PROGRESS)
            elif state.phase in (Phase.RUNNING, Phase.PAUSED):
                if not state.plan_file:
                    await self._idle_loop(_shutdown_event)
                    return
                if state.phase == Phase.PAUSED:
                    state = self.checkpoint.mark_running(state)
                    self._current_state = state
                await self.notifier.notify("resumed", NotifyLevel.PROGRESS)

            await self._execute_plan(state, _shutdown_event)
            await self._idle_loop(_shutdown_event)

        except LockHeldError:
            print("another tero2 instance is running")
            sys.exit(EXIT_LOCK_HELD)
        except Exception:
            if self._current_state and self._current_state.phase == Phase.RUNNING:
                with suppress(Exception):
                    self.checkpoint.mark_failed(self._current_state, "unexpected error")
                with suppress(Exception):
                    await self._emit_error("unexpected fatal error")
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
        """Top-level dispatcher: SORA pipeline for builder configs, legacy otherwise."""
        if "builder" in self.config.roles:
            await self._execute_sora(state, shutdown_event)
        else:
            await self._execute_legacy(state, shutdown_event)

    async def _execute_legacy(
        self, state: AgentState, shutdown_event: asyncio.Event | None = None
    ) -> None:
        ctx = self._build_runner_context(state, shutdown_event)
        ctx.reset()
        self._ctx = ctx
        await self._run_legacy_agent(ctx, shutdown_event)

    def _build_runner_context(
        self, state: AgentState, shutdown_event: asyncio.Event | None
    ) -> RunnerContext:
        return RunnerContext(
            config=self.config,
            disk=self.disk,
            checkpoint=self.checkpoint,
            notifier=self.notifier,
            state=state,
            cb_registry=self.cb_registry,
            project_path=str(self.project_path),
            shutdown_event=shutdown_event,
            dispatcher=self._dispatcher,
            command_queue=self._command_queue,
        )

    async def _emit_phase(self, phase: SoraPhase) -> None:
        if self._dispatcher is not None:
            await self._dispatcher.emit(
                make_event(
                    "phase_change",
                    role="runner",
                    data={"sora_phase": phase.value},
                    priority=True,
                )
            )

    async def _emit_done(self) -> None:
        if self._dispatcher is not None:
            await self._dispatcher.emit(make_event("done", role="runner", priority=True))

    async def _emit_error(self, msg: str) -> None:
        # A48: use getattr so this works even when Runner is constructed via
        # __new__ without calling __init__ (e.g. in tests).
        dispatcher = getattr(self, "_dispatcher", None)
        if dispatcher is not None:
            await dispatcher.emit(
                make_event("error", role="runner", data={"message": msg}, priority=True)
            )

    async def _drain_commands(self, state: AgentState) -> tuple[AgentState, bool]:
        """Drain command queue at a phase boundary.

        Returns (updated_state, should_continue).
        On stop/pause commands the state is persisted and should_continue=False.
        Commands whose kind is not recognised by this loop are NOT silently
        dropped — they produce a warning so that dead TUI bindings surface.
        """
        if self._command_queue is None:
            return state, True
        while not self._command_queue.empty():
            try:
                cmd = self._command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if cmd.kind == "stop":
                state = self.checkpoint.mark_failed(
                    state, f"stopped via {cmd.source or 'command'}"
                )
                self._current_state = state
                return state, False
            if cmd.kind == "pause":
                state = self.checkpoint.mark_paused(state, f"paused via {cmd.source or 'command'}")
                self._current_state = state
                return state, False
            if cmd.kind == "switch_provider":
                role = cmd.data.get("role", "")
                provider = cmd.data.get("provider", "")
                model = cmd.data.get("model", "")
                if role and provider and role in self.config.roles:
                    self.config.roles[role].provider = provider
                    if "model" in cmd.data:
                        self.config.roles[role].model = model
                    log.info("hot-swap: %s provider → %s model → %s", role, provider, model or "(unchanged)")
                    if self._dispatcher is not None:
                        await self._dispatcher.emit(
                            make_event(
                                "provider_switch",
                                role=role,
                                data={"role": role, "provider": provider, "model": model},
                                priority=True,
                            )
                        )
                continue
            if cmd.kind == "steer":
                text = cmd.data.get("text", "") if cmd.data else ""
                if not text:
                    log.warning("runner: steer command with empty text — ignoring")
                    continue
                if text.startswith("stuck_option_"):
                    # stuck_option_N is an opaque code produced by the stuck
                    # dialog — each option means a different recovery action
                    # (temp-up, rollback, diversify, coach, human escalation)
                    # and those semantics live in the escalation subsystem.
                    # Marshal through STEER.md so execute_phase picks it up on
                    # the next attempt boundary and treats it as a human hint.
                    self.disk.write_steer(f"[stuck-option] {text}")
                    log.info("runner: stuck option %s persisted to STEER.md", text)
                else:
                    self.disk.write_steer(text)
                    log.info(
                        "runner: steer text persisted to STEER.md (%d chars)",
                        len(text),
                    )
                continue
            if cmd.kind == "skip_task":
                # Soft skip: signal execute_phase to advance past the current
                # task at its next attempt boundary. We cannot bail out of the
                # running attempt here, only set a flag the phase checks.
                if self._ctx is not None:
                    self._ctx.skip_requested = True
                self._skip_current_task = True  # legacy mirror
                log.info(
                    "runner: skip_task requested — current attempt will finish, "
                    "then execute_phase advances to the next task"
                )
                continue
            log.warning(
                "runner: dropping unsupported command %r from %s (data=%r) — "
                "no handler registered at phase boundary",
                cmd.kind,
                cmd.source or "unknown",
                cmd.data,
            )
        return state, True

    async def _apply_switch_provider(self, cmd: Command) -> None:
        """Apply a switch_provider command to config.roles in-place.

        Used by _idle_loop so that RoleSwap commands sent before a plan is
        picked still take effect by the time _execute_plan starts.
        """
        role = cmd.data.get("role", "")
        provider = cmd.data.get("provider", "")
        model = cmd.data.get("model", "")
        if role and provider and role in self.config.roles:
            self.config.roles[role].provider = provider
            if "model" in cmd.data:
                self.config.roles[role].model = model
            log.info(
                "idle hot-swap: %s provider → %s model → %s",
                role,
                provider,
                model or "(unchanged)",
            )
            if self._dispatcher is not None:
                await self._dispatcher.emit(
                    make_event(
                        "provider_switch",
                        role=role,
                        data={"role": role, "provider": provider, "model": model},
                        priority=True,
                    )
                )

    async def _run_legacy_agent(
        self, ctx: RunnerContext, shutdown_event: asyncio.Event | None = None
    ) -> None:
        state = ctx.state
        plan_content = self.disk.read_plan(state.plan_file)
        if not plan_content or not plan_content.strip():
            state = self.checkpoint.mark_failed(state, "plan file is empty or missing")
            self._current_state = state
            await self.notifier.notify("failed — empty plan", NotifyLevel.ERROR)
            await self._emit_error("plan file is empty or missing")
            return
        retry_cfg = self.config.retry
        reflexion_ctx = ReflexionContext()
        max_attempts = min(
            self.config.retry.max_retries,
            self.config.reflexion.max_cycles + 1,
        )

        for attempt in range(state.retry_count, max_attempts):
            # A47: check shutdown at the top of the outer retry loop so that a
            # shutdown signal is honoured immediately on the next iteration even
            # when no PAUSE is active.
            if shutdown_event and shutdown_event.is_set():
                log.info("shutdown requested — exiting retry loop")
                return
            inject_prompt = ""
            if attempt > 0:
                stuck = check_stuck(state, self.config.stuck_detection)
                if stuck.signal != StuckSignal.NONE:
                    action = decide_escalation(
                        stuck,
                        ctx.escalation_level,
                        ctx.div_steps,
                        self.config.escalation,
                    )
                    if action.level != EscalationLevel.NONE:
                        ctx.escalation_history.append(action.level)
                    state = await execute_escalation(
                        action,
                        state,
                        self.disk,
                        self.notifier,
                        self.checkpoint,
                        stuck_result=stuck,
                        escalation_history=ctx.escalation_history,
                    )
                    self._current_state = state
                    ctx.escalation_level = action.level
                    if action.should_pause:
                        return
                    if action.level == EscalationLevel.DIVERSIFICATION:
                        ctx.div_steps += 1
                    elif action.level == EscalationLevel.BACKTRACK_COACH:
                        ctx.div_steps = 0
                    inject_prompt = action.inject_prompt
                else:
                    ctx.escalation_level = EscalationLevel.NONE

            if attempt > 0:
                wait = min(
                    retry_cfg.chain_retry_wait_s * retry_cfg.backoff_base ** min(attempt - 1, 10),
                    300,
                )
                jitter = random.uniform(0, retry_cfg.chain_retry_wait_s * 0.1)
                _remaining = wait + jitter
                _tick = 5.0
                while _remaining > 0:
                    if shutdown_event and shutdown_event.is_set():
                        log.info("shutdown requested — exiting during retry wait")
                        return
                    await asyncio.sleep(min(_tick, _remaining))
                    _remaining -= _tick

            override = await self._check_override()
            if override:
                state = self._handle_override(override, state)
                if state.phase == Phase.FAILED:
                    self._current_state = state
                    await self.notifier.notify("stopped by OVERRIDE.md", NotifyLevel.ERROR)
                    await self._emit_error("stopped by OVERRIDE.md")
                    return
                if state.phase == Phase.PAUSED:
                    await self.notifier.notify(
                        "paused — remove PAUSE from OVERRIDE.md to resume",
                        NotifyLevel.STUCK,
                    )
                    while await self._override_contains_pause():
                        override_now = await self._check_override()
                        if override_now and self._RE_STOP.search(override_now):
                            state = self.checkpoint.mark_failed(
                                state, "STOP directive in OVERRIDE.md"
                            )
                            self._current_state = state
                            await self.notifier.notify("stopped by OVERRIDE.md", NotifyLevel.ERROR)
                            await self._emit_error("stopped by OVERRIDE.md")
                            return
                        if shutdown_event and shutdown_event.is_set():
                            log.info("shutdown requested during PAUSE — exiting")
                            return
                        for _ in range(12):  # 12 × 5 s = 60 s max
                            await asyncio.sleep(5)
                            _poll = await self._check_override()
                            if _poll and self._RE_STOP.search(_poll):
                                state = self.checkpoint.mark_failed(
                                    state, "STOP directive in OVERRIDE.md"
                                )
                                self._current_state = state
                                await self.notifier.notify(
                                    "stopped by OVERRIDE.md", NotifyLevel.ERROR
                                )
                                await self._emit_error("stopped by OVERRIDE.md")
                                return
                            if shutdown_event and shutdown_event.is_set():
                                log.info("shutdown requested during PAUSE — exiting")
                                return
                            if not await self._override_contains_pause():
                                break
                    # PAUSE is gone — but it may have been replaced by STOP
                    override_after_pause = await self._check_override()
                    if override_after_pause and self._RE_STOP.search(override_after_pause):
                        state = self.checkpoint.mark_failed(
                            state, "STOP directive in OVERRIDE.md"
                        )
                        self._current_state = state
                        await self.notifier.notify("stopped by OVERRIDE.md", NotifyLevel.ERROR)
                        await self._emit_error("stopped by OVERRIDE.md")
                        return
                    log.info("PAUSE cleared — resuming")
                    state = self.checkpoint.mark_running(state)
                    self._current_state = state
                    ctx.reset()

            effective_plan = plan_content
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
            ctx.state = state
            success, captured_output = await ctx.run_agent(chain, effective_plan)
            state = ctx.state
            self._current_state = state

            if success:
                state = self.checkpoint.mark_completed(state)
                self._current_state = state
                await self.notifier.notify("done", NotifyLevel.DONE)
                await self._emit_done()
                return

            truncated_output = (
                captured_output[:MAX_BUILDER_OUTPUT_CHARS] + "... [truncated]"
                if len(captured_output) > MAX_BUILDER_OUTPUT_CHARS
                else captured_output
            )
            reflexion_ctx = add_attempt(
                reflexion_ctx,
                builder_output=truncated_output,
                verifier_feedback=f"Attempt {attempt + 1} did not succeed (no explicit verifier)",
            )

            state = self.checkpoint.increment_retry(state)
            self._current_state = state
            log.warning(f"attempt {attempt + 1} failed, retrying...")

        stuck = check_stuck(state, self.config.stuck_detection)
        if stuck.signal != StuckSignal.NONE:
            action = EscalationAction(level=EscalationLevel.HUMAN, should_pause=True)
            ctx.escalation_history.append(EscalationLevel.HUMAN)
            state = await execute_escalation(
                action,
                state,
                self.disk,
                self.notifier,
                self.checkpoint,
                stuck_result=stuck,
                escalation_history=ctx.escalation_history,
            )
            self._current_state = state
            ctx.escalation_level = EscalationLevel.HUMAN
            return

        state = self.checkpoint.mark_failed(state, "all retries exhausted")
        self._current_state = state
        msg = f"failed after {self.config.retry.max_retries} attempts"
        await self.notifier.notify(msg, NotifyLevel.ERROR)
        await self._emit_error(msg)

    async def _execute_sora(
        self, state: AgentState, shutdown_event: asyncio.Event | None = None
    ) -> None:
        ctx = self._build_runner_context(state, shutdown_event)
        self._ctx = ctx

        if self.plan_file:
            try:
                plan_content = self.plan_file.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"plan file not found: {self.plan_file}"
                ) from exc
            roadmap_path = f"{ctx.milestone_path}/ROADMAP.md"
            ctx.disk.write_file(roadmap_path, plan_content)

        if "reviewer" in self.config.roles and not _phase_already_done(
            state.sora_phase, SoraPhase.HARDENING
        ):
            state = self.checkpoint.set_sora_phase(state, SoraPhase.HARDENING)
            self._current_state = state
            await self._emit_phase(SoraPhase.HARDENING)
            state, cont = await self._drain_commands(state)
            if not cont:
                return
            harden_result = await run_harden(ctx)
            if not harden_result.success:
                msg = f"Harden failed: {harden_result.error}"
                await self.notifier.notify(msg, NotifyLevel.ERROR)
                await self._emit_error(msg)
                return

        if "scout" in self.config.roles and not _phase_already_done(
            state.sora_phase, SoraPhase.SCOUT
        ):
            state = self.checkpoint.set_sora_phase(state, SoraPhase.SCOUT)
            self._current_state = state
            await self._emit_phase(SoraPhase.SCOUT)
            state, cont = await self._drain_commands(state)
            if not cont:
                return
            scout_result = await run_scout(ctx)
            if not scout_result.success:
                # Scout is non-fatal — reduced context quality is acceptable.
                log.warning("scout phase did not succeed (non-fatal): %s", scout_result.error)

        if "coach" in self.config.roles and not _phase_already_done(
            state.sora_phase, SoraPhase.COACH
        ):
            state = self.checkpoint.set_sora_phase(state, SoraPhase.COACH)
            self._current_state = state
            await self._emit_phase(SoraPhase.COACH)
            state, cont = await self._drain_commands(state)
            if not cont:
                return
            coach_result = await run_coach(ctx, CoachTrigger.FIRST_RUN)
            if not coach_result.success:
                # Coach is non-fatal — previous strategic documents remain on disk.
                log.warning("coach phase did not succeed (non-fatal): %s", coach_result.error)

        slice_plan = None  # set below, before it is needed by run_execute
        execute_already_done = _phase_already_done(state.sora_phase, SoraPhase.EXECUTE)

        if not _phase_already_done(state.sora_phase, SoraPhase.ARCHITECT):
            state = self.checkpoint.set_sora_phase(state, SoraPhase.ARCHITECT)
            self._current_state = state
            await self._emit_phase(SoraPhase.ARCHITECT)
            state, cont = await self._drain_commands(state)
            if not cont:
                return
            result = await run_architect(ctx, slice_id=state.current_slice or "S01")
            if not result.success:
                msg = f"Architect failed: {result.error}"
                await self.notifier.notify(msg, NotifyLevel.ERROR)
                await self._emit_error(msg)
                return
            state = ctx.state
            slice_plan = result.data["slice_plan"]
        elif not execute_already_done:
            # Architect was done but Execute was not — load the existing plan from disk
            # for crash recovery.  The safe variant returns an empty SlicePlan when
            # the file is missing; run_execute will then surface the "no tasks" error.
            slice_plan = _load_slice_plan_from_disk_safe(ctx, state.current_slice or "S01")
        # else: both Architect and Execute are done (e.g. SLICE_DONE) — slice_plan
        # stays None because it is not needed for the current iteration.

        if not execute_already_done:
            state = self.checkpoint.set_sora_phase(state, SoraPhase.EXECUTE)
            self._current_state = state
            await self._emit_phase(SoraPhase.EXECUTE)
            state, cont = await self._drain_commands(state)
            if not cont:
                return
            exec_result = await run_execute(ctx, slice_plan)
            if not exec_result.success:
                msg = f"Execute failed: {exec_result.error}"
                state = self.checkpoint.mark_failed(state, msg)
                self._current_state = state
                await self.notifier.notify(msg, NotifyLevel.ERROR)
                await self._emit_error(msg)
                return

        max_slices = self.config.max_slices
        extra_slices_done = 0
        _slice_loop_completed = False
        limit_reached = False
        while extra_slices_done < max_slices - 1:
            state = self.checkpoint.set_sora_phase(state, SoraPhase.SLICE_DONE)
            self._current_state = state
            await self._emit_phase(SoraPhase.SLICE_DONE)

            if "coach" in self.config.roles:
                await run_coach(ctx, CoachTrigger.END_OF_SLICE)

            next_slice = _read_next_slice(ctx)
            if next_slice is None:
                _slice_loop_completed = True
                break

            state.current_slice = next_slice
            state.current_task_index = 0
            state = self.checkpoint.save(state)
            self._current_state = state
            extra_slices_done += 1

            state = self.checkpoint.set_sora_phase(state, SoraPhase.ARCHITECT)
            self._current_state = state
            await self._emit_phase(SoraPhase.ARCHITECT)
            state, cont = await self._drain_commands(state)
            if not cont:
                return
            if shutdown_event and shutdown_event.is_set():
                return
            result = await run_architect(ctx, slice_id=next_slice)
            if not result.success:
                msg = f"Architect failed on {next_slice}: {result.error}"
                state = self.checkpoint.mark_failed(state, msg)
                self._current_state = state
                await self.notifier.notify(msg, NotifyLevel.ERROR)
                await self._emit_error(msg)
                break
            slice_plan = result.data["slice_plan"]

            state = self.checkpoint.set_sora_phase(state, SoraPhase.EXECUTE)
            self._current_state = state
            await self._emit_phase(SoraPhase.EXECUTE)
            state, cont = await self._drain_commands(state)
            if not cont:
                return
            if shutdown_event and shutdown_event.is_set():
                return
            exec_result = await run_execute(ctx, slice_plan)
            if not exec_result.success:
                msg = f"Execute failed on {next_slice}: {exec_result.error}"
                state = self.checkpoint.mark_failed(state, msg)
                self._current_state = state
                await self.notifier.notify(msg, NotifyLevel.ERROR)
                await self._emit_error(msg)
                break
        else:
            limit_reached = True
            msg = (
                f"extra slice limit reached ({max_slices} additional slices beyond S01) "
                f"— stopping. Check TASK_QUEUE.md."
            )
            await self.notifier.notify(msg, NotifyLevel.ERROR)
            await self._emit_error(msg)

        if _slice_loop_completed:
            state = self.checkpoint.mark_completed(state)
            self._current_state = state
            await self.notifier.notify("done", NotifyLevel.DONE)
            await self._emit_done()

    async def _wait_for_command(self, timeout: float | None) -> Command | None:
        """Poll the command queue, returning None if the timeout expires."""
        if self._command_queue is None:
            return None
        try:
            return await asyncio.wait_for(self._command_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def _resolve_plan(self, text: str) -> str:
        """Resolve plan text to a usable file path.

        If *text* names an existing file, returns it as-is.
        Otherwise writes the text to ``.sora/strategic/ROADMAP.md`` (backing
        up any previous content to ``ROADMAP.md.bak``) and returns that path.
        """
        candidate = Path(text.strip())
        if candidate.is_file():
            return str(candidate)
        roadmap_rel = "strategic/ROADMAP.md"
        existing = self.disk.read_file(roadmap_rel)
        if existing:
            self.disk.write_file("strategic/ROADMAP.md.bak", existing)
        self.disk.write_file(roadmap_rel, text)
        return str(self.disk.sora_dir / roadmap_rel)

    async def _idle_loop(self, shutdown_event: asyncio.Event | None = None) -> None:
        """Wait for ``new_plan`` or ``stop`` commands when the runner is idle.

        Exits immediately when there is no command queue (headless / standalone
        mode).  When ``config.idle_timeout_s > 0`` the loop exits after that
        many seconds without receiving any command.
        """
        if self._command_queue is None:
            return

        idle_timeout = float(self.config.idle_timeout_s) if self.config.idle_timeout_s > 0 else None
        poll_s = min(_IDLE_POLL_S, idle_timeout) if idle_timeout is not None else _IDLE_POLL_S
        elapsed = 0.0
        log.info("idle: waiting for new_plan or stop (timeout=%s)", idle_timeout)

        while True:
            if shutdown_event and shutdown_event.is_set():
                return

            cmd = await self._wait_for_command(timeout=poll_s)

            if cmd is None:
                elapsed += poll_s
                if idle_timeout is not None and elapsed >= idle_timeout:
                    log.info("idle: timeout after %.0fs — exiting", elapsed)
                    return
                continue

            if cmd.kind == "stop":
                log.info("idle: stop command received")
                return

            if cmd.kind == "switch_provider":
                await self._apply_switch_provider(cmd)
                continue

            if cmd.kind == "new_plan":
                plan_text = cmd.data.get("text", "")
                if not plan_text:
                    log.warning("idle: new_plan command with empty text — ignoring")
                    continue
                plan_path = self._resolve_plan(plan_text)
                self.plan_file = Path(plan_path)
                new_state = self.checkpoint.mark_started(plan_path)
                self._current_state = new_state
                await self.notifier.notify("started", NotifyLevel.PROGRESS)
                await self._execute_plan(new_state, shutdown_event)
                # Reset elapsed after each plan execution to restart the idle timer
                elapsed = 0.0
                continue

            log.warning(
                "idle: dropping unsupported command %r from %s (data=%r) — "
                "no handler registered in idle loop",
                cmd.kind,
                cmd.source or "unknown",
                cmd.data,
            )

    def _build_chain(self, start_index: int = 0, *, role: str = "executor") -> ProviderChain:
        role_cfg = self.config.roles.get(role)
        if role_cfg is None:
            from tero2.errors import ConfigError

            raise ConfigError(f"no {role!r} role configured")
        all_names = [role_cfg.provider] + role_cfg.fallback
        names = all_names[start_index:]
        providers = []
        for i, n in enumerate(names):
            override = role_cfg.model if start_index + i == 0 else ""
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

    def _handle_override(self, content: str, state: AgentState) -> AgentState:
        if self._RE_STOP.search(content):
            self._current_state = self.checkpoint.mark_failed(state, "STOP directive in OVERRIDE.md")
            object.__setattr__(state, "phase", self._current_state.phase)
            return self._current_state
        if self._RE_PAUSE.search(content) and state.phase != Phase.PAUSED:
            self._current_state = self.checkpoint.mark_paused(state, "PAUSE directive in OVERRIDE.md")
            object.__setattr__(state, "phase", self._current_state.phase)
            return self._current_state
        return state
