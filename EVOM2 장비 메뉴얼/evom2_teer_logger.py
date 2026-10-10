"""
EVOM2 TEER logger  -  EVOM2 BNC OUTPUT -> NI DAQ analog input -> CSV

EVOM2 analog output scale (manual p.18):
    OHMS mode      : 1 ohm = 1 mV   -> R[ohm] = V * 1000
    MILLIVOLTS mode: 1 mV  = 10 mV  -> Vm[mV] = V * 100
NOTE: the OUTPUT is a 12.5 Hz square wave (0 V <-> V); the display shows the amplitude V.
      A plain average gives V/2, so this script measures the amplitude (top 40% mean - bottom 40% mean).

Usage (PowerShell):
    python evom2_teer_logger.py                 # interactive: Enter = record, q = quit
    python evom2_teer_logger.py --mode mv       # EVOM2 FUNCTION switch on MILLIVOLTS
    python evom2_teer_logger.py --continuous 2  # auto-record every 2 s
    python evom2_teer_logger.py --list          # show NI devices
"""
import argparse
import csv
import datetime as dt
import statistics
import sys
import time

try:
    import nidaqmx
    from nidaqmx.constants import TerminalConfiguration, AcquisitionType
    from nidaqmx.system import System
except ImportError:
    sys.exit("nidaqmx 패키지가 없습니다:  python -m pip install nidaqmx")

SCALE = {"ohm": (1000.0, "ohm"), "mv": (100.0, "mV")}


def list_devices():
    devs = list(System.local().devices)
    if not devs:
        print("연결된 NI DAQ 장비가 없습니다. USB 연결과 NI MAX를 확인하세요.")
    for d in devs:
        print(f"{d.name:8s} {d.product_type:15s} AI channels: {[c.name for c in d.ai_physical_chans]}")
    return devs


def pick_channel(user_channel):
    if user_channel:
        return user_channel
    devs = list(System.local().devices)
    for d in devs:
        chans = list(d.ai_physical_chans)
        if chans:
            return chans[0].name  # e.g. "Dev1/ai0"
    sys.exit("아날로그 입력이 있는 NI 장비를 찾지 못했습니다. --channel Dev1/ai0 처럼 직접 지정하세요.")


def read_average(channel, rate, duration, terminal):
    n = max(int(rate * duration), 2)
    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(channel, terminal_config=terminal,
                                             min_val=-10.0, max_val=10.0)
        task.timing.cfg_samp_clk_timing(rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=n)
        data = task.read(number_of_samples_per_channel=n, timeout=duration + 5)
    mean, sd = statistics.mean(data), statistics.pstdev(data)
    srt = sorted(data)
    k = max(1, int(n * 0.4))
    amp = statistics.mean(srt[-k:]) - statistics.mean(srt[:k])
    square = abs(mean) > 0.02 and sd > 0.3 * abs(mean)
    return (amp if square else mean), sd


def main():
    ap = argparse.ArgumentParser(description="EVOM2 -> NI DAQ TEER logger")
    ap.add_argument("--mode", choices=SCALE, default="ohm", help="EVOM2 FUNCTION switch: ohm (default) or mv")
    ap.add_argument("--channel", help="NI channel, e.g. Dev1/ai0 (default: first AI channel found)")
    ap.add_argument("--terminal", choices=["rse", "diff", "nrse"], default="rse")
    ap.add_argument("--rate", type=float, default=1000.0, help="sample rate Hz (default 1000)")
    ap.add_argument("--duration", type=float, default=1.0, help="averaging window s (default 1.0)")
    ap.add_argument("--continuous", type=float, metavar="SEC", help="auto-record every SEC seconds")
    ap.add_argument("--out", help="CSV file (default teer_log_YYYYMMDD.csv)")
    ap.add_argument("--list", action="store_true", help="list NI devices and exit")
    a = ap.parse_args()

    if a.list:
        list_devices()
        return

    terminal = {"rse": TerminalConfiguration.RSE, "diff": TerminalConfiguration.DIFF,
                "nrse": TerminalConfiguration.NRSE}[a.terminal]
    factor, unit = SCALE[a.mode]
    channel = pick_channel(a.channel)
    out = a.out or f"teer_log_{dt.date.today():%Y%m%d}.csv"

    print(f"channel={channel}  terminal={a.terminal}  mode={a.mode} (x{factor:g} -> {unit})  file={out}")
    print("EVOM2: 충전기 분리, 20분 워밍업, 1000 ohm 테스트 저항으로 R ADJ 보정 후 사용하세요.")

    new_file = True
    try:
        new_file = open(out, "r", encoding="utf-8").read(1) == ""
    except FileNotFoundError:
        pass

    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "label", "voltage_V", "value", "unit", "noise_sd_V", "mode"])
        i = 0
        while True:
            if a.continuous:
                label = f"t{i}"
            else:
                s = input("\n전극을 well에 넣고 안정되면 Enter (라벨 입력 가능, q=종료): ").strip()
                if s.lower() == "q":
                    break
                label = s or f"sample{i}"
            mean_v, sd_v = read_average(channel, a.rate, a.duration, terminal)
            value = mean_v * factor
            ts = dt.datetime.now().isoformat(timespec="seconds")
            w.writerow([ts, label, f"{mean_v:.5f}", f"{value:.1f}", unit, f"{sd_v:.5f}", a.mode])
            f.flush()
            print(f"{ts}  {label:10s}  {value:8.1f} {unit}   (V={mean_v:.4f}, sd={sd_v:.4f})")
            i += 1
            if a.continuous:
                try:
                    time.sleep(a.continuous)
                except KeyboardInterrupt:
                    break
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
