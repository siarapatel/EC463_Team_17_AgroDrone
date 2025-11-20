#!/usr/bin/env python3
"""
Dual IMX219 Sequential Capture Script for Raspberry Pi 5
Captures RAW (SRGGB10) and RGB888 images from two cameras with locked 3A parameters.
Supports RAM disk for high-speed captures with flush to permanent storage.
"""

import os
import time
import json
import pathlib
import shutil
from datetime import datetime, timezone
from typing import Dict
import numpy as np
from PIL import Image
from picamera2 import Picamera2


def ensure_dir(path: str):
    """Create directory if it doesn't exist."""
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def init_camera(cam_num: int, exposure_time: int, analogue_gain: float):
    """
    Initialize and configure a single camera with locked 3A parameters.

    Args:
        cam_num: Camera index (0 or 1)
        exposure_time: Exposure time in microseconds
        analogue_gain: Analogue gain value

    Returns:
        Configured Picamera2 instance
    """
    picam = Picamera2(cam_num)

    # Create still configuration with RAW and main (RGB) streams
    # RAW stream: SRGGB10 unpacked format
    # Main stream: RGB888 for PNG output
    # Display: None for performance
    config = picam.create_still_configuration(
        main={"format": "RGB888"},  # RGB888 for main stream
        raw={"format": "SRGGB10"},  # RAW unpacked 10-bit Bayer
        display=None,  # No display for faster operation
        buffer_count=2,  # Minimal buffer count
    )

    picam.configure(config)

    # Lock all 3A parameters
    picam.set_controls(
        {
            "AeEnable": False,  # Disable auto-exposure
            "AwbEnable": False,  # Disable auto white balance
            "ExposureTime": int(exposure_time),
            "AnalogueGain": float(analogue_gain),
            "ColourGains": (1.0, 1.0),  # Fixed color gains as specified
        }
    )

    return picam


def capture_from_camera(
    picam: Picamera2,
    cam_num: int,
    timestamp: str,
    outdir: str,
    burst_count: int = 1,
    jpeg_quality: int = 90,
):
    """
    Capture RAW and RGB888 images from a single camera with burst mode.

    Args:
        picam: Configured Picamera2 instance
        cam_num: Camera number (0 or 1)
        timestamp: Timestamp string for filenames
        outdir: Output directory path
        burst_count: Number of consecutive frames to capture
        jpeg_quality: JPEG quality for RGB images (1-100)

    Returns:
        Dictionary containing capture metadata for all burst captures
    """
    burst_captures = []

    for burst_idx in range(burst_count):
        # Capture both streams atomically using a request
        # This ensures main and raw come from the same frame
        request = picam.capture_request()
        try:
            # Extract arrays from both streams
            rgb_array = request.make_array("main")
            raw_array = request.make_array("raw")

            # Get metadata from the request
            metadata = request.get_metadata()
        finally:
            # Always release the request to free buffers
            request.release()

        # Generate filenames with burst index
        base_name = f"{timestamp}_cam{cam_num}_burst{burst_idx:02d}"
        rgb_path = os.path.join(outdir, f"{base_name}_rgb.jpg")
        raw_path = os.path.join(outdir, f"{base_name}_raw.npy")

        # Save RGB888 as JPEG (much faster than PNG)
        rgb_image = Image.fromarray(rgb_array, mode="RGB")
        rgb_image.save(rgb_path, format="JPEG", quality=jpeg_quality, optimize=False)

        # Save RAW as numpy array
        np.save(raw_path, raw_array)

        # Prepare metadata for this capture
        capture_info = {
            "burst_index": burst_idx,
            "rgb_path": rgb_path,
            "raw_path": raw_path,
            "rgb_shape": list(rgb_array.shape),
            "raw_shape": list(raw_array.shape),
            "raw_dtype": str(raw_array.dtype),
            "metadata": {
                "ExposureTime": metadata.get("ExposureTime"),
                "AnalogueGain": metadata.get("AnalogueGain"),
                "ColourGains": metadata.get("ColourGains"),
                "SensorTimestamp": metadata.get("SensorTimestamp"),
            },
        }

        burst_captures.append(capture_info)

    # Return consolidated info
    return {
        "camera_index": cam_num,
        "timestamp": timestamp,
        "burst_count": burst_count,
        "jpeg_quality": jpeg_quality,
        "burst_captures": burst_captures,
    }


def sequential_capture_cycle(
    picam0: Picamera2,
    picam1: Picamera2,
    outdir: str,
    burst_count: int = 1,
    jpeg_quality: int = 90,
):
    """
    Perform one complete capture cycle from both cameras sequentially.

    Args:
        picam0: Camera 0 instance
        picam1: Camera 1 instance
        outdir: Output directory
        burst_count: Number of burst captures per camera
        jpeg_quality: JPEG quality for RGB images
        save_metadata: Whether to save JSON metadata file

    Returns:
        Tuple of (timestamp, capture_info_dict)
    """
    # Generate timestamp for this capture cycle
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")

    # Set out dir
    outdir = os.path.expanduser("~/export")

    # Capture from Camera 0 (with burst)
    start_cam0 = time.time()
    cam0_info = capture_from_camera(
        picam0, 0, timestamp, outdir, burst_count, jpeg_quality
    )
    cam0_duration = time.time() - start_cam0

    # Capture from Camera 1 (immediately after Camera 0 completes)
    start_cam1 = time.time()
    cam1_info = capture_from_camera(
        picam1, 1, timestamp, outdir, burst_count, jpeg_quality
    )
    cam1_duration = time.time() - start_cam1

    # Calculate inter-camera delay
    inter_camera_delay = start_cam1 - start_cam0

    # Save metadata JSON if requested
    metadata_dict = {
        "capture_timestamp": timestamp,
        "burst_count": burst_count,
        "jpeg_quality": jpeg_quality,
        "timing": {
            "camera_0_duration_s": cam0_duration,
            "camera_0_per_frame_s": cam0_duration / burst_count,
            "camera_1_duration_s": cam1_duration,
            "camera_1_per_frame_s": cam1_duration / burst_count,
            "inter_camera_delay_s": inter_camera_delay,
        },
        "camera_0": cam0_info,
        "camera_1": cam1_info,
    }

    metadata_path = os.path.join(outdir, f"{timestamp}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata_dict, f, indent=2, default=str)

    return timestamp, {"camera_0": cam0_info, "camera_1": cam1_info}

