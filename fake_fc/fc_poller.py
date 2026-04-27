#!/usr/bin/env python3
"""
Diagnostic poller for validating MSP telemetry against the real FC.

Default mode preserves the existing NAV-focused workflow:
  python3 fc_poller.py
  python3 fc_poller.py --port /dev/cu.usbserial-1410
  python3 fc_poller.py --interval 0.5
  python3 fc_poller.py --raw

Position verification modes add MSP_RAW_GPS and MSP_ALTITUDE checks:
  python3 fc_poller.py --position
  python3 fc_poller.py --position --raw --count 10
  python3 fc_poller.py --position-only --raw
"""

import argparse
import struct
import sys
import time


MSP_STATUS = 101
MSP_RAW_GPS = 106
MSP_ALTITUDE = 109
MSP_NAV_STATUS = 121


# ---------------------------------------------------------------------------
# MSP V2 framing
# ---------------------------------------------------------------------------

def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc


def build_request(function: int, payload: bytes = b"") -> bytes:
    size = len(payload)
    msg = bytearray(9 + size)
    msg[0] = ord("$")
    msg[1] = ord("X")
    msg[2] = ord("<")
    msg[3] = 0
    msg[4] = function & 0xFF
    msg[5] = (function >> 8) & 0xFF
    msg[6] = size & 0xFF
    msg[7] = (size >> 8) & 0xFF
    if payload:
        msg[8:8 + size] = payload
    msg[-1] = crc8_dvb_s2(msg[3:-1])
    return bytes(msg)


