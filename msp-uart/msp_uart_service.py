#!/usr/bin/env python3
"""
AgroDrone MSP UART Service
--------------------------
Owns /dev/serial0 exclusively. Polls MSP_NAV_STATUS at 5 Hz and exposes two
small local IPC surfaces:

  msp-events.sock  — push-only mission status stream
  msp-control.sock — request/response control channel for mission uploads

Events socket messages:
  {
    "type": "mission_status",
    "waypoint": N,
    "mission_complete": false,
    "position": {...}
  }

Control socket messages:
  client → service: {"type": "upload_request"}
  service → client: {"type": "upload_response", "success": true, "error": null}
"""

import json
import os
import queue
import select
import signal
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration (overridable via environment variables)
# ---------------------------------------------------------------------------
SERIAL_PORT          = os.environ.get("MSP_SERIAL_PORT",          "/dev/serial0")
BAUD_RATE            = int(os.environ.get("MSP_BAUD_RATE",        "115200"))
POLL_INTERVAL        = float(os.environ.get("MSP_POLL_INTERVAL",  "0.2"))  # 5 Hz
SERIAL_TIMEOUT       = float(os.environ.get("MSP_SERIAL_TIMEOUT", "0.05"))
TELEMETRY_TIMEOUT    = float(os.environ.get("MSP_TELEMETRY_TIMEOUT", "0.05"))
TELEMETRY_STALE_SECS = float(os.environ.get("MSP_TELEMETRY_STALE_SECS", "1.0"))
EVENTS_SOCKET_PATH   = os.environ.get("MSP_EVENTS_SOCKET_PATH",   "/run/agrodrone/msp-events.sock")
CONTROL_SOCKET_PATH  = os.environ.get("MSP_CONTROL_SOCKET_PATH",  "/run/agrodrone/msp-control.sock")
WAYPOINTS_PATH       = os.environ.get("WAYPOINTS_PATH",
                                       "/home/sr-design/agrodrone-system/waypoints.json")

# ---------------------------------------------------------------------------
# MSP V2 constants
# ---------------------------------------------------------------------------
MSP_RAW_GPS     = 106
MSP_ALTITUDE    = 109
MSP_ATTITUDE    = 108
MSP_NAV_STATUS  = 121
MSP_SET_WP      = 209
MSP_SAVE_NVRAM  = 19

WAYPOINT_ACTION = 0x01   # INAV: "Fly here"
LAST_WP_FLAG    = 0xA5   # marks final waypoint

# ---------------------------------------------------------------------------
# MSP V2 protocol helpers
# ---------------------------------------------------------------------------

def _crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc


def build_msp_v2(function: int, payload: bytes = b"") -> bytes:
    """Build a complete MSP V2 request frame."""
    flag = 0
    size = len(payload)
    msg = bytearray(9 + size)
    msg[0] = ord("$")
    msg[1] = ord("X")
    msg[2] = ord("<")
    msg[3] = flag
    msg[4] = function & 0xFF
    msg[5] = (function >> 8) & 0xFF
    msg[6] = size & 0xFF
    msg[7] = (size >> 8) & 0xFF
    if payload:
        msg[8:8 + size] = payload
    msg[-1] = _crc8_dvb_s2(msg[3:-1])
    return bytes(msg)


