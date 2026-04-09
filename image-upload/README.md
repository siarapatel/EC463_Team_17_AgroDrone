# Image Upload Setup #

## Add the following vars to .env in the base of the repo ##

`SYSTEM_PATH=/path/to/system/dir`

`EDGENODE_IP=user@edge-node-ip`

## Prepare the systemd services ##
`sudo cp -r services/* /etc/systemd/system`

`sudo systemctl enable --now agrodrone-image-upload.path`