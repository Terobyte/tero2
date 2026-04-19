"""Execute phase — runs Builder (+ Verifier) for every Task in a SlicePlan.

``run_execute(ctx, slice_plan)`` iterates over all Tasks, invoking
:class:`~tero2.players.builder.BuilderPlayer` (and optionally
:class:`~tero2.players.verifier.VerifierPlayer`) for each one.

On Verifier failure the phase automatically retries the task with
:mod:`~tero2.reflexion` context injected, up to
``config.reflexion.max_cycles`` additional attempts.

Execute failure on one task is **non-fatal for subsequent tasks** — the
phase continues with the remaining tasks and reports the aggregate result.
The overall ``PhaseResult.success`` is ``True`` only when every task passes.

Per-task safety controls:
    - Stuck counters (``tool_repeat_count``, ``last_tool_hash``,
      ``retry_count``, ``steps_in_task``) are reset at each task boundary
      to address Bug 16 where ``increment_retry`` leaves stale values.
    - ``OVERRIDE.md`` is checked at each task boundary and each attempt.
    - ``STEER.md`` is reloaded at each task boundary and each attempt.
    - ``shutdown_event`` is checked at each task boundary.
    - Escalation (``decide_escalation`` / ``execute_escalation``) runs
      before each attempt using ``ctx.escalation_level`` / ``_div_steps``
      / ``_escalation_history``.
    - ``ANOMALY`` verdicts trigger :func:`~tero2.triggers.check_triggers`
      and optionally invoke :func:`~tero2.phases.coach_phase.run_coach`.
    - ``metrics.json`` is updated via ``disk.write_metrics`` after every task.

Crash recovery:
    ``ctx.state.current_task_index`` is persisted to *task_index* **before**
    a task starts (crash → re-run that task) and advanced to
    *task_index + 1* **after** it passes (crash → skip completed task).
    A final advance to ``len(tasks)`` ensures the slice is not replayed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from tero2.disk_layer import DiskLayer
from tero2.escalation import EscalationLevel, decide_escalation, execute_escalation
from tero2.phases.context import PhaseResult, RunnerContext
from tero2.players.architect import SlicePlan, Task
from tero2.players.builder import BuilderPlayer
from tero2.players.verifier import Verdict, VerifierPlayer
from tero2.reflexion import MAX_BUILDER_OUTPUT_CHARS, ReflexionContext, add_attempt
from tero2.state import SoraPhase
from tero2.stuck_detection import check_stuck
from tero2.triggers import check_triggers

log = logging.getLogger(__name__)


async def run_execute(
    ctx: RunnerContext,
    slice_plan: SlicePlan,
) -> PhaseResult:
    """Execute all Tasks in *slice_plan* using Builder + optional Verifier.

    For each Task in the slice:

    1. Build a prompt from the task plan and persona.
    2. Run :class:`~tero2.players.builder.BuilderPlayer` (full agentic mode).
    3. Run :class:`~tero2.players.verifier.VerifierPlayer` when the
       ``"verifier"`` role is configured.
    4. On Verifier FAIL: retry with :mod:`~tero2.reflexion` context injected
       (up to ``config.reflexion.max_cycles`` additional attempts).
    5. Append per-task result to the aggregate summary.

    Tasks that exceed the retry budget are logged as failed but do not
    stop the remaining tasks from running.

    Args:
        ctx:        Shared runner context.  Requires a ``"builder"`` role.
        slice_plan: Parsed slice plan from
                    :func:`~tero2.phases.architect_phase.run_architect`.

    Returns:
        :class:`~tero2.phases.context.PhaseResult` with ``data`` set to::

            {
                "slice_id": "<S0X>",
                "completed": {"T01": "<summary_path>", ...},
            }

        ``success`` is ``True`` only when every task passes verification.
    """
    try:
        builder_chain = ctx.build_chain("builder")
    except Exception as exc:
        log.error("execute: cannot build builder chain: %s", exc)
        return PhaseResult(success=False, error=str(exc))

    # Verifier: when the role is configured it is required — any chain-build
    # failure is a misconfiguration, not a graceful skip.  SORA config
    # validation already enforces roles.verifier whenever roles.builder is
    # present, so a build failure here means something is broken.
    verifier_chain = None
    if ctx.config.roles.get("verifier") is not None:
        try:
            verifier_chain = ctx.build_chain("verifier")
        except Exception as exc:
            log.error("execute: verifier role is configured but chain build failed: %s", exc)
            return PhaseResult(
                success=False,
                error=f"verifier misconfiguration: {exc}",
            )

    # Commands from [verifier] config take priority; per-task must-have commands
    # are extracted as a fallback when no explicit config is present.
    global_verify_commands = list(ctx.config.verifier.commands)

    working_dir = str(ctx.disk.project_path)
    persona_prompt = ctx.personas.load_or_default("builder").system_prompt
    context_hints = ctx.disk.read_file("strategic/CONTEXT_HINTS.md") or ""
    max_cycles = ctx.config.reflexion.max_cycles

    tasks = slice_plan.tasks
    if not tasks:
        return PhaseResult(
            success=False,
            error=f"slice {slice_plan.slice_id} has no tasks",
            data={"slice_id": slice_plan.slice_id, "completed": {}},
        )
    # Restore crash-recovery offset from persisted state.
    start_index = ctx.state.current_task_index
    # Capture whether the task at start_index was genuinely in-progress when the
    # process last stopped.  Checked at loop-time (below) BEFORE we overwrite the
    # flag for the current task so that "clean advance to task N" (flag=False) is
    # not mis-classified as "interrupted task N" (flag=True).
    _resumed_from_interrupt = ctx.state.task_in_progress

    completed: dict[str, str] = {}  # task_id → summary_path
    all_passed = True

    for task_index, task in enumerate(tasks):
        if task_index < start_index:
            log.info(
                "execute: skipping already-completed task %s (index %d < %d)",
                task.id,
                task_index,
                start_index,
            )
            summary_path = f"{slice_plan.slice_dir}/{task.id}-SUMMARY.md"
            if (ctx.disk.sora_dir / summary_path).exists():
                completed[task.id] = summary_path
            else:
                log.warning(
                    "execute: skipped task %s has no summary on disk — marking run failed",
                    task.id,
                )
                all_passed = False
            continue

        # ── Task-boundary safety checks ───────────────────────────────────

        # Shutdown check at task boundary.
        if ctx.shutdown_event and ctx.shutdown_event.is_set():
            log.info("execute: shutdown requested — stopping at task boundary")
            return PhaseResult(
                success=False,
                error="shutdown requested",
                data={"slice_id": slice_plan.slice_id, "completed": completed},
            )

        # OVERRIDE.md STOP / PAUSE check at task boundary.
        override_result = _check_override(ctx, slice_plan.slice_id, completed)
        if override_result is not None:
            return override_result

        # STEER.md reload at task boundary — use as effective context_hints
        # when the human has posted new steering instructions.
        steer_content = ctx.disk.read_steer()
        if steer_content:
            context_hints = steer_content

        # Per-task stuck counter reset (Bug 16 fix).
        # increment_retry leaves these counters stale across task transitions;
        # we reset them explicitly so check_stuck starts fresh each task.
        ctx.state.tool_repeat_count = 0
        ctx.state.last_tool_hash = ""
        ctx.state.retry_count = 0
        ctx.state.steps_in_task = 0

        # Persist task start position for crash recovery.
        # Saving task_index (not task_index+1) means recovery re-runs this
        # task from scratch if the process dies during it.
        # task_in_progress=True marks that this task is in flight; it is cleared
        # to False only after the task passes (see post-task bookkeeping below).
        ctx.state.task_in_progress = True
        ctx.state.sora_phase = SoraPhase.EXECUTE
        ctx.state.current_task = task.id
        ctx.state.current_task_index = task_index
        ctx.checkpoint.save(ctx.state)

        task_plan = _format_task_plan(task)

        # Interrupted-task reflexion seeding (crash recovery).
        # When we are resuming at a previously-started task (start_index > 0
        # and we are exactly at that index), the task was in progress before
        # the crash.  Seed the reflexion context so the builder knows.
        reflexion_ctx = ReflexionContext()
        if task_index == start_index and _resumed_from_interrupt:
            reflexion_ctx = add_attempt(
                reflexion_ctx,
                builder_output="",
                verifier_feedback=(
                    "[crash recovery] task was interrupted — resuming from the start"
                ),
            )

        task_passed = False

        for attempt in range(max_cycles + 1):
            # OVERRIDE.md check each attempt iteration.
            override_result = _check_override(ctx, slice_plan.slice_id, completed)
            if override_result is not None:
                return override_result

            # STEER.md reload on each attempt.
            steer_content = ctx.disk.read_steer()
            effective_hints = steer_content if steer_content else context_hints

            # Escalation integration — evaluate stuck state before every
            # attempt so we can inject diversification prompts or pause.
            stuck_result = check_stuck(ctx.state, ctx.config.stuck_detection)
            esc_action = decide_escalation(
                stuck_result,
                ctx.escalation_level,
                ctx.div_steps,
                ctx.config.escalation,
            )
            if esc_action.level != EscalationLevel.NONE:
                old_level = ctx.escalation_level
                ctx.escalation_level = esc_action.level
                if esc_action.level.value > old_level.value:
                    ctx.escalation_history.append(esc_action.level)
                ctx.state = await execute_escalation(
                    esc_action,
                    ctx.state,
                    ctx.disk,
                    ctx.notifier,
                    ctx.checkpoint,
                    stuck_result=stuck_result,
                    escalation_history=ctx.escalation_history,
                )
                if esc_action.should_pause:
                    log.info("execute: human escalation — pausing for STEER.md input")
                    return PhaseResult(
                        success=False,
                        error="human escalation — waiting for STEER.md",
                        data={
                            "slice_id": slice_plan.slice_id,
                            "completed": completed,
                        },
                    )
                if esc_action.level == EscalationLevel.DIVERSIFICATION:
                    ctx.div_steps += 1

            reflexion_section = reflexion_ctx.to_prompt()
            if esc_action.inject_prompt:
                reflexion_section = esc_action.inject_prompt + "\n\n" + reflexion_section

            builder = BuilderPlayer(
                builder_chain,
                ctx.disk,
                working_dir=working_dir,
            )
            builder_result = await builder.run(
                task_plan=task_plan,
                persona_prompt=persona_prompt,
                reflexion_context=reflexion_section,
                context_hints=effective_hints,
                task_id=task.id,
                slice_id=slice_plan.slice_id,
                milestone_path=ctx.milestone_path,
                ctx=ctx,
            )

            if not builder_result.success:
                log.error(
                    "execute: builder failed for %s (attempt %d/%d): %s",
                    task.id,
                    attempt + 1,
                    max_cycles + 1,
                    builder_result.error,
                )
                ctx.state.retry_count += 1  # keep stuck-detection retry counter live
                truncated = builder_result.captured_output[:MAX_BUILDER_OUTPUT_CHARS]
                reflexion_ctx = add_attempt(
                    reflexion_ctx,
                    builder_output=truncated,
                    verifier_feedback=f"Builder did not complete (attempt {attempt + 1})",
                )
                continue

            # No verifier configured → builder success is sufficient.
            if verifier_chain is None:
                log.info("execute: task %s done (no verifier)", task.id)
                completed[task.id] = builder_result.output_file
                task_passed = True
                break

            verifier = VerifierPlayer(
                verifier_chain,
                ctx.disk,
                working_dir=working_dir,
            )
            verify_commands = global_verify_commands or _extract_must_have_commands(task)
            verify_result = await verifier.run(
                builder_output=builder_result.captured_output,
                task_id=task.id,
                verify_commands=verify_commands,
            )

            if verify_result.success:
                log.info(
                    "execute: task %s PASS (attempt %d/%d)",
                    task.id,
                    attempt + 1,
                    max_cycles + 1,
                )
                completed[task.id] = builder_result.output_file
                task_passed = True
                break

            log.warning(
                "execute: task %s %s (attempt %d/%d)",
                task.id,
                verify_result.verdict,
                attempt + 1,
                max_cycles + 1,
            )

            # ANOMALY → check_triggers → Coach.
            # Import lazily to avoid any circular-import risk at module load.
            if verify_result.verdict == Verdict.ANOMALY:
                from tero2.phases.coach_phase import run_coach  # noqa: PLC0415

                # Write the ANOMALY event to EVENT_JOURNAL.md so that
                # check_triggers/_check_anomaly can find it.  Without this
                # write the journal never contains "ANOMALY" and the trigger
                # never fires, skipping Coach entirely.
                _ts = datetime.now(timezone.utc).isoformat()
                ctx.disk.append_file(
                    "persistent/EVENT_JOURNAL.md",
                    f"\n## ANOMALY [{_ts}]\ntask={task.id} attempt={attempt + 1}\n",
                )

                trigger_result = check_triggers(ctx.state, ctx.disk, ctx.config)
                if trigger_result.should_fire:
                    log.info(
                        "execute: ANOMALY — invoking Coach (trigger=%s)",
                        trigger_result.trigger.value,
                    )
                    await run_coach(ctx, trigger_result.trigger)
                    # Refresh context hints after Coach updated strategy docs.
                    new_hints = ctx.disk.read_file("strategic/CONTEXT_HINTS.md")
                    if new_hints:
                        context_hints = new_hints

            ctx.state.retry_count += 1  # keep stuck-detection retry counter live
            truncated = builder_result.captured_output[:MAX_BUILDER_OUTPUT_CHARS]
            reflexion_ctx = add_attempt(
                reflexion_ctx,
                builder_output=truncated,
                verifier_feedback=verify_result.captured_output,
                failed_tests=verify_result.failed_tests,
                must_haves_failed=verify_result.must_haves_failed,
            )

        # ── Post-task bookkeeping ──────────────────────────────────────────

        if not task_passed:
            log.error(
                "execute: task %s failed after %d attempt(s)",
                task.id,
                max_cycles + 1,
            )
            all_passed = False
        else:
            # Advance checkpoint AFTER each successfully completed task.
            # Saving task_index+1 ensures crash recovery skips this task
            # and resumes at the next one without re-running completed work.
            # Clear task_in_progress so the NEXT task is not misclassified as
            # interrupted on crash recovery.
            ctx.state.task_in_progress = False
            ctx.state.current_task_index = task_index + 1
            ctx.checkpoint.save(ctx.state)

        # Update metrics after each task regardless of outcome.
        _update_task_metrics(ctx.disk, task.id, task_passed)

    # Advance past the last task so crash recovery does not re-run on resume.
    ctx.state.task_in_progress = False
    ctx.state.current_task_index = len(tasks)
    ctx.checkpoint.save(ctx.state)

    log.info(
        "execute: slice %s complete — %d/%d task(s) passed",
        slice_plan.slice_id,
        len(completed),
        len(tasks),
    )
    return PhaseResult(
        success=all_passed,
        error="" if all_passed else f"{len(tasks) - len(completed)} task(s) failed",
        data={"slice_id": slice_plan.slice_id, "completed": completed},
    )


# ── Internal helpers ──────────────────────────────────────────────────────


def _check_override(
    ctx: RunnerContext,
    slice_id: str,
    completed: dict[str, str],
) -> PhaseResult | None:
    """Check ``OVERRIDE.md`` for STOP or PAUSE commands.

    Returns a :class:`~tero2.phases.context.PhaseResult` if action is
    required, ``None`` if ``OVERRIDE.md`` is absent or contains neither
    keyword.

    Side-effects:
        - Clears ``OVERRIDE.md`` after reading a recognised command.
        - Calls ``ctx.checkpoint.mark_paused()`` on PAUSE (updates
          ``ctx.state`` in place).

    Args:
        ctx:       Shared runner context.
        slice_id:  Current slice identifier — forwarded to the result data.
        completed: Tasks completed so far — forwarded to the result data.

    Returns:
        :class:`~tero2.phases.context.PhaseResult` on STOP/PAUSE, else ``None``.
    """
    override = ctx.disk.read_override()
    if not override:
        return None
    if _RE_STOP.search(override):
        log.info("execute: STOP override received")
        ctx.disk.clear_override()
        return PhaseResult(
            success=False,
            error="STOP requested via OVERRIDE.md",
            data={"slice_id": slice_id, "completed": completed},
        )
    if _RE_PAUSE.search(override):
        log.info("execute: PAUSE override received")
        ctx.state = ctx.checkpoint.mark_paused(ctx.state, "paused via OVERRIDE.md")
        ctx.disk.clear_override()
        return PhaseResult(
            success=False,
            error="PAUSE requested via OVERRIDE.md",
            data={"slice_id": slice_id, "completed": completed},
        )
    return None


_RE_STOP = re.compile(r"^\s*STOP\s*$", re.MULTILINE | re.IGNORECASE)
_RE_PAUSE = re.compile(r"^\s*PAUSE\s*$", re.MULTILINE | re.IGNORECASE)

# Matches command-like text in must-have strings.
# Group 1: backtick-quoted inline command  e.g. "`swift test`"
# Group 2: line starting with a known CLI tool (after "- " prefix already stripped)
_CMD_RE = re.compile(
    r"`([^`]+)`"   # backtick-quoted inline command
    r"|(?:^|[-*]\s*)((?:cd|swift|ctest|make|cmake|cargo|npm|yarn|pnpm|pytest|go|\./)[^\n`]*)",
    re.MULTILINE,
)


def _extract_must_have_commands(task: Task) -> list[str]:
    """Extract shell commands from a Task's must-have list.

    Looks for backtick-quoted commands and bullet lines beginning with known
    executables (swift, ctest, make, etc.).  Returns empty list when no
    command-like must-haves are found, causing the Verifier to use its
    default Python fallback (ruff + pytest).
    """
    cmds: list[str] = []
    for item in task.must_haves:
        for match in _CMD_RE.finditer(item):
            cmd = (match.group(1) or match.group(2) or "").strip()
            if cmd and cmd not in cmds:
                cmds.append(cmd)
    return cmds


def _update_task_metrics(disk: DiskLayer, task_id: str, passed: bool) -> None:
    """Increment ``tasks_attempted`` / ``tasks_passed`` counters in ``metrics.json``.

    Reads, mutates, and writes ``metrics.json`` atomically via
    :meth:`~tero2.disk_layer.DiskLayer.write_metrics`.  Missing keys are
    initialised to 0 so that the first call creates a valid document.

    Args:
        disk:    :class:`~tero2.disk_layer.DiskLayer` for the project.
        task_id: Identifier of the completed task (used for logging only).
        passed:  ``True`` when the task passed verification.
    """
    metrics = disk.read_metrics()
    metrics.setdefault("tasks_attempted", 0)
    metrics.setdefault("tasks_passed", 0)
    metrics["tasks_attempted"] += 1
    if passed:
        metrics["tasks_passed"] += 1
    disk.write_metrics(metrics)
    log.debug(
        "execute: metrics updated — task %s %s (total %d/%d)",
        task_id,
        "passed" if passed else "failed",
        metrics["tasks_passed"],
        metrics["tasks_attempted"],
    )


def _format_task_plan(task: Task) -> str:
    """Format a :class:`~tero2.players.architect.Task` as a prompt section.

    Output shape::

        ## T01: <description>

        **Must-haves:**
        - item 1
        - item 2

    Args:
        task: Task to format.

    Returns:
        Markdown string ready to be embedded in a Builder prompt.
    """
    lines = [f"## {task.id}: {task.description}"]
    if task.must_haves:
        lines.append("")
        lines.append("**Must-haves:**")
        for item in task.must_haves:
            lines.append(f"- {item}")
    return "\n".join(lines)
