from picamera2 import Picamera2
import json
import os
import pathlib
import select
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration via environment variables
# Set in the systemd unit file — never hardcoded here.
#
#   NDVI_TEST_MODE         "1" to run test captures instead of connecting to UART service
#   NDVI_TEST_COUNT        Number of test captures (default: 3)
#   MSP_EVENTS_SOCKET_PATH Path to msp_uart_service events socket (default: /run/agrodrone/msp-events.sock)
#   SYSTEM_PATH            Root system directory (default: /home/sr-design/agrodrone-system)
# ---------------------------------------------------------------------------
TEST_MODE   = os.environ.get("NDVI_TEST_MODE",  "0") == "1"
TEST_COUNT  = int(os.environ.get("NDVI_TEST_COUNT", "3"))
SYSTEM_PATH = os.environ.get("SYSTEM_PATH", "/home/sr-design/agrodrone-system")
MSP_EVENTS_SOCKET_PATH = os.environ.get("MSP_EVENTS_SOCKET_PATH", "/run/agrodrone/msp-events.sock")

WAYPOINTS_PATH  = os.path.join(SYSTEM_PATH, "waypoints.json")


def read_fpid(waypoints_path: str) -> str:
    """Read the flight plan ID from waypoints.json."""
    with open(waypoints_path, "r") as f:
        return json.load(f)["fpid"]


MISSION_ID      = str(uuid.uuid4())
FLIGHT_PLAN_ID  = read_fpid(WAYPOINTS_PATH)
FLIGHT_PLAN_DIR = os.path.join(SYSTEM_PATH, "flightplans", FLIGHT_PLAN_ID)
MISSION_PATH    = os.path.join(FLIGHT_PLAN_DIR, MISSION_ID)

WP = 0  # Current waypoint number — set from polled nav data in flight mode

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
try:
    pathlib.Path(FLIGHT_PLAN_DIR).mkdir(parents=True, exist_ok=True)
    os.mkdir(MISSION_PATH)
    print(f"Mission directory '{MISSION_PATH}' created.")
except FileExistsError:
    print(f"Directory '{MISSION_PATH}' already exists.")
except PermissionError:
    print(f"Permission denied: Unable to create '{MISSION_PATH}'.")
except Exception as e:
    print(f"An error occurred: {e}")


# ---------------------------------------------------------------------------
# Shared shutdown state
# ---------------------------------------------------------------------------
_shutdown_event  = threading.Event()
_offload_on_exit = False   # True only on clean mission shutdown (SIGTERM / nav state)


class _MSPTransientError(Exception):
    """Raised when we cannot reach the MSP service; lets systemd restart the unit."""


class _MissionSyncError(Exception):
    """Raised when capture state and hub state diverge in a way we cannot recover."""


def request_shutdown(signum=None, frame=None):
    """Signal handler / nav-state callback: request a clean exit and queue offload."""
    global _offload_on_exit
    print("Shutdown requested.")
    _offload_on_exit = True
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def ensure_dir(path: str):
    """Create directory if it doesn't exist."""
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def make_still_config(picam: Picamera2):
    """Apply still-capture configuration to a camera."""
    config = picam.create_still_configuration({"size": (800, 600), "format": "RGB888"})
    picam.configure(config)
    picam.options['quality'] = 90


def lock_exposure(picam: Picamera2) -> dict:
    """
    Snapshot current AE/AWB state and freeze it as manual controls.
    Note: set_controls() takes several frames to propagate on a running camera.
    Call time.sleep(0.3) for test purposes, but all funky frames will flush between WPs in flight
    """
    metadata = picam.capture_metadata()
    controls = {c: metadata[c] for c in ["ExposureTime", "AnalogueGain", "ColourGains"]}
    picam.set_controls(controls)
    return controls


def start_cameras(picam0: Picamera2, picam1: Picamera2):
    """Configure and start both cameras with locked exposure."""
    make_still_config(picam0)
    picam0.start()
    lock_exposure(picam0)

    make_still_config(picam1)
    picam1.start()
    lock_exposure(picam1)


