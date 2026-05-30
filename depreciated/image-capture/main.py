from gpiozero import Button
from signal import pause
from dual_capture import capture


PIN = 27  # BCM Pin


def on_press():
    print("Starting Capture")
    capture()
    print("\n\n\n\n\n")


def main():
    try:
        print("Hello from image-capture!")
        button = Button(PIN, pull_up=False)  # pull_up=False == pull-down
        button.when_pressed = on_press
        pause()  # Keeps the script alive and waiting
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
