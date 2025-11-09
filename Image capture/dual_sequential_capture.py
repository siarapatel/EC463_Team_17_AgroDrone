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
import numpy as np
from PIL import Image
from picamera2 import Picamera2


def ensure_dir(path: str):
    """Create directory if it doesn't exist."""
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def flush_ramdisk_to_storage(ramdisk_path: str, final_path: str):
    """
    Copy all files from RAM disk to permanent storage.
    
    Args:
        ramdisk_path: Source directory (RAM disk)
        final_path: Destination directory (permanent storage)
    
    Returns:
        Tuple of (files_copied, total_bytes)
    """
    print(f"\n{'='*60}")
    print("Flushing RAM disk to permanent storage...")
    print(f"{'='*60}")
    print(f"Source: {ramdisk_path}")
    print(f"Destination: {final_path}")
    
    ensure_dir(final_path)
    
    files_copied = 0
    total_bytes = 0
    start_time = time.time()
    
    # Copy all files from ramdisk to final destination
    for item in os.listdir(ramdisk_path):
        src = os.path.join(ramdisk_path, item)
        dst = os.path.join(final_path, item)
        
        if os.path.isfile(src):
            file_size = os.path.getsize(src)
            shutil.copy2(src, dst)
            files_copied += 1
            total_bytes += file_size
            print(f"  [{files_copied}] {item} ({file_size / 1024 / 1024:.2f} MB)")
    
    duration = time.time() - start_time
    total_mb = total_bytes / 1024 / 1024
    speed_mbps = total_mb / duration if duration > 0 else 0
    
    print(f"\nFlush complete:")
    print(f"  - Files copied: {files_copied}")
    print(f"  - Total size: {total_mb:.2f} MB")
    print(f"  - Duration: {duration:.2f}s")
    print(f"  - Transfer speed: {speed_mbps:.2f} MB/s")
    print(f"{'='*60}")
    
    return files_copied, total_bytes


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
    print(f"Initializing Camera {cam_num}...")
    picam = Picamera2(cam_num)
    
    # Create still configuration with RAW and main (RGB) streams
    # RAW stream: SRGGB10 unpacked format
    # Main stream: RGB888 for PNG output
    # Display: None for performance
    config = picam.create_still_configuration(
        main={"format": "RGB888"},  # RGB888 for main stream
        raw={"format": "SRGGB10"},  # RAW unpacked 10-bit Bayer
        display=None,  # No display for faster operation
        buffer_count=2  # Minimal buffer count
    )
    
    picam.configure(config)
    
    # Lock all 3A parameters
    picam.set_controls({
        "AeEnable": False,  # Disable auto-exposure
        "AwbEnable": False,  # Disable auto white balance
        "ExposureTime": int(exposure_time),
        "AnalogueGain": float(analogue_gain),
        "ColourGains": (1.0, 1.0)  # Fixed color gains as specified
    })
    
    print(f"Camera {cam_num} configured:")
    print(f"  - RAW format: SRGGB10 (unpacked)")
    print(f"  - RGB format: RGB888")
    print(f"  - ExposureTime: {exposure_time} µs")
    print(f"  - AnalogueGain: {analogue_gain}")
    print(f"  - ColourGains: (1.0, 1.0)")
    
    return picam


