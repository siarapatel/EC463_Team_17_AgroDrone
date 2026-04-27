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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

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

WAYPOINTS_PATH = os.path.join(SYSTEM_PATH, "waypoints.json")


def log_state(message: str) -> None:
    print(f"[ndvi] {message}")


def read_fpid(waypoints_path: str) -> str:
    with open(waypoints_path, "r") as f:
        return json.load(f)["fpid"]


# ---------------------------------------------------------------------------
# Runtime mission context — unset until the first waypoint is accepted.
# MISSION_ID and FLIGHT_PLAN_ID are minted here, not at module load, so that
# a new UUID is generated for every mission even when waypoints.json is reused.
# ---------------------------------------------------------------------------

@dataclass
class MissionContext:
    mission_id:      str
    flight_plan_id:  str
    flight_plan_dir: str
    mission_path:    str


_mission_ctx: Optional[MissionContext] = None
WP = 0  # Current waypoint number — updated at runtime


def _default_position_snapshot() -> dict:
    return {
        "lat": None,
        "lon": None,
        "alt_rel_m": None,
        "gps_valid": False,
        "alt_valid": False,
        "stale": True,
        "timestamp": None,
    }


def _position_snapshot_from_event(event: dict) -> dict:
    position = event.get("position") or {}
    return {
        "lat": position.get("lat"),
        "lon": position.get("lon"),
        "alt_rel_m": position.get("alt_rel_m"),
        "gps_valid": bool(position.get("gps_valid", False)),
        "alt_valid": bool(position.get("alt_valid", False)),
        "stale": bool(position.get("stale", True)),
        "timestamp": position.get("timestamp"),
    }


_accepted_position_snapshot = _default_position_snapshot()


def initialize_mission_context() -> MissionContext:
    global _mission_ctx
    flight_plan_id  = read_fpid(WAYPOINTS_PATH)
    mission_id      = str(uuid.uuid4())
    flight_plan_dir = os.path.join(SYSTEM_PATH, "flightplans", flight_plan_id)
    mission_path    = os.path.join(flight_plan_dir, mission_id)
    pathlib.Path(mission_path).mkdir(parents=True, exist_ok=True)
    log_state(
        f"Mission initialized | fpid={flight_plan_id} id={mission_id} "
        f"path={mission_path}"
    )
    _mission_ctx = MissionContext(
        mission_id=mission_id,
        flight_plan_id=flight_plan_id,
        flight_plan_dir=flight_plan_dir,
        mission_path=mission_path,
    )
    return _mission_ctx


# ---------------------------------------------------------------------------
# Shared shutdown state
# ---------------------------------------------------------------------------
_shutdown_event  = threading.Event()
_offload_on_exit = False


class _MSPTransientError(Exception):
    """Raised when we cannot reach the MSP service; lets systemd restart the unit."""


class _MissionSyncError(Exception):
    """Raised when capture state and hub state diverge in a way we cannot recover."""


def request_shutdown(signum=None, frame=None):
    global _offload_on_exit
    print("Shutdown requested.")
    _offload_on_exit = True
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def ensure_dir(path: str):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def make_still_config(picam: Picamera2):
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
    make_still_config(picam0)
    picam0.start()
    lock_exposure(picam0)

    make_still_config(picam1)
    picam1.start()
    lock_exposure(picam1)


def stop_cameras(picam0: Picamera2, picam1: Picamera2):
    picam0.stop()
    picam1.stop()
    log_state("Cameras stopped")


