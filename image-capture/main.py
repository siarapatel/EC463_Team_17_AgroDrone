import serial
from dual_capture import capture

PORT = "/dev/serial0"
BAUD = 115200
TRIGGER_BYTE = b"c"     # only trigger on 'c'

def main():
    print("Hello from image-capture! Waiting for UART trigger...")

    with serial.Serial(PORT, BAUD, timeout=0.1) as ser:
        ser.reset_input_buffer()

        while True:
            b = ser.read(1)          # returns b'' on timeout
            if not b:
                continue

            if b == TRIGGER_BYTE:
                print("Starting Capture")
                capture()
                outdir = os.environ.get("AGRO_OUTDIR")
                if not outdir:
                   raise RuntimeError( "AGRO_OUTDIR is not set. Configure it in the systemd unit "
                    '(e.g. Environment="AGRO_OUTDIR=/home/sr-design/export").')
                capture(outdir=outdir)
                print("\n\n\n\n\n")
            else:
                # ignore noise/other characters
                pass

if __name__ == "__main__":
    main()