def read_msp_v2_response(ser) -> Optional[tuple]:
    """
    Read one complete MSP V2 response from the serial port.
    Reads the 3-byte preamble, then the 5-byte meta (flag + function + size),
    then exactly payload_size + 1 bytes (payload + CRC).
    Returns (function_code, payload_bytes) or None on error/CRC failure.
    """
    header = ser.read(3)
    if len(header) < 3 or header[:3] != b"$X>":
        return None

    # flag(1) + function_lo(1) + function_hi(1) + size_lo(1) + size_hi(1)
    rest = ser.read(5)
    if len(rest) < 5:
        return None

    flag         = rest[0]  # noqa: F841
    function     = rest[1] | (rest[2] << 8)
    payload_size = rest[3] | (rest[4] << 8)

    body = ser.read(payload_size + 1)   # payload + CRC
    if len(body) < payload_size + 1:
        return None

    payload      = body[:payload_size]
    received_crc = body[payload_size]

    crc_data = bytes([flag, rest[1], rest[2], rest[3], rest[4]]) + payload
    if _crc8_dvb_s2(crc_data) != received_crc:
        return None

    return function, payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def _parse_nav_status(payload: bytes) -> Optional[dict]:
    """
    Parse raw MSP_NAV_STATUS payload into a dict of named fields.

    INAV encodes this as 5 uint8 fields followed by one int16 (7 bytes total):
      uint8  nav_mode
      uint8  nav_state
      uint8  nav_activeWpAction
      uint8  nav_activeWpNumber
      uint8  nav_error
      int16  nav_headingHoldTarget
    """
    if len(payload) < 7:
        return None
    nav_mode, nav_state, nav_activeWpAction, nav_activeWpNumber, \
        nav_error, nav_headingHoldTarget = struct.unpack("<BBBBBh", payload[:7])
    return {
        "nav_mode":              nav_mode,
        "nav_state":             nav_state,
        "nav_activeWpNumber":    nav_activeWpNumber,
        "nav_activeWpAction":    nav_activeWpAction,
        "nav_error":             nav_error,
        "nav_headingHoldTarget": nav_headingHoldTarget,
    }


def _parse_raw_gps(payload: bytes) -> Optional[dict]:
    """
    Parse the leading MSP_RAW_GPS fields exposed by INAV.

    Layout used here matches the classic MSP definition:
      uint8   fix
      uint8   num_sat
      int32   lat (degE7)
      int32   lon (degE7)
      int16   alt_msl_m
      uint16  speed_cm_s
      uint16  ground_course_tenths_deg
    """
    if len(payload) < 16:
        return None

    fix, num_sat, lat_raw, lon_raw, alt_msl_m, speed_cm_s, ground_course_tenths = (
        struct.unpack("<BBiihHH", payload[:16])
    )
    return {
        "fix": fix,
        "num_sat": num_sat,
        "lat": lat_raw / 10_000_000.0,
        "lon": lon_raw / 10_000_000.0,
        "alt_msl_m": float(alt_msl_m),
        "speed_m_s": speed_cm_s / 100.0,
        "ground_course_deg": ground_course_tenths / 10.0,
    }


def _parse_altitude(payload: bytes) -> Optional[dict]:
    """
    Parse MSP_ALTITUDE.

    INAV exposes estimated relative altitude in centimetres followed by the
    variometer reading in centimetres per second.
    """
    if len(payload) < 6:
        return None

    alt_cm, variometer_cm_s = struct.unpack("<ih", payload[:6])
    return {
        "alt_rel_m": alt_cm / 100.0,
        "variometer_m_s": variometer_cm_s / 100.0,
    }


def _parse_attitude(payload: bytes) -> Optional[dict]:
    """
    Parse MSP_ATTITUDE.

    INAV encodes roll and pitch in decidegrees (int16) and heading in degrees
    (int16, range 0–360).
    """
    if len(payload) < 6:
        return None

    _roll_dd, _pitch_dd, heading_deg = struct.unpack("<hhh", payload[:6])
    return {
        "heading_deg": float(heading_deg % 360),
    }

def _mission_status_from_nav(nav: dict) -> dict:
    """
    Reduce raw FC nav fields to the only state the capture pipeline needs.
    This is snapshot-based rather than edge-based so reconnecting clients can
    resume from the latest status without reconstructing a hidden state machine.
    """
    waypoint = 0
    if nav["nav_activeWpAction"] == WAYPOINT_ACTION and nav["nav_activeWpNumber"] > 0:
        waypoint = nav["nav_activeWpNumber"]

    mission_complete = nav["nav_mode"] == 2 and nav["nav_state"] == 10
    return {
        "type": "mission_status",
        "waypoint": waypoint,
        "mission_complete": mission_complete,
    }

# ---------------------------------------------------------------------------
# Mission upload (runs on the serial thread — no concurrent serial access)
# ---------------------------------------------------------------------------

