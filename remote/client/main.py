"""CLI entrypoint for `max-client` (`python -m remote.client.main`)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
from typing import Sequence

from remote.auth import PairingConfig
from remote.client.agent import ClientAgent
from remote.client.status_ui import ClientStatusServer, launch_status_ui_window
from remote.drivers.base import DriverBundle
from remote.drivers.simulated import create_simulated_bundle


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="max-client",
        description="Remote 3ds Max Deployment Client Agent",
    )
    parser.add_argument("--connect-file", help="Path to pairing connect.json")
    parser.add_argument("--server-url", help="Operator or Relay server URL")
    parser.add_argument(
        "--mode",
        choices=("direct", "relay"),
        default="direct",
        help="Transport mode (direct or relay)",
    )
    parser.add_argument("--session-id", help="Pairing session ID")
    parser.add_argument("--token", help="Pairing bearer token")
    parser.add_argument("--data-dir", help="Persistent client state/logs directory")
    parser.add_argument("--iso-dir", help="ISO cache / download directory")
    parser.add_argument("--assets-dir", help="Optional post-config assets directory")
    parser.add_argument(
        "--driver",
        choices=("auto", "windows", "simulated"),
        default="auto",
        help="Driver bundle backend",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Do not open Edge/browser window for local Status UI",
    )
    parser.add_argument(
        "--status-port",
        type=int,
        default=8770,
        help="Localhost Status UI port (default: 8770, 0 for ephemeral)",
    )
    parser.add_argument("--client-id", help="Explicit client identifier")
    return parser


def _resolve_pairing(args: argparse.Namespace) -> PairingConfig | None:
    if args.connect_file:
        raw = Path(args.connect_file).read_text(encoding="utf-8")
        return PairingConfig.from_connect_json(raw)

    if args.server_url and args.session_id and args.token:
        return PairingConfig(
            mode=args.mode,
            server_url=args.server_url,
            session_id=args.session_id,
            token=args.token,
        )

    candidates: list[Path] = []
    if os.environ.get("MAX_APP_ROOT"):
        candidates.append(Path(os.environ["MAX_APP_ROOT"]) / "connect.json")
    candidates.append(Path.cwd() / "connect.json")
    for cand in candidates:
        if cand.is_file():
            return PairingConfig.from_connect_json(cand.read_text(encoding="utf-8"))
    return None


def _create_drivers(choice: str, data_dir: Path | None) -> DriverBundle:
    use_windows = choice == "windows" or (choice == "auto" and os.name == "nt")
    if use_windows:
        from remote.drivers.windows import create_windows_bundle

        return create_windows_bundle(data_dir=data_dir)
    sim_dir = (data_dir / "sim") if data_dir is not None else None
    return create_simulated_bundle(work_dir=sim_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    pairing = _resolve_pairing(args)
    data_dir = Path(args.data_dir) if args.data_dir else None
    drivers = _create_drivers(args.driver, data_dir)

    agent = ClientAgent(
        pairing=pairing,
        drivers=drivers,
        data_dir=data_dir,
        iso_dir=args.iso_dir,
        assets_dir=args.assets_dir,
        client_id=args.client_id,
    )
    status_server = ClientStatusServer(agent, host="127.0.0.1", port=args.status_port)
    status_server.start()
    launch_status_ui_window(status_server.url, headless=args.headless)

    def _handle_sig(_signum: int, _frame: object) -> None:
        agent.stop()

    prev_int = signal.signal(signal.SIGINT, _handle_sig)
    prev_term = signal.signal(signal.SIGTERM, _handle_sig)
    try:
        agent.start()
        agent.wait()
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        agent.stop()
        status_server.shutdown()

    return agent.exit_code


if __name__ == "__main__":
    sys.exit(main())
