import serial   # PySerial: handles UART/USB-Serial communication
import struct   # Packs Python objects into C-style raw bytes for the FC
import time


class MissionUploader:
    def __init__(self, port="/dev/ttyUSB0", baud=115200):
        """
        Opens a serial connection to the flight controller.
        Default port is /dev/ttyUSB0 for USB-C/USB-Serial adapters.
        Use /dev/ttyACM0 for CDC-ACM devices, or /dev/serial0 for GPIO UART.
        Run `ls /dev/tty*` before and after plugging in to find your port.
        """
        self.ser = serial.Serial(port, baud, timeout=1)

        # MSP V2 Command IDs
        self.MSP_SET_WP    = 209   # Upload a single waypoint to the FC
        self.MSP_SAVE_NVRAM = 19   # Persist mission to NVRAM (survives reboot)

        # INAV Waypoint Action Types (from MSP spec)
        self.WP_ACTION_WAYPOINT    = 0x01   # Standard fly-to waypoint
        self.WP_ACTION_POSHOLD_TIME = 0x03  # Hold position for P1 seconds
        self.WP_ACTION_RTH         = 0x04   # Return to Home (P1=1 triggers land)

        # Protocol flag marking the final waypoint in a mission
        self.LAST_WP_FLAG = 0xA5

    # -------------------------------------------------------------------------
    # CRC / Checksum Helpers
    # -------------------------------------------------------------------------

    def calculate_DVB_S2_checksum(self, data) -> int:
        """Calculates the DVB-S2 CRC used by MSP V2."""
        checksum = 0x00
        for byte in data:
            checksum ^= byte
            for _ in range(8):
                if checksum & 0x80:
                    checksum = (checksum << 1) ^ 0xD5
                else:
                    checksum <<= 1
                checksum &= 0xFF
        return checksum

    def CRC_DVB_S2_check(self, message) -> bool:
        """Verifies a received message's checksum."""
        checksum = self.calculate_DVB_S2_checksum(message[3:-1])
        if checksum == message[-1]:
            return True
        print(f"CRC check failed — message: {repr(message[-1])}, calculated: {repr(checksum)}")
        return False

    # -------------------------------------------------------------------------
    # Packet Construction
    # -------------------------------------------------------------------------

    def create_msp_request(self, function, payload=b"") -> bytes:
        """Builds a complete MSP V2 packet with header, payload, and CRC."""
        flag = 0
        size = len(payload)
        message = bytearray(9 + size)
        message[0] = ord("$")
        message[1] = ord("X")
        message[2] = ord("<")
        message[3] = flag
        message[4] = function & 0xFF          # Function ID low byte
        message[5] = (function >> 8) & 0xFF   # Function ID high byte
        message[6] = size & 0xFF              # Payload size low byte
        message[7] = (size >> 8) & 0xFF       # Payload size high byte
        if payload:
            message[8: 8 + size] = payload
        message[-1] = self.calculate_DVB_S2_checksum(message[3:-1])
        return bytes(message)

    def _create_packet(self, cmd, payload=b"") -> bytes:
        """Convenience wrapper around create_msp_request."""
        return self.create_msp_request(cmd, payload)

    # -------------------------------------------------------------------------
    # Low-Level Waypoint Senders
    # -------------------------------------------------------------------------

    def _send_waypoint(self, index, action, lat, lon, alt_cm, p1, p2, p3, flag):
        """
        Internal: packs and sends a single waypoint over serial.
        All fields explicit — callers must set flag deliberately.

        Packet format (MSP_SET_WP):
          B  - WP index
          B  - Action type
          i  - Latitude  (degrees * 10,000,000, signed)
          i  - Longitude (degrees * 10,000,000, signed)
          i  - Altitude  (centimeters, signed)
          h  - P1
          h  - P2
          h  - P3
          B  - Flag (0x00 normal, 0xA5 = last waypoint)
        """
        lat_int = int(lat * 10_000_000)
        lon_int = int(lon * 10_000_000)

        payload = struct.pack(
            "<BBiiihhhB",
            index, action, lat_int, lon_int, alt_cm, p1, p2, p3, flag
        )
        packet = self._create_packet(self.MSP_SET_WP, payload)
        self.ser.write(packet)

        # Brief pause to avoid overrunning the FC's UART buffer, then clear it
        time.sleep(0.1)
        if self.ser.in_waiting:
            self.ser.read(self.ser.in_waiting)

    def upload_waypoint(self, index, action, lat, lon, alt_cm, p1=0, p2=0, p3=1):
        """
        Public: uploads a standard (non-terminal) waypoint.
        Flag is always 0x00 — mission termination is handled by upload_mission().
        """
        self._send_waypoint(index, action, lat, lon, alt_cm, p1, p2, p3, flag=0x00)
        print(f"  WP {index:>2} | action={action} | {lat:.7f}, {lon:.7f} | alt={alt_cm}cm")

    def _upload_terminal_rth(self, index, lat=0, lon=0, alt_cm=0):
        """
        Internal: uploads the final RTH+Land waypoint with LAST_WP_FLAG set.
        lat/lon/alt are ignored by INAV for RTH but included for protocol compliance.
        P1=1 instructs INAV to land after returning home.
        """
        self._send_waypoint(
            index,
            self.WP_ACTION_RTH,
            lat, lon, alt_cm,
            p1=1, p2=0, p3=0,
            flag=self.LAST_WP_FLAG
        )
        print(f"  WP {index:>2} | action=RTH+LAND | flag=LAST_WP (0xA5)")

    # -------------------------------------------------------------------------
    # Mission Upload
    # -------------------------------------------------------------------------

    def upload_mission(self, mission):
        """
        Uploads a complete mission from a list of (lat, lon, alt_m) tuples.

        Mission structure:
          WP 0       — Home position (WAYPOINT action, alt=0, no hold)
          WP 1 to N  — Mission waypoints (POSHOLD_TIME, holds for P1 seconds)
          WP N+1     — RTH + Land (auto-appended, LAST_WP_FLAG set here)

        P1/P2/P3 for POSHOLD waypoints: p1=2 (hold 2s), p2=51, p3=0
        The LAST_WP_FLAG is never set on a real waypoint — only on the RTH cap.
        This ensures INAV always executes every mission waypoint before returning.
        """
        if not mission:
            raise ValueError("Mission must contain at least one waypoint.")

        print(f"\nUploading mission: {len(mission)} waypoint(s) + home + RTH")
        print("-" * 55)

        # WP 0: Home — standard waypoint at ground level, no hold
        home = mission[0]
        self.upload_waypoint(
            0,
            self.WP_ACTION_WAYPOINT,
            home[0], home[1],
            alt_cm=0,
            p1=0, p2=0, p3=1
        )

        # WP 1 to N: Mission waypoints as POSHOLD_TIME
        for i, point in enumerate(mission):
            wp_index = i + 1
            alt_cm = point[2] * 100  # Convert meters to centimeters
            self.upload_waypoint(
                wp_index,
                self.WP_ACTION_POSHOLD_TIME,
                point[0], point[1],
                alt_cm,
                p1=2, p2=51, p3=0
            )

        # WP N+1: RTH + Land — always the terminal waypoint
        rth_index = len(mission) + 1
        self._upload_terminal_rth(rth_index)

        print("-" * 55)
        print(f"Mission structure complete. Total packets sent: {rth_index + 1}")

    # -------------------------------------------------------------------------
    # Save & Close
    # -------------------------------------------------------------------------

    def save_mission(self):
        """Persists the uploaded mission to NVRAM. Must be called after upload."""
        packet = self._create_packet(self.MSP_SAVE_NVRAM, b"")
        self.ser.write(packet)
        time.sleep(0.5)  # Give FC time to write to NVRAM
        print("Mission saved to NVRAM.")

    def close(self):
        """Closes the serial connection."""
        self.ser.close()
        print("Serial connection closed.")


# =============================================================================
# EXAMPLE USAGE
# =============================================================================
# Waypoint format: (lat, lon, alt_meters)
# To find your USB port: run `ls /dev/tty*` before and after plugging in.
# Common ports:
#   /dev/ttyUSB0  — CH340, FTDI, CP2102 USB-Serial adapters
#   /dev/ttyACM0  — CDC-ACM devices (some FCs enumerate this way)
#   /dev/serial0  — Raspberry Pi GPIO UART (if using direct UART instead of USB)
#
# If you get a permissions error: sudo usermod -aG dialout $USER (then re-login)
# =============================================================================

if __name__ == "__main__":

    new_mission = [
        (42.3456347, -71.1132435, 12),
        (42.3459037, -71.1128325, 12),
        (42.3458440, -71.1123549, 12),
    ]

    uploader = MissionUploader(port="/dev/ttyUSB0", baud=115200)

    print("Starting upload...")
    try:
        uploader.upload_mission(new_mission)
        uploader.save_mission()
        print("\nUpload complete.")
    except Exception as e:
        print(f"\nError during upload: {e}")
    finally:
        uploader.close()