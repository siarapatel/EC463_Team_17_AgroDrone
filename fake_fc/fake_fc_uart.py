#!/usr/bin/env python3
"""
Fake FC UART responder for bench-testing AgroDrone's MSP UART service.

Run this on a second Raspberry Pi wired to the main Pi over 3.3V UART:

  Main Pi TX -> Fake Pi RX
  Main Pi RX -> Fake Pi TX
  GND        -> GND

The main Pi's `msp_uart_service.py` will poll MSP_NAV_STATUS. This script
responds with a scripted sequence of NAV payloads so you can validate the Pi
side without the real flight controller.

It also accepts MSP_SET_WP and MSP_SAVE_NVRAM writes so mission upload tests
do not crash immediately, though upload ACK behavior is intentionally minimal.

Examples:

  python3 fake_fc_uart.py --port /dev/serial0 --scenario nominal
  python3 fake_fc_uart.py --port /dev/ttyAMA0 --scenario skip
  python3 fake_fc_uart.py --port /dev/serial0 --scenario custom --waypoints 1,2,3,4
"""

import argparse
import struct
import time


MSP_RAW_GPS = 106
MSP_ALTITUDE = 109
MSP_NAV_STATUS = 121
MSP_SET_WP = 209
MSP_SAVE_NVRAM = 19

WAYPOINT_ACTION = 0x01
BASE_LAT = 42.3890975
BASE_LON = -71.1384045
BASE_ALT_MSL_M = 34.0
BASE_ALT_REL_M = 12.3


def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc


def build_msp_v2_response(function: int, payload: bytes = b"") -> bytes:
    flag = 0
    size = len(payload)
    msg = bytearray(9 + size)
    msg[0] = ord("$")
    msg[1] = ord("X")
    msg[2] = ord(">")
    msg[3] = flag
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


def read_msp_v2_request(ser):
    header = read_exact(ser, 3)
    if len(header) < 3 or header != b"$X<":
        return None

    rest = read_exact(ser, 5)
    if len(rest) < 5:
        return None

    flag = rest[0]
    function = rest[1] | (rest[2] << 8)
    payload_size = rest[3] | (rest[4] << 8)

    body = read_exact(ser, payload_size + 1)
    if len(body) < payload_size + 1:
        return None

    payload = body[:payload_size]
    received_crc = body[payload_size]
    crc_data = bytes([flag, rest[1], rest[2], rest[3], rest[4]]) + payload
    if crc8_dvb_s2(crc_data) != received_crc:
        return None

    return function, payload


def nav_payload(nav_mode: int, nav_state: int, wp_num: int, wp_action: int) -> bytes:
    # INAV format: uint8 mode, uint8 state, uint8 action, uint8 wp_num,
    #              uint8 error, int16 heading  (7 bytes total)
    return struct.pack(
        "<BBBBBh",
        nav_mode,
        nav_state,
        wp_action,
        wp_num,
        0,   # nav_error
        0,   # nav_headingHoldTarget
    )


def raw_gps_payload(
    lat_deg: float,
    lon_deg: float,
    alt_msl_m: float,
    fix: int = 1,
    num_sat: int = 10,
    speed_cm_s: int = 350,
    ground_course_deg: float = 91.0,
) -> bytes:
    return struct.pack(
        "<BBiihHH",
        fix,
        num_sat,
        int(round(lat_deg * 10_000_000)),
        int(round(lon_deg * 10_000_000)),
        int(round(alt_msl_m)),
        speed_cm_s,
        int(round(ground_course_deg * 10.0)),
    )


def altitude_payload(alt_rel_m: float, variometer_cm_s: int = 0) -> bytes:
    return struct.pack("<ih", int(round(alt_rel_m * 100.0)), variometer_cm_s)


