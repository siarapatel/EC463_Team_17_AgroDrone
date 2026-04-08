#!/bin/bash
set -euo pipefail

# Remove the trigger file immediately so the .path unit doesn't re-fire while we run
rm -f /tmp/offload_requested

WAYPOINTS_FILE="${SYSTEM_PATH}/waypoints.json"
FLIGHTPLANS_DIR="${SYSTEM_PATH}/flightplans"

# Read fpid from waypoints.json
FPID=$(python3 -c "import json,sys; print(json.load(open('${WAYPOINTS_FILE}'))['fpid'])")

if [ -z "$FPID" ]; then
    echo "ERROR: could not read fpid from ${WAYPOINTS_FILE}"
    exit 1
fi

FPID_DIR="${FLIGHTPLANS_DIR}/${FPID}"

if [ ! -d "$FPID_DIR" ]; then
    echo "ERROR: flight plan directory not found: ${FPID_DIR}"
    exit 1
fi

echo "Uploading missions for flight plan: ${FPID}"

for mission_dir in "${FPID_DIR}"/*/; do
    [ -d "$mission_dir" ] || continue
    mission_id=$(basename "$mission_dir")
    echo "  Syncing mission ${mission_id}..."
    rsync -avz "$mission_dir" "${RSYNC_DEST}/${FPID}/${mission_id}/" \
        || echo "  WARNING: rsync failed for ${mission_id}"
done

echo "Upload complete for flight plan ${FPID}."

# Touch a file on the remote machine to trigger its systemd .path unit
REMOTE_HOST="${RSYNC_DEST%%:*}"
ssh "$REMOTE_HOST" "touch /tmp/images_sent"
echo "Sent trigger to ${REMOTE_HOST}:/tmp/images_sent"
