from aesrdevicelib.sensors.gps_read import GPSRead
try:
    from aesrdevicelib.sensors.blue_esc import BlueESC
    from aesrdevicelib.sensors.bno055 import BNO055
except ImportError:  # SMBus doesn't exist: probably debugging
    print("Can't import BlueESC or/and BNO055. WILL ENTER DEBUG")
from . import util
import threading
import time
import sys
import math


CONTROL_TIMEOUT = 2  # Seconds

DISTANCE_DEADBAND = 0.1  # meters

FULL_PWR_DISTANCE = 10  # meters
MIN_PWR = 0.05  # out of 1

MAX_MTR_PWR = 20


def scale_m_distance(m: float):
    if abs(m) < DISTANCE_DEADBAND:
        return 0
    x = (-1 * FULL_PWR_DISTANCE * MIN_PWR) / (MIN_PWR - 1)  # applied to make m=FULL =>1 and m=0 =>MIN_PWR
    return (m/(FULL_PWR_DISTANCE+x)) + MIN_PWR


def scale_limit(s):
    max_ = 1
    min_ = -max_
    return max(min_, min(max_, s))


class ThrusterControl(threading.Thread):
    def __init__(self, *args, gps: GPSRead=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.AUTO_TARGETS = [{'lat': 41.73505, 'lon': -71.319}, {'lat': 41.736, 'lon': -71.320}]

        self._DEBUG = False  # will enable if motors fail to initialize

        self.running = False
        self.auto_force_disable = False
        self.auto_target = None
        self.movement = {'x_trans': 0, 'y_trans': 0, 'xy_rot': 0, 'ts': None}  # Trans and rots are gains [-1.0, 1]

        """ ---- DEVICES ---- """
        # GPS Setup:
        if gps is None:
            print("No GPS, attempting to connect")
            try:
                self.gps = GPSRead()
                print("Successfully connected GPS")
            except:
                self.gps = None
                print("Error connecting to GPS")


        # BlueESC instances
        try:
            self.thrusters = {"f": BlueESC(0x2a), "b": BlueESC(0x2d), "l": BlueESC(0x2b),
                              "r": BlueESC(0x2c)}
        except (IOError, NameError):
            print("Thruster setup error: " + str(sys.exc_info()[1]))
            print("Disabling thrusters -- DEBUG MODE")
            self._DEBUG = True
            self.thrusters = None

        # BNO055 sensor setup
        try:
            self.imu = BNO055()
            time.sleep(1)
            self.imu.setExternalCrystalUse(True)

            print("Wait for IMU calibration [move it around]...")
            while not self.imu.getCalibration() == (0x03,) * 4:
                time.sleep(0.5)
            print("IMU calibration finished.")
        except (IOError, NameError):
            print("\nIMU setup error: " + str(sys.exc_info()[1]))
            print("Disabling IMU...")
            self.imu = None

    @staticmethod
    def print_debug(*args, **kwargs):
        print("ThrusterControl: DEBUG -", *args, **kwargs)

    def start(self):
        self.running = True
        super().start()

    def close(self):
        self.stop_thrusters()
        self.gps.close()
        self.running = False
        self.join()

    def stop_thrusters(self):
        if isinstance(self.thrusters, dict):
            for k, v in self.thrusters:
                self.thrusters[k].setPower(0)
        else:
            self.print_debug("Stop Thrusters.")

    def manual_control(self, x=0, y=0, rot=0):
        self.movement = {'x_trans': x, 'y_trans': y, 'xy_rot': rot, 'ts': time.time()}

    def drive_thrusters(self, x, y, rot):
        pwrs = {
            'f': MAX_MTR_PWR * scale_limit(x + rot),
            'b': MAX_MTR_PWR * scale_limit(x - rot),
            'l': MAX_MTR_PWR * scale_limit(y + rot),
            'r': MAX_MTR_PWR * scale_limit(y - rot)}

        if isinstance(self.thrusters, dict):
            for k, v in pwrs.items():
                self.thrusters[k].startPower(v)
        else:
            self.print_debug("Thrusters Power: {}".format(pwrs))

    def auto_enabled(self) -> bool:
        if self.auto_target is None:
            return False
        return True

    def set_auto_target(self, lat=None, lon=None):
        if lat is None or lon is None:
            self.auto_target = None
        else:
            self.auto_target = {'lat': lat, 'lon': lon}

    def get_next_auto_target(self) -> dict:
        try:
            t = self.AUTO_TARGETS[0]
            del self.AUTO_TARGETS[0]
        except IndexError:
            t = None
        return t

    def next_auto_target(self):
        self.auto_target = self.get_next_auto_target()

    def get_remaining_waypoints(self):
        return len(self.AUTO_TARGETS)

    def disable_auto(self):
        self.print_debug("Auto Disable")
        self.auto_target = None

    def run(self):
        while self.running:
            time.sleep(0.2)
            # Recent control check:
            if self.movement['ts'] is None or ((time.time()-self.movement['ts']) > CONTROL_TIMEOUT):
                self.auto_target = None
                self.stop_thrusters()
                continue

            """--- Contact with control ---"""
            # Check if a control value is not None nor 0 (manual input -> cut off) or the position is incorrect
            #   or auto is not disabled
            if not isinstance(self.auto_target, dict)\
                    or not all((v == 0 or k is 'ts') for k, v in self.movement.items())\
                    or self.auto_force_disable:
                self.auto_target = None
            else:
                """--- Autonomous ---"""
                try:
                    loc = self.gps.readLocationData()
                except ValueError:
                    if self._DEBUG:
                        loc = {'lat': 41.735, 'lon': -71.319}
                    else:
                        self.stop_thrusters()
                        self.print_debug("AUTONOMOUS: NO GPS -- STOPPING THRUSTERS!")
                        continue

                # get position differences in meters
                pos_diff_m = util.gps_coord_mdiff((loc['lat'],loc['lon']),
                                                  (self.auto_target['lat'], self.auto_target['lon']))

                self.print_debug("Autonomous Diff: {}".format(pos_diff_m))

                self.drive_thrusters(scale_m_distance(pos_diff_m[0]), scale_m_distance(pos_diff_m[1]), 0)
                # TODO: IMU read and angle hold

            if self.auto_target is None:
                self.drive_thrusters(self.movement['x_trans'], self.movement['y_trans'], self.movement['xy_rot'])
