#!/usr/bin/env python3
"""
Connect to AgroDrone's events socket and print each message with a timestamp.

Useful for quickly validating what `msp_uart_service.py` is publishing during
bench tests.

Examples:

  python3 watch_events.py
  python3 watch_events.py --socket /run/agrodrone/msp-events.sock
"""

import argparse
import json
import select
import socket
from datetime import datetime


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch AgroDrone mission status events")
    parser.add_argument(
        "--socket",
        default="/run/agrodrone/msp-events.sock",
        help="Path to the Unix events socket",
    )
    args = parser.parse_args()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(args.socket)
    print(f"[watch-events] Connected to {args.socket}")

    buf = b""
    try:
        while True:
            r, _, _ = select.select([sock], [], [], 1.0)
            if not r:
                continue

            chunk = sock.recv(4096)
            if not chunk:
                print("[watch-events] Socket closed by peer")
                break

            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                if not line_bytes:
                    continue

                now = datetime.now().isoformat(timespec="seconds")
                try:
                    payload = json.loads(line_bytes)
                except json.JSONDecodeError:
                    print(f"[{now}] raw={line_bytes!r}")
                    continue

                print(f"[{now}] {json.dumps(payload, sort_keys=True)}")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
