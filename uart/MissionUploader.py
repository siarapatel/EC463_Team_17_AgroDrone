import serial  # Imports PySerial library, allows it to talk to UART HW
import struct  # FC is in C and expects data in byte sizes. Python uses objects. struct smashes pthon objects into C style raw bytes
import time


class MissionUploader:
    def __init__(
        self, port="/dev/serial0", baud=115200
    ):  # Need to change the port we are opening
        self.ser = serial.Serial(port, baud, timeout=1)  # Opens connection to FC

        # MSP Command IDs

        # When FC sees 209, it knows the next bytes sent are a waypoint
        # When FC sees 250 it knows to save everything learned to EEPROM
        self.MSP_SET_WP = 209
        self.MSP_EEPROM_WRITE = 250

        # INAV Constants
        self.WAYPOINT_ACTION = 0x01  # "Fly here" type of waypoint
        self.RTH_ACTION = 0x04  # Return to Home
        self.LAST_WP_FLAG = 0xA5  # Flag to indicate end of mission, last waypoint

    # Function to check if received checksum matches
    def CRC_DVB_S2_check(self, message) -> bool:
        checksum = self.calculate_DVB_S2_checksum(message[3:-1])
        if checksum == message[-1]:
            return True
        else:
            print("CRC check failed")
            print(
                "Message CRC: "
                + repr(message[-1])
                + ", calculated CRC: "
                + repr(checksum)
            )
            return False

    # Function to calculate the DVB-S2 CRC for MSP V2
    # I have actually no clue how the algorithm works, but it apparently does.
    def calculate_DVB_S2_checksum(self, data) -> int:
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

    def create_msp_request(self, function, payload=b""):
        # Fixed to handle payloads and return bytes
        flag = 0
        size = len(payload)
        message = bytearray(9 + size)
        message[0] = ord("$")
        message[1] = ord("X")
        message[2] = ord("<")
        message[3] = flag
        message[4] = function & 0xFF  # Low byte
        message[5] = (function >> 8) & 0xFF  # High byte
        message[6] = size & 0xFF  # Low byte
        message[7] = (size >> 8) & 0xFF  # High byte
        if payload:
            message[8 : 8 + size] = payload
        message[-1] = self.calculate_DVB_S2_checksum(message[3:-1])
        return bytes(message)

    def _create_packet(self, cmd, payload=b""):
        # Wrapper using the same V2 format
        return self.create_msp_request(cmd, payload)

    def upload_waypoint(self, index, lat, lon, alt_cm, is_last=False):
        """
        Uploads a single waypoint.
        index: 1-based index (0 is usually HOME)
        lat/lon: Floating point degrees (e.g., 42.3601)
        alt_cm: Altitude in CENTIMETERS (e.g., 2000 = 20m)
        """

        # 1. Convert to Integers (INAV expects degrees * 10,000,000)
        lat_int = int(lat * 10_000_000)
        lon_int = int(lon * 10_000_000)

        # 2. Set Flags
        flag = self.LAST_WP_FLAG if is_last else 0x00
        p1 = 0  # Hold time (s) or speed. If 0, drone flies at default nav_auto_speed
        p2 = 0  # Often unused, basically just leave at 0
        p3 = 0  # IMPORTANT In newest INAV version can be used as a "Bitfield" to trigger specific logic conditions
        # Leave at zero for now we will look into it later.

        # 3. Pack Structure
        # Format: < B (Index), B (Action), i (Lat), i (Lon), i (Alt), h (P1), h (P2), h (P3), B (Flag)
        # IMPORTANT: Gemini suggested fix of making it iii instead of iII so lat long and alt are signed integers
        payload = struct.pack(
            "<BBiiihhhB",
            index,
            self.WAYPOINT_ACTION,
            lat_int,
            lon_int,
            alt_cm,
            p1,
            p2,
            p3,
            flag,
        )

        # Create packet then send it over to the FC
        packet = self._create_packet(self.MSP_SET_WP, payload)
        self.ser.write(packet)

        # 4. Wait for ACK (Important!)
        # INAV sends a response. If we spam too fast, we might overrun the buffer.
        time.sleep(0.1)
        # If there are bytes waiting, pull data out of buffer (clear buffer)
        if self.ser.in_waiting:
            self.ser.read(self.ser.in_waiting)  # Clear buffer

        print(f"Uploaded WP {index}: {lat}, {lon}")

    def save_mission(self):
        # Saves the uploaded mission to EEPROM
        packet = self._create_packet(self.MSP_EEPROM_WRITE, b"")
        self.ser.write(packet)
        time.sleep(0.5)  # Give it time to write
        print("Mission Saved to EEPROM")

    def close(self):
        self.ser.close()


# --- EXAMPLE USAGE ---
if __name__ == "__main__":
    uploader = MissionUploader()

    # This list would come from Web App JSON
    # spec: https://github.com/iNavFlight/inav/blob/master/src/main/msp/msp_protocol.h
    # The spec says: (WP#,lat, lon, alt, flags)
    new_mission = [
        (43.3459037, -90, 20),
        (42.3459055, -91, 25),
        (41.3459055, -92, 25),
    ]

    print("Starting Upload...")

    # set home first or it tweaks
    uploader.upload_waypoint(0, 42.361145, -71.057083, 0, False)

    try:
        # Start at Index 1 (Index 0 is Home/Origin)
        for i, point in enumerate(new_mission):
            waypoint_num = i + 1
            is_last_point = i == len(new_mission) - 1

            # Convert Altitude from Meters to CM for the function
            alt_cm = point[2] * 100

            uploader.upload_waypoint(
                waypoint_num, point[0], point[1], alt_cm, is_last_point
            )

        # CRITICAL: Save to EEPROM or it will vanish on reboot
        uploader.save_mission()
        print("Upload Complete.")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        uploader.close()