def capture_from_camera(picam: Picamera2, cam_num: int, timestamp: str, outdir: str):
    """
    Capture RAW and RGB888 images from a single camera.
    
    Args:
        picam: Configured Picamera2 instance
        cam_num: Camera number (0 or 1)
        timestamp: Timestamp string for filenames
        outdir: Output directory path
    
    Returns:
        Dictionary containing capture metadata
    """
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
    
    # Generate filenames
    base_name = f"{timestamp}_cam{cam_num}"
    rgb_path = os.path.join(outdir, f"{base_name}_rgb.png")
    raw_path = os.path.join(outdir, f"{base_name}_raw.npy")
    
    # Save RGB888 as PNG
    rgb_image = Image.fromarray(rgb_array, mode='RGB')
    rgb_image.save(rgb_path, format='PNG')
    
    # Save RAW as numpy array
    np.save(raw_path, raw_array)
    
    # Prepare metadata for this capture
    capture_info = {
        "camera_index": cam_num,
        "timestamp": timestamp,
        "rgb_path": rgb_path,
        "raw_path": raw_path,
        "rgb_shape": rgb_array.shape,
        "raw_shape": raw_array.shape,
        "raw_dtype": str(raw_array.dtype),
        "metadata": {
            "ExposureTime": metadata.get("ExposureTime"),
            "AnalogueGain": metadata.get("AnalogueGain"),
            "ColourGains": metadata.get("ColourGains"),
            "SensorTimestamp": metadata.get("SensorTimestamp"),
        }
    }
    
    print(f"Camera {cam_num} captured:")
    print(f"  - RGB888: {rgb_path} {rgb_array.shape}")
    print(f"  - RAW: {raw_path} {raw_array.shape} dtype={raw_array.dtype}")
    
    return capture_info


