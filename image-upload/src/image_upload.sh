#!/bin/bash
set -euo pipefail

# Remove the trigger file immediately so the .path unit doesn't re-fire while we run
rm -f /tmp/offload_requested

WAYPOINTS_FILE="${SYSTEM_PATH}/waypoints.json"
FPID=$(python3 -c "import json; print(json.load(open('${WAYPOINTS_FILE}'))['fpid'])")

if [ -z "$FPID" ]; then
    echo "ERROR: could not read fpid from ${WAYPOINTS_FILE}"
    exit 1
fi

FPID_DIR="${SYSTEM_PATH}/flightplans/${FPID}"

if [ ! -d "$FPID_DIR" ]; then
    echo "ERROR: flight plan directory not found: ${FPID_DIR}"
    exit 1
fi

echo "Uploading missions for flight plan: ${FPID}"

for mission_dir in "${FPID_DIR}"/*/; do
    [ -d "$mission_dir" ] || continue
    mission_id=$(basename "$mission_dir")
    echo "  Syncing mission ${mission_id}..."
    ssh "$EDGENODE_IP" "mkdir -p ~/system/flightplans/${FPID}/${mission_id}"
    rsync -avz "$mission_dir" "${EDGENODE_IP}:~/system/flightplans/${FPID}/${mission_id}/" \
        || echo "  WARNING: rsync failed for ${mission_id}"
done

echo "Upload complete for flight plan ${FPID}."

# Touch a file on the remote machine to trigger its systemd .path unit
ssh "$EDGENODE_IP" "touch /tmp/start_processing"
echo "Sent trigger to ${EDGENODE_IP}:/tmp/start_processing"