#!/usr/bin/env python3
import os
import time
import json
import signal
import pathlib
import multiprocessing as mp
from datetime import datetime

# Optional fallback for RAW->.npy
try:
    import numpy as np
except Exception:
    np = None

# Picamera2 imports
from picamera2 import Picamera2
try:
    # DNGWriter is available on recent picamera2 builds
    from picamera2 import DNGWriter
    HAVE_DNG = True
except Exception:
    HAVE_DNG = False


def _ensure_dir(path: str):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def meter_and_lock(picam: Picamera2, meter_time_s: float = 1.0):
    """
    Let the camera run with auto-exposure/auto-WB briefly to find good values,
    then lock AE/AWB by freezing those values.
    """
    picam.start()
    time.sleep(meter_time_s)  # allow 3A to settle

    md = picam.capture_metadata()
    # Extract current settings
    exp_us = md.get("ExposureTime")              # in microseconds
    again   = md.get("AnalogueGain")
    cgains  = md.get("ColourGains")              # (R, B) gains

    # Safety defaults if metadata missing
    if exp_us is None: exp_us = 10000
    if again  is None: again  = 1.0
    if cgains is None: cgains = (1.0, 1.0)

    # Disable auto and set fixed controls
    picam.set_controls({
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": int(exp_us),
        "AnalogueGain": float(again),
        "ColourGains": (float(cgains[0]), float(cgains[1])),
    })

    # Give driver a moment to apply locks
    time.sleep(0.05)
    return {
        "ExposureTime_us": int(exp_us),
        "AnalogueGain": float(again),
        "ColourGains": [float(cgains[0]), float(cgains[1])]
    }


def camera_worker(cam_index: int,
                  outdir: str,
                  stamp: str,
                  ready_barrier: mp.Barrier,
                  go_event: mp.Event,
                  write_json_meta: bool = True):
    """
    One process per camera:
      1) Open camera
      2) Configure for stills with RAW stream
      3) Meter, lock AE/AWB
      4) Wait for parent 'go' event
      5) Capture JPEG + RAW (DNG or NPY)
    """
    try:
        _ensure_dir(outdir)
        picam = Picamera2(camera_num=cam_index)

        # Create still config WITH a RAW stream enabled for DNG/RAW saving.
        # We let Picamera2 choose sensible sizes; you can set `main={"size": (4056, 3040)}`
        # for HQ sensors, etc., if you want full res.
        cfg = picam.create_still_configuration(
            main={},                      # default full-res
            lores=None,
            raw={}                        # enable raw stream
        )
        picam.configure(cfg)

        # Meter and lock exposure/WB
        metered = meter_and_lock(picam, meter_time_s=1.0)

        # Signal we’re ready (both processes will sync here)
        ready_barrier.wait()

        # Wait for the parent to say "go" so both cams capture together
        go_event.wait()

        # Filenames (aligned by common timestamp)
        base = f"{stamp}_cam{cam_index}"
        jpg_path = os.path.join(outdir, f"{base}.jpg")
        dng_path = os.path.join(outdir, f"{base}.dng")
        rawnpy_path = os.path.join(outdir, f"{base}.raw.npy")
        meta_path = os.path.join(outdir, f"{base}.json")

        # Capture JPEG (main stream)
        # Keep running; still capture is quick if already started.
        picam.capture_file(jpg_path, name="main")

        # Capture RAW
        raw_md = picam.capture_metadata()  # metadata near time of capture

        saved_raw = False
        if HAVE_DNG:
            try:
                dng = DNGWriter(picam)
                # DNGWriter will grab the most recent RAW buffer from the camera
                dng.capture(dng_path, raw_md)
                saved_raw = True
            except Exception:
                saved_raw = False

        # Fallback: save raw buffer to .npy if DNG unavailable
        if not saved_raw:
            # Grab raw as numpy array and save (requires numpy)
            if np is None:
                # Couldn’t save RAW; leave only JPEG
                pass
            else:
                # `capture_arrays` returns dict when multiple streams are present.
                # We request raw stream only for safety.
                raw_frame = picam.capture_array("raw")
                np.save(rawnpy_path, raw_frame)

        # Save simple metadata useful for post-processing/registration
        if write_json_meta:
            all_md = {
                "camera_index": cam_index,
                "timestamp": stamp,
                "metered_locks": metered,
                "raw_saved": "dng" if saved_raw else ("npy" if np is not None else "none"),
                "paths": {
                    "jpeg": jpg_path,
                    "dng": dng_path if saved_raw else None,
                    "raw_npy": rawnpy_path if (not saved_raw and np is not None) else None
                },
                "capture_metadata": raw_md
            }
            # Some metadata keys may not be JSON serializable; make safe
            def _safe(o):
                try:
                    json.dumps(o)
                    return o
                except Exception:
                    return str(o)

            safe_md = {k: _safe(v) for k, v in all_md.items()}
            with open(meta_path, "w") as f:
                json.dump(safe_md, f, indent=2)

        # Small grace before stopping
        time.sleep(0.05)
        picam.stop()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Best-effort logging to stdout (visible in console)
        print(f"[cam{cam_index}] ERROR: {e}")
    finally:
        try:
            picam.stop()
        except Exception:
            pass


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Dual CSI capture (JPEG + RAW) with exposure/WB lock for NDVI.")
    ap.add_argument("--outdir", default="captures", help="Output directory")
    ap.add_argument("--pair-count", type=int, default=1, help="How many synchronized pairs to capture")
    ap.add_argument("--interval", type=float, default=0.0, help="Seconds between pairs (>=0)")
    args = ap.parse_args()

    _ensure_dir(args.outdir)

    # Clean shutdown on Ctrl+C
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    for i in range(args.pair_count):
        # Common timestamp for both cameras (UTC-ish, filesystem-safe)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S_%f")[:-3]  # millisecond resolution

        # Barrier for “both ready” + event for “go now”
        ready_barrier = mp.Barrier(2)
        go_event = mp.Event()

        # Start both workers
        p0 = mp.Process(target=camera_worker, args=(0, args.outdir, stamp, ready_barrier, go_event))
        p1 = mp.Process(target=camera_worker, args=(1, args.outdir, stamp, ready_barrier, go_event))
        p0.start(); p1.start()

        # Wait until both children report “ready”
        ready_barrier.wait()

        # Fire both at once
        go_event.set()

        # Join
        p0.join(); p1.join()

        if i + 1 < args.pair_count and args.interval > 0:
            time.sleep(args.interval)


if __name__ == "__main__":
    mp.set_start_method("spawn")  # robust on Pi OS
    main()