def sequential_reconfig(picam0: Picamera2, picam1: Picamera2):
    """Re-lock exposure on both cameras without restarting them."""
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
    """Capture one image from each camera and write a per-capture metadata JSON."""
    ctx = _mission_ctx
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")

    metadata_dict = {
        "capture_timestamp": timestamp,
        "waypoint": WP,
        "position": dict(_accepted_position_snapshot),
        "camera_0": capture_from_camera(picam0, 0, timestamp, ctx.mission_path),
        "camera_1": capture_from_camera(picam1, 1, timestamp, ctx.mission_path),
    }

    metadata_path = os.path.join(ctx.mission_path, f"{timestamp}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata_dict, f, indent=2, default=str)

    log_state(f"Capture complete | waypoint={WP} timestamp={timestamp}")
    return metadata_dict


# ---------------------------------------------------------------------------
# Capture callback (called from both flight and test modes)
# ---------------------------------------------------------------------------

_capture_lock = threading.Lock()


def on_capture_press(picam0: Picamera2, picam1: Picamera2):
    """
    Trigger a dual-camera capture. Non-blocking: drops the call if a capture
    is already in progress. _mission_ctx and WP must be set by the caller.
    """
    if _shutdown_event.is_set():
        log_state(f"Capture skipped | waypoint={WP} reason=shutdown_in_progress")
        return
    if not _capture_lock.acquire(blocking=False):
        log_state(f"Capture skipped | waypoint={WP} reason=capture_already_running")
        return
    try:
        log_state(f"Capture start | waypoint={WP}")
        sequential_capture(picam0, picam1)
        if WP % 5 == 0:
            log_state(f"Exposure relock | waypoint={WP}")
            sequential_reconfig(picam0, picam1)
    finally:
        _capture_lock.release()


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

_MAX_RECONNECT   = 5
_RECONNECT_DELAY = 3.0


def run_flight(picam0: Picamera2, picam1: Picamera2):
    """
    Production flight mode.

    Stays idle until the first nonzero waypoint event. Mission context is
    initialized exactly once — on waypoint 1. If the process restarts
    mid-mission and the first nonzero waypoint seen is >1, raises
    _MissionSyncError (exit 2, not restarted) as the fail-stop policy.

    Replayed mission_complete=True snapshots from the hub are ignored while
    idle to prevent a Restart=always restart loop after clean mission exit.
    """
    global WP, _accepted_position_snapshot

    _accepted_position_snapshot = _default_position_snapshot()
    signal.signal(signal.SIGTERM, request_shutdown)

    while not _shutdown_event.is_set():
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
                log_state(
                    f"Cannot connect to MSP events socket "
                    f"(attempt {attempt}/{_MAX_RECONNECT}): {e}"
                )
                time.sleep(_RECONNECT_DELAY)

        if sock is None:
            raise _MSPTransientError(
                f"Cannot reach MSP events socket at {MSP_EVENTS_SOCKET_PATH} "
                f"after {_MAX_RECONNECT} attempts"
            )

        log_state(f"Connected to MSP events socket at {MSP_EVENTS_SOCKET_PATH}")

        buf = b""
        try:
            while not _shutdown_event.is_set():
                r, _, _ = select.select([sock], [], [], 1.0)
                if not r:
                    continue

                try:
                    chunk = sock.recv(4096)
                except OSError as e:
                    log_state(f"Socket error: {e} — will retry connection")
                    break
                if not chunk:
                    log_state("MSP events socket closed — will retry")
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

                    log_state(
                        "Event received | "
                        f"waypoint={event.get('waypoint', 0)} "
                        f"mission_complete={event.get('mission_complete', False)} "
                        f"gps_valid={(event.get('position') or {}).get('gps_valid', False)} "
                        f"alt_valid={(event.get('position') or {}).get('alt_valid', False)} "
                        f"stale={(event.get('position') or {}).get('stale', True)}"
                    )

                    if event.get("mission_complete"):
                        if _mission_ctx is None:
                            log_state("Mission-complete snapshot ignored while idle")
                            continue
                        log_state("Mission complete received — shutting down")
                        request_shutdown()
                        break

                    next_wp = int(event.get("waypoint", 0))
                    if next_wp <= 0:
                        log_state(f"Waypoint ignored | waypoint={next_wp} reason=non_positive")
                        continue

                    if _mission_ctx is None:
                        # Idle: mission may only start on waypoint 1
                        if next_wp > 1:
                            raise _MissionSyncError(
                                f"Mid-mission restart: first waypoint is {next_wp}, expected 1"
                            )
                        log_state("First active waypoint received — initializing mission context")
                        initialize_mission_context()
                        WP = next_wp
                        _accepted_position_snapshot = _position_snapshot_from_event(event)
                        log_state(f"Waypoint accepted | local={WP} reason=mission_start")
                        on_capture_press(picam0, picam1)
                    else:
                        # Mission active: strict sequential sync
                        if next_wp == WP:
                            log_state(f"Waypoint ignored | waypoint={next_wp} reason=duplicate")
                            continue
                        if next_wp < WP:
                            raise _MissionSyncError(
                                f"Waypoint went backward: local={WP}, hub={next_wp}"
                            )
                        if next_wp > WP + 1:
                            raise _MissionSyncError(
                                f"Missed waypoint transition: local={WP}, hub={next_wp}"
                            )
                        previous_wp = WP
                        WP = next_wp
                        _accepted_position_snapshot = _position_snapshot_from_event(event)
                        log_state(f"Waypoint accepted | local={previous_wp} next={WP}")
                        on_capture_press(picam0, picam1)

        finally:
            sock.close()

    log_state("Exiting flight loop")


def run_test(picam0: Picamera2, picam1: Picamera2):
    """Test mode: fire TEST_COUNT capture cycles with a short delay."""
    global WP, _accepted_position_snapshot
    WP = 0
    initialize_mission_context()
    _accepted_position_snapshot = _default_position_snapshot()
    log_state(f"TEST MODE: running {TEST_COUNT} capture cycle(s) into {_mission_ctx.mission_path}")
    for _ in range(TEST_COUNT):
        if _shutdown_event.is_set():
            log_state("Shutdown during test — stopping early")
            break
        sequential_capture(picam0, picam1)
        if WP % 5 == 0:
            log_state(f"Exposure relock | waypoint={WP}")
            sequential_reconfig(picam0, picam1)
        WP += 1
        time.sleep(0.5)
    log_state("Test complete")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _offload_on_exit

    log_state(f"Starting | mode={'TEST' if TEST_MODE else 'FLIGHT'}")

    picam2_a = Picamera2(0)
    picam2_b = Picamera2(1)
    start_cameras(picam2_a, picam2_b)
    log_state("Cameras ready — waiting for mission events")

    exit_status = 0
    try:
        if TEST_MODE:
            run_test(picam2_a, picam2_b)
            _offload_on_exit = True
        else:
            run_flight(picam2_a, picam2_b)
    except _MSPTransientError as e:
        log_state(f"[TRANSIENT ERROR] {e}")
        exit_status = 1
    except _MissionSyncError as e:
        log_state(f"[SYNC ERROR] {e}")
        exit_status = 2
    except KeyboardInterrupt:
        log_state("Keyboard interrupt received")
    finally:
        stop_cameras(picam2_a, picam2_b)
        if _mission_ctx is not None:
            metadata_path = os.path.join(_mission_ctx.mission_path, "metadata.json")
            with open(metadata_path, "w") as f:
                f.write(f"Completed flight: {_mission_ctx.mission_id} with {WP} waypoints.\n")
            if _offload_on_exit:
                open("/tmp/offload_requested", "w").close()
                log_state("Offload trigger written to /tmp/offload_requested")
        elif _offload_on_exit:
            log_state("Shutdown with no mission active — offload skipped")

    if exit_status:
        sys.exit(exit_status)


if __name__ == "__main__":
    main()
