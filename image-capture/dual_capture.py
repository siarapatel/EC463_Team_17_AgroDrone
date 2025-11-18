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


def capture():
    class Args:
        def __init__(
            self,
            exposure=1000,
            gain=1.0,
            jpeg_quality=90,
            outdir="captures",
            burst=5,
            no_metadata=False,
        ):
            self.exposure = exposure
            self.gain = gain
            self.jpeg_quality = jpeg_quality
            self.outdir = outdir
            self.burst = burst
            self.no_metadata = no_metadata

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

        # Brief settling time for 3A locks to take effect
        time.sleep(0.2)

        # Perform one burst capture set
        sequential_capture_cycle(
            picam0,
            picam1,
            working_dir,
            burst_count=args.burst,
            jpeg_quality=args.jpeg_quality,
        )

    finally:
        # Clean shutdown
        print("\nStopping cameras...")
        for cam in (picam0, picam1):
            try:
                cam.stop()
                cam.close()  # release hardware
            except Exception:
                pass
        print("Cameras stopped.")


if __name__ == "__main__":
    capture()