def _upload_waypoint(ser, index: int, lat: float, lon: float,
                     alt_cm: int, is_last: bool) -> None:
    lat_int = int(lat * 10_000_000)
    lon_int = int(lon * 10_000_000)
    flag = LAST_WP_FLAG if is_last else 0x00
    payload = struct.pack(
        "<BBiiihhhB",
        index,
        WAYPOINT_ACTION,
        lat_int,
        lon_int,
        alt_cm,
        0,    # P1 — hold time / speed; 0 = use nav_auto_speed
        0,    # P2 — unused
        1,    # P3 — logic condition bitfield
        flag,
    )
    ser.write(build_msp_v2(MSP_SET_WP, payload))
    time.sleep(0.1)
    if ser.in_waiting:
        ser.read(ser.in_waiting)
    print(f"[msp-uart] Uploaded WP {index}: {lat}, {lon}")


def run_upload(ser) -> tuple:
    """
    Read waypoints.json and upload the full mission to the FC.
    Returns (success: bool, error: str | None).
    Only called from the serial thread.
    """
    try:
        with open(WAYPOINTS_PATH) as f:
            data = json.load(f)
        waypoints = data["waypoints"]
    except Exception as e:
        return False, f"Failed to read {WAYPOINTS_PATH}: {e}"

    if not waypoints:
        return False, "Waypoints list is empty"

    print(f"[msp-uart] Uploading {len(waypoints)} waypoints…")
    try:
        # WP 0: home position (altitude 0)
        _upload_waypoint(ser, 0, waypoints[0]["lat"], waypoints[0]["lng"],
                         alt_cm=0, is_last=False)

        # WP 1..N: mission waypoints
        for i, point in enumerate(waypoints):
            wp_index = i + 1
            is_last  = (i == len(waypoints) - 1)
            alt_cm   = int(point["alt_m"] * 100)
            _upload_waypoint(ser, wp_index, point["lat"], point["lng"],
                             alt_cm, is_last)

        # Save to EEPROM
        ser.write(build_msp_v2(MSP_SAVE_NVRAM, b""))
        print("[msp-uart] Mission saved to NVRAM")
        time.sleep(0.5)

    except Exception as e:
        return False, f"Upload error: {e}"

    return True, None

# ---------------------------------------------------------------------------
# Upload request dataclass (shared between control client threads and serial thread)
# ---------------------------------------------------------------------------

@dataclass
class UploadRequest:
    reply_event: threading.Event = field(default_factory=threading.Event)
    result: list = field(default_factory=lambda: [False, "not set"])


@dataclass
class TelemetryCache:
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_rel_m: Optional[float] = None
    heading_deg: Optional[float] = None
    timestamp_utc: Optional[str] = None
    last_refresh_monotonic: Optional[float] = None
    _gps_source_valid: bool = False
    _alt_source_valid: bool = False
    _heading_source_valid: bool = False
    _gps_sample_monotonic: Optional[float] = None
    _alt_sample_monotonic: Optional[float] = None
    _heading_sample_monotonic: Optional[float] = None

    def _mark_refresh(self, now_monotonic: float) -> None:
        self.last_refresh_monotonic = now_monotonic
        self.timestamp_utc = _utc_now_iso()

    def update_raw_gps(self, gps: dict) -> None:
        now_monotonic = time.monotonic()
        self._mark_refresh(now_monotonic)
        self._gps_sample_monotonic = now_monotonic

        if gps["fix"] > 0:
            self.lat = gps["lat"]
            self.lon = gps["lon"]
            self._gps_source_valid = True
        else:
            self._gps_source_valid = False

    def update_altitude(self, altitude: dict) -> None:
        now_monotonic = time.monotonic()
        self._mark_refresh(now_monotonic)
        self._alt_sample_monotonic = now_monotonic
        self.alt_rel_m = altitude["alt_rel_m"]
        self._alt_source_valid = True

    def update_attitude(self, attitude: dict) -> None:
        now_monotonic = time.monotonic()
        self._mark_refresh(now_monotonic)
        self._heading_sample_monotonic = now_monotonic
        self.heading_deg = attitude["heading_deg"]
        self._heading_source_valid = True

    def snapshot(self) -> dict:
        now_monotonic = time.monotonic()
        gps_fresh = (
            self._gps_sample_monotonic is not None
            and (now_monotonic - self._gps_sample_monotonic) <= TELEMETRY_STALE_SECS
        )
        alt_fresh = (
            self._alt_sample_monotonic is not None
            and (now_monotonic - self._alt_sample_monotonic) <= TELEMETRY_STALE_SECS
        )
        heading_fresh = (
            self._heading_sample_monotonic is not None
            and (now_monotonic - self._heading_sample_monotonic) <= TELEMETRY_STALE_SECS
        )
        gps_valid = (
            self._gps_source_valid
            and gps_fresh
            and self.lat is not None
            and self.lon is not None
        )
        alt_valid = (
            self._alt_source_valid
            and alt_fresh
            and self.alt_rel_m is not None
        )
        heading_valid = (
            self._heading_source_valid
            and heading_fresh
            and self.heading_deg is not None
        )
        stale = (
            self.last_refresh_monotonic is None
            or (now_monotonic - self.last_refresh_monotonic) > TELEMETRY_STALE_SECS
        )
        return {
            "lat": self.lat if gps_valid else None,
            "lon": self.lon if gps_valid else None,
            "alt_rel_m": self.alt_rel_m if alt_valid else None,
            "heading_deg": self.heading_deg if heading_valid else None,
            "gps_valid": gps_valid,
            "alt_valid": alt_valid,
            "heading_valid": heading_valid,
            "stale": stale,
            "timestamp": self.timestamp_utc,
        }