def stop_cameras(picam0: Picamera2, picam1: Picamera2):
    """Stop both cameras cleanly."""
    picam0.stop()
    picam1.stop()
    print("Cameras stopped.")


def sequential_reconfig(picam0: Picamera2, picam1: Picamera2):
    """
    Re-lock exposure on both cameras without restarting them.
    Sleeps briefly so controls settle before the next capture.
    """
    for picam in (picam0, picam1):
        lock_exposure(picam)
    time.sleep(0.3)  # ~10 frames @ 30 fps


# ---------------------------------------------------------------------------
# Capture logic
# ---------------------------------------------------------------------------

def capture_from_camera(
    picam: Picamera2,
    cam_num: int,
    timestamp: str,
    outdir: str,
) -> dict:
    """Capture a JPEG from a single camera. Returns capture metadata dict."""
    image_path = os.path.join(outdir, f"{timestamp}_cam{cam_num}.jpg")
    picam.capture_file(image_path)

    meta = picam.capture_metadata()
    return {
        "camera_index": cam_num,
        "timestamp":    timestamp,
        "capture_info": {
            "image_path":      image_path,
            "ExposureTime":    meta.get("ExposureTime"),
            "AnalogueGain":    meta.get("AnalogueGain"),
            "ColourGains":     meta.get("ColourGains"),
            "SensorTimestamp": meta.get("SensorTimestamp"),
        },
    }


