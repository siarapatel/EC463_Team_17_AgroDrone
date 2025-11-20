# Systemd files

## Setup

```bash
sudo cp systemd/* /etc/systemd/system/ 
sudo systemctl enable --now agro-capture.service
sudo systemctl enable --now file-transfer-watcher.service
```

## Usage

To disable a service:

```bash
sudo systemctl stop [service-name].service
```

To enable a service:

```bash
sudo systemctl start [service-name].service
```

## Explanation

These two `systemd` files ensure that the file transfer & image capture services
are always alive on the sensing device.
