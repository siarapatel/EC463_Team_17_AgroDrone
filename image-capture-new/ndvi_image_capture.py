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


def log_state(message: str) -> None:
    print(f"[ndvi] {message}")


# ---------------------------------------------------------------------------
# Runtime mission context — set once at startup.
# ---------------------------------------------------------------------------

@dataclass
class MissionContext:
    mission_id:      str
    flight_plan_id:  str
    flight_plan_dir: str
    mission_path:    str


_mission_ctx: Optional[MissionContext] = None
_capture_count = 0


def _default_position_snapshot() -> dict:
    return {
        "lat": None,
        "lon": None,
        "alt_rel_m": None,
        "heading_deg": None,
        "gps_valid": False,
        "alt_valid": False,
        "heading_valid": False,
        "stale": True,
        "timestamp": None,
    }


def _position_snapshot_from_event(event: dict) -> dict:
    position = event.get("position") or {}
    return {
        "lat": position.get("lat"),
        "lon": position.get("lon"),
        "alt_rel_m": position.get("alt_rel_m"),
        "heading_deg": position.get("heading_deg"),
        "gps_valid": bool(position.get("gps_valid", False)),
        "alt_valid": bool(position.get("alt_valid", False)),
        "heading_valid": bool(position.get("heading_valid", False)),
        "stale": bool(position.get("stale", True)),
        "timestamp": position.get("timestamp"),
    }


_last_position_snapshot: dict = _default_position_snapshot()
_position_snapshot_lock: threading.Lock = threading.Lock()


def initialize_mission_context() -> MissionContext:
    global _mission_ctx
    flight_plan_id  = str(uuid.uuid4())
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


def sequential_capture(
    picam0: Picamera2,
    picam1: Picamera2,
    position_snapshot: dict,
    capture_index: int,
) -> dict:
    """Capture one image from each camera and write a per-capture metadata JSON."""
    ctx = _mission_ctx
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")

    metadata_dict = {
        "capture_timestamp": timestamp,
        "waypoint": capture_index,
        "position": dict(position_snapshot),
        "camera_0": capture_from_camera(picam0, 0, timestamp, ctx.mission_path),
        "camera_1": capture_from_camera(picam1, 1, timestamp, ctx.mission_path),
    }

    metadata_path = os.path.join(ctx.mission_path, f"{timestamp}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata_dict, f, indent=2, default=str)

    log_state(f"Capture complete | capture={capture_index} timestamp={timestamp}")
    return metadata_dict


# ---------------------------------------------------------------------------
# Background metadata polling thread
# ---------------------------------------------------------------------------

_POLL_RECONNECT_DELAY = 1.5


def _poll_metadata_loop() -> None:
    """
    Background daemon thread. Connects to msp-events.sock and updates
    _last_position_snapshot with each received position event.
    Reconnects silently on any failure. Exits when _shutdown_event is set.
    """
    global _last_position_snapshot

    while not _shutdown_event.is_set():
        sock = None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(MSP_EVENTS_SOCKET_PATH)
            sock = s
            log_state("Metadata poll thread connected to MSP events socket")
        except (FileNotFoundError, ConnectionRefusedError) as e:
            log_state(f"Metadata poll: cannot connect ({e}) — retrying in {_POLL_RECONNECT_DELAY}s")
            _shutdown_event.wait(_POLL_RECONNECT_DELAY)
            continue

        buf = b""
        try:
            while not _shutdown_event.is_set():
                r, _, _ = select.select([sock], [], [], 1.0)
                if not r:
                    continue
                try:
                    chunk = sock.recv(4096)
                except OSError as e:
                    log_state(f"Metadata poll: socket error ({e}) — reconnecting")
                    break
                if not chunk:
                    log_state("Metadata poll: socket closed — reconnecting")
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
                    snapshot = _position_snapshot_from_event(event)
                    with _position_snapshot_lock:
                        _last_position_snapshot = snapshot
        finally:
            if sock:
                sock.close()

        if not _shutdown_event.is_set():
            _shutdown_event.wait(_POLL_RECONNECT_DELAY)

    log_state("Metadata poll thread exiting")


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def run_flight(picam0: Picamera2, picam1: Picamera2) -> None:
    """
    Production flight mode.

    Starts a background thread to poll MSP position data non-blocking, then
    captures every 10 seconds until SIGTERM. Images are stamped with the last
    received position; null values are used if the MSP service is unavailable.
    """
    global _capture_count

    signal.signal(signal.SIGTERM, request_shutdown)

    poll_thread = threading.Thread(target=_poll_metadata_loop, daemon=True, name="metadata-poll")
    poll_thread.start()

    initialize_mission_context()
    _capture_count = 0
    log_state("Flight capture loop starting — interval=10s")

    while not _shutdown_event.wait(10):
        with _position_snapshot_lock:
            position = dict(_last_position_snapshot)

        log_state(f"Capture start | capture={_capture_count}")
        sequential_capture(picam0, picam1, position, _capture_count)
        _capture_count += 1

        if _capture_count % 5 == 0:
            log_state(f"Exposure relock | capture={_capture_count}")
            sequential_reconfig(picam0, picam1)

    log_state("Exiting flight loop")


def run_test(picam0: Picamera2, picam1: Picamera2) -> None:
    """Test mode: fire TEST_COUNT capture cycles at the same 10-second interval."""
    global _capture_count

    poll_thread = threading.Thread(target=_poll_metadata_loop, daemon=True, name="metadata-poll")
    poll_thread.start()

    initialize_mission_context()
    _capture_count = 0
    log_state(f"TEST MODE: running {TEST_COUNT} capture cycle(s) into {_mission_ctx.mission_path}")

    for _ in range(TEST_COUNT):
        if _shutdown_event.is_set():
            log_state("Shutdown during test — stopping early")
            break

        with _position_snapshot_lock:
            position = dict(_last_position_snapshot)

        sequential_capture(picam0, picam1, position, _capture_count)
        _capture_count += 1

        if _capture_count % 5 == 0:
            log_state(f"Exposure relock | capture={_capture_count}")
            sequential_reconfig(picam0, picam1)

        _shutdown_event.wait(10)

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
    log_state("Cameras ready")

    exit_status = 0
    try:
        if TEST_MODE:
            run_test(picam2_a, picam2_b)
            _offload_on_exit = True
        else:
            run_flight(picam2_a, picam2_b)
    except KeyboardInterrupt:
        log_state("Keyboard interrupt received")
    finally:
        stop_cameras(picam2_a, picam2_b)
        if _mission_ctx is not None:
            metadata_path = os.path.join(_mission_ctx.mission_path, "metadata.json")
            with open(metadata_path, "w") as f:
                f.write(f"Completed flight: {_mission_ctx.mission_id} with {_capture_count} captures.\n")
            if _offload_on_exit:
                open("/tmp/offload_requested", "w").close()
                log_state("Offload trigger written to /tmp/offload_requested")
        elif _offload_on_exit:
            log_state("Shutdown with no mission active — offload skipped")

    if exit_status:
        sys.exit(exit_status)


if __name__ == "__main__":
    main()
