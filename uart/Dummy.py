import serial
import time

ser = serial.Serial("/dev/ttyAMA0", 115200, timeout=1)  # or ttyS0 on older Pi
while 1:
    ser.write(b"hello\n")
    time.sleep(0.5)
