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

Calculate required RAM:
- Each capture cycle = 4 files (~16MB RAW + ~3MB PNG per camera)
- Approximate: **~40MB per cycle**

**Recommended: 1GB** (handles ~25 capture cycles)

Other sizing options:
- **512MB**: ~10-12 cycles
- **1GB**: ~25 cycles (recommended)
- **2GB**: ~50 cycles
- **4GB**: ~100 cycles

Pi 5 RAM options: 2GB, 4GB, or 8GB total

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
- Inter-camera delay: ~0.5-1.5s

**With RAM disk** (tmpfs):
- Write speed: ~5-10 GB/s
- Inter-camera delay: ~0.2-0.4s

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
Exposure: 15000 µs
Gain: 1.5
============================================================
...
[Captures complete]
...
============================================================
Flushing RAM disk to permanent storage...
============================================================
Source: /mnt/ramdisk/captures
Destination: /home/pi/flight_data
  [1] 20241109_142530_123456_cam0_raw.npy (12.34 MB)
  [2] 20241109_142530_123456_cam0_rgb.png (2.56 MB)
  ...

Flush complete:
  - Files copied: 40
  - Total size: 384.50 MB
  - Duration: 4.23s
  - Transfer speed: 90.88 MB/s
============================================================

✓ All files saved to: /home/pi/flight_data
```

