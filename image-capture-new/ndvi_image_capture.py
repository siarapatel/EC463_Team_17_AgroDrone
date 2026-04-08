from picamera2 import Picamera2
import signal
import threading
import time
from datetime import datetime, timezone
import pathlib
import os
from gpiozero import Button
import json

# ---------------------------------------------------------------------------
# Configuration via environment variables
# Set in the systemd unit file — never hardcoded here.
#
#   NDVI_TEST_MODE      "1" to run test captures instead of waiting for GPIO
#   NDVI_TEST_COUNT     Number of test captures (default: 3)
#   NDVI_SAVE_PATH      Output directory for images and metadata JSON
#   NDVI_CAPTURE_PIN    BCM pin number for waypoint capture trigger (default: 27)
#   NDVI_KILL_PIN       BCM pin number for graceful shutdown trigger (default: 17)
# ---------------------------------------------------------------------------
TEST_MODE   = os.environ.get("NDVI_TEST_MODE",   "0") == "1"
TEST_COUNT  = int(os.environ.get("NDVI_TEST_COUNT",  "3"))
IMAGE_SAVE_PATH   = os.environ.get("NDVI_SAVE_PATH",   "/home/sr-design/agrodrone-system/flight-images")
CAPTURE_PIN = int(os.environ.get("NDVI_CAPTURE_PIN", "27"))
KILL_PIN    = int(os.environ.get("NDVI_KILL_PIN",    "17"))
FLIGHT_NUMBER = os.environ.get("OS_FLIGHT_NUMBER", "UnknownFlight")

WP = 0  # Global waypoint counter

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
try:
    os.mkdir(IMAGE_SAVE_PATH)
    print(f"Directory '{IMAGE_SAVE_PATH}' created successfully.")
except FileExistsError:
    print(f"Directory '{IMAGE_SAVE_PATH}' already exists.")
except PermissionError:
    print(f"Permission denied: Unable to create '{IMAGE_SAVE_PATH}'.")
except Exception as e:
    print(f"An error occurred: {e}")


# ---------------------------------------------------------------------------
# Shared shutdown event — set by kill pin or SIGTERM, unblocks main loop
# ---------------------------------------------------------------------------
_shutdown_event = threading.Event()


def request_shutdown(signum=None, frame=None):
    """Signal handler and kill-pin callback: request a clean exit."""
    print("Shutdown requested.")
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
    ensure_dir(IMAGE_SAVE_PATH)

    metadata_dict = {
        "capture_timestamp": timestamp,
        "waypoint": WP,
        "camera_0": capture_from_camera(picam0, 0, timestamp, IMAGE_SAVE_PATH),
        "camera_1": capture_from_camera(picam1, 1, timestamp, IMAGE_SAVE_PATH),
    }
    metadata_path = os.path.join(IMAGE_SAVE_PATH, f"{timestamp}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata_dict, f, indent=2, default=str)

    print(f"[WP {WP}] Captured at {timestamp}")
    return metadata_dict


# ---------------------------------------------------------------------------
# GPIO callbacks
# ---------------------------------------------------------------------------

_capture_lock = threading.Lock()

def on_capture_press(picam0: Picamera2, picam1: Picamera2):
    global WP
    if _shutdown_event.is_set():
        return
    if not _capture_lock.acquire(blocking=False):  # drop the call if already capturing
        return
    try:
        sequential_capture(picam0, picam1)
        if WP % 5 == 0:
            sequential_reconfig(picam0, picam1)
        WP += 1
    finally:
        _capture_lock.release()

def on_kill_press():
    """Kill-pin callback: trigger clean shutdown."""
    print(f"Kill pin (BCM {KILL_PIN}) triggered.")
    request_shutdown()
    # I'll think of something better later
    open("/tmp/offload_requested", "w").close() # So the rsyc triggering offload service has something to look for
    


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def run_gpio(picam0: Picamera2, picam1: Picamera2):
    """
    Production flight mode.
    Blocks on _shutdown_event until the kill pin or SIGTERM fires.
    """
    """ I don't understand this line, but allegedly:
        Without this line:
            SIGTERM would immediately terminate the process
            Your cleanup (stop_cameras, etc.) might not run
        With it:
            You get a graceful shutdown path
    """
    signal.signal(signal.SIGTERM, request_shutdown)

    capture_button = Button(CAPTURE_PIN, pull_up=False)
    kill_button    = Button(KILL_PIN,    pull_up=False)

    capture_button.when_pressed = lambda: on_capture_press(picam0, picam1)
    kill_button.when_pressed    = on_kill_press

    print(f"Ready. Capture pin: BCM {CAPTURE_PIN} | Kill pin: BCM {KILL_PIN}")
    _shutdown_event.wait()
    print("Exiting main loop.")


def run_test(picam0: Picamera2, picam1: Picamera2):
    """
    Test mode: fire NDVI_TEST_COUNT capture cycles with a short delay.
    No GPIO hardware required.
    """
    global WP
    print(f"TEST MODE: running {TEST_COUNT} capture cycle(s) into {IMAGE_SAVE_PATH}")
    for _ in range(TEST_COUNT):
        if _shutdown_event.is_set():
            print("Shutdown during test — stopping early.")
            break
        sequential_capture(picam0, picam1)
        if WP % 5 == 0:
            print(f"[WP {WP}] Re-locking exposure...")
            sequential_reconfig(picam0, picam1)
        WP += 1
        time.sleep(0.5) #fine now that we only capture once per waypoint
    print("Test complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print(f"Starting NDVI capture | mode={'TEST' if TEST_MODE else 'FLIGHT'} | "
          f"save_path={IMAGE_SAVE_PATH}")

    picam2_a = Picamera2(0)
    picam2_b = Picamera2(1)
    start_cameras(picam2_a, picam2_b)

    try:
        if TEST_MODE:
            run_test(picam2_a, picam2_b)
        else:
            run_gpio(picam2_a, picam2_b)
    except KeyboardInterrupt:
        print("Keyboard interrupt received.")
    finally:
        stop_cameras(picam2_a, picam2_b)
        flight_log_folder_path = "/home/sr-design/agrodrone-system/flight-logs" 
        ensure_dir(flight_log_folder_path)
        flight_log_path = os.path.join(flight_log_folder_path,f"{FLIGHT_NUMBER}.txt")
        with open(flight_log_path, "w") as f:
            f.write(f"Completed flight: {FLIGHT_NUMBER} with {WP} waypoints.\n")

if __name__ == "__main__":
    main()
