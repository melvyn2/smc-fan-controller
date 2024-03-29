#!/usr/bin/env python
import csv
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback

FAN_ZONES = [0, 1]
FAN_ZONE_OFFSETS = [0, -30]
IPMI_SDR_TEMP_SENSOR_FILTER = ("CPU",)  # Filter temperature sensors with those that start with any of these
# Temp to fan curve
TEMPERATURE_CURVE = [(0, 0),
                     (40, 20),
                     (60, 50),
                     (80, 80),
                     (90, 100)]
LOOP_DELAY = 3
DEBUG = int(os.environ.get("SFC_DEBUG", "0"))

FAN_PRESET_STANDARD = 0
FAN_PRESET_FULL = 1
FAN_PRESET_OPTIMAL = 2
FAN_PRESET_HEAVYIO = 4
FAN_PRESETS_STR = {
    FAN_PRESET_STANDARD: "standard",
    FAN_PRESET_FULL: "full",
    FAN_PRESET_OPTIMAL: "optimal",
    FAN_PRESET_HEAVYIO: "heavyio"
}

IPMI_SDR_TEMP_TYPE = "temperature"
IPMI_SDR_FAN_TYPE = "fan"
IPMI_SDR_FULL_CSV_KEYS = ["name", "value", "unit", "status", "entity_id", "entity_name", "type", "nominal", "minimum",
                          "maximum", "unr", "uc", "unc", "lnr", "lc", "lnc", "unknown_1", "unknown_2"]
IPMI_SDR_CONCISE_CSV_KEYS = ["name", "value", "unit", "status"]

IPMI_GET_ZONE_SPEED = "0x30 0x70 0x66 0x00 0x{zone:02x}"
IPMI_SET_ZONE_SPEED = "0x30 0x70 0x66 0x01 0x{zone:02x} 0x{speed:02x}"
IPMI_GET_FAN_PRESET = "0x30 0x45 0x00"
IPMI_SET_FAN_PRESET = "0x30 0x45 0x01 0x{preset:02}"


def generate_curve_coefficients(input_coords):
    curve: list[tuple[int, int]] = sorted(input_coords, key=lambda x: x[0])
    previous = curve.pop(0)
    temperature_funcs: dict[int, tuple[int, int]] = {}
    for coord in curve:
        x_coords, y_coords = zip(previous, coord)
        m = (y_coords[1] - y_coords[0]) / (x_coords[1] - x_coords[0])
        b = y_coords[0] - (m * x_coords[0])
        temperature_funcs.update({coord[0]: (m, b)})
        previous = coord
    return temperature_funcs


def target_fan_speed(curve: dict[int, tuple[int, int]], temperature: int) -> int:
    # This requires python 3.6+ for insertion-ordered dict entries
    for segment in curve.items():
        if temperature <= segment[0]:
            return int(segment[1][0] * temperature + segment[1][1])
    return 100


def ipmi_cmd(raw_cmd: str):
    if DEBUG:
        timer = time.time()
    s = subprocess.run(f"ipmitool {raw_cmd} 2>&1", shell=True, capture_output=True, encoding='ascii')
    if s.returncode != 0:
        print(" Error: Problem running ipmitool", file=sys.stderr)
        print(f" Command: ipmitool {raw_cmd}", file=sys.stderr)
        print(f" Return code: {s.returncode}", file=sys.stderr)
        print(f" Output: {s.stdout.strip()}", file=sys.stderr)
        return False
    elif DEBUG:
        print(f" Command: ipmitool {raw_cmd}", file=sys.stderr)
        print(f" Return code: {s.returncode}", file=sys.stderr)
        print(f" Output: {s.stdout.strip()}", file=sys.stderr)
        # noinspection PyUnboundLocalVariable
        print(f" Time Elapsed: {time.time() - timer}")

    out: str = s.stdout.strip()
    if out:
        return out
    else:
        return True


def ipmi_sdr_sensors_from_type(sensor_type: str):
    # This isn't used for temperature because IPMITool fetches all the sensors individually, which can be very slow
    csv_data = ipmi_cmd(f"-c sdr type {sensor_type}")
    if csv_data is False:
        return False

    data = csv.reader(csv_data.splitlines())
    return [dict(zip(IPMI_SDR_CONCISE_CSV_KEYS, sensor_data)) for sensor_data in data]


def ipmi_sdr_sensors_from_name(sensors: list[str]):
    sep = "' '"
    csv_data = ipmi_cmd(f"-c sdr get '{sep.join(sensors)}'")
    if csv_data is False:
        return False

    data = csv.reader(csv_data.splitlines())
    return [dict(zip(IPMI_SDR_FULL_CSV_KEYS, sensor_data)) for sensor_data in data]


def get_system_temps(sensors: list[str]):
    data: list[dict] = ipmi_sdr_sensors_from_name(sensors)
    if data is False:
        print("Error: unable to get current system temperatures", file=sys.stderr)
        return False
    temps: map = map(lambda sensor: int(sensor["value"]),
                     filter(lambda sensor: sensor["status"] != "ns", data))
    return list(temps)