def read_exact(ser, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = ser.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def read_response_detail(ser) -> dict:
    header = read_exact(ser, 3)
    if len(header) < 3:
        return {"ok": False, "error": "timeout"}
    if header != b"$X>":
        return {"ok": False, "error": "bad_preamble", "header": header}

    rest = read_exact(ser, 5)
    if len(rest) < 5:
        return {"ok": False, "error": "short_meta", "received": len(rest)}

    flag = rest[0]
    function = rest[1] | (rest[2] << 8)
    size = rest[3] | (rest[4] << 8)

    body = read_exact(ser, size + 1)
    if len(body) < size + 1:
        return {
            "ok": False,
            "error": "short_body",
            "function": function,
            "expected": size + 1,
            "received": len(body),
        }

    payload = body[:size]
    received_crc = body[size]
    crc_data = bytes([flag, rest[1], rest[2], rest[3], rest[4]]) + payload
    expected_crc = crc8_dvb_s2(crc_data)
    if expected_crc != received_crc:
        return {
            "ok": False,
            "error": "crc",
            "function": function,
            "received_crc": received_crc,
            "expected_crc": expected_crc,
        }

    return {"ok": True, "function": function, "payload": payload}


# ---------------------------------------------------------------------------
# Payload decoders
# ---------------------------------------------------------------------------

def _print_raw(payload: bytes, raw: bool) -> None:
    if raw:
        print(f"  raw ({len(payload)}B): {payload.hex(' ')}")


def decode_status(payload: bytes, raw: bool):
    _print_raw(payload, raw)
    if len(payload) < 11:
        print(f"  [!] Status payload short: {len(payload)} bytes (expected >= 11)")
        return None

    cycle_time, i2c_errors, sensors, flight_mode, profile = struct.unpack(
        "<HHIHB",
        payload[:11],
    )
    print(f"  cycle_time  = {cycle_time} µs")
    print(f"  i2c_errors  = {i2c_errors}")
    print(f"  sensors     = 0x{sensors:08X}")
    print(f"  flight_mode = 0x{flight_mode:08X}")
    print(f"  profile     = {profile}")
    return {
        "cycle_time": cycle_time,
        "i2c_errors": i2c_errors,
        "sensors": sensors,
        "flight_mode": flight_mode,
        "profile": profile,
    }


def decode_nav_status(payload: bytes, raw: bool):
    _print_raw(payload, raw)
    if len(payload) < 7:
        print(f"  [!] Payload too short: {len(payload)} bytes (expected 7)")
        return None

    nav_mode, nav_state, wp_action, wp_num, nav_error, heading_hold = struct.unpack(
        "<BBBBBh",
        payload[:7],
    )
    complete = nav_mode == 2 and nav_state == 10

    print(
        f"  nav_mode    = {nav_mode}"
        + (" (RTH/land)" if nav_mode == 2 else " (normal)" if nav_mode == 0 else "")
    )
    print(
        f"  nav_state   = {nav_state}"
        + (
            " (landed/idle)"
            if nav_state == 10
            else " (in-flight)"
            if nav_state != 0
            else " (idle)"
        )
    )
    print(f"  active_wp   = {wp_num}")
    print(f"  wp_action   = 0x{wp_action:02X}" + (" (Fly here)" if wp_action == 0x01 else ""))
    print(f"  nav_error   = {nav_error}")
    print(f"  heading_tgt = {heading_hold}")
    if complete:
        print("  ** MISSION COMPLETE **")
    return {
        "nav_mode": nav_mode,
        "nav_state": nav_state,
        "wp_action": wp_action,
        "wp_num": wp_num,
        "nav_error": nav_error,
        "heading_hold": heading_hold,
        "mission_complete": complete,
    }


def decode_raw_gps(payload: bytes, raw: bool):
    _print_raw(payload, raw)
    if len(payload) < 16:
        print(f"  [!] RAW_GPS payload short: {len(payload)} bytes (expected >= 16)")
        return None

    fix, num_sat, lat_raw, lon_raw, alt_msl_m, speed_cm_s, ground_course_tenths = (
        struct.unpack("<BBiihHH", payload[:16])
    )
    lat = lat_raw / 10_000_000.0
    lon = lon_raw / 10_000_000.0
    speed_m_s = speed_cm_s / 100.0
    ground_course_deg = ground_course_tenths / 10.0

    print(f"  fix         = {fix}")
    print(f"  num_sat     = {num_sat}")
    print(f"  lat         = {lat:.7f}")
    print(f"  lon         = {lon:.7f}")
    print(f"  alt_msl_m   = {alt_msl_m:.1f}")
    print(f"  speed_m_s   = {speed_m_s:.2f}")
    print(f"  course_deg  = {ground_course_deg:.1f}")
    return {
        "fix": fix,
        "num_sat": num_sat,
        "lat": lat,
        "lon": lon,
        "alt_msl_m": float(alt_msl_m),
        "speed_m_s": speed_m_s,
        "ground_course_deg": ground_course_deg,
    }


def decode_altitude(payload: bytes, raw: bool):
    _print_raw(payload, raw)
    if len(payload) < 6:
        print(f"  [!] ALTITUDE payload short: {len(payload)} bytes (expected >= 6)")
        return None

    alt_cm, variometer_cm_s = struct.unpack("<ih", payload[:6])
    alt_rel_m = alt_cm / 100.0
    variometer_m_s = variometer_cm_s / 100.0

    print(f"  alt_rel_m   = {alt_rel_m:.2f}")
    print(f"  vario_m_s   = {variometer_m_s:.2f}")
    return {
        "alt_rel_m": alt_rel_m,
        "variometer_m_s": variometer_m_s,
    }


# ---------------------------------------------------------------------------
# Poll / decode helpers
# ---------------------------------------------------------------------------

def log_response_error(result: dict, expected_function: int) -> None:
    error = result["error"]
    if error == "timeout":
        print(f"  [!] No response for function {expected_function} (timeout)")
    elif error == "bad_preamble":
        print(f"  [!] Bad preamble for function {expected_function}: {result['header']!r}")
    elif error == "short_meta":
        print(
            f"  [!] Short response header for function {expected_function}: "
            f"{result['received']} byte(s)"
        )
    elif error == "short_body":
        print(
            f"  [!] Short payload for function {expected_function}: "
            f"{result['received']}/{result['expected']} byte(s)"
        )
    elif error == "crc":
        print(
            f"  [!] CRC mismatch for function {expected_function}: "
            f"got 0x{result['received_crc']:02X}, expected 0x{result['expected_crc']:02X}"
        )
    else:
        print(f"  [!] Unknown response error for function {expected_function}: {result}")


def request_and_decode(ser, function: int, label: str, decoder, raw: bool):
    print(label)
    ser.write(build_request(function))
    result = read_response_detail(ser)
    if not result["ok"]:
        log_response_error(result, function)
        return None

    if result["function"] != function:
        print(
            f"  [!] Unexpected function code: got {result['function']}, "
            f"expected {function}"
        )
        _print_raw(result["payload"], raw)
        return None

    return decoder(result["payload"], raw)


def print_position_summary(gps_data, altitude_data) -> None:
    lat = None
    lon = None
    if gps_data is not None and gps_data["fix"] > 0:
        lat = gps_data["lat"]
        lon = gps_data["lon"]

    alt_rel_m = altitude_data["alt_rel_m"] if altitude_data is not None else None
    print("  latest_position")
    print(f"    lat       = {lat if lat is not None else 'None'}")
    print(f"    lon       = {lon if lon is not None else 'None'}")
    print(f"    alt_rel_m = {alt_rel_m if alt_rel_m is not None else 'None'}")


# ---------------------------------------------------------------------------
# Port detection
# ---------------------------------------------------------------------------

def find_port() -> str | None:
    from serial.tools import list_ports

    candidates = [
        p for p in list_ports.comports()
        if any(
            p.device.startswith(prefix)
            for prefix in (
                "/dev/cu.usbserial",
                "/dev/cu.usbmodem",
                "/dev/ttyUSB",
                "/dev/ttyACM",
            )
        )
    ]
    if not candidates:
        print("[poller] No USB serial ports found. Connect adapter or use --port.")
        return None
    if len(candidates) == 1:
        print(f"[poller] Auto-detected: {candidates[0].device}  ({candidates[0].description})")
        return candidates[0].device
    print("[poller] Multiple ports found — use --port to specify one:")
    for candidate in candidates:
        print(f"    {candidate.device}  ({candidate.description})")
    return None


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def run_startup_probes(ser, raw: bool, include_position: bool) -> None:
    print("── Startup Probes ───────────────────────────────────")
    request_and_decode(ser, MSP_STATUS, "MSP_STATUS", decode_status, raw)
    if include_position:
        request_and_decode(ser, MSP_NAV_STATUS, "MSP_NAV_STATUS", decode_nav_status, raw)
        request_and_decode(ser, MSP_RAW_GPS, "MSP_RAW_GPS", decode_raw_gps, raw)
        request_and_decode(ser, MSP_ALTITUDE, "MSP_ALTITUDE", decode_altitude, raw)
    print()


def run_nav_loop(ser, interval: float, raw: bool, count: int) -> None:
    print("── MSP_NAV_STATUS polling ────────────────────────────")
    poll_num = 0
    try:
        while True:
            poll_num += 1
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] poll #{poll_num}")
            request_and_decode(ser, MSP_NAV_STATUS, "MSP_NAV_STATUS", decode_nav_status, raw)

            if count and poll_num >= count:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n[poller] Done. {poll_num} poll(s).")


