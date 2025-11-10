#!/usr/bin/env python3
"""
Capture Timing Analysis Script
Analyzes metadata JSON files from dual_sequential_capture.py and provides
detailed per-frame timing analysis plus summary statistics.
"""

import json
import os
import statistics
import argparse
from pathlib import Path
from typing import Dict, List, Tuple


def load_metadata(json_path: str) -> Dict:
    """
    Load metadata from JSON file.
    
    Args:
        json_path: Path to metadata JSON file
        
    Returns:
        Dictionary containing metadata
    """
    with open(json_path, 'r') as f:
        return json.load(f)


def calculate_intervals(timestamps: List[int]) -> List[float]:
    """
    Calculate intervals between consecutive timestamps.
    
    Args:
        timestamps: List of timestamps in nanoseconds
        
    Returns:
        List of intervals in milliseconds
    """
    intervals = []
    for i in range(1, len(timestamps)):
        interval_ns = timestamps[i] - timestamps[i-1]
        interval_ms = interval_ns / 1e6  # Convert to milliseconds
        intervals.append(interval_ms)
    return intervals


def analyze_camera(camera_data: Dict, camera_num: int, show_per_frame: bool = True) -> Tuple[str, Dict]:
    """
    Analyze burst capture data for one camera.
    
    Args:
        camera_data: Camera data from metadata
        camera_num: Camera number (0 or 1)
        show_per_frame: Whether to include per-frame details
        
    Returns:
        Tuple of (formatted_text, summary_stats_dict)
    """
    burst_captures = camera_data['burst_captures']
    burst_count = camera_data['burst_count']
    
    # Extract timestamps and metadata
    timestamps = [burst['metadata']['SensorTimestamp'] for burst in burst_captures]
    intervals = calculate_intervals(timestamps)
    
    # Calculate statistics
    if intervals:
        avg_interval = statistics.mean(intervals)
        std_interval = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
        avg_fps = 1000.0 / avg_interval if avg_interval > 0 else 0.0
        min_fps = 1000.0 / max(intervals) if intervals else 0.0
        max_fps = 1000.0 / min(intervals) if intervals else 0.0
        total_duration = sum(intervals)
    else:
        avg_interval = 0.0
        std_interval = 0.0
        avg_fps = 0.0
        min_fps = 0.0
        max_fps = 0.0
        total_duration = 0.0
    
    # Format output
    output_lines = []
    output_lines.append("=" * 80)
    output_lines.append(f"CAMERA {camera_num} - PER-FRAME ANALYSIS")
    output_lines.append("=" * 80)
    
    if show_per_frame:
        for idx, burst in enumerate(burst_captures):
            metadata = burst['metadata']
            output_lines.append(f"Frame {idx}:")
            output_lines.append(f"  SensorTimestamp: {metadata['SensorTimestamp']} ns")
            
            if idx > 0:
                output_lines.append(f"  Interval from previous: {intervals[idx-1]:.2f} ms")
            
            output_lines.append(f"  ExposureTime: {metadata['ExposureTime']} µs")
            output_lines.append(f"  AnalogueGain: {metadata['AnalogueGain']}")
            output_lines.append(f"  ColourGains: {metadata['ColourGains']}")
            output_lines.append("")
    
    # Summary statistics
    output_lines.append(f"CAMERA {camera_num} - SUMMARY STATISTICS")
    output_lines.append("-" * 40)
    output_lines.append(f"Total frames: {burst_count}")
    
    if intervals:
        interval_str = ", ".join([f"{iv:.2f}" for iv in intervals])
        output_lines.append(f"Frame intervals (ms): [{interval_str}]")
        output_lines.append(f"Average interval: {avg_interval:.2f} ms")
        output_lines.append(f"Std deviation: {std_interval:.2f} ms")
        output_lines.append(f"Average FPS: {avg_fps:.1f}")
        output_lines.append(f"Min FPS: {min_fps:.1f}")
        output_lines.append(f"Max FPS: {max_fps:.1f}")
        output_lines.append(f"Total burst duration: {total_duration:.2f} ms")
    else:
        output_lines.append("No interval data (single frame)")
    
    output_lines.append("")
    
    summary_stats = {
        'burst_count': burst_count,
        'intervals': intervals,
        'avg_interval': avg_interval,
        'std_interval': std_interval,
        'avg_fps': avg_fps,
        'min_fps': min_fps,
        'max_fps': max_fps,
        'total_duration': total_duration,
        'first_timestamp': timestamps[0] if timestamps else None
    }
    
    return "\n".join(output_lines), summary_stats


