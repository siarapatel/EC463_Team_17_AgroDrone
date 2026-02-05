#!/usr/bin/env python3
"""
Dual IMX219 Sequential Capture Script for Raspberry Pi 5
Captures RGB888 images from two cameras with modifiable 3A parameters.
"""

import os
import time
import json
import pathlib
from datetime import datetime, timezone
from PIL import Image
from picamera2 import Picamera2


def ensure_dir(path: str):
    """Create directory if it doesn't exist."""
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def init_camera(cam_num: int, exposure_time: int, analogue_gain: float):
    """
    Args:
        cam_num: Camera index (0 or 1)
        exposure_time: Not used when 3A is enabled (kept for API compatibility)
        analogue_gain: Not used when 3A is enabled (kept for API compatibility)
    Returns:
        Configured Picamera2 instance
    """
    picam = Picamera2(cam_num)

    # Can have 2 streams if desired
    # Main stream: RGB888 for PNG output
    # RAW stream: SRGGB10 unpacked format - deprecated
    # Display: None for performance
    config = picam.create_still_configuration(
        main={"format": "RGB888"},  # RGB888 for main stream
        display=None,  # No display for faster operation
        buffer_count=2,  # Minimal buffer count - research
    )

    picam.configure(config)

    # Enable 3A parameters for automatic adjustments and prettier pictures
    picam.set_controls(
        {
            "AeEnable": True,  # Enable auto-exposure
            "AwbEnable": True,  # Enable auto white balance
            # Manual controls (ExposureTime, AnalogueGain, ColourGains) are not set
            # when auto modes are enabled - the camera adjusts these automatically
        }
    )

    return picam


def capture_from_camera(
    picam: Picamera2, # Currently configured Picamera2 instance
    cam_num: int, # Track camera 0 or 1 for metadata
    timestamp: str, # Want timestamps for metadata
    outdir: str, # Pi OS file directory to be saved to
    burst_count: int = 1, # number frames per capture
    jpeg_quality: int = 90, # 0 = most losses, 100 = least losse
):
    """
    Capture RGB888 images from a single camera with burst mode.
    Returns:
        Dictionary containing capture metadata for all burst captures
    """
    burst_captures = []

    for burst_idx in range(burst_count):
        # Was formating as a capture request when getting RGB and RAW files.
        # This ensured main and raw come from the same frame
        request = picam.capture_request()
        try:
            # Extract arrays from rgb stream
            rgb_array = request.make_array("main")
            # Get metadata from the request
            metadata = request.get_metadata()
        finally:
            # Need to release the request to free buffers
            request.release()

        # Generate filenames with burst index
        base_name = f"{timestamp}_cam{cam_num}_burst{burst_idx:02d}"
        rgb_path = os.path.join(outdir, f"{base_name}_rgb.jpg")

        # Save RGB888 as JPEG (much faster than PNG)
        rgb_image = Image.fromarray(rgb_array, mode="RGB")
        # Save argument comes from PIL. "Optimize" doesn't seem benefitial to our use case
        rgb_image.save(rgb_path, format="JPEG", quality=jpeg_quality, optimize=False)

        # Prepare metadata for this capture
        capture_info = {
            "burst_index": burst_idx,
            "rgb_path": rgb_path,
            "rgb_shape": list(rgb_array.shape),
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
    picam0: Picamera2, #camera0 instance
    picam1: Picamera2, #camera1 instance
    outdir: str, #output directory
    burst_count: int = 1, #Number of captures taken by each camera in successive burst
    jpeg_quality: int = 90, # 0 = most losses, 100 = least losse.
    no_metadata: bool = False, 
):
    """
    Perform one complete capture cycle from both cameras sequentially.
    Returns:
        Tuple of (timestamp, capture_info_dict)
    """
    # Generate timestamp for this capture cycle
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    # outdir set by systemd environment
    ensure_dir(outdir)

    # Capture from Camera 0 (with burst)
    start_cam0 = time.time()
    cam0_info = capture_from_camera(picam0, 0, timestamp, outdir, burst_count, jpeg_quality)
    cam0_duration = time.time() - start_cam0

    # Capture from Camera 1 (immediately after Camera 0 completes)
    start_cam1 = time.time()
    cam1_info = capture_from_camera(picam1, 1, timestamp, outdir, burst_count, jpeg_quality)
    cam1_duration = time.time() - start_cam1

    # Calculate inter-camera delay for testing
    # inter_camera_delay = start_cam1 - start_cam0

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
            # "inter_camera_delay_s": inter_camera_delay,
        },
        "camera_0": cam0_info,
        "camera_1": cam1_info,
    }

    if not no_metadata:
        metadata_path = os.path.join(outdir, f"{timestamp}_metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata_dict, f, indent=2, default=str)



    return timestamp, {"camera_0": cam0_info, "camera_1": cam1_info}
