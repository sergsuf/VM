"""CLI entrypoint for `max-operator` (`python -m remote.operator.main`)."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from typing import Sequence
import webbrowser

from remote.operator.server import OperatorServer


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="max-operator",
        description="Remote 3ds Max Deployment Operator Control Panel",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--iso-dir", help="Directory containing 3ds Max ISO images")
    parser.add_argument("--assets-dir", help="Directory containing post-config assets")
    parser.add_argument("--updates-dir", help="Directory containing client app update bundles")
    parser.add_argument(
        "--mode",
        choices=("direct", "relay"),
        default="direct",
        help="Transport mode (direct or relay)",
    )
    parser.add_argument("--relay-url", help="Relay server base URL (required in relay mode)")
    parser.add_argument("--session-id", help="Explicit session ID")
    parser.add_argument("--token", help="Explicit bearer token")
    parser.add_argument("--public-url", help="Public/LAN URL advertised in pairing config")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the Operator Web UI in a browser",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.mode == "relay" and not args.relay_url:
        parser.error("--relay-url is required when --mode relay")

    server = OperatorServer(
        host=args.host,
        port=args.port,
        iso_dir=args.iso_dir,
        assets_dir=args.assets_dir,
        updates_dir=args.updates_dir,
        mode=args.mode,
        relay_url=args.relay_url,
        session_id=args.session_id,
        token=args.token,
        public_url=args.public_url,
    )
    server.start()

    pairing = server.pairing
    print(f"Operator Web UI : {server.base_url}", flush=True)
    print(f"Mode            : {pairing.mode}", flush=True)
    print(f"Session ID      : {pairing.session_id}", flush=True)
    print(f"Pairing URI     : {pairing.to_pairing_uri()}", flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(server.base_url)
        except Exception:
            pass

    stop_event = threading.Event()

    def _handle_sig(_signum: int, _frame: object) -> None:
        stop_event.set()

    prev_int = signal.signal(signal.SIGINT, _handle_sig)
    prev_term = signal.signal(signal.SIGTERM, _handle_sig)
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=0.5)
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