_command_queue: queue.Queue = queue.Queue()

# ---------------------------------------------------------------------------
# Event subscriber list and last emitted mission status
# ---------------------------------------------------------------------------

_event_subscribers: list = []
_event_subscribers_lock = threading.Lock()
_last_emitted_status: Optional[dict] = None
_status_lock = threading.Lock()


def _neutral_position_snapshot() -> dict:
    return {
        "lat": None,
        "lon": None,
        "alt_rel_m": None,
        "gps_valid": False,
        "alt_valid": False,
        "stale": True,
        "timestamp": None,
    }


def _neutral_replay_status() -> dict:
    return {
        "type": "mission_status",
        "waypoint": 0,
        "mission_complete": False,
        "position": _neutral_position_snapshot(),
    }


_replay_status: dict = _neutral_replay_status()


def _format_status(status: dict) -> str:
    position = status.get("position") or {}
    return (
        f"waypoint={status.get('waypoint')} "
        f"mission_complete={status.get('mission_complete')} "
        f"gps_valid={position.get('gps_valid')} "
        f"alt_valid={position.get('alt_valid')} "
        f"stale={position.get('stale')}"
    )


def _is_terminal_complete(status: dict) -> bool:
    return bool(status.get("mission_complete"))


def _is_neutral_idle(status: dict) -> bool:
    return (
        status.get("type") == "mission_status"
        and int(status.get("waypoint", 0)) == 0
        and not bool(status.get("mission_complete"))
    )


