from picamera2 import Picamera2
from libcamera import controls
from time import sleep

picam2 = Picamera2()
picam2.configure(picam2.create_still_configuration())
picam2.start(); sleep(2)

for lp in [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0]:
    picam2.set_controls({"AfMode": controls.AfModeEnum.Manual, "LensPosition": lp})
    sleep(1.5)  # let the lens actuator physically move and settle
    picam2.capture_file(f"focus_{lp:.1f}.jpg")

picam2.set_controls({"AfMode": controls.AfModeEnum.Auto})
picam2.autofocus_cycle()
md = picam2.capture_metadata()
picam2.capture_file("focus_auto.jpg")
print(f"AF landed at LensPosition={md.get('LensPosition'):.2f}, AfState={md.get('AfState')}")
picam2.stop()
