# Systemd files

> **Note:** The files in this directory are legacy. Active service files now live in
> `image-capture-new/` (camera capture), `image-upload/services/` (post-mission upload),
> and `msp-uart/` (UART service + mission upload client). See below for the current
> deployment procedure.

---

## Current deployment (msp-uart branch and later)

```bash
# 1. Copy all active service/path units
sudo cp msp-uart/agrodrone-msp-uart.service           /etc/systemd/system/
sudo cp msp-uart/agrodrone-mission-upload-client.path  /etc/systemd/system/
sudo cp msp-uart/agrodrone-mission-upload-client.service /etc/systemd/system/
sudo cp image-capture-new/ndvi-capture.service        /etc/systemd/system/
sudo cp image-upload/services/agrodrone-image-upload.path    /etc/systemd/system/
sudo cp image-upload/services/agrodrone-image-upload.service /etc/systemd/system/

# 2. Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now agrodrone-msp-uart.service
sudo systemctl enable --now agrodrone-mission-upload-client.path
sudo systemctl enable --now ndvi-capture.service
sudo systemctl enable --now agrodrone-image-upload.path
```

## Useful commands

```bash
# Live logs
journalctl -u agrodrone-msp-uart -f
journalctl -u ndvi-capture -f

# Status
sudo systemctl status agrodrone-msp-uart
sudo systemctl status ndvi-capture

# Stop a service
sudo systemctl stop <service-name>

# Test the events socket manually (requires socat)
socat - UNIX-CONNECT:/run/agrodrone/msp-events.sock

# Test the control socket manually (requires socat)
printf '{"type":"upload_request"}\n' | socat - UNIX-CONNECT:/run/agrodrone/msp-control.sock
```

## Service dependency graph

```
agrodrone-msp-uart.service          (always running, owns /dev/serial0)
  └── agrodrone-mission-upload-client.path  (watches waypoints.json)
        └── agrodrone-mission-upload-client.service  (oneshot upload)

ndvi-capture.service                (Requires agrodrone-msp-uart)

agrodrone-image-upload.path         (watches /tmp/offload_requested)
  └── agrodrone-image-upload.service  (oneshot rsync to edge node)
```