def _send_status_to_subscribers(status: dict) -> None:
    """Send one mission status JSON line to all subscribers; drop dead connections."""
    line = (json.dumps(status) + "\n").encode()
    with _event_subscribers_lock:
        subscriber_count = len(_event_subscribers)
    print(
        f"[msp-uart] Emitting mission_status | {_format_status(status)} "
        f"subscribers={subscriber_count}"
    )
    with _event_subscribers_lock:
        alive = []
        for conn in _event_subscribers:
            try:
                conn.sendall(line)
                alive.append(conn)
            except (BrokenPipeError, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
        _event_subscribers[:] = alive


def _handle_status_update(status: dict) -> None:
    """
    Maintain separate live and replay state.

    - `_last_emitted_status` deduplicates live broadcasts to current subscribers.
    - `_replay_status` is what newly connected subscribers receive.
    """
    global _last_emitted_status, _replay_status

    with _status_lock:
        if status == _last_emitted_status:
            return

        suppress_live_broadcast = (
            _is_neutral_idle(status)
            and _last_emitted_status is not None
            and _is_terminal_complete(_last_emitted_status)
        )
        suppress_repeated_terminal_complete = (
            _is_terminal_complete(status)
            and _last_emitted_status is not None
            and _is_terminal_complete(_last_emitted_status)
        )

        _last_emitted_status = dict(status)
        if _is_terminal_complete(status):
            _replay_status = _neutral_replay_status()
        else:
            _replay_status = dict(status)

    if suppress_live_broadcast:
        print("[msp-uart] Suppressing live neutral reset after terminal completion")
        return
    if suppress_repeated_terminal_complete:
        print("[msp-uart] Suppressing repeated mission-complete snapshot")
        return

    _send_status_to_subscribers(status)


def _send_replay_status(conn: socket.socket, status: dict) -> None:
    """Send the current replay snapshot to one newly connected client."""
    conn.sendall((json.dumps(status) + "\n").encode())

# ---------------------------------------------------------------------------
# Serial loop (the ONLY code path that ever touches the serial port)
# ---------------------------------------------------------------------------

def _request_msp_payload(ser, function: int, timeout: float) -> Optional[bytes]:
    previous_timeout = ser.timeout
    deadline = time.monotonic() + timeout
    try:
        ser.write(build_msp_v2(function))
        while time.monotonic() < deadline:
            ser.timeout = max(0.001, deadline - time.monotonic())
            result = read_msp_v2_response(ser)
            if result is None:
                if ser.in_waiting:
                    ser.read(ser.in_waiting)
                return None

            response_function, payload = result
            if response_function == function:
                return payload
        return None
    finally:
        ser.timeout = previous_timeout


def _build_status_snapshot(nav: dict, telemetry_cache: TelemetryCache) -> dict:
    status = _mission_status_from_nav(nav)
    status["position"] = telemetry_cache.snapshot()
    return status


def serial_loop(shutdown: threading.Event) -> None:
    import serial

    print(f"[msp-uart] Opening {SERIAL_PORT} @ {BAUD_RATE}")
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=SERIAL_TIMEOUT)

    print("[msp-uart] Serial port open — starting poll loop")
    telemetry_cache = TelemetryCache()
    next_telemetry_function = MSP_ALTITUDE
    _TELEMETRY_SEQUENCE = (MSP_ALTITUDE, MSP_RAW_GPS, MSP_ATTITUDE)

    try:
        while not shutdown.is_set():
            # Check for pending upload requests first
            try:
                req: UploadRequest = _command_queue.get_nowait()
                print("[msp-uart] Upload request received — pausing poll")
                success, error = run_upload(ser)
                req.result[:] = [success, error]
                req.reply_event.set()
                print(f"[msp-uart] Upload complete: success={success} error={error}")
            except queue.Empty:
                pass

            # Poll NAV first so mission-state publication stays independent from
            # best-effort position telemetry.
            nav_payload = _request_msp_payload(ser, MSP_NAV_STATUS, SERIAL_TIMEOUT)
            if nav_payload is not None:
                nav = _parse_nav_status(nav_payload)
                if nav:
                    _handle_status_update(_build_status_snapshot(nav, telemetry_cache))

            # At most one opportunistic telemetry request per loop, alternating
            # altitude and GPS so mission flow never waits on both.
            if _command_queue.empty():
                telemetry_payload = _request_msp_payload(
                    ser,
                    next_telemetry_function,
                    TELEMETRY_TIMEOUT,
                )
                if telemetry_payload is not None:
                    if next_telemetry_function == MSP_ALTITUDE:
                        altitude = _parse_altitude(telemetry_payload)
                        if altitude is not None:
                            telemetry_cache.update_altitude(altitude)
                    elif next_telemetry_function == MSP_RAW_GPS:
                        gps = _parse_raw_gps(telemetry_payload)
                        if gps is not None:
                            telemetry_cache.update_raw_gps(gps)
                    elif next_telemetry_function == MSP_ATTITUDE:
                        attitude = _parse_attitude(telemetry_payload)
                        if attitude is not None:
                            telemetry_cache.update_attitude(attitude)

                next_idx = (_TELEMETRY_SEQUENCE.index(next_telemetry_function) + 1) % len(_TELEMETRY_SEQUENCE)
                next_telemetry_function = _TELEMETRY_SEQUENCE[next_idx]

            time.sleep(POLL_INTERVAL)
    finally:
        ser.close()
        print("[msp-uart] Serial port closed")

