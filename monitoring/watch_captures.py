#!/usr/bin/env python3
"""
Live watcher for capture metadata files.

Polls the flightplans directory every 2 seconds and prints a summary
line whenever a new *_metadata.json file appears. Use this during a
flight to confirm images are landing on disk.

If this shows captures arriving but monitor_events.py shows bad GPS,
the images exist but position data will be null — a FC/GPS issue.
If monitor_events.py shows healthy events but nothing appears here,
the ndvi-capture service is the problem.

Usage:
  python3 watch_captures.py
  python3 watch_captures.py --path /home/sr-design/agrodrone-system/flightplans
"""

import argparse
import glob
import json
import os
import time

SYSTEM_PATH = os.environ.get("SYSTEM_PATH", "/home/sr-design/agrodrone-system")
POLL_INTERVAL = 2.0


def scan(base: str) -> dict:
    """Return {filepath: mtime} for all metadata JSONs under base."""
    pattern = os.path.join(base, "**", "*_metadata.json")
    return {p: os.path.getmtime(p) for p in glob.glob(pattern, recursive=True)}


def summarise(path: str) -> str:
    try:
        with open(path) as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return f"  (could not read: {e})"

    pos   = meta.get("position") or {}
    wp    = meta.get("waypoint", "?")
    gps   = pos.get("gps_valid", False)
    lat   = pos.get("lat")
    lon   = pos.get("lon")
    alt   = pos.get("alt_rel_m")
    stale = pos.get("stale", True)

    coord = f"lat={lat:.6f} lon={lon:.6f}" if (gps and lat is not None) else "GPS INVALID"
    alt_s = f"alt={alt:.1f}m" if alt is not None else "alt=None"
    flags = []
    if not gps:
        flags.append("GPS!")
    if stale:
        flags.append("STALE!")
    flag_s = " " + " ".join(flags) if flags else ""

    cam0 = (meta.get("camera_0") or {}).get("capture_info", {}).get("image_path", "?")
    cam1 = (meta.get("camera_1") or {}).get("capture_info", {}).get("image_path", "?")

    return (
        f"  wp={wp} | {coord} | {alt_s}{flag_s}\n"
        f"  cam0: {cam0}\n"
        f"  cam1: {cam1}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch for new capture metadata files")
    parser.add_argument(
        "--path",
        default=os.path.join(SYSTEM_PATH, "flightplans"),
        help="Directory to watch (searched recursively)",
    )
    args = parser.parse_args()

    base = args.path
    if not os.path.isdir(base):
        print(f"[watch] Directory not found: {base}")
        print(f"[watch] Set SYSTEM_PATH or pass --path to override.")

    print(f"[watch] Watching {base} for new captures (poll every {POLL_INTERVAL:.0f}s)")
    print("[watch] Ctrl+C to stop\n")

    known = scan(base) if os.path.isdir(base) else {}
    if known:
        print(f"[watch] {len(known)} existing capture(s) found — watching for new ones\n")

    total_new = 0
    try:
        while True:
            time.sleep(POLL_INTERVAL)
            if not os.path.isdir(base):
                continue
            current = scan(base)
            new_files = sorted(p for p in current if p not in known)
            for path in new_files:
                ts = time.strftime("%H:%M:%S")
                fname = os.path.basename(path)
                print(f"[{ts}] NEW CAPTURE: {fname}")
                print(summarise(path))
                print()
                total_new += 1
            known = current
    except KeyboardInterrupt:
        print(f"\n[watch] Stopped. {total_new} new capture(s) observed this session.")


if __name__ == "__main__":
    main()
