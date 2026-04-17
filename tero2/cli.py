"""CLI entry point for tero2.

Commands:
    tero2 run <project_path> --plan <plan.md>   — run agent on project
    tero2 status <project_path>                  — show current state
    tero2 init <project_path>                    — initialize .sora/ structure
    tero2 telegram                               — start Telegram plan-input bot
    tero2 harden <project_path> --plan <plan.md> — harden plan with Reviewer loop
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


def cmd_run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project_path = Path(args.project_path).expanduser().resolve()

    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory")
        sys.exit(1)

    plan_file = Path(args.plan).expanduser()
    if not plan_file.is_absolute():
        plan_file = (project_path / plan_file).resolve()
    else:
        plan_file = plan_file.resolve()

    if not plan_file.is_file():
        print(f"error: plan file not found: {plan_file}")
        sys.exit(1)

    from tero2.config import load_config
    from tero2.runner import Runner

    config = None
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        if not config_path.is_file():
            print(f"error: config file not found: {config_path}")
            sys.exit(1)
        config = load_config(project_path, override_path=config_path)

    runner = Runner(project_path, plan_file, config=config)
    asyncio.run(runner.run())


def cmd_status(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).expanduser().resolve()
    runtime_dir = project_path / ".sora" / "runtime"
    state_file = runtime_dir / "STATE.json"

    if not runtime_dir.is_dir():
        print("not initialized — run `tero2 init` first")
        return

    from tero2.state import AgentState

    state = AgentState.from_file(state_file)
    print(f"phase:    {state.phase.value}")
    print(f"task:     {state.current_task or '(none)'}")
    print(f"retry:    {state.retry_count}")
    print(f"steps:    {state.steps_in_task}")
    print(f"checkpoint: {state.last_checkpoint or '(none)'}")
    if state.error_message:
        print(f"error:    {state.error_message}")


def cmd_init(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).expanduser().resolve()

    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory")
        sys.exit(1)

    from tero2.disk_layer import DiskLayer

    disk = DiskLayer(project_path)
    disk.init()
    print(f"initialized .sora/ in {project_path}")


def cmd_harden(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project_path = Path(args.project_path).expanduser().resolve()
    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory")
        sys.exit(1)

    plan_file = Path(args.plan).expanduser()
    if not plan_file.is_absolute():
        plan_file = (project_path / plan_file).resolve()
    else:
        plan_file = plan_file.resolve()

    if not plan_file.is_file():
        print(f"error: plan file not found: {plan_file}")
        sys.exit(1)

    from tero2.circuit_breaker import CircuitBreakerRegistry
    from tero2.config import load_config
    from tero2.disk_layer import DiskLayer
    from tero2.phases.context import RunnerContext
    from tero2.phases.harden_phase import run_harden
    from tero2.state import AgentState

    config = load_config(project_path)

    if "reviewer" not in config.roles:
        print("error: [roles.reviewer] must be configured for tero2 harden")
        sys.exit(1)

    if args.rounds is not None:
        config.plan_hardening.max_rounds = args.rounds
    if args.debug:
        config.plan_hardening.debug = True

    disk = DiskLayer(project_path)
    disk.init()
    disk.write_file("milestones/M001/PLAN.md", plan_file.read_text())

    state = AgentState()
    state.plan_file = str(plan_file)

    ctx = RunnerContext(
        config=config,
        disk=disk,
        state=state,
        cb_registry=CircuitBreakerRegistry(
            failure_threshold=config.retry.cb_failure_threshold,
            recovery_timeout_s=config.retry.cb_recovery_timeout_s,
        ),
    )

    result = asyncio.run(run_harden(ctx))

    if result.success:
        hardened_path = disk.sora_dir / "milestones" / "M001" / "PLAN.md"
        print(f"hardening complete — {hardened_path}")
    else:
        print(f"error: hardening failed — {result.error}")
        sys.exit(1)


def cmd_telegram(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from tero2.config import load_config

    project_path = Path(args.project or ".").expanduser().resolve()
    config = load_config(project_path)
    if not config.telegram or not config.telegram.bot_token:
        print("error: telegram bot_token not configured")
        sys.exit(1)
    if not config.telegram.allowed_chat_ids:
        print("warning: telegram.allowed_chat_ids is empty — bot will ignore all messages")

    from tero2.telegram_input import TelegramInputBot

    bot = TelegramInputBot(config)
    print("starting Telegram input bot — Ctrl+C to stop")
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        asyncio.run(bot.stop())
        print("bot stopped")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tero2", description="Immortal Runner")

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run agent on a project with a plan")
    run_parser.add_argument("project_path", help="Path to the project root")
    run_parser.add_argument("--plan", required=True, help="Path to the markdown plan file")
    run_parser.add_argument("--config", help="Override config file path")
    run_parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    run_parser.set_defaults(func=cmd_run)

    status_parser = subparsers.add_parser("status", help="Show current runner state")
    status_parser.add_argument("project_path", help="Path to the project root")
    status_parser.set_defaults(func=cmd_status)

    init_parser = subparsers.add_parser("init", help="Initialize .sora/ directory structure")
    init_parser.add_argument("project_path", help="Path to the project root")
    init_parser.set_defaults(func=cmd_init)

    telegram_parser = subparsers.add_parser("telegram", help="Start Telegram plan-input bot")
    telegram_parser.add_argument("--project", help="Project path (default: cwd)", default=None)
    telegram_parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    telegram_parser.set_defaults(func=cmd_telegram)

    harden_parser = subparsers.add_parser(
        "harden", help="Harden a plan with Reviewer convergence loop"
    )
    harden_parser.add_argument("project_path", help="Path to the project root")
    harden_parser.add_argument("--plan", required=True, help="Path to the plan file to harden")
    harden_parser.add_argument(
        "--rounds", type=int, default=None, help="Max hardening rounds (overrides config)"
    )
    harden_parser.add_argument(
        "--debug", action="store_true", help="Enable verbose per-round output"
    )
    harden_parser.set_defaults(func=cmd_harden)

    return parser


if __name__ == "__main__":
    main()