# ---------------------------------------------------------------------------
# Events socket server  — push-only; clients connect and receive domain events
# ---------------------------------------------------------------------------

def events_socket_server(shutdown: threading.Event) -> None:
    """
    Accept event subscribers and add them to the broadcast list. No per-client
    threads — disconnect detection is handled lazily by _broadcast_status() when
    the next sendall() fails.
    """
    try:
        os.unlink(EVENTS_SOCKET_PATH)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(EVENTS_SOCKET_PATH), exist_ok=True)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(EVENTS_SOCKET_PATH)
    server.listen(8)
    server.settimeout(1.0)
    print(f"[msp-uart] events socket listening on {EVENTS_SOCKET_PATH}")

    while not shutdown.is_set():
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        with _event_subscribers_lock:
            _event_subscribers.append(conn)

        # Replay the subscriber-safe snapshot on every new connection.
        with _status_lock:
            replay_status = dict(_replay_status)
        try:
            _send_replay_status(conn, replay_status)
        except OSError:
            with _event_subscribers_lock:
                try:
                    _event_subscribers.remove(conn)
                except ValueError:
                    pass
            conn.close()

        print(f"[msp-uart] Event subscriber connected: {addr}")

    server.close()
    try:
        os.unlink(EVENTS_SOCKET_PATH)
    except FileNotFoundError:
        pass
    print("[msp-uart] events socket server stopped")

# ---------------------------------------------------------------------------
# Control socket server  — request/response; handles upload commands
# ---------------------------------------------------------------------------

def _control_client_handler(conn: socket.socket, addr) -> None:
    """Handle upload requests from a single control client."""
    print(f"[msp-uart] Control client connected: {addr}")
    buf = b""
    try:
        while True:
            r, _, _ = select.select([conn], [], [], 1.0)
            if not r:
                continue

            try:
                chunk = conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break

            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line_bytes)
                except json.JSONDecodeError:
                    continue

                if msg.get("type") == "upload_request":
                    req = UploadRequest()
                    _command_queue.put(req)
                    req.reply_event.wait()
                    success, error = req.result
                    response = json.dumps({
                        "type":    "upload_response",
                        "success": success,
                        "error":   error,
                    }) + "\n"
                    try:
                        conn.sendall(response.encode())
                    except OSError:
                        return
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print(f"[msp-uart] Control client disconnected: {addr}")


def control_socket_server(shutdown: threading.Event) -> None:
    _serve(CONTROL_SOCKET_PATH, _control_client_handler, shutdown,
           label="control")

# ---------------------------------------------------------------------------
# Shared socket accept loop
# ---------------------------------------------------------------------------

def _serve(path: str, handler, shutdown: threading.Event, label: str) -> None:
    """Bind a Unix domain socket at `path`, accept connections, dispatch to `handler`."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(8)
    server.settimeout(1.0)

    print(f"[msp-uart] {label} socket listening on {path}")

    while not shutdown.is_set():
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        t = threading.Thread(target=handler, args=(conn, addr), daemon=True)
        t.start()

    server.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    print(f"[msp-uart] {label} socket server stopped")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    shutdown = threading.Event()

    def _handle_sigterm(signum, frame):
        print("[msp-uart] SIGTERM received — shutting down")
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT,  _handle_sigterm)

    events_thread = threading.Thread(target=events_socket_server, args=(shutdown,), daemon=True)
    control_thread = threading.Thread(target=control_socket_server, args=(shutdown,), daemon=True)

    events_thread.start()
    control_thread.start()

    try:
        serial_loop(shutdown)
    except Exception as e:
        print(f"[msp-uart] FATAL: serial loop failed: {e}")
        shutdown.set()
        sys.exit(1)

    print("[msp-uart] Exited")


if __name__ == "__main__":
    main()