def sequential_capture_cycle(picam0: Picamera2, picam1: Picamera2, 
                             outdir: str, save_metadata: bool = True):
    """
    Perform one complete capture cycle from both cameras sequentially.
    
    Args:
        picam0: Camera 0 instance
        picam1: Camera 1 instance
        outdir: Output directory
        save_metadata: Whether to save JSON metadata file
    
    Returns:
        Tuple of (timestamp, capture_info_dict)
    """
    # Generate timestamp for this capture cycle
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    
    print(f"\n{'='*60}")
    print(f"Starting capture cycle: {timestamp}")
    print(f"{'='*60}")
    
    # Capture from Camera 0
    print("\n[1/2] Capturing from Camera 0...")
    start_cam0 = time.time()
    cam0_info = capture_from_camera(picam0, 0, timestamp, outdir)
    cam0_duration = time.time() - start_cam0
    
    # Capture from Camera 1 (immediately after Camera 0 completes)
    print("\n[2/2] Capturing from Camera 1...")
    start_cam1 = time.time()
    cam1_info = capture_from_camera(picam1, 1, timestamp, outdir)
    cam1_duration = time.time() - start_cam1
    
    # Calculate inter-camera delay
    inter_camera_delay = start_cam1 - start_cam0
    
    print(f"\nTiming:")
    print(f"  - Camera 0 capture time: {cam0_duration:.3f}s")
    print(f"  - Camera 1 capture time: {cam1_duration:.3f}s")
    print(f"  - Inter-camera delay: {inter_camera_delay:.3f}s")
    
    # Save metadata JSON if requested
    if save_metadata:
        metadata_dict = {
            "capture_timestamp": timestamp,
            "timing": {
                "camera_0_duration_s": cam0_duration,
                "camera_1_duration_s": cam1_duration,
                "inter_camera_delay_s": inter_camera_delay
            },
            "camera_0": cam0_info,
            "camera_1": cam1_info
        }
        
        metadata_path = os.path.join(outdir, f"{timestamp}_metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata_dict, f, indent=2, default=str)
        
        print(f"\nMetadata saved: {metadata_path}")
    
    return timestamp, {"camera_0": cam0_info, "camera_1": cam1_info}


def main():
    """Main execution function with CLI argument parsing."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Sequential dual camera capture for IMX219 modules on Raspberry Pi 5",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="""
RAM Disk Setup:
  To use RAM disk for high-speed captures, first create a tmpfs mount:
  
  sudo mkdir -p /mnt/ramdisk
  sudo mount -t tmpfs -o size=1G tmpfs /mnt/ramdisk
  
  Then run with: --ramdisk /mnt/ramdisk/captures
  
  To make it permanent, add to /etc/fstab:
  tmpfs /mnt/ramdisk tmpfs defaults,size=1G 0 0
        """
    )
    parser.add_argument(
        "--outdir", 
        default="captures",
        help="Final output directory for captured images (permanent storage)"
    )
    parser.add_argument(
        "--ramdisk",
        default=None,
        help="RAM disk path for fast writes (will flush to --outdir after all captures)"
    )
    parser.add_argument(
        "--count", 
        type=int, 
        default=1,
        help="Number of capture cycles to perform"
    )
    parser.add_argument(
        "--exposure", 
        type=int, 
        default=10000,
        help="Exposure time in microseconds"
    )
    parser.add_argument(
        "--gain", 
        type=float, 
        default=1.0,
        help="Analogue gain value"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Interval between capture cycles in seconds"
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip saving JSON metadata files"
    )
    
    args = parser.parse_args()
    
    # Determine working directory (ramdisk if specified, otherwise final outdir)
    use_ramdisk = args.ramdisk is not None
    working_dir = args.ramdisk if use_ramdisk else args.outdir
    final_dir = args.outdir
    
    # Create working directory
    ensure_dir(working_dir)
    if use_ramdisk:
        ensure_dir(final_dir)  # Also ensure final destination exists
    
    print("="*60)
    print("Dual IMX219 Sequential Capture")
    print("="*60)
    if use_ramdisk:
        print(f"Mode: RAM DISK (high-speed)")
        print(f"Working directory: {working_dir}")
        print(f"Final directory: {final_dir}")
    else:
        print(f"Mode: DIRECT WRITE")
        print(f"Output directory: {args.outdir}")
    print(f"Capture cycles: {args.count}")
    print(f"Exposure: {args.exposure} µs")
    print(f"Gain: {args.gain}")
    print(f"Interval: {args.interval}s")
    print("="*60)
    
    # Initialize both cameras
    picam0 = init_camera(0, args.exposure, args.gain)
    picam1 = init_camera(1, args.exposure, args.gain)
    
    # Start both cameras (pre-start for minimal capture delay)
    print("\nStarting both cameras...")
    picam0.start()
    picam1.start()
    
    # Brief settling time for 3A locks to take effect
    time.sleep(0.2)
    
    print("\nCameras ready. Beginning capture sequence...")
    
    try:
        # Perform capture cycles
        for cycle in range(args.count):
            print(f"\n{'#'*60}")
            print(f"# Capture Cycle {cycle + 1} of {args.count}")
            print(f"{'#'*60}")
            
            sequential_capture_cycle(
                picam0, 
                picam1, 
                working_dir,  # Write to ramdisk if specified, otherwise final dir
                save_metadata=not args.no_metadata
            )
            
            # Wait between cycles if interval specified
            if cycle + 1 < args.count and args.interval > 0:
                print(f"\nWaiting {args.interval}s before next cycle...")
                time.sleep(args.interval)
        
        print(f"\n{'='*60}")
        print(f"All {args.count} capture cycle(s) completed!")
        print(f"{'='*60}")
        
        # Flush RAM disk to permanent storage if using ramdisk
        if use_ramdisk:
            try:
                flush_ramdisk_to_storage(working_dir, final_dir)
                print(f"\n✓ All files saved to: {final_dir}")
            except Exception as e:
                print(f"\n⚠ WARNING: Failed to flush RAM disk: {e}")
                print(f"   Files still available in RAM disk: {working_dir}")
                raise
        else:
            print(f"\n✓ All files saved to: {args.outdir}")
        
    except KeyboardInterrupt:
        print("\n\nCapture interrupted by user (Ctrl+C)")
    except Exception as e:
        print(f"\n\nERROR: {e}")
        raise
    finally:
        # Clean shutdown
        print("\nStopping cameras...")
        try:
            picam0.stop()
        except:
            pass
        try:
            picam1.stop()
        except:
            pass
        print("Cameras stopped.")


if __name__ == "__main__":
    main()

