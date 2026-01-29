import json
import time
from pymavlink import mavutil

# On your computer, make sure to do pip3 install pymavlink pyserial
# Replace this with your actual port name found typing in terminal 'ls /dev/tty.*'
device_name = '/dev/tty.usbserial-D30JAZ54'
baud_rate = 57600
output_file = 'drone_data.json'

def main():
    print(f"Bridge Active on {device_name} | Writing to {output_file}")
    
    try:
        connection = mavutil.mavlink_connection(device_name, baud=baud_rate) # Opens USB port
        connection.wait_heartbeat() # Pauses script execution until we hear first valid signal from drone
        print("Heartbeat detected! Streaming data...")
    except Exception as e:
        print(f"Error connecting: {e}")
        return

    # Initialize with default values so the file exists immediately
    data = {
        # SYS_STATUS
        "voltage_battery": 0.0,    # Volts
        "current_battery": 0.0,    # Amps
        "battery_remaining": 0,    # Percent
        
        # GPS_RAW_INT
        "satellites_visible": 0,
        "gps_hdop": 99.9,          # Meters (Want like testing <1.5)
        
        # GLOBAL_POSITION_INT
        "lat": 0.0,                # Degrees
        "lon": 0.0,                # Degrees
        "alt_msl": 0.0,            # Meters (Above Sea Level)
        "alt_rel": 0.0,            # Meters (Above Home/Takeoff)
        "heading": 0.0,            # Degrees
        "vx": 0.0,                 # m/s (North speed)
        "vy": 0.0,                 # m/s (East speed)
        "vz": 0.0,                 # m/s (Vertical speed)
        
        "timestamp": 0
    }

    last_save = time.time()

    while True:
        msg = connection.recv_match(blocking=False) # Checks radio for one new message. False bec if no new message keep moving
        
        if msg:
            msg_type = msg.get_type() # Need message type first (Ask Ryan B if you have a question. Look at MAVLink documentation)

            #INAV sends all these as ints to save space, need to get measurments in their proper units

            # Battery Data (SYS_STATUS)
            if msg_type == 'SYS_STATUS':
                data["voltage_battery"] = msg.voltage_battery / 1000.0  # mV to V
                data["current_battery"] = msg.current_battery / 100.0   # cA to A
                data["battery_remaining"] = msg.battery_remaining       # percent

            # GPS Accuracy (GPS_RAW_INT)
            if msg_type == 'GPS_RAW_INT':
                data["satellites_visible"] = msg.satellites_visible
                data["gps_hdop"] = msg.eph / 100.0  # cm -> m

            # Position & Velocity (GLOBAL_POSITION_INT)
            if msg_type == 'GLOBAL_POSITION_INT':
                data["lat"] = msg.lat / 1e7           # degE7 -> Degrees
                data["lon"] = msg.lon / 1e7           # degE7 -> Degrees
                data["alt_msl"] = msg.alt / 1000.0    # mm -> meters
                data["alt_rel"] = msg.relative_alt / 1000.0 # mm -> meters
                data["heading"] = msg.hdg / 100.0     # cdeg -> degrees
                data["vx"] = msg.vx / 100.0           # cm/s -> m/s
                data["vy"] = msg.vy / 100.0           # cm/s -> m/s
                data["vz"] = msg.vz / 100.0           # cm/s -> m/s

        # Write to JSON every 0.2 seconds (5Hz) (IDK What to set this to this is just a number I pulled from online)
        if time.time() - last_save > 0.2:
            data["timestamp"] = time.time()
            with open(output_file, 'w') as f: # Open file in write mode
                json.dump(data, f, indent=4) # Put our current data in the JSON file
            last_save = time.time()
            
            # Simple Console Dashboard
            print(f"{data['voltage_battery']:.1f}V | {data['satellites_visible']} Sats | {data['alt_rel']:.1f}m Alt", end='\r')

        time.sleep(0.001)

if __name__ == "__main__":
    main()