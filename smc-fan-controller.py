#!/usr/bin/env python
import csv
import os
import shutil
import subprocess
import sys
import time

DEFAULT_FAN_ZONES = [0, 1]
IPMI_SDR_TEMP_SENSOR_FILTER = ("CPU",)  # Filter temperature sensors with those that start with any of these
# Temp to fan curve
TEMPERATURE_CURVE = [(0, 0),
                     (30, 0),
                     (40, 30),
                     (60, 50),
                     (80, 80),
                     (90, 100)]
LOOP_DELAY = 5

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

IPMI_SDR_CSV_KEYS = ["name", "value", "unit", "status"]
IPMI_SDR_TEMP_TYPE = "TEMP"
IPMI_SDR_FAN_TYPE = "FAN"

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
            return segment[1][0] * temperature + segment[1][1]
    return 100


def ipmi_sdr_cmd(sensor_type: str):
    cmd: str = f"ipmitool -c sdr type {sensor_type}"
    s = subprocess.run(f"{cmd} 2>&1", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if s.returncode != 0:
        print(" Error: Problem running ipmitool", file=sys.stderr)
        print(f" Command: {cmd}", file=sys.stderr)
        print(f" Return code: {s.returncode}", file=sys.stderr)
        print(f" Output: {s.stdout.decode('ascii').strip()}", file=sys.stderr)
        return False

    data = csv.reader(s.stdout.decode('ascii').splitlines())
    return [dict(zip(IPMI_SDR_CSV_KEYS, sensor_data)) for sensor_data in data]


def get_system_temps():
    temp_sensors: list[dict] = ipmi_sdr_cmd(IPMI_SDR_TEMP_TYPE)
    if temp_sensors is False:
        print("Error: unable to get current system temperatures", file=sys.stderr)
        return False
    cpu_temps: map = map(lambda sensor: sensor["value"],
                         filter(lambda sensor: sensor["name"].startswith(IPMI_SDR_TEMP_SENSOR_FILTER),
                                filter(lambda sensor: sensor["status"] != "ns", temp_sensors)))
    return list(cpu_temps)


def get_fan_rpms():
    fan_sensors: list[dict] = ipmi_sdr_cmd(IPMI_SDR_FAN_TYPE)
    if fan_sensors is False:
        print("Error: unable to get current fan RPMs", file=sys.stderr)
        return False
    fan_rpms: map = map(lambda sensor: sensor["value"],
                        filter(lambda sensor: sensor["status"] != "ns", fan_sensors))
    return list(fan_rpms)


def ipmi_raw_cmd(raw_cmd: str):
    cmd: str = f"ipmitool raw {raw_cmd}"
    s = subprocess.run(f"{cmd} 2>&1", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if s.returncode != 0:
        print(" Error: Problem running ipmitool", file=sys.stderr)
        print(f" Command: {cmd}", file=sys.stderr)
        print(f" Return code: {s.returncode}", file=sys.stderr)
        print(f" Output: {s.stdout.decode('ascii').strip()}", file=sys.stderr)
        return False

    out: bytes = s.stdout.strip()
    if out:
        return out
    else:
        return True


def get_fan_preset():
    res = ipmi_raw_cmd(IPMI_GET_FAN_PRESET)
    if res is False:
        print("Error: could not get current fan preset", file=sys.stderr)
        return False
    return int(res)


def set_fan_preset(preset: int):
    if preset not in FAN_PRESETS_STR:
        print("Warning: setting fan preset to unknown preset", file=sys.stderr)

    if ipmi_raw_cmd(IPMI_SET_FAN_PRESET.format(preset=preset)):
        print("Updated preset to " + FAN_PRESETS_STR.get(preset, "unknown"))
        return True
    else:
        print("Error: could not update fan preset", file=sys.stderr)
        return False


def check_preset_full(set_to_full: bool = False):
    preset: int = get_fan_preset()

    if preset != FAN_PRESET_FULL:
        if set_to_full:
            print("Seting BMC fan preset to Full...")
            set_fan_preset(FAN_PRESET_FULL)
            print("Waiting 5 seconds to let fans spin up...")
            time.sleep(5)
        else:
            print("Warning: Fan preset is not Full Speed, BMC will override curve speeds", file=sys.stderr)
    return preset


# noinspection PyDefaultArgument
def get_zone_speed(zones: list[int] = DEFAULT_FAN_ZONES):
    res: list[int] = []
    for zone in zones:
        speed = int(ipmi_raw_cmd(IPMI_GET_ZONE_SPEED.format(zone=zone)), 16)
        if speed is False:
            print(f"Error: unable to get zone {zone} speed")
        else:
            res.append(speed)
    return res


# noinspection PyDefaultArgument
def set_zone_speed(speed: int, zones: list[int] = DEFAULT_FAN_ZONES):
    res: bool = True
    for zone in zones:
        if ipmi_raw_cmd(IPMI_SET_ZONE_SPEED.format(zone=zone, speed=speed)):
            print(f"Set fans on zone {zone} to {speed:02}%")
        else:
            print(f"Error: Unable to update fan zone {zone}", file=sys.stderr)
            res = False
    return res


def quit_and_reset_preset(preset: int, clean: bool = True):
    print("Resetting preset to optimal before quitting")
    if not set_fan_preset(preset):
        print("CRITICAL: Fan preset could not be reset, fans may be locked too low!"
              " Overheat possible!", file=sys.stderr)
        exit(2)
    exit(0 if clean else 1)


if __name__ == '__main__':
    if not shutil.which('ipmitool'):
        print("Error: smc-fan-controller requires ipmitool to be installed and in your PATH", file=sys.stderr)
        sys.exit(1)
    if os.geteuid() != 0:
        print("Warning: ipmitool access requires root;"
              " you may see misleading 'No such file or directory' errors", file=sys.stderr)
    try:
        original_preset = check_preset_full(True)
        original_preset = FAN_PRESET_OPTIMAL if original_preset is False else original_preset  # Set fallback to optimal
        temp_curve_dict = generate_curve_coefficients(TEMPERATURE_CURVE)
        while True:
            temps = get_system_temps()
            if temps is False:
                raise IOError("Could not get system temperatures")
            target_speed = target_fan_speed(temp_curve_dict, max(temps))
            print(f"Got temperature {max(temps)}, setting speed to {target_speed}")
            if set_zone_speed(target_speed) is False:
                raise IOError("Could not set fan speed")
            time.sleep(LOOP_DELAY)
    except KeyboardInterrupt:
        # noinspection PyUnboundLocalVariable
        quit_and_reset_preset(original_preset)
    except Exception as e:
        print(f"Error: Encountered {e}: {e.args}", file=sys.stderr)
        # If original_preset wasn't set, no changes were made and the program can crash without consequence
        # noinspection PyUnboundLocalVariable
        quit_and_reset_preset(original_preset, False)