def sequential_capture(picam0: Picamera2, picam1: Picamera2) -> dict:
    """
    Capture one image from each camera and write a metadata JSON.
    Returns the metadata dict.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    ensure_dir(MISSION_PATH)

    metadata_dict = {
        "capture_timestamp": timestamp,
        "waypoint": WP,
        "camera_0": capture_from_camera(picam0, 0, timestamp, MISSION_PATH),
        "camera_1": capture_from_camera(picam1, 1, timestamp, MISSION_PATH),
    }

    metadata_path = os.path.join(MISSION_PATH, f"{timestamp}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata_dict, f, indent=2, default=str)

    print(f"[WP {WP}] Captured at {timestamp}")
    return metadata_dict


# ---------------------------------------------------------------------------
# Capture callback (called from both flight and test modes)
# ---------------------------------------------------------------------------

_capture_lock = threading.Lock()

def on_capture_press(picam0: Picamera2, picam1: Picamera2):
    """
    Trigger a dual-camera capture. Non-blocking: drops the call if a capture
    is already in progress. WP must be set by the caller before invoking this.
    """
    if _shutdown_event.is_set():
        return
    if not _capture_lock.acquire(blocking=False):  # drop if already capturing
        return
    try:
        sequential_capture(picam0, picam1)
        if WP % 5 == 0:
            sequential_reconfig(picam0, picam1)
    finally:
        _capture_lock.release()


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

_MAX_RECONNECT   = 5
_RECONNECT_DELAY = 3.0  # seconds between reconnect attempts


def run_flight(picam0: Picamera2, picam1: Picamera2):
    """
    Production flight mode.
    Connects to the MSP events socket and consumes mission status snapshots:
      mission_status.waypoint          → capture exactly once per waypoint
      mission_status.mission_complete  → begin clean shutdown

    The hub publishes state snapshots rather than edge-triggered events. That
    keeps reconnect behavior simple: on reconnect we resume from the latest
    known status. If the waypoint number jumps, we fail loudly instead of
    silently skipping captures.

    Transient disconnects are retried up to _MAX_RECONNECT times. On exhaustion,
    raises _MSPTransientError so systemd can restart this unit without triggering
    the post-mission image offload.
    """
    global WP

    signal.signal(signal.SIGTERM, request_shutdown)

    while not _shutdown_event.is_set():
        # --- Connect with retries ---
        sock = None
        for attempt in range(1, _MAX_RECONNECT + 1):
            if _shutdown_event.is_set():
                return
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(MSP_EVENTS_SOCKET_PATH)
                sock = s
                break
            except (FileNotFoundError, ConnectionRefusedError) as e:
                print(f"Cannot connect to MSP events socket "
                      f"(attempt {attempt}/{_MAX_RECONNECT}): {e}")
                time.sleep(_RECONNECT_DELAY)

        if sock is None:
            raise _MSPTransientError(
                f"Cannot reach MSP events socket at {MSP_EVENTS_SOCKET_PATH} "
                f"after {_MAX_RECONNECT} attempts"
            )

        print(f"Connected to MSP events socket at {MSP_EVENTS_SOCKET_PATH}")

        # --- Event read loop ---
        buf = b""
        try:
            while not _shutdown_event.is_set():
                r, _, _ = select.select([sock], [], [], 1.0)
                if not r:
                    continue

                try:
                    chunk = sock.recv(4096)
                except OSError as e:
                    print(f"Socket error: {e} — will retry connection.")
                    break
                if not chunk:
                    print("MSP events socket closed — will retry.")
                    break

                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    try:
                        event = json.loads(line_bytes)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") != "mission_status":
                        continue

                    if event.get("mission_complete"):
                        print("Mission complete — shutting down.")
                        request_shutdown()
                        break

                    next_wp = int(event.get("waypoint", 0))
                    if next_wp <= 0 or next_wp == WP:
                        continue
                    if next_wp < WP:
                        raise _MissionSyncError(
                            f"Waypoint went backward: local={WP}, hub={next_wp}"
                        )
                    if next_wp > WP + 1:
                        raise _MissionSyncError(
                            f"Missed waypoint transition: local={WP}, hub={next_wp}"
                        )

                    WP = next_wp
                    on_capture_press(picam0, picam1)

        finally:
            sock.close()

    print("Exiting flight loop.")


def run_test(picam0: Picamera2, picam1: Picamera2):
    """
    Test mode: fire NDVI_TEST_COUNT capture cycles with a short delay.
    No socket or hardware required.
    """
    global WP
    print(f"TEST MODE: running {TEST_COUNT} capture cycle(s) into {MISSION_PATH}")
    for _ in range(TEST_COUNT):
        if _shutdown_event.is_set():
            print("Shutdown during test — stopping early.")
            break
        sequential_capture(picam0, picam1)
        if WP % 5 == 0:
            print(f"[WP {WP}] Re-locking exposure...")
            sequential_reconfig(picam0, picam1)
        WP += 1
        time.sleep(0.5)
    print("Test complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _offload_on_exit

    print(f"Starting NDVI capture | mode={'TEST' if TEST_MODE else 'FLIGHT'} | "
          f"save_path={MISSION_PATH}")

    picam2_a = Picamera2(0)
    picam2_b = Picamera2(1)
    start_cameras(picam2_a, picam2_b)

    exit_status = 0
    try:
        if TEST_MODE:
            run_test(picam2_a, picam2_b)
            _offload_on_exit = True  # test runs always offload
        else:
            run_flight(picam2_a, picam2_b)
    except _MSPTransientError as e:
        # MSP service unreachable — let systemd restart us; do NOT trigger offload
        print(f"[TRANSIENT ERROR] {e}")
        exit_status = 1
    except _MissionSyncError as e:
        print(f"[SYNC ERROR] {e}")
        exit_status = 2
    except KeyboardInterrupt:
        print("Keyboard interrupt received.")
    finally:
        stop_cameras(picam2_a, picam2_b)
        ensure_dir(MISSION_PATH)
        metadata_path = os.path.join(MISSION_PATH, "metadata.json")
        with open(metadata_path, "w") as f:
            f.write(f"Completed flight: {MISSION_ID} with {WP} waypoints.\n")
        if _offload_on_exit:
            open("/tmp/offload_requested", "w").close()
            print("Offload trigger written to /tmp/offload_requested")

    if exit_status:
        sys.exit(exit_status)

if __name__ == "__main__":
    main()
