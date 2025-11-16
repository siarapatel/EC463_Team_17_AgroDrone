#!/usr/bin/env python3
"""
Dual IMX219 Alternating Capture Script for Raspberry Pi 5
Alternates captures between Camera 0 and Camera 1 for each burst frame.
Provides better temporal synchronization (~50ms vs ~500ms lag).
Supports RAM disk for high-speed captures with flush to permanent storage.
"""

import os
import time
import json
import pathlib
import shutil
from datetime import datetime, timezone
from typing import Dict, List
import numpy as np
from PIL import Image
from picamera2 import Picamera2


def ensure_dir(path: str):
    """Create directory if it doesn't exist."""
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def get_ramdisk_usage(ramdisk_path: str) -> Dict:
    """
    Get RAM disk space usage information (similar to du -h and df -h).
    
    Args:
        ramdisk_path: Path to RAM disk
    
    Returns:
        Dictionary with usage statistics
    """
    # Get disk usage statistics (similar to df -h)
    usage = shutil.disk_usage(ramdisk_path)
    
    # Calculate directory size (similar to du -h)
    total_size = 0
    file_count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(ramdisk_path):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.isfile(filepath):
                    total_size += os.path.getsize(filepath)
                    file_count += 1
    except Exception as e:
        print(f"Warning: Could not calculate directory size: {e}")
    
    return {
        'total_gb': usage.total / (1024**3),
        'used_gb': usage.used / (1024**3),
        'free_gb': usage.free / (1024**3),
        'percent_used': (usage.used / usage.total * 100) if usage.total > 0 else 0,
        'dir_size_mb': total_size / (1024**2),
        'file_count': file_count
    }


def print_ramdisk_status(ramdisk_path: str, label: str = "RAM Disk Status"):
    """
    Print formatted RAM disk usage information.
    
    Args:
        ramdisk_path: Path to RAM disk
        label: Label for the status output
    """
    try:
        stats = get_ramdisk_usage(ramdisk_path)
        
        print(f"\n{'='*60}")
        print(f"{label}")
        print(f"{'='*60}")
        print(f"Total Size:      {stats['total_gb']:.2f} GB")
        print(f"Used:            {stats['used_gb']:.2f} GB ({stats['percent_used']:.1f}%)")
        print(f"Available:       {stats['free_gb']:.2f} GB")
        print(f"Directory Size:  {stats['dir_size_mb']:.2f} MB ({stats['file_count']} files)")
        print(f"{'='*60}")
        
    except Exception as e:
        print(f"\n⚠ Could not get RAM disk status: {e}")


def check_ramdisk_availability(ramdisk_path: str, flush_start_time: float, 
                               check_at_second: float = 4.0) -> bool:
    """
    Check if RAM disk is ready after flush operation at specific time.
    
    Args:
        ramdisk_path: Path to RAM disk
        flush_start_time: Time when flush started (from time.time())
        check_at_second: When to check (seconds after flush_start_time)
    
    Returns:
        True if available, False otherwise
    """
    # Calculate how long to wait
    elapsed = time.time() - flush_start_time
    wait_time = max(0, check_at_second - elapsed)
    
    if wait_time > 0:
        print(f"\nWaiting {wait_time:.1f}s until 4th second check...")
        time.sleep(wait_time)
    else:
        print(f"\n4th second already passed (flush took {elapsed:.1f}s)")
    
    # Perform check (should complete within 1 second)
    print("Checking RAM disk availability...")
    check_start = time.time()
    
    try:
        # Test write
        test_file = os.path.join(ramdisk_path, ".ramdisk_test")
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        
        # Show status
        print_ramdisk_status(ramdisk_path, "RAM Disk Status (Post-Flush)")
        
        check_duration = time.time() - check_start
        total_time = time.time() - flush_start_time
        
        print(f"✓ RAM disk ready (check took {check_duration:.2f}s, total {total_time:.1f}s)")
        return True
        
    except Exception as e:
        check_duration = time.time() - check_start
        print(f"⚠ RAM disk not ready: {e} (check took {check_duration:.2f}s)")
        return False


