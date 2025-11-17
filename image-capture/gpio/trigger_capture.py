from gpiozero import Button
from signal import pause
import dual_capture_functions

TRIGGER_PIN = 17
trigger = Button(TRIGGER_PIN, pull_up=False)
dc = dual_capture_functions()


class Args:
    def __init__(self, exposure=1000, gain=1.0, jpeg_quality=90, outdir="captures",burst=5, no_metadata=False):
        self.exposure = exposure
        self.gain = gain
        self.jpeg_quality = jpeg_quality
        self.outdir = outdir
        self.burst = burst
        self.no_metadata = no_metadata

def on_trigger():
    # Call your existing capture routine
    args = Args()  # instantiate and assign
    working_dir = args.outdir

    # Create working directory
    dc.ensure_dir(working_dir)
    
    # Initialize both cameras
    picam0 = dc.init_camera(0, args.exposure, args.gain)
    picam1 = dc.init_camera(1, args.exposure, args.gain)
    
    # Start both cameras (pre-start for minimal capture delay)
    picam0.start()
    picam1.start()
    
    # Brief settling time for 3A locks to take effect
    dc.time.sleep(0.2)
         
    # Perform one burst capture set
    dc.sequential_capture_cycle(picam0, picam1, working_dir,burst_count=args.burst, jpeg_quality=args.jpeg_quality)

trigger.when_pressed = on_trigger


    