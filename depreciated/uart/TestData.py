import serial
import struct
import time

# MSP V2 Message Structure
# Offset |    Usage     | CRC | Comment
#   0    |      $       | No  | Same lead-in as V1
#   1    |      X       | No  | 'X' in place of 'M' in V1
#   2    |     type     | No  | '<' = request, '>' = response, '!' = error
#   3    |     flag     | Yes | uint8, usually 0, see https://github.com/iNavFlight/inav/wiki/MSP-V2#message-flags
#   4    |   function   | Yes | uint16, https://github.com/iNavFlight/inav/tree/master/src/main/msp (uint16 = 2 bytes)
#   6    | payload size | Yes | uint16 (little endian) payload size in bytes (uint16 = 2 bytes)
#   8    |   payload    | Yes | n (up to 65535 bytes) payload
#  n+8   |   checksum   | No  | uint8, (n = payload size), crc8_dvb_s2 checksum

############################################
############# Helper functions #############
############################################


# Function to check if received checksum matches
def CRC_DVB_S2_check(message) -> bool:
    checksum = calculate_DVB_S2_checksum(message[3:-1])
    if checksum == message[-1]:
        return True
    else:
        print("CRC check failed")
        print(
            "Message CRC: " + repr(message[-1]) + ", calculated CRC: " + repr(checksum)
        )
        return False


# Function to calculate the DVB-S2 CRC for MSP V2
# I have actually no clue how the algorithm works, but it apparently does.
def calculate_DVB_S2_checksum(data) -> int:
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


# Function to create an MSP V2 request
def create_msp_request(function):
    message = [0] * 9  # length constant as most requests don't contain payload
    message[0] = ord("$")  # ASCII $ as integer, $ = message start
    message[1] = ord("X")  # ASCII X as integer, X = MSP V2
    message[2] = ord("<")  # ASCII < as integer, < = request
    message[3] = 0  # flag, 0 for almost all cases
    message[4] = function  # function number (0 - 65535)
    message[6] = 0  # payload size (No payload for requests)
    #   message[8] = 0		    # payload (No payload for requests)
    message[8] = calculate_DVB_S2_checksum(
        message[3:8]
    )  # CRC8/DVB-S2 checksum at the end
    return message


# Function to parse MSP response
def parse_msp_response(message):
    if response[:3] != b"$X>":
        print(
            "Invalid response header"
        )  # Messages without this header are not valid responses
        return None
    else:
        # flag = message[3]			# currently not used
        function = message[4]  # function number
        # payload_size = message[6]   # currently not used
        payload = response[8:-1]  # requested data
        return function, payload


##############################################
######### Initialize some parameters #########
##############################################

# Configure the serial connection
# UART interface on Raspberry Pi pins GPIO14 and GPIO15 (physical header pins 8 and 10)
# Needs to be enabled in raspi-config first
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = (
    115200  # Default for INAV MSP, can be changed in INAV Configurator if necessary
)

# MSP commands, see https://github.com/iNavFlight/inav/tree/master/src/main/msp for full list
MSP_RAW_IMU = 102  # Command to request IMU data (accelerometer, gyroscope, magnetometer), 3x2 bytes each
MSP_RC = 105  # Command to request RC channel values (roll, pitch, yaw, throttle, aux1, ..., aux13), 1x2 bytes each

#############################################
############ Program starts here ############
#############################################

# Open serial port
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

# Execute loop
# Program can usually be cancelled by pressing CTRL + C (yes, the copy shortcut)
while 1:
    # Create request
    msp_request = create_msp_request(
        MSP_RAW_IMU
    )  # IMU data can be tested more easily by moving around the flight controller
    # msp_request = create_msp_request(MSP_RC)		# RC channel data can be compared more easily to the values in the INAV configurator

    ser.write(msp_request)  # Send request
    response = ser.read(ser.in_waiting)  # Read response

    if response:
        if CRC_DVB_S2_check(response):
            function, payload = parse_msp_response(response)
            if function == MSP_RAW_IMU:
                # Parse accelerometer data (bytes 0-5 of the payload)
                ax, ay, az = struct.unpack("<hhh", payload[:6])
                # Convert accelerometer data to g (assuming default scaling in INAV)
                accel_scale = 512.0  # seems to match, scale for ±4g range
                ax_g, ay_g, az_g = ax / accel_scale, ay / accel_scale, az / accel_scale

                # Parse gyroscope data (bytes 6-11 of the payload)
                gx, gy, gz = struct.unpack("<hhh", payload[6:12])
                # Convert gyroscope data to degrees per second (dps)
                gyro_scale = 4.0  # seems to +/- match, scale for ±2000dps range
                gx_dps, gy_dps, gz_dps = (
                    gx / gyro_scale,
                    gy / gyro_scale,
                    gz / gyro_scale,
                )

                # Magnetometer values would be bytes 13-18 of the payload, but I don't have one on my setup

                # Print values
                print(
                    "ax = {:+f}g, ay = {:+f}g, az = {:+f}g, gx = {:+f}dps, gy = {:+f}dps, gz = {:+f}dps".format(
                        ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps
                    )
                )

            elif function == MSP_RC:
                # Parse RC all channel values, no conversion necessary
                # The channel assignment is from a Radiomaster Boxer ELSR controller
                (
                    roll,
                    pitch,
                    yaw,
                    throttle,
                    sa,
                    sb,
                    sc,
                    sd,
                    se,
                    sf,
                    s1,
                    s2,
                    aux9,
                    aux10,
                    aux11,
                    aux12,
                    aux13,
                ) = struct.unpack("<hhhhhhhhhhhhhhhhh", payload)

                # Print values
                print(
                    "roll = {:}, pitch = {:}, yaw = {:}, throttle = {:}, sa = {:}, sb = {:}, sc = {:}, sd = {:}, se = {:}, sf = {:}, s1 = {:}, s2 = {:}, aux9 = {:}, aux10 = {:}, aux11 = {:}, aux12 = {:}, aux13 = {:}".format(
                        roll,
                        pitch,
                        yaw,
                        throttle,
                        sa,
                        sb,
                        sc,
                        sd,
                        se,
                        sf,
                        s1,
                        s2,
                        aux9,
                        aux10,
                        aux11,
                        aux12,
                        aux13,
                    )
                )

            else:
                print("Unexpected function: " + repr(function))
        else:
            print("CRC check failed")
    else:
        print("No response received")
    time.sleep(0.2)
