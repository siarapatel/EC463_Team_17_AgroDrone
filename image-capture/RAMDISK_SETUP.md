# RAM Disk Setup for High-Speed Captures

## Quick Start

### 1. Create RAM Disk (One-time setup per boot)

```bash
# Create mount point
sudo mkdir -p /mnt/ramdisk

# Mount tmpfs (1GB recommended)
sudo mount -t tmpfs -o size=1G tmpfs /mnt/ramdisk

# Verify it's mounted
df -h | grep ramdisk
```

### 2. Run Captures with RAM Disk

```bash
# Basic usage with RAM disk
./dual_sequential_capture.py --ramdisk /mnt/ramdisk/captures --outdir /home/pi/flight_data

# Multiple cycles
./dual_sequential_capture.py \
  --ramdisk /mnt/ramdisk/captures \
  --outdir /home/pi/flight_data \
  --count 50 \
  --exposure 15000 \
  --gain 1.5
```

## Sizing the RAM Disk

Calculate required RAM with burst mode (default: 5 bursts per camera):
- Each capture cycle = 21 files (10 RAW + 10 JPEG + 1 metadata JSON)
- RAW files: ~16MB each × 10 = ~160MB
- JPEG files: ~3MB each × 10 = ~30MB
- Approximate: **~60MB per cycle** (with burst=5)

**Recommended: 1GB** (handles ~16 capture cycles with burst=5)

Other sizing options:
- **512MB**: ~8 cycles (burst=5)
- **1GB**: ~16 cycles (burst=5) - recommended
- **2GB**: ~33 cycles (burst=5)
- **4GB**: ~66 cycles (burst=5)

Pi 5 RAM options: 2GB, 4GB, or 8GB total

**Note**: With burst mode, each cycle produces 20 images (10 per camera: 5 RAW + 5 JPEG each)

## Permanent Mount (Optional)

To auto-mount on boot, add to `/etc/fstab`:

```bash
# Edit fstab
sudo nano /etc/fstab

# Add this line:
tmpfs /mnt/ramdisk tmpfs defaults,size=1G,mode=0777 0 0

# Save and reboot to test
sudo reboot
```

## Performance Comparison

**Without RAM disk** (SD card):
- Write speed: ~20-90 MB/s
- Inter-camera delay: ~1.0-2.0s (with burst=5)
- Burst capture time: ~0.8-1.2s per camera

**With RAM disk** (tmpfs):
- Write speed: ~5-10 GB/s
- Inter-camera delay: ~0.4-0.6s (with burst=5)
- Burst capture time: ~0.3-0.5s per camera

**JPEG vs PNG**: JPEG encoding is ~5x faster than PNG, reducing capture time significantly for burst mode.

## Workflow

1. **Captures go to RAM** (fast writes during flight)
2. **Script completes** all capture cycles
3. **Auto-flush** copies all files to permanent storage
4. **You're done!** Files are on SD card/USB storage

## Important Notes

⚠️ **Data is volatile until flush completes**
- If script crashes before flush, data may be lost
- Always wait for "Flush complete" message
- For critical missions, consider not using RAM disk

✓ **Best for**:
- Burst captures (minimize inter-camera delay)
- High-frequency timelapse
- Flight missions with many waypoints

✗ **Not recommended for**:
- Single captures (overhead not worth it)
- Very long sessions (hundreds of cycles)
- Systems with limited RAM

## Troubleshooting

### Check RAM usage
```bash
free -h
```

### Check RAM disk space
```bash
df -h /mnt/ramdisk
```

### Unmount RAM disk
```bash
sudo umount /mnt/ramdisk
```

### Permission errors
```bash
sudo chmod 777 /mnt/ramdisk
```

## Example Output

```
============================================================
Dual IMX219 Sequential Capture
============================================================
Mode: RAM DISK (high-speed)
Working directory: /mnt/ramdisk/captures
Final directory: /home/pi/flight_data
Capture cycles: 10
Burst captures per camera: 5
Exposure: 15000 µs
Gain: 1.5
JPEG quality: 90
Interval: 0.0s
============================================================
...
[1/2] Capturing from Camera 0...
  Capturing 5 burst frames...
    [1/5] Captured - SensorTimestamp: 1234567890123456
    [2/5] Captured - SensorTimestamp: 1234567923456789
    [3/5] Captured - SensorTimestamp: 1234567956790123
    [4/5] Captured - SensorTimestamp: 1234567990123456
    [5/5] Captured - SensorTimestamp: 1234568023456789

[2/2] Capturing from Camera 1...
  Capturing 5 burst frames...
    [1/5] Captured - SensorTimestamp: 1234568523456789
    ...

[Captures complete]
...
============================================================
Flushing RAM disk to permanent storage...
============================================================
Source: /mnt/ramdisk/captures
Destination: /home/pi/flight_data
  [1] 20241109_142530_123456_cam0_burst00_raw.npy (16.24 MB)
  [2] 20241109_142530_123456_cam0_burst00_rgb.jpg (2.89 MB)
  [3] 20241109_142530_123456_cam0_burst01_raw.npy (16.24 MB)
  [4] 20241109_142530_123456_cam0_burst01_rgb.jpg (2.91 MB)
  ...

Flush complete:
  - Files copied: 210
  - Total size: 612.30 MB
  - Duration: 6.74s
  - Transfer speed: 90.84 MB/s
============================================================

✓ All files saved to: /home/pi/flight_data
```

