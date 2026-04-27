#!/usr/bin/env python3
"""
Pre/post-flight health check for the AgroDrone capture pipeline.

Prints PASS / WARN / FAIL for each check so you can quickly confirm
the system is ready before launch or diagnose what went wrong after.

Usage:
  python3 health_check.py
"""

import glob
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time

SYSTEM_PATH       = os.environ.get("SYSTEM_PATH", "/home/sr-design/agrodrone-system")
EVENTS_SOCKET     = os.environ.get("MSP_EVENTS_SOCKET_PATH", "/run/agrodrone/msp-events.sock")
CONTROL_SOCKET    = os.environ.get("MSP_CONTROL_SOCKET_PATH", "/run/agrodrone/msp-control.sock")
WARN_FREE_MB      = 500
SERVICES          = ["agrodrone-msp-uart.service", "ndvi-capture.service"]
JOURNAL_LINES     = 30

PASS = "\033[32mPASS\033[0m"
WARN = "\033[33mWARN\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def result(label: str, status: str, detail: str = "") -> None:
    detail_str = f"  {detail}" if detail else ""
    print(f"  [{status}] {label}{detail_str}")


def check_services() -> None:
    print("\n── Services ──")
    for svc in SERVICES:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()
            if out == "active":
                result(svc, PASS, "active")
            else:
                result(svc, FAIL, f"state={out!r} — run: journalctl -u {svc} -n 30")
        except FileNotFoundError:
            result(svc, WARN, "systemctl not available (not on Pi?)")
        except subprocess.TimeoutExpired:
            result(svc, FAIL, "systemctl timed out")


def check_sockets() -> None:
    print("\n── IPC Sockets ──")
    for path in (EVENTS_SOCKET, CONTROL_SOCKET):
        name = os.path.basename(path)
        if not os.path.exists(path):
            result(name, FAIL, f"missing: {path}")
        elif not stat.S_ISSOCK(os.stat(path).st_mode):
            result(name, FAIL, f"exists but is not a socket: {path}")
        else:
            result(name, PASS, path)


def check_events_socket_responsive() -> None:
    print("\n── Events socket responsiveness ──")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(EVENTS_SOCKET)
        # Read one event with a short timeout
        s.settimeout(3.0)
        data = s.recv(4096)
        s.close()
        if data:
            try:
                event = json.loads(data.split(b"\n")[0])
                pos = event.get("position") or {}
                gps = pos.get("gps_valid", False)
                wp  = event.get("waypoint", 0)
                result("Live event received", PASS,
                       f"waypoint={wp} gps_valid={gps}")
                if not gps:
                    result("GPS validity", WARN,
                           "gps_valid=false — FC may not have a fix yet, or GPS not connected")
                else:
                    result("GPS validity", PASS, "gps_valid=true")
            except (json.JSONDecodeError, IndexError):
                result("Live event received", WARN, "received data but could not parse JSON")
        else:
            result("Live event received", WARN, "connected but received no data within 3s")
    except (FileNotFoundError, ConnectionRefusedError) as e:
        result("Events socket connect", FAIL,
               f"{e} — agrodrone-msp-uart.service may be down")
    except socket.timeout:
        result("Live event received", WARN,
               "connected but no event in 3s — service running but FC not communicating?")


def check_disk() -> None:
    print("\n── Disk space ──")
    try:
        usage = shutil.disk_usage(SYSTEM_PATH)
        free_mb = usage.free // (1024 * 1024)
        pct_used = 100 * usage.used // usage.total
        detail = f"{free_mb} MB free ({pct_used}% used) on {SYSTEM_PATH}"
        if free_mb < WARN_FREE_MB:
            result("Disk space", FAIL if free_mb < 100 else WARN, detail)
        else:
            result("Disk space", PASS, detail)
    except FileNotFoundError:
        result("Disk space", WARN, f"SYSTEM_PATH not found: {SYSTEM_PATH}")


def check_captures() -> None:
    print("\n── Recent captures ──")
    pattern = os.path.join(SYSTEM_PATH, "flightplans", "**", "*_metadata.json")
    files = sorted(glob.glob(pattern, recursive=True))

    if not files:
        result("Metadata files", WARN, f"none found under {SYSTEM_PATH}/flightplans/")
        return

    result("Metadata files found", PASS, f"{len(files)} total")

    latest = files[-1]
    age = time.time() - os.path.getmtime(latest)
    age_str = f"{age:.0f}s ago" if age < 120 else f"{age/60:.1f}m ago"
    result("Most recent capture", PASS if age < 300 else WARN,
           f"{os.path.basename(latest)} ({age_str})")

    try:
        with open(latest) as f:
            meta = json.load(f)
        pos = meta.get("position") or {}
        wp  = meta.get("waypoint", "?")
        gps = pos.get("gps_valid", False)
        lat = pos.get("lat")
        lon = pos.get("lon")
        coord_str = f"lat={lat:.6f} lon={lon:.6f}" if (gps and lat) else "no valid coords"
        result(f"Latest metadata (wp={wp})", PASS if gps else WARN,
               f"gps_valid={gps} {coord_str}")
    except (json.JSONDecodeError, OSError) as e:
        result("Latest metadata parse", FAIL, str(e))


def check_journal() -> None:
    print("\n── Recent journal errors ──")
    for svc in SERVICES:
        try:
            out = subprocess.run(
                ["journalctl", "-u", svc, "-n", str(JOURNAL_LINES), "--no-pager", "-o", "short"],
                capture_output=True, text=True, timeout=5
            )
            lines = out.stdout.strip().splitlines()
            errors = [l for l in lines if any(k in l.lower() for k in ("error", "fail", "fatal", "exception", "traceback"))]
            if errors:
                result(svc, WARN, f"{len(errors)} error line(s) in last {JOURNAL_LINES} log entries:")
                for e in errors[-5:]:
                    print(f"       {e}")
            else:
                result(svc, PASS, f"no errors in last {JOURNAL_LINES} log entries")
        except FileNotFoundError:
            result(svc, WARN, "journalctl not available")
        except subprocess.TimeoutExpired:
            result(svc, WARN, "journalctl timed out")


def main() -> None:
    print("=" * 60)
    print("  AgroDrone Capture Pipeline — Health Check")
    print("=" * 60)

    check_services()
    check_sockets()
    check_events_socket_responsive()
    check_disk()
    check_captures()
    check_journal()

    print("\n" + "=" * 60)
    print("  Done. Fix any FAIL items before launch.")
    print("  WARN items may be acceptable depending on mission phase.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
