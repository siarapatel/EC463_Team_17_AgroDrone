#!/usr/bin/env python3
"""
Live monitor for the MSP events socket.

Subscribes to msp-events.sock and prints one formatted line per event.
Use this during or after a flight to determine whether the flight controller
is sending good data to the Pi.

Diagnosis guide:
  gps_valid=N or stale=Y  → FC not providing valid GPS — problem is the FC or antenna
  waypoint stuck at 0     → Mission not loaded or not started on FC
  No events at all        → agrodrone-msp-uart.service may be down, or FC not talking
  Events healthy, no captures → Problem is on Pi side (ndvi-capture service)

Usage:
  python3 monitor_events.py
  python3 monitor_events.py --socket /run/agrodrone/msp-events.sock
"""

import argparse
import json
import os
import select
import socket
import sys
import time

DEFAULT_SOCKET = os.environ.get("MSP_EVENTS_SOCKET_PATH", "/run/agrodrone/msp-events.sock")
STALE_ALERT_SECS = 2.0
MAX_CONNECT_ATTEMPTS = 10
CONNECT_DELAY = 2.0


def connect(path: str) -> socket.socket:
    for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            print(f"[monitor] Connected to {path}")
            return s
        except (FileNotFoundError, ConnectionRefusedError) as e:
            print(f"[monitor] Cannot connect (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}): {e}")
            if attempt < MAX_CONNECT_ATTEMPTS:
                time.sleep(CONNECT_DELAY)
    print("[monitor] FATAL: could not connect to events socket. Is agrodrone-msp-uart running?")
    sys.exit(1)


def fmt_event(event: dict, gps_ever_valid: bool) -> str:
    pos       = event.get("position") or {}
    wp        = event.get("waypoint", 0)
    complete  = event.get("mission_complete", False)
    gps_valid = pos.get("gps_valid", False)
    alt_valid = pos.get("alt_valid", False)
    stale     = pos.get("stale", True)
    lat       = pos.get("lat")
    lon       = pos.get("lon")
    alt       = pos.get("alt_rel_m")

    ts = time.strftime("%H:%M:%S")

    if complete:
        status = "MISSION COMPLETE"
    elif wp > 0:
        status = f"WP={wp:>3}"
    else:
        status = "IDLE    "

    gps_str = f"lat={lat:>12.7f} lon={lon:>13.7f}" if (gps_valid and lat is not None) else "GPS INVALID             "
    alt_str = f"alt={alt:>6.1f}m" if (alt_valid and alt is not None) else "alt=------"

    flags = []
    if not gps_valid:
        flags.append("GPS!")
    if not alt_valid:
        flags.append("ALT!")
    if stale:
        flags.append("STALE!")
    if not gps_ever_valid:
        flags.append("GPS-NEVER-VALID")
    flag_str = " [" + " ".join(flags) + "]" if flags else ""

    return f"[{ts}] {status} | {gps_str} | {alt_str}{flag_str}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Live MSP events monitor")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Path to msp-events.sock")
    args = parser.parse_args()

    print(f"[monitor] Connecting to {args.socket} ...")
    sock = connect(args.socket)

    buf = b""
    last_event_time = time.monotonic()
    gps_ever_valid = False
    event_count = 0

    print("[monitor] Listening — Ctrl+C to stop\n")
    print(f"  {'TIME':>8}  {'STATUS':>12}  {'POSITION':>36}  {'ALT':>10}")
    print("  " + "-" * 80)

    try:
        while True:
            r, _, _ = select.select([sock], [], [], 1.0)

            now = time.monotonic()
            gap = now - last_event_time

            if not r:
                if gap > STALE_ALERT_SECS:
                    print(f"[monitor] WARNING: no event for {gap:.0f}s — socket may be dead or service down")
                continue

            chunk = sock.recv(4096)
            if not chunk:
                print("[monitor] Socket closed by server.")
                break

            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                try:
                    event = json.loads(line_bytes)
                except json.JSONDecodeError:
                    continue

                if event.get("type") != "mission_status":
                    continue

                pos = event.get("position") or {}
                if pos.get("gps_valid"):
                    gps_ever_valid = True

                last_event_time = time.monotonic()
                event_count += 1
                print(fmt_event(event, gps_ever_valid))

    except KeyboardInterrupt:
        print(f"\n[monitor] Stopped. Total events received: {event_count}")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
