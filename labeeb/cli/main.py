"""Command-line interface for Labeeb Orchestrator."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from labeeb.config import load_config
from labeeb.core.controller import LabeebController, doctor
from labeeb.errors import ControllerError
from labeeb.models import TERMINAL_PHASES, VERSION


def load_intent(args: argparse.Namespace) -> str:
    if args.intent_file:
        return pathlib.Path(args.intent_file).read_text(encoding="utf-8").strip()
    if args.intent:
        return args.intent.strip()
    raise ControllerError("Provide --intent or --intent-file")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Labeeb Orchestrator V1 deterministic controller")
    p.add_argument("--config", default=None, help="Path to config.toml (default: search ./, ~/.config/labeeb-controller, and installation root)")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check required local executables and configuration")

    s = sub.add_parser("start", help="Create a goal from one engineering intent")
    s.add_argument("--intent")
    s.add_argument("--intent-file")
    s.add_argument("--workspace", required=True)
    s.add_argument("--repo", required=True, help="OWNER/REPO")
    s.add_argument("--branch", required=True)
    s.add_argument("--risk", action="append", default=[])
    s.add_argument("--allow-path", action="append", default=[])
    s.add_argument("--validate", action="append", default=[])
    s.add_argument("--preauthorize-plan", action="store_true")
    s.add_argument("--deadline-hours", type=float)
    s.add_argument("--background", action="store_true")

    r = sub.add_parser("run", help="Run/recover a goal until terminal state")
    r.add_argument("goal_id")
    r.add_argument("--once", action="store_true")

    a = sub.add_parser("approve", help="Approve a goal waiting at PLAN_GATE")
    a.add_argument("goal_id")

    st = sub.add_parser("status", help="Show persisted goal state")
    st.add_argument("goal_id")

    bg = sub.add_parser("background", help="Start an existing goal in a detached controller process")
    bg.add_argument("goal_id")

    stop = sub.add_parser("stop", help="Request a safe controller stop")
    stop.add_argument("goal_id")

    ub = sub.add_parser("unblock", help="Unblock a BLOCKED goal and retry from the appropriate phase")
    ub.add_argument("goal_id")
    ub.add_argument("--background", action="store_true", help="Re-launch controller in background after unblocking")

    w = sub.add_parser("web", help="Start the Labeeb Web API / UI server")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--reload", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    try:
        if args.command == "doctor":
            print(json.dumps(doctor(config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "start":
            ctl = LabeebController.create_goal(
                config,
                intent=load_intent(args),
                workspace=args.workspace,
                repo=args.repo,
                branch=args.branch,
                risk_tags=args.risk,
                allowed_paths=args.allow_path,
                validation_commands=args.validate,
                preauthorize_plan=args.preauthorize_plan,
                deadline_hours=args.deadline_hours,
            )
            print(json.dumps({"goal_id": ctl.goal_id, "state": str(ctl.paths.state)}, indent=2))
            if args.background:
                pid = ctl.start_background(str(pathlib.Path(__file__).resolve()))
                print(json.dumps({"background_pid": pid, "goal_id": ctl.goal_id}, indent=2))
            return 0
        if args.command == "web":
            import uvicorn
            from labeeb.api.app import create_app

            app = create_app(config)
            print(f"Starting Labeeb Controller Web Server on http://{args.host}:{args.port}")
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
            return 0

        ctl = LabeebController(config, args.goal_id)
        if args.command == "run":
            state = ctl.run(once=args.once)
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0 if state["phase"] == "PASS" else (2 if state["phase"] in TERMINAL_PHASES else 0)
        if args.command == "approve":
            ctl.approve_plan()
            print(json.dumps({"goal_id": ctl.goal_id, "phase": ctl.status()["phase"]}, indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(ctl.status(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "background":
            pid = ctl.start_background(str(pathlib.Path(__file__).resolve()))
            print(json.dumps({"goal_id": ctl.goal_id, "pid": pid}, indent=2))
            return 0
        if args.command == "stop":
            ctl.request_stop()
            print(json.dumps({"goal_id": ctl.goal_id, "stop_requested": True}, indent=2))
            return 0
        if args.command == "unblock":
            result = ctl.unblock_and_retry(background=args.background)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        raise ControllerError(f"Unknown command: {args.command}")
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ControllerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