def build_sequence(name: str, custom_waypoints: list[int]) -> list[bytes]:
    if name == "nominal":
        return [
            nav_payload(0, 0, 0, 0),
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 2, WAYPOINT_ACTION),
            nav_payload(0, 0, 3, WAYPOINT_ACTION),
            nav_payload(2, 10, 3, WAYPOINT_ACTION),
        ]

    if name == "duplicate":
        return [
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 2, WAYPOINT_ACTION),
            nav_payload(2, 10, 2, WAYPOINT_ACTION),
        ]

    if name == "skip":
        return [
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 3, WAYPOINT_ACTION),
            nav_payload(2, 10, 3, WAYPOINT_ACTION),
        ]

    if name == "backward":
        return [
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 2, WAYPOINT_ACTION),
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(2, 10, 1, WAYPOINT_ACTION),
        ]

    if name == "custom":
        sequence = [nav_payload(0, 0, wp, WAYPOINT_ACTION) for wp in custom_waypoints]
        if custom_waypoints:
            sequence.append(nav_payload(2, 10, custom_waypoints[-1], WAYPOINT_ACTION))
        return sequence

    raise ValueError(f"Unknown scenario: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake MSP flight controller over UART")
    parser.add_argument("--port", default="/dev/serial0", help="UART port to open")
    parser.add_argument("--baud", type=int, default=115200, help="UART baud rate")
    parser.add_argument(
        "--scenario",
        default="nominal",
        choices=["nominal", "duplicate", "skip", "backward", "custom"],
        help="Predefined nav-status scenario to replay",
    )
    parser.add_argument(
        "--waypoints",
        default="1,2,3",
        help="Comma-separated waypoint list for --scenario custom",
    )
    parser.add_argument(
        "--hold-polls",
        type=int,
        default=5,
        help="How many MSP_NAV_STATUS requests each state is repeated for",
    )
    args = parser.parse_args()

    custom_waypoints = [int(x) for x in args.waypoints.split(",") if x.strip()]
    sequence = build_sequence(args.scenario, custom_waypoints)
    sequence_index = 0
    polls_at_current_state = 0

    import serial

    ser = serial.Serial(args.port, args.baud, timeout=1)
    print(
        f"[fake-fc] Listening on {args.port} @ {args.baud} | "
        f"scenario={args.scenario} | hold_polls={args.hold_polls}"
    )

    try:
        while True:
            request = read_msp_v2_request(ser)
            if request is None:
                continue

            function, payload = request

            if function == MSP_NAV_STATUS:
                current_payload = sequence[sequence_index]
                ser.write(build_msp_v2_response(MSP_NAV_STATUS, current_payload))

                nav_mode, nav_state, wp_action, wp_num, _, _ = struct.unpack(
                    "<BBBBBh", current_payload
                )
                print(
                    f"[fake-fc] NAV reply | mode={nav_mode} state={nav_state} "
                    f"wp={wp_num} action={wp_action}"
                )

                polls_at_current_state += 1
                if polls_at_current_state >= args.hold_polls and sequence_index < len(sequence) - 1:
                    sequence_index += 1
                    polls_at_current_state = 0

            elif function == MSP_RAW_GPS:
                position_step = sequence_index + (polls_at_current_state / max(args.hold_polls, 1))
                gps_payload = raw_gps_payload(
                    lat_deg=BASE_LAT + (position_step * 0.00001),
                    lon_deg=BASE_LON - (position_step * 0.00001),
                    alt_msl_m=BASE_ALT_MSL_M + (position_step * 0.4),
                    speed_cm_s=350 + int(position_step * 10),
                    ground_course_deg=91.0 + position_step,
                )
                ser.write(build_msp_v2_response(MSP_RAW_GPS, gps_payload))
                fix, num_sat, lat_raw, lon_raw, alt_msl_m, _, _ = struct.unpack(
                    "<BBiihHH",
                    gps_payload,
                )
                print(
                    f"[fake-fc] GPS reply | fix={fix} sats={num_sat} "
                    f"lat={lat_raw / 1e7:.7f} lon={lon_raw / 1e7:.7f} "
                    f"alt_msl={alt_msl_m}m"
                )

            elif function == MSP_ALTITUDE:
                position_step = sequence_index + (polls_at_current_state / max(args.hold_polls, 1))
                alt_payload = altitude_payload(
                    alt_rel_m=BASE_ALT_REL_M + (position_step * 0.5),
                    variometer_cm_s=15,
                )
                ser.write(build_msp_v2_response(MSP_ALTITUDE, alt_payload))
                alt_cm, variometer_cm_s = struct.unpack("<ih", alt_payload)
                print(
                    f"[fake-fc] ALT reply | alt_rel={alt_cm / 100.0:.2f}m "
                    f"vario={variometer_cm_s / 100.0:.2f}m/s"
                )

            elif function == MSP_SET_WP:
                print(f"[fake-fc] Received MSP_SET_WP payload ({len(payload)} bytes)")
                ser.write(build_msp_v2_response(MSP_SET_WP, b""))

            elif function == MSP_SAVE_NVRAM:
                print("[fake-fc] Received MSP_SAVE_NVRAM")
                ser.write(build_msp_v2_response(MSP_SAVE_NVRAM, b""))

            else:
                print(f"[fake-fc] Ignoring unsupported function {function}")

            time.sleep(0.01)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