def run_position_loop(ser, interval: float, raw: bool, count: int, position_only: bool) -> None:
    header = "── Position polling (NAV-first, one telemetry request per loop) ──"
    if position_only:
        header = "── Position-only polling ─────────────────────────────"
    print(header)

    cycle_num = 0
    next_telemetry_function = MSP_ALTITUDE
    latest_gps = None
    latest_altitude = None

    try:
        while True:
            cycle_num += 1
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] cycle #{cycle_num}")

            if not position_only:
                request_and_decode(
                    ser,
                    MSP_NAV_STATUS,
                    "MSP_NAV_STATUS",
                    decode_nav_status,
                    raw,
                )

            if next_telemetry_function == MSP_ALTITUDE:
                latest = request_and_decode(
                    ser,
                    MSP_ALTITUDE,
                    "MSP_ALTITUDE",
                    decode_altitude,
                    raw,
                )
                if latest is not None:
                    latest_altitude = latest
                next_telemetry_function = MSP_RAW_GPS
            else:
                latest = request_and_decode(
                    ser,
                    MSP_RAW_GPS,
                    "MSP_RAW_GPS",
                    decode_raw_gps,
                    raw,
                )
                if latest is not None:
                    latest_gps = latest
                next_telemetry_function = MSP_ALTITUDE

            print_position_summary(latest_gps, latest_altitude)

            if count and cycle_num >= count:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n[poller] Done. {cycle_num} cycle(s).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Poll real FC over UART and decode MSP responses")
    parser.add_argument("--port", default=None, help="Serial port (auto-detected if omitted)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="Poll interval in seconds (default 0.2 = 5 Hz)",
    )
    parser.add_argument("--raw", action="store_true", help="Also print raw hex payload bytes")
    parser.add_argument("--count", type=int, default=0, help="Stop after N loop iterations (0 = run forever)")
    parser.add_argument(
        "--position",
        action="store_true",
        help="Probe MSP_RAW_GPS and MSP_ALTITUDE and mirror the runtime polling strategy",
    )
    parser.add_argument(
        "--position-only",
        action="store_true",
        help="Skip continuous NAV polling and focus on GPS + altitude verification",
    )
    args = parser.parse_args()

    if args.position_only:
        args.position = True

    port = args.port or find_port()
    if port is None:
        sys.exit(1)

    import serial

    ser = serial.Serial(port, args.baud, timeout=1)
    print(f"[poller] Opened {port} @ {args.baud}bps  (Ctrl-C to stop)\n")

    try:
        run_startup_probes(ser, args.raw, include_position=args.position)
        if args.position:
            run_position_loop(
                ser,
                interval=args.interval,
                raw=args.raw,
                count=args.count,
                position_only=args.position_only,
            )
        else:
            run_nav_loop(
                ser,
                interval=args.interval,
                raw=args.raw,
                count=args.count,
            )
    finally:
        ser.close()


if __name__ == "__main__":
    main()