def format_report(metadata: Dict, cam0_analysis: Tuple, cam1_analysis: Tuple, 
                 json_filename: str) -> str:
    """
    Generate complete formatted timing report.
    
    Args:
        metadata: Full metadata dictionary
        cam0_analysis: Tuple of (text, stats) for camera 0
        cam1_analysis: Tuple of (text, stats) for camera 1
        json_filename: Name of JSON file being analyzed
        
    Returns:
        Formatted report string
    """
    cam0_text, cam0_stats = cam0_analysis
    cam1_text, cam1_stats = cam1_analysis
    
    output_lines = []
    
    # Header
    output_lines.append("=" * 80)
    output_lines.append("CAPTURE TIMING ANALYSIS")
    output_lines.append("=" * 80)
    output_lines.append(f"Metadata file: {json_filename}")
    output_lines.append(f"Capture timestamp: {metadata.get('capture_timestamp', 'N/A')}")
    output_lines.append(f"Burst count: {metadata.get('burst_count', 'N/A')} frames per camera")
    output_lines.append(f"JPEG quality: {metadata.get('jpeg_quality', 'N/A')}")
    output_lines.append("")
    
    # Camera 0 analysis
    output_lines.append(cam0_text)
    
    # Camera 1 analysis
    output_lines.append(cam1_text)
    
    # Inter-camera timing
    output_lines.append("=" * 80)
    output_lines.append("INTER-CAMERA TIMING")
    output_lines.append("=" * 80)
    
    if cam0_stats['first_timestamp'] and cam1_stats['first_timestamp']:
        cam0_first = cam0_stats['first_timestamp']
        cam1_first = cam1_stats['first_timestamp']
        inter_delay = abs(cam1_first - cam0_first) / 1e6  # Convert to ms
        
        output_lines.append(f"Camera 0 first frame: {cam0_first} ns")
        output_lines.append(f"Camera 1 first frame: {cam1_first} ns")
        output_lines.append(f"Inter-camera delay: {inter_delay:.2f} ms")
    else:
        output_lines.append("Timestamp data unavailable")
    
    output_lines.append("")
    
    # Overall performance
    output_lines.append("=" * 80)
    output_lines.append("OVERALL CYCLE PERFORMANCE")
    output_lines.append("=" * 80)
    
    timing_data = metadata.get('timing', {})
    cam0_duration = timing_data.get('camera_0_duration_s', 0)
    cam1_duration = timing_data.get('camera_1_duration_s', 0)
    inter_camera_delay_s = timing_data.get('inter_camera_delay_s', 0)
    
    total_cycle_time = cam0_duration + cam1_duration
    burst_count = metadata.get('burst_count', 0)
    total_images = burst_count * 4  # 2 images per frame (RAW + JPEG) × 2 cameras
    throughput = total_images / total_cycle_time if total_cycle_time > 0 else 0
    
    output_lines.append(f"Total cycle time: {total_cycle_time:.3f} s")
    output_lines.append(f"Total images: {total_images} ({burst_count * 2} per camera)")
    output_lines.append(f"Capture throughput: {throughput:.1f} images/second")
    output_lines.append("")
    output_lines.append("Python timing (from metadata):")
    output_lines.append(f"  - Camera 0 duration: {cam0_duration:.3f} s "
                       f"({timing_data.get('camera_0_per_frame_s', 0):.3f} s/frame)")
    output_lines.append(f"  - Camera 1 duration: {cam1_duration:.3f} s "
                       f"({timing_data.get('camera_1_per_frame_s', 0):.3f} s/frame)")
    output_lines.append(f"  - Inter-camera delay: {inter_camera_delay_s:.3f} s")
    
    output_lines.append("")
    output_lines.append("=" * 80)
    
    return "\n".join(output_lines)


def find_metadata_files(input_path: str) -> List[str]:
    """
    Find all metadata JSON files in the given path.
    
    Args:
        input_path: Path to file or directory
        
    Returns:
        List of metadata JSON file paths
    """
    path = Path(input_path)
    
    if path.is_file():
        if path.suffix == '.json' and 'metadata' in path.name:
            return [str(path)]
        else:
            raise ValueError(f"File must be a metadata JSON file: {input_path}")
    
    elif path.is_dir():
        # Find all metadata JSON files in directory
        metadata_files = list(path.glob("*_metadata.json"))
        if not metadata_files:
            raise ValueError(f"No metadata JSON files found in directory: {input_path}")
        return [str(f) for f in sorted(metadata_files)]
    
    else:
        raise ValueError(f"Path does not exist: {input_path}")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="Analyze timing from dual camera capture metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        '-i', '--input',
        required=True,
        help="Path to metadata JSON file or directory containing metadata files"
    )
    parser.add_argument(
        '-o', '--output',
        help="Save output to text file (prints to console if not specified)"
    )
    parser.add_argument(
        '--summary-only',
        action='store_true',
        help="Show only summary statistics (skip per-frame details)"
    )
    
    args = parser.parse_args()
    
    # Find metadata files
    try:
        metadata_files = find_metadata_files(args.input)
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    
    print(f"Found {len(metadata_files)} metadata file(s) to analyze\n")
    
    # Analyze each file
    all_reports = []
    
    for json_path in metadata_files:
        try:
            # Load metadata
            metadata = load_metadata(json_path)
            
            # Analyze both cameras
            cam0_analysis = analyze_camera(
                metadata['camera_0'], 
                0, 
                show_per_frame=not args.summary_only
            )
            cam1_analysis = analyze_camera(
                metadata['camera_1'], 
                1, 
                show_per_frame=not args.summary_only
            )
            
            # Generate report
            report = format_report(
                metadata, 
                cam0_analysis, 
                cam1_analysis,
                os.path.basename(json_path)
            )
            
            all_reports.append(report)
            
        except Exception as e:
            print(f"Error analyzing {json_path}: {e}")
            continue
    
    # Combine all reports
    full_output = "\n\n".join(all_reports)
    
    # Output results
    if args.output:
        with open(args.output, 'w') as f:
            f.write(full_output)
        print(f"Analysis saved to: {args.output}")
    else:
        print(full_output)
    
    return 0


if __name__ == "__main__":
    exit(main())

