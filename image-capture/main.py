import RPi.GPIO as GPIO
import time
from dual_sequential_capture import dual_caputure


pin = 27  # BCM Pin


# on a loop, check the gpio state
def main():
    print("Hello from image-capture!")
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    # Start watching for the gpio value
    while True:
        state = GPIO.input(pin)  # returns 0 (low) or 1 (high)
        if state:
            dual_caputure
            time.sleep(5)  # dumb sleep to not do duplicates
        time.sleep(0.1)
    GPIO.cleanup()


if __name__ == "__main__":
    main()