def flush_ramdisk_to_storage(ramdisk_path: str, final_path: str, clear_ramdisk: bool = True):
    """
    Copy all files from RAM disk to permanent storage.
    
    Args:
        ramdisk_path: Source directory (RAM disk)
        final_path: Destination directory (permanent storage)
        clear_ramdisk: Whether to delete files from RAM disk after copy
    
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
    
    # Optional: Clear RAM disk after flush
    if clear_ramdisk:
        for item in os.listdir(ramdisk_path):
            src = os.path.join(ramdisk_path, item)
            if os.path.isfile(src):
                try:
                    os.remove(src)
                except Exception as e:
                    print(f"Warning: Could not remove {item}: {e}")
        print("  ✓ RAM disk cleared")
    
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
    # Main stream: RGB888 for JPEG output
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


def capture_single_frame(picam: Picamera2, cam_num: int, burst_idx: int,
                        timestamp: str, outdir: str, jpeg_quality: int = 90) -> Dict:
    """
    Capture a single frame (RAW + RGB) from one camera.
    
    Args:
        picam: Configured Picamera2 instance
        cam_num: Camera number (0 or 1)
        burst_idx: Index of this burst frame
        timestamp: Timestamp string for filenames
        outdir: Output directory path
        jpeg_quality: JPEG quality for RGB images
    
    Returns:
        Dictionary containing capture metadata
    """
    # Capture both streams atomically using a request
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
    
    # Save RGB888 as JPEG
    rgb_image = Image.fromarray(rgb_array, mode='RGB')
    rgb_image.save(rgb_path, format='JPEG', quality=jpeg_quality, optimize=False)
    
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
        }
    }
    
    return capture_info


def alternating_capture_cycle(picam0: Picamera2, picam1: Picamera2,
                              outdir: str, burst_count: int = 5, jpeg_quality: int = 90,
                              save_metadata: bool = True):
    """
    Perform one capture cycle alternating between cameras for better sync.
    
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
    
    print(f"\n{'='*60}")
    print(f"Starting alternating capture cycle: {timestamp}")
    print(f"{'='*60}")
    
    cam0_captures = []
    cam1_captures = []
    frame_pairs = []
    
    cycle_start = time.time()
    
    # Alternate between cameras for each burst frame
    for burst_idx in range(burst_count):
        print(f"\n  Frame Pair {burst_idx + 1}/{burst_count}:")
        
        # Capture from Camera 0
        print(f"    Cam0...", end='', flush=True)
        cam0_start = time.time()
        cam0_frame = capture_single_frame(picam0, 0, burst_idx, timestamp, outdir, jpeg_quality)
        cam0_duration = time.time() - cam0_start
        cam0_captures.append(cam0_frame)
        print(f" {cam0_duration:.3f}s")
        
        # Immediately capture from Camera 1
        print(f"    Cam1...", end='', flush=True)
        cam1_start = time.time()
        cam1_frame = capture_single_frame(picam1, 1, burst_idx, timestamp, outdir, jpeg_quality)
        cam1_duration = time.time() - cam1_start
        cam1_captures.append(cam1_frame)
        print(f" {cam1_duration:.3f}s")
        
        # Calculate inter-frame delay (Camera 0 to Camera 1)
        cam0_timestamp = cam0_frame['metadata']['SensorTimestamp']
        cam1_timestamp = cam1_frame['metadata']['SensorTimestamp']
        inter_frame_delay_ms = (cam1_timestamp - cam0_timestamp) / 1e6 if cam0_timestamp and cam1_timestamp else 0
        
        frame_pairs.append({
            'pair_index': burst_idx,
            'cam0_timestamp': cam0_timestamp,
            'cam1_timestamp': cam1_timestamp,
            'inter_frame_delay_ms': inter_frame_delay_ms,
            'cam0_duration_s': cam0_duration,
            'cam1_duration_s': cam1_duration
        })
        
        print(f"    Inter-frame delay: {inter_frame_delay_ms:.2f}ms")
    
    cycle_duration = time.time() - cycle_start
    
    # Calculate statistics
    avg_cam0_time = sum([p['cam0_duration_s'] for p in frame_pairs]) / burst_count
    avg_cam1_time = sum([p['cam1_duration_s'] for p in frame_pairs]) / burst_count
    avg_inter_delay = sum([p['inter_frame_delay_ms'] for p in frame_pairs]) / burst_count
    
    print(f"\nTiming Summary:")
    print(f"  - Total cycle time: {cycle_duration:.3f}s")
    print(f"  - Avg Camera 0 time: {avg_cam0_time:.3f}s per frame")
    print(f"  - Avg Camera 1 time: {avg_cam1_time:.3f}s per frame")
    print(f"  - Avg inter-frame delay: {avg_inter_delay:.2f}ms")
    
    # Save metadata JSON if requested
    if save_metadata:
        metadata_dict = {
            "capture_mode": "alternating",
            "capture_timestamp": timestamp,
            "burst_count": burst_count,
            "jpeg_quality": jpeg_quality,
            "timing": {
                "total_cycle_time_s": cycle_duration,
                "avg_camera_0_time_s": avg_cam0_time,
                "avg_camera_1_time_s": avg_cam1_time,
                "avg_inter_frame_delay_ms": avg_inter_delay
            },
            "camera_0": {
                "camera_index": 0,
                "timestamp": timestamp,
                "burst_count": burst_count,
                "jpeg_quality": jpeg_quality,
                "burst_captures": cam0_captures
            },
            "camera_1": {
                "camera_index": 1,
                "timestamp": timestamp,
                "burst_count": burst_count,
                "jpeg_quality": jpeg_quality,
                "burst_captures": cam1_captures
            },
            "frame_pairs": frame_pairs
        }
        
        metadata_path = os.path.join(outdir, f"{timestamp}_metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata_dict, f, indent=2, default=str)
        
        print(f"\nMetadata saved: {metadata_path}")
    
    return timestamp, {
        "camera_0": {"burst_captures": cam0_captures},
        "camera_1": {"burst_captures": cam1_captures},
        "frame_pairs": frame_pairs
    }


def main():
    """Main execution function with CLI argument parsing."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Alternating dual camera capture for IMX219 modules on Raspberry Pi 5",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="""
Capture Mode: ALTERNATING
  This script alternates between Camera 0 and Camera 1 for each burst frame,
  providing better temporal synchronization (~50ms vs ~500ms).
  
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
    parser.add_argument(
        "--burst",
        type=int,
        default=5,
        help="Number of burst captures per camera (default: 5)"
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=90,
        help="JPEG quality for RGB images (1-100, default: 90)"
    )
    
    args = parser.parse_args()
    
    # Determine working directory (ramdisk if specified, otherwise final outdir)
    use_ramdisk = args.ramdisk is not None
    
    if not use_ramdisk:
        print("Not using RAM disk - writing directly to storage")
        
    working_dir = args.ramdisk if use_ramdisk else args.outdir
    final_dir = args.outdir
    
    # Create working directory
    ensure_dir(working_dir)
    if use_ramdisk:
        ensure_dir(final_dir)  # Also ensure final destination exists
    
    BURST_SETS = 5  # Fixed at 5 sets per cycle
    
    print("="*60)
    print("Dual IMX219 ALTERNATING Capture with Incremental Flush")
    print("="*60)
    if use_ramdisk:
        print(f"Mode: RAM DISK (high-speed with incremental flush)")
        print(f"Working directory: {working_dir}")
        print(f"Final directory: {final_dir}")
    else:
        print(f"Mode: DIRECT WRITE")
        print(f"Output directory: {args.outdir}")
    print(f"Capture mode: ALTERNATING (Cam0 → Cam1 → Cam0 → ...)")
    print(f"Capture cycles: {args.count}")
    print(f"Burst sets per cycle: {BURST_SETS}")
    print(f"Burst captures per set: {args.burst}")
    print(f"Images per burst set: {args.burst * 4} ({args.burst * 2} per camera)")
    print(f"Total images per cycle: {BURST_SETS * args.burst * 4}")
    print(f"Exposure: {args.exposure} µs")
    print(f"Gain: {args.gain}")
    print(f"JPEG quality: {args.jpeg_quality}")
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
    
    print("\nCameras ready. Beginning alternating capture sequence...")
    
    try:
        # Perform capture cycles with 5 burst sets each
        for cycle in range(args.count):
            print(f"\n{'='*60}")
            print(f"CAPTURE CYCLE {cycle + 1} of {args.count}")
            print(f"{'='*60}")
            
            for burst_set in range(BURST_SETS):
                print(f"\n{'#'*60}")
                print(f"# Burst Set {burst_set + 1}/{BURST_SETS}")
                print(f"{'#'*60}")
                
                # Perform one alternating burst capture set
                alternating_capture_cycle(
                    picam0, 
                    picam1, 
                    working_dir,
                    burst_count=args.burst,
                    jpeg_quality=args.jpeg_quality,
                    save_metadata=not args.no_metadata
                )
                
                # Show RAM usage and flush after each burst set (if using ramdisk)
                if use_ramdisk:
                    print_ramdisk_status(working_dir, f"RAM Usage After Set {burst_set + 1}")
                    
                    # Start flush and record time
                    flush_start = time.time()
                    flush_ramdisk_to_storage(working_dir, final_dir, clear_ramdisk=True)
                    
                    # Check availability at 4th second (complete by 5th second)
                    if burst_set < BURST_SETS - 1:  # Not the last set
                        check_ramdisk_availability(working_dir, flush_start, check_at_second=4.0)
                    else:
                        print("\n✓ All burst sets in this cycle completed")
            
            # Wait between cycles if interval specified
            if cycle + 1 < args.count and args.interval > 0:
                print(f"\nWaiting {args.interval}s before next cycle...")
                time.sleep(args.interval)
        
        print(f"\n{'='*60}")
        print(f"ALL CYCLES COMPLETED!")
        print(f"{'='*60}")
        print(f"Total burst sets: {args.count * BURST_SETS}")
        print(f"Total images: {args.count * BURST_SETS * args.burst * 4}")
        
        # Final message
        if use_ramdisk:
            print(f"\n✓ All files saved to: {final_dir}")
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

