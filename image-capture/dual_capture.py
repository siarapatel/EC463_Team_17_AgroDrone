#!/usr/bin/env python3
"""
Dual IMX219 Sequential Capture Script for Raspberry Pi 5
Captures RAW (SRGGB10) and RGB888 images from two cameras with locked 3A parameters.
Supports RAM disk for high-speed captures with flush to permanent storage.
"""

import time

from dual_capture_functions import (
    ensure_dir,
    init_camera,
    sequential_capture_cycle,
)


def capture(
    outdir: str,
    exposure: int = 1000,
    gain: float = 1.0,
    jpeg_quality: int = 90,
    burst: int = 5,
    no_metadata: bool = False,
):
    class Args:
        def __init__(
            self,
            exposure=exposure,
            gain=gain,
            jpeg_quality=jpeg_quality,
            outdir=outdir,
            burst=burst,
            no_metadata=no_metadata,
        ):
            self.exposure = exposure
            self.gain = gain
            self.jpeg_quality = jpeg_quality
            self.outdir = outdir
            self.burst = burst
    picam0 = None
    picam1 = None

    try:
        args = Args()  # instantiate and assign
        working_dir = args.outdir
        # Create working directory
        ensure_dir(working_dir)
        # Initialize both cameras
        picam0 = init_camera(0, args.exposure, args.gain)
        picam1 = init_camera(1, args.exposure, args.gain)
        # Start both cameras (pre-start for minimal capture delay)
        picam0.start()
        picam1.start()
        # Settling time for 3A algorithms (auto-exposure, auto-white-balance) to converge
        print("Waiting for 3A algorithms to stabilize...")
        time.sleep(0.5)
        # Perform one burst capture set

        sequential_capture_cycle(
            picam0,
            picam1,
            working_dir,
            burst_count=args.burst,
            jpeg_quality=args.jpeg_quality,
            no_metadata=args.no_metadata,
        )

    finally:
        # Clean shutdown
        print("\nStopping cameras...")
        for cam in (picam0, picam1):
            if cam is None:
                continue
            try:
                cam.stop()
            except Exception as e:
                print(f"[cleanup] stop failed: {e}")
            try:
                cam.close()  # release hardware
            except Exception as e:
                print(f"[cleanup] close failed: {e}")
        print("Cameras stopped.")


if __name__ == "__main__":
    capture()