def get_fan_rpms():
    fan_sensors: list[dict] = ipmi_sdr_sensors_from_type(IPMI_SDR_FAN_TYPE)
    if fan_sensors is False:
        print("Error: unable to get current fan RPMs", file=sys.stderr)
        return False
    fan_rpms: map = map(lambda sensor: int(sensor["value"]),
                        filter(lambda sensor: sensor["status"] != "ns", fan_sensors))
    return list(fan_rpms)


def get_fan_preset():
    res = ipmi_cmd("raw " + IPMI_GET_FAN_PRESET)
    if res is False:
        print("Error: could not get current fan preset", file=sys.stderr)
        return False
    return int(res)


def set_fan_preset(preset: int):
    if preset not in FAN_PRESETS_STR:
        print("Warning: setting fan preset to unknown preset", file=sys.stderr)

    if ipmi_cmd("raw " + IPMI_SET_FAN_PRESET.format(preset=preset)):
        print("Updated preset to " + FAN_PRESETS_STR.get(preset, "unknown"))
        return True
    else:
        print("Error: could not update fan preset", file=sys.stderr)
        return False


def check_preset_full(set_to_full: bool = False):
    preset: int = get_fan_preset()

    if preset != FAN_PRESET_FULL:
        if set_to_full:
            set_fan_preset(FAN_PRESET_FULL)
        else:
            print("Warning: Fan preset is not Full Speed, BMC will override curve speeds", file=sys.stderr)


# noinspection PyDefaultArgument
def get_zone_speed(fan_zone: int):
    speed = ipmi_cmd("raw " + IPMI_GET_ZONE_SPEED.format(zone=fan_zone))
    if speed is False:
        print(f"Error: unable to get zone {fan_zone} speed")
        return False
    return int(speed, 16)


# noinspection PyDefaultArgument
def set_zone_speed(fan_zone: int, speed: int):
    if ipmi_cmd("raw " + IPMI_SET_ZONE_SPEED.format(zone=fan_zone, speed=speed)):
        if DEBUG:
            print(f"Set fans on zone {fan_zone} to {speed:02}%")
        return True
    else:
        print(f"Error: Unable to update fan zone {fan_zone}", file=sys.stderr)
        return False


def quit_and_reset_preset(*_signal_args, clean: bool = True):
    if not set_fan_preset(EXIT_PRESET):
        print("CRITICAL: Fan preset could not be reset, fans may be locked too low!"
              " Overheat possible!", file=sys.stderr)
        exit(2)
    exit(0 if clean else 1)


def main_loop(temp_sensors: list[str], temperature_curve: dict[int, tuple[int, int]]):
    time.sleep(LOOP_DELAY)

    temps = get_system_temps(temp_sensors)
    if temps is False:
        raise IOError("Could not get system temperatures")
    if DEBUG:
        print(f"Got temperature {max(temps)}")

    target_speed = target_fan_speed(temperature_curve, max(temps))

    for zone, offset in zip(FAN_ZONES, FAN_ZONE_OFFSETS):
        if set_zone_speed(zone, max(min(target_speed + offset, 100), 0)) is False:
            raise IOError("Could not set fan speed")


if __name__ == '__main__':
    if not shutil.which('ipmitool'):
        print("Error: smc-fan-controller requires ipmitool to be installed and in your PATH", file=sys.stderr)
        sys.exit(1)
    if os.geteuid() != 0:
        print("Warning: ipmitool access requires root;"
              " you may see misleading 'No such file or directory' errors", file=sys.stderr)

    EXIT_PRESET = get_fan_preset()
    EXIT_PRESET = FAN_PRESET_OPTIMAL if EXIT_PRESET is False else EXIT_PRESET  # Set fallback to optimal

    temp_curve = generate_curve_coefficients(TEMPERATURE_CURVE)

    sdr_temp_sensors = list(filter(lambda name: name.startswith(IPMI_SDR_TEMP_SENSOR_FILTER),
                                   map(lambda sensor: sensor["name"], ipmi_sdr_sensors_from_type(IPMI_SDR_TEMP_TYPE))))
    print(f"Using IPMI temperature sensors: {sdr_temp_sensors}")

    # noinspection PyTypeChecker
    signal.signal(signal.SIGINT, quit_and_reset_preset)
    # noinspection PyTypeChecker
    signal.signal(signal.SIGTERM, quit_and_reset_preset)

    # noinspection PyBroadException
    try:
        check_preset_full(True)
        while True:
            # noinspection PyTypeChecker
            main_loop(sdr_temp_sensors, temp_curve)
    except KeyboardInterrupt:
        quit_and_reset_preset()
    except Exception as e:
        print(traceback.format_exc(), file=sys.stderr)
        # If original_preset wasn't set, no changes were made and the program can crash without consequence
        quit_and_reset_preset(clean=False)
