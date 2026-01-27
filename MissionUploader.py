import serial # Imports PySerial library, allows it to talk to UART HW
import struct # FC is in C and expects data in byte sizes. Python uses objects. struct smashes pthon objects into C style raw bytes
import time

class MissionUploader:
    def __init__(self, port='/dev/serial0', baud=115200): # Need to change the port we are opening
        self.ser = serial.Serial(port, baud, timeout=1) # Opens connection to FC
        
        # MSP Command IDs
        
        # When FC sees 214, it knows the next bytes sent are a waypoint
        # When FC sees 250 it knows to save everything learned to EEPROM
        self.MSP_SET_WP = 214
        self.MSP_EEPROM_WRITE = 250
        
        # INAV Constants
        self.WAYPOINT_ACTION = 0x01  # "Fly here" type of waypoint
        self.RTH_ACTION = 0x04       # Return to Home
        self.LAST_WP_FLAG = 0xA5     # Flag to indicate end of mission, last waypoint
    
    def _create_packet(self, cmd, payload):
        # Wraps the payload in the MSP $M< header and checksum
        header = b'$M<' # Every MSP message must start with these 3 bytes. MultiWii incoming message
        size = len(payload)
        checksum = size ^ cmd
        for byte in payload:
            checksum ^= byte # When FC gets data it does same math. If matches ours, no data corruption happened 
        
        # Header + Size + Command + Data + Checksum
        return header + struct.pack('<BB', size, cmd) + payload + struct.pack('<B', checksum)

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
        p1 = 0 # Hold time (s) or speed. If 0, drone flies at default nav_auto_speed
        p2 = 0 # Often unused, basically just leave at 0
        p3 = 0 # IMPORTANT In newest INAV version can be used as a "Bitfield" to trigger specific logic conditions
               # Leave at zero for now we will look into it later.
        
        # 3. Pack Structure
        # Format: < B (Index), B (Action), i (Lat), i (Lon), i (Alt), h (P1), h (P2), h (P3), B (Flag)
        # IMPORTANT: Gemini suggested fix of making it iii instead of iII so lat long and alt are signed integers
        payload = struct.pack('<BBiiihhhB', 
                              index, 
                              self.WAYPOINT_ACTION, 
                              lat_int, 
                              lon_int, 
                              alt_cm, 
                              p1, p2, p3, 
                              flag)
        
        # Create packet then send it over to the FC
        packet = self._create_packet(self.MSP_SET_WP, payload)
        self.ser.write(packet)
        
        # 4. Wait for ACK (Important!)
        # INAV sends a response. If we spam too fast, we might overrun the buffer.
        time.sleep(0.1) 
        # If there are bytes waiting, pull data out of buffer (clear buffer)
        if self.ser.in_waiting:
            self.ser.read(self.ser.in_waiting) # Clear buffer
            
        print(f"Uploaded WP {index}: {lat}, {lon}")

    def save_mission(self):
        #Saves the uploaded mission to EEPROM
        packet = self._create_packet(self.MSP_EEPROM_WRITE, b'')
        self.ser.write(packet)
        time.sleep(0.5) # Give it time to write
        print("Mission Saved to EEPROM")

    def close(self):
        self.ser.close()







# --- EXAMPLE USAGE ---
if __name__ == "__main__":
    uploader = MissionUploader()
    
    # This list would come from Web App JSON
    # Format: (Lat, Lon, Altitude_Meters)
    new_mission = [
        (42.3601, -71.0589, 20),
        (42.3605, -71.0595, 20),
        (42.3610, -71.0580, 25), # Flying higher at the end
    ]

    print("Starting Upload...")
    
    try:
        # Start at Index 1 (Index 0 is Home/Origin)
        for i, point in enumerate(new_mission):
            wp_index = i + 1
            is_last_point = (i == len(new_mission) - 1)
            
            # Convert Altitude from Meters to CM for the function
            alt_cm = point[2] * 100 
            
            uploader.upload_waypoint(wp_index, point[0], point[1], alt_cm, is_last_point)

        # CRITICAL: Save to EEPROM or it will vanish on reboot
        uploader.save_mission()
        print("Upload Complete.")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        uploader.close()