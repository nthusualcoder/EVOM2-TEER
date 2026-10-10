"""
EVOM2 TEER 측정 프로그램 (NI USB-6003 사용)

실행:  EVOM2_TEER.exe 더블클릭  (또는 EVOM2_TEER_실행.bat / python EVOM2_TEER.py)
  1) [새로고침] -> 장비 목록에서 Dev?/ai0 [USB-6003] 선택 -> [연결]   (입력 방식 기본 DIFF)
  2) EVOM2 FUNCTION 스위치에 맞춰 모드(OHMS / MILLIVOLTS) 선택
  3) 세포 없는 insert 에 전극을 넣고 [blank 측정 시작] -> 30초 평균이 blank 로 저장
  4) insert 종류 선택 -> 오른쪽 플레이트 그림에서 well 클릭(= 라벨) -> 기록 방식 선택 후 [기록]
       수동  : [정지]를 누를 때까지          반자동: 정한 시간 동안
       자동  : N초 측정 -> M초 휴식(그동안 전극을 옆 well 로 옮김) -> 다음 well N초 측정 … K개 well
     CSV 에는 초당 5행씩 모두 저장하고, 한 번의 기록을 대표하는 값은 마지막 3초 평균
     (전극을 옮긴 직후 흔들리는 앞부분 제외). 표의 강조 행·플레이트 숫자·시간 추적 그래프가 이 값을 쓴다.
  5) [시간 추적 그래프]: 폴더 안 CSV 들을 파일 수정 날짜 순서로 놓고 well 별 패널로 일간 추이를 그림
     (하루에 CSV 하나 = 측정 1회. 기본 파일명 teer_log_YYYYMMDD.csv 는 날마다 새로 생김)
  6) [업데이트 확인]: GitHub 릴리스에 새 버전이 있으면 받아서 바꾼 뒤 다시 실행

DIFF 배선: 노란 선(BNC 중심) -> 2번(AI0+),  검은 선(실드) -> 3번(AI4 = AI0-) 과 1번(AI GND) 둘 다.
RSE 배선 : 노란 선 -> 2번(AI0),  검은 선 -> 1번(AI GND).

EVOM2 아날로그 출력 (매뉴얼 p.18 + 2026-09-07 실측):
  OHMS 모드 1 Ω = 1 mV, MILLIVOLTS 모드 1 mV = 10 mV.
  출력은 보통 DC(화면값 그대로)이고, 첫 RSE 테스트에서는 0 V <-> V 의 12.5 Hz 사각파로 읽힌 적이 있다.
  신호 처리 '자동'은 사각파면 진폭(위 40% 평균 - 아래 40% 평균), 아니면 평균으로 계산해 두 경우 모두 화면값과 맞춘다.

TEER (WPI 매뉴얼 Appendix A):  TEER [Ω·cm²] = (R_total - R_blank) × 막 면적 [cm²]
"""
import collections
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import queue
import re
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import types
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, TerminalConfiguration
    from nidaqmx.system import System
    NIDAQMX_OK = True
    NIDAQMX_ERR = ""
except Exception as _e:
    NIDAQMX_OK = False
    NIDAQMX_ERR = f"{type(_e).__name__}: {_e}"

APP_VERSION = "1.0.0"
GITHUB_REPO = "nthusualcoder/EVOM2-TEER"          # 업데이트를 받아 오는 GitHub 저장소 (공개)
SRC_IN_REPO = "EVOM2 장비 코드/EVOM2_TEER.py"      # .py 로 실행할 때 업데이트로 받는 파일 (저장소 안 경로)
UPDATE_EXE = "EVOM2_TEER_update.exe"              # 업데이트하는 동안에만 잠깐 생기는 새 exe

RATE = 1000            # 샘플링 속도 (Hz)
CHUNK = 100            # 한 번에 읽는 샘플 수 (0.1초)
WINDOW = 10            # 값 계산에 쓰는 chunk 수 (1초 이동창)
ROWS_PER_SEC = 5       # 기록할 때 초당 저장 행 수
TAIL_SEC = 3.0         # 한 번의 기록을 대표하는 값 = 마지막 3초 평균
ABORT_MARK = "중단"    # 중단된 기록에 덧붙이는 표시 행의 rec_mode (읽을 때 그 기록을 뺌)
BLANK_SEC = 30         # blank 측정 시간 기본값 (초)
SIM_NAME = "시뮬레이션 (장비 없이 테스트)"
MODES = {"OHMS (저항)": (1000.0, "Ω"), "MILLIVOLTS (전압)": (100.0, "mV")}
PROCS = ["자동", "사각파 진폭", "단순 평균"]
REC_MODES = ["수동 (정지 누를 때까지)", "반자동 (정한 시간 동안)", "자동 (well 순서대로 이동)"]
WELLS = [("24-well", 0.33), ("12-well", 1.12), ("6-well", 4.67), ("96-well", 0.143)]
PLATES = {"24-well": (4, 6), "12-well": (3, 4), "6-well": (2, 3), "96-well": (8, 12)}   # (행 수, 열 수)
WELL_RE = re.compile(r"[A-H](1[0-2]|[1-9])")      # well ID 로 쓰인 라벨 (A1 … H12)
TREND_Y = {"TEER (Ω·cm²)": ("teer", "Ω·cm²"), "R − blank (Ω)": ("rb", "Ω"), "R (Ω)": ("r", "Ω")}
TREND_TIME = ["파일 수정 날짜", "CSV 안 기록 시각"]
TREND_MAX = 12         # 시간 추적 창에 한 번에 그리는 well 패널 수 (넘으면 앞의 12개만 그리고 알림)
EXPORT_PREFIX = "TEER_요약_"   # 시간 추적 표를 내보낸 파일 (측정 CSV 로 읽지 않음)
# exe(PyInstaller)로 실행되면 exe가 있는 폴더, .py로 실행되면 .py가 있는 폴더를 기준으로 저장
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "evom2_settings.json")

# 테마: 기본(밝음) / Claude Code 풍(어두운 배경 + 주황 포인트)
THEMES = {
    "light": dict(bg="#f4f4f4", fg="#202020", muted="#666666", accent="#1a4d8f", accent2="#d97757",
                  rec="#b00020", panel="#ffffff", field="#ffffff", border="#c8c8c8", btn="#e6e6e6",
                  btn_active="#d0d0d0", sel="#cfe0f7", grid="#e0e0e0", on_accent="#ffffff"),
    "claude": dict(bg="#1f1e1d", fg="#ece6dc", muted="#a09a90", accent="#d97757", accent2="#7fb3d5",
                   rec="#ff7b72", panel="#262523", field="#2e2c2a", border="#3d3a37", btn="#3a3835",
                   btn_active="#4a4744", sel="#5a4030", grid="#33312e", on_accent="#1f1e1d"),
}


def analyze(samples):
    """샘플 묶음 -> (평균 V, 표준편차 V, 사각파 진폭 V, 사각파 여부)"""
    n = len(samples)
    mean = statistics.mean(samples)
    sd = statistics.pstdev(samples) if n > 1 else 0.0
    s = sorted(samples)
    k = max(1, int(n * 0.4))
    amp = statistics.mean(s[-k:]) - statistics.mean(s[:k])
    is_square = abs(mean) > 0.02 and sd > 0.3 * abs(mean)
    return mean, sd, amp, is_square


def load_settings():
    d = {"cal": {m: 1.0 for m in MODES}, "areas": {w: a for w, a in WELLS},
         "folder": HERE, "filename": "", "well": WELLS[0][0], "theme": "light",
         "terminal": "DIFF", "blank_sec": BLANK_SEC}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        d["cal"].update({m: float(v) for m, v in saved.get("cal", {}).items() if m in MODES})
        d["areas"].update({w: float(v) for w, v in saved.get("areas", {}).items() if w in d["areas"]})
        if os.path.isdir(saved.get("folder", "")):
            d["folder"] = saved["folder"]
        # 날짜 이름(teer_log_YYYYMMDD.csv)은 다음 날 새 파일로 넘어가게 기억하지 않는다
        # (시간 추적 그래프가 CSV 하나 = 하루로 보기 때문). 직접 지은 이름은 그대로 기억.
        name = saved.get("filename", "")
        d["filename"] = "" if re.fullmatch(r"teer_log_\d{8}\.csv", name) else name
        if saved.get("well") in d["areas"]:
            d["well"] = saved["well"]
        if saved.get("theme") in THEMES:
            d["theme"] = saved["theme"]
        if saved.get("terminal") in ("RSE", "DIFF"):
            d["terminal"] = saved["terminal"]
        d["blank_sec"] = int(saved.get("blank_sec", BLANK_SEC))
    except Exception:
        pass
    return d


def save_settings(d):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fmt_val(v):
    return "-" if v is None else f"{v:,.1f}"


# ----------------------------------------------------------------------------- 측정 기록 읽기
def tail_summary(rows):
    """한 번의 기록 행들 [(경과초, R, R−blank, TEER), …] -> 마지막 TAIL_SEC 초 평균 {"r","rb","teer","n"}.
    0.2초 간격이면 마지막 15행. 3초보다 짧은 기록은 전부, 한 행뿐인 기록(blank)은 그 값."""
    end = max(r[0] for r in rows)
    tail = [r for r in rows if r[0] > end - TAIL_SEC + 0.05]

    def mean(j):
        xs = [r[j] for r in tail if r[j] is not None]
        return statistics.mean(xs) if xs else None
    return {"r": mean(1), "rb": mean(2), "teer": mean(3), "n": len(tail)}


def read_text(path):
    """CSV 파일 -> 글자. 이 프로그램이 쓰는 UTF-8(BOM) 이 기본이고, 엑셀에서 다시 저장한 CP949 도 읽는다.
    저장 도중 전원이 꺼져 생긴 NUL 문자는 지운다."""
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc).replace("\x00", "")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def read_runs(path):
    """측정 CSV -> 기록(run) 요약 목록 (파일에 적힌 순서 = 시간순). 읽을 수 없는 파일이면 [].
    기록 = 같은 run_id·라벨의 행들, 요약 = {"well"(라벨), "run_id", "t"(시작 시각, 못 읽으면 None), "insert",
    "mode", "rows"(행 수), "r", "rb", "teer", "n"(평균한 행 수)} 이고 값은 마지막 TAIL_SEC 초 평균.
    전압(mV) 행과 옛 형식(열 18개 미만) 행, 숫자가 깨진 행, 중단 표시(ABORT_MARK)가 붙은 기록은 건너뛴다."""
    groups, aborted = {}, set()
    try:
        for row in csv.reader(io.StringIO(read_text(path), newline="")):
            if len(row) < 18 or row[0] == "timestamp":
                continue
            if row[14] == ABORT_MARK:
                aborted.add((row[3], row[1]))
                continue
            if row[5] != "Ω":
                continue
            try:
                vals = (float(row[2]), float(row[4]), float(row[7]) if row[7] else None,
                        float(row[10]) if row[10] else None)
            except ValueError:
                continue
            if not all(v is None or math.isfinite(v) for v in vals):
                continue
            g = groups.setdefault((row[3], row[1]), {"t": None, "insert": row[8], "mode": row[14], "rows": []})
            if g["t"] is None:
                try:
                    g["t"] = dt.datetime.fromisoformat(row[0])
                except ValueError:
                    pass            # 엑셀이 시각 형식을 바꿔 저장한 경우 (파일 수정 날짜 기준이면 상관없음)
            g["rows"].append(vals)
    except (OSError, csv.Error):
        pass                        # 읽은 데까지만 씀
    return [{"well": k[0], "run_id": k[1], "t": g["t"], "insert": g["insert"], "mode": g["mode"],
             "rows": len(g["rows"]), **tail_summary(g["rows"])} for k, g in groups.items() if k not in aborted]


def load_sessions(folder, by_file=True, plate=WELLS[0][0]):
    """폴더의 측정 CSV 들 -> {"sessions": 측정 회차 목록(시간순), "csv", "files", "skipped", "plate"}.
    by_file=True : CSV 하나 = 회차 하나, 회차 시각 = 파일 수정 날짜 (하루에 파일 하나씩 쓸 때, 기본)
    by_file=False: CSV 안에 기록된 시각의 날짜별로 묶음 (여러 날을 한 파일에 이어 썼을 때)
    회차의 well 값 = 그 회차에서 그 well 의 마지막 기록(의 마지막 TAIL_SEC 초 평균). 플레이트 숫자와 같은 규칙이라
    중단됐거나 잘못 잰 기록은 다시 재면 새 값으로 바뀐다."""
    groups, used, skipped, inserts = {}, set(), 0, collections.Counter()
    try:
        names = sorted(n for n in os.listdir(folder)
                       if n.lower().endswith(".csv") and not n.startswith(EXPORT_PREFIX))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            continue
        for run in read_runs(path):
            if not WELL_RE.fullmatch(run["well"]):
                skipped += run["well"] != "blank"          # blank 은 well 이 아니니 조용히 뺌
                continue
            if by_file:
                key, when = name, mtime
            elif run["t"] is not None:
                key, when = run["t"].date(), run["t"]
            else:
                skipped += 1
                continue
            g = groups.setdefault(key, {"time": when, "files": set(), "runs": {}})
            g["time"] = min(g["time"], when)
            g["files"].add(name)
            g["runs"].setdefault(run["well"], []).append(run)
            used.add(name)
            inserts[run["insert"]] += 1
    sessions = []
    for g in groups.values():
        wells = {}
        for wl, runs in g["runs"].items():
            last = runs[-1] if by_file else max(runs, key=lambda r: r["t"])    # 파일 안 순서 = 시간순
            wells[wl] = {"r": last["r"], "rb": last["rb"], "teer": last["teer"], "runs": len(runs)}
        sessions.append({"time": g["time"], "files": sorted(g["files"]), "wells": wells,
                         "label": g["time"].strftime("%m-%d %H:%M" if by_file else "%m-%d")})
    sessions.sort(key=lambda s: s["time"])
    best = next((p for p, _ in inserts.most_common() if p in PLATES), plate)
    return {"sessions": sessions, "csv": len(names), "files": len(used), "skipped": skipped, "plate": best}


def nice_ticks(lo, hi, n=5):
    """[lo, hi] 를 덮는 깔끔한 눈금 (1·2·5 × 10^k 간격)"""
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    return [first + step * k for k in range(math.ceil((hi - first) / step - 1e-9) + 1)]


def time_ticks(t0, t1, max_n=6):
    """t0~t1 을 덮는 깔끔한 시간 눈금 -> ([datetime, …], strftime 형식). 첫 눈금 ≤ t0, 마지막 눈금 ≥ t1 이라
    그대로 x 축 범위로 쓴다 (y 축의 nice_ticks 와 같은 방식). 하루 이상 간격이면 날짜만 쓴다."""
    span = max((t1 - t0).total_seconds(), 1.0)
    steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200,
             86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 91 * 86400]
    step = next((s for s in steps if span / s <= max_n), steps[-1])
    base = dt.datetime.combine(t0.date(), dt.time())                  # t0 날짜의 자정에 맞춤
    ticks = [base + dt.timedelta(seconds=math.floor((t0 - base).total_seconds() / step) * step)]
    while ticks[-1] < t1 or len(ticks) < 2:
        ticks.append(ticks[-1] + dt.timedelta(seconds=step))
    fmt = "%m-%d" if step >= 86400 else "%m-%d %H:%M" if ticks[0].date() != ticks[-1].date() else "%H:%M"
    return ticks, fmt


# ----------------------------------------------------------------------------- 플레이트 그림
def plate_wells(plate):
    rows, cols = PLATES[plate]
    return [f"{chr(65 + i)}{j + 1}" for i in range(rows) for j in range(cols)]     # A1, A2, … 가로 순서


def plate_layout(cw, ch, rows, cols):
    """SBS 규격 플레이트(127.8 × 85.5 mm, 왼쪽 위 A1 모서리 모따기)를 cw × ch 캔버스에 맞춘 배치
    -> (윤곽 x, y, 너비, 높이, 모따기 길이, well 칸 크기, 첫 칸 x, 첫 칸 y)"""
    pw = min(cw - 6, (ch - 6) * 127.8 / 85.5)
    ph = pw * 85.5 / 127.8
    px, py = (cw - pw) / 2, (ch - ph) / 2
    lx, ty = pw * 0.08, ph * 0.13                     # 행 이름(왼쪽)·열 번호(위) 자리
    gw, gh = pw - lx - pw * 0.04, ph - ty - ph * 0.06
    cell = min(gw / cols, gh / rows)
    return px, py, pw, ph, ph * 0.14, cell, px + lx + (gw - cell * cols) / 2, py + ty + (gh - cell * rows) / 2


def paint_plate(c, t, plate, look):
    """플레이트 윤곽 + 행·열 이름 + well 원. look(wid) -> (채움색, 테두리색, 테두리 굵기, 글자색, 아랫줄 글자)"""
    rows, cols = PLATES[plate]
    px, py, pw, ph, k, cell, x0, y0 = plate_layout(int(c["width"]), int(c["height"]), rows, cols)
    c.delete("all")
    c.create_polygon(px + k, py, px + pw, py, px + pw, py + ph, px, py + ph, px, py + k,
                     fill=t["btn"], outline=t["border"], width=2)
    small = ("Segoe UI", 8)
    for j in range(cols):
        c.create_text(x0 + cell * (j + 0.5), (py + y0) / 2, text=j + 1, fill=t["muted"], font=small)
    for i in range(rows):
        c.create_text((px + x0) / 2, y0 + cell * (i + 0.5), text=chr(65 + i), fill=t["muted"], font=small)
        for j in range(cols):
            wid = f"{chr(65 + i)}{j + 1}"
            fill, outline, width, ink, sub = look(wid)
            cx, cy, r = x0 + cell * (j + 0.5), y0 + cell * (i + 0.5), cell / 2 - 3
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=outline, width=width)
            if cell < 34:                      # 96-well 은 칸이 작아 글자를 넣지 않음
                continue
            c.create_text(cx, cy - 6 if sub else cy, text=wid, fill=ink, font=small)
            if sub:
                c.create_text(cx, cy + 7, text=sub, fill=ink, font=("Segoe UI", 8, "bold"))


def plate_hit(c, plate, x, y):
    """플레이트 그림의 클릭 위치 -> ("well", "B2") / ("row", "B") / ("col", "2") / None"""
    rows, cols = PLATES[plate]
    px, py, pw, ph, k, cell, x0, y0 = plate_layout(int(c["width"]), int(c["height"]), rows, cols)
    i, j = math.floor((y - y0) / cell), math.floor((x - x0) / cell)
    if 0 <= i < rows and 0 <= j < cols:
        return "well", f"{chr(65 + i)}{j + 1}"
    if 0 <= i < rows and px <= x < x0:
        return "row", chr(65 + i)
    if 0 <= j < cols and py <= y < y0:
        return "col", str(j + 1)
    return None


# ----------------------------------------------------------------------------- 업데이트 (GitHub 릴리스)
def version_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def ssl_context():
    """HTTPS 인증서 확인 = Windows 인증서 저장소 + (있으면) certifi 묶음. 새로 설치한 PC 처럼 저장소에
    루트 인증서가 아직 덜 받아져 있어도 GitHub 에 접속되게 한다. 확인 자체를 끄지는 않는다."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def latest_release():
    """GitHub 최신 릴리스 -> (버전 "1.2.0", exe 파일 정보 dict 또는 None, 릴리스 dict). 인터넷이 안 되면 예외."""
    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                                 headers={"User-Agent": "EVOM2-TEER-updater",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10, context=ssl_context()) as resp:
        rel = json.load(resp)
    assets = rel.get("assets") or []
    exe = (next((a for a in assets if a.get("name") == "EVOM2_TEER.exe"), None)
           or next((a for a in assets if str(a.get("name", "")).lower().endswith(".exe")), None))
    return str(rel.get("tag_name", "")).lstrip("vV"), exe, rel


def download(url, dest, size=None, digest=None, progress=None):
    """url -> dest. 받는 동안은 dest.part 에 쓰고, 크기와 SHA-256(GitHub 가 알려 준 값)이 맞을 때만 dest 로 바꾼다."""
    h, n, part = hashlib.sha256(), 0, dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "EVOM2-TEER-updater"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp, open(part, "wb") as f:
            while True:
                b = resp.read(1 << 16)
                if not b:
                    break
                f.write(b)
                h.update(b)
                n += len(b)
                if progress and n // (1 << 20) != (n - len(b)) // (1 << 20):      # 1 MB 마다
                    progress(n, size)
        if size and n != size:
            raise OSError(f"받은 크기가 다릅니다 ({n:,} / {size:,} bytes)")
        if digest and digest.startswith("sha256:") and h.hexdigest() != digest.split(":", 1)[1].lower():
            raise OSError("SHA-256 검사 실패 (받은 파일이 손상됨)")
        os.replace(part, dest)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def apply_update(target, extra):
    """업데이트 2단계 (새로 받은 EVOM2_TEER_update.exe 로 실행됨): 옛 프로그램이 완전히 닫혀 exe 를 놓아 줄 때까지
    기다렸다가 자신을 target(옛 exe 자리, 이름 그대로)에 복사하고 target 을 다시 실행한다."""
    for _ in range(240):                          # 최대 2분
        try:
            shutil.copyfile(sys.executable, target)
            break
        except OSError:
            time.sleep(0.5)
    else:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("업데이트 실패", f"{target}\n을(를) 바꾸지 못했습니다.\n\n프로그램을 모두 닫은 뒤 "
                             f"{sys.executable}\n을(를) 위 이름으로 직접 바꿔 쓰세요.")
        return
    subprocess.Popen([target, *extra], cwd=os.path.dirname(target))


# ----------------------------------------------------------------------------- 장비 읽기 스레드
class Reader(threading.Thread):
    def __init__(self, channel, terminal, out_q, simulate=False):
        super().__init__(daemon=True)
        self.channel, self.terminal, self.q, self.simulate = channel, terminal, out_q, simulate
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        try:
            self._run_sim() if self.simulate else self._run_daq()
        except Exception as e:
            self.q.put(("error", str(e)))

    def _run_sim(self):
        """실제 EVOM2 출력과 같은 0 V <-> V 의 12.5 Hz 사각파 (V = 1.0 V = 1000 Ω)"""
        import random
        base, i = 1.0, 0
        while not self._stop_evt.is_set():
            time.sleep(CHUNK / RATE)
            base += random.uniform(-0.002, 0.002)
            chunk = []
            for _ in range(CHUNK):
                high = (i // (RATE // 25)) % 2 == 0
                chunk.append((base if high else 0.0) + random.gauss(0, 0.003))
                i += 1
            self.q.put(("data", chunk))

    def _run_daq(self):
        term = {"RSE": TerminalConfiguration.RSE, "DIFF": TerminalConfiguration.DIFF}[self.terminal]
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(self.channel, terminal_config=term,
                                                 min_val=-10.0, max_val=10.0)
            task.timing.cfg_samp_clk_timing(RATE, sample_mode=AcquisitionType.CONTINUOUS,
                                            samps_per_chan=RATE * 10)
            task.start()
            while not self._stop_evt.is_set():
                data = task.read(number_of_samples_per_channel=CHUNK, timeout=3.0)
                self.q.put(("data", list(data)))


# ----------------------------------------------------------------------------- 시간 추적 그래프 창
class TrendWindow(tk.Toplevel):
    """여러 날에 걸친 측정을 well 별 패널로 보여 준다.
    한 점 = 한 회차(기본: CSV 파일 하나 = 하루, 시각 = 파일 수정 날짜)의 그 well 값 (기록들의 마지막 3초 평균).
    모든 패널이 x·y 축을 공유하고, 각 패널의 회색 선 = 같이 고른 다른 well (비교용).
    아래 표에 모든 값이 있고, 점 근처에 마우스를 대면 그 점의 값이 뜬다."""

    def __init__(self, app):
        super().__init__(app)
        self.app, self.hits, self.sessions, self.sel = app, [], [], []
        self.res = {"csv": 0, "files": 0, "skipped": 0}
        self.plate_type = app.well_var.get()
        self.title("시간 추적 그래프 · well 별 일간 추이")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1440, sw - 60)}x{min(940, sh - 90)}")
        self.minsize(980, 620)

        bar = ttk.Frame(self)                      # 필터 줄: 아래 그래프와 표 전체에 적용
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Label(bar, text="폴더:").pack(side="left")
        self.folder_var = tk.StringVar(value=app.folder_var.get().strip() or HERE)
        fe = ttk.Entry(bar, textvariable=self.folder_var, width=50)
        fe.pack(side="left", padx=4)
        fe.bind("<Return>", lambda e: self.reload())
        ttk.Button(bar, text="폴더 선택...", command=self.choose_folder).pack(side="left")
        ttk.Label(bar, text="    시간 기준:").pack(side="left")
        self.time_var = tk.StringVar(value=TREND_TIME[0])
        ttk.Combobox(bar, textvariable=self.time_var, values=TREND_TIME, width=15,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(bar, text="    Y축:").pack(side="left")
        self.y_var = tk.StringVar(value=list(TREND_Y)[0])
        ttk.Combobox(bar, textvariable=self.y_var, values=list(TREND_Y), width=14,
                     state="readonly").pack(side="left", padx=4)
        ttk.Button(bar, text="새로고침", command=self.reload).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="표 CSV 저장...", command=self.export).pack(side="left", padx=2)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.plate = tk.Canvas(left, width=300, height=200, highlightthickness=0)
        self.plate.pack(anchor="w")
        self.plate.bind("<Button-1>", self._plate_click)
        ttk.Label(left, style="Muted.TLabel", justify="left",
                  text="well 클릭 = 그래프에 넣기 / 빼기\n행 이름(A, B …)·열 번호 클릭 = 그 줄 전체\n"
                       "진한 색 = 그래프에 있음 · 연한 색 = 기록 있음").pack(anchor="w", pady=(2, 6))
        bf = ttk.Frame(left)
        bf.pack(anchor="w")
        ttk.Button(bf, text="기록 있는 well 전부", command=self.select_all).pack(side="left")
        ttk.Button(bf, text="모두 빼기", command=self.select_none).pack(side="left", padx=4)
        self.info = tk.StringVar()
        ttk.Label(left, textvariable=self.info, style="Muted.TLabel", justify="left",
                  wraplength=300).pack(anchor="w", pady=8)
        self.canvas = tk.Canvas(body, highlightthickness=1)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(10, 0))

        tf = ttk.Frame(self)                       # 표: 그래프의 모든 값 (행 = well, 열 = 회차)
        tf.pack(fill="x", padx=8, pady=6)
        self.tree = ttk.Treeview(tf, show="headings", height=7)
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        tf.columnconfigure(0, weight=1)

        self.time_var.trace_add("write", lambda *a: self.reload())
        self.y_var.trace_add("write", lambda *a: self.redraw())
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda e: self.canvas.delete("tip"))
        self.reload()

    # ---- 데이터 · 선택
    def order(self, wid):
        ids = plate_wells(self.plate_type)
        return (ids.index(wid) if wid in ids else len(ids), wid)

    def wells_with_data(self):
        return sorted({w for s in self.sessions for w in s["wells"]}, key=self.order)

    def reload(self):
        self.res = load_sessions(self.folder_var.get().strip(), self.time_var.get() == TREND_TIME[0],
                                 self.app.well_var.get())
        self.sessions, self.plate_type = self.res["sessions"], self.res["plate"]
        have = self.wells_with_data()
        self.sel = [w for w in self.sel if w in have] or have[:5]     # 처음에는 기록 있는 well 앞의 5개
        self.redraw()

    def choose_folder(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.folder_var.get() or HERE,
                                    title="측정 CSV 가 있는 폴더")
        if d:
            self.folder_var.set(os.path.normpath(d))
            self.reload()

    def select_all(self):
        self.sel = self.wells_with_data()
        self.redraw()

    def select_none(self):
        self.sel = []
        self.redraw()

    def _plate_click(self, e):
        hit = plate_hit(self.plate, self.plate_type, e.x, e.y)
        if not hit:
            return
        kind, v = hit
        group = [w for w in self.wells_with_data()
                 if (w == v if kind == "well" else w[0] == v if kind == "row" else w[1:] == v)]
        if not group:
            return
        if all(w in self.sel for w in group):
            self.sel = [w for w in self.sel if w not in group]
        else:
            self.sel = sorted(set(self.sel) | set(group), key=self.order)
        self.redraw()

    # ---- 그리기
    def redraw(self):
        t = THEMES[self.app.theme]
        self.configure(bg=t["bg"])
        self.plate.configure(bg=t["bg"])
        key, unit = TREND_Y[self.y_var.get()]
        have = set(self.wells_with_data())

        def look(wid):
            if wid in self.sel:
                return t["accent"], t["fg"], 2, t["on_accent"], None
            if wid in have:
                return t["sel"], t["border"], 1, t["fg"], None
            return t["panel"], t["border"], 1, t["muted"], None
        paint_plate(self.plate, t, self.plate_type, look)
        self._fill_table(key, t)
        self._draw_panels(key, unit, t)

    def _draw_panels(self, key, unit, t):
        c = self.canvas
        c.configure(bg=t["panel"], highlightbackground=t["border"])
        c.delete("all")
        self.hits = []
        series = {}
        for wl in self.sel:
            pts = [(s["time"], s["wells"][wl][key], s) for s in self.sessions
                   if wl in s["wells"] and s["wells"][wl][key] is not None]
            if pts:
                series[wl] = pts
        shown = list(series)[:TREND_MAX]
        notes = [f"CSV {self.res['csv']}개 중 측정 기록 {self.res['files']}개 · 회차 {len(self.sessions)}개 · "
                 f"기록 있는 well {len(self.wells_with_data())}개"]
        if self.time_var.get() == TREND_TIME[0]:
            notes.append("시각 = 각 CSV 의 수정 날짜 (하루 = 파일 하나. 엑셀에서 다시 저장하면 그 날짜로 바뀜)")
        if self.res["skipped"]:
            notes.append(f"well 이름이 아닌 라벨의 기록 {self.res['skipped']}개는 뺐습니다")
        if len(series) > TREND_MAX:
            notes.append(f"고른 well {len(series)}개 중 앞의 {TREND_MAX}개만 그렸습니다 (값은 아래 표에 모두 있음)")
        self.info.set("\n".join(notes))
        w, h = c.winfo_width(), c.winfo_height()
        if not shown or w < 200 or h < 150:
            c.create_text(w / 2, h / 2, fill=t["muted"], font=("Segoe UI", 10), justify="center",
                          text="왼쪽 플레이트에서 well 을 고르세요." if self.sessions else
                          "이 폴더에 측정 CSV 가 없습니다.\n[폴더 선택...]으로 측정 CSV 가 있는 폴더를 고르세요.")
            return
        allv = [v for wl in shown for _, v, _ in series[wl]]
        yt = nice_ticks(min(allv), max(allv), 4)
        t0 = min(tm for wl in shown for tm, _, _ in series[wl])
        t1 = max(tm for wl in shown for tm, _, _ in series[wl])
        if t1 <= t0:                              # 회차가 하나뿐이면 가운데에 놓음
            t0, t1 = t0 - dt.timedelta(hours=12), t1 + dt.timedelta(hours=12)
        n = len(shown)
        cols = 1 if n == 1 else 2 if n <= 6 else 3
        cw, ch = (w - 8) / cols, (h - 8) / math.ceil(n / cols)
        xt, fmt = time_ticks(t0, t1, max(2, int((cw - 100) // 80)))
        lo, hi = xt[0], xt[-1]
        span = (hi - lo).total_seconds()
        tick = ("Segoe UI", 8)
        for idx, wl in enumerate(shown):
            ox, oy = 4 + (idx % cols) * cw, 4 + (idx // cols) * ch
            L, R, T, B = ox + 56, ox + cw - 18, oy + 30, oy + ch - 24

            def X(tm):
                return L + (R - L) * (tm - lo).total_seconds() / span

            def Y(v):
                return B - (B - T) * (v - yt[0]) / (yt[-1] - yt[0])
            last_t, last_v, _ = series[wl][-1]
            c.create_text(ox + 10, oy + 14, anchor="w", text=wl, fill=t["fg"], font=("Segoe UI", 11, "bold"))
            c.create_text(R, oy + 14, anchor="e", fill=t["muted"], font=("Segoe UI", 9),
                          text=f"최근 {last_v:,.1f} {unit}  ({last_t:%m-%d})  ·  측정 {len(series[wl])}회")
            for v in yt:
                c.create_line(L, Y(v), R, Y(v), fill=t["grid"])
                c.create_text(L - 6, Y(v), anchor="e", text=f"{v:,g}", fill=t["muted"], font=tick)
            for k, tm in enumerate(xt):            # 양 끝 눈금 글자는 안쪽으로 붙여 y 축 글자와 겹치지 않게
                c.create_text(X(tm), B + 5, anchor="nw" if k == 0 else "ne" if k == len(xt) - 1 else "n",
                              text=tm.strftime(fmt), fill=t["muted"], font=tick)
            c.create_line(L, B, R, B, fill=t["border"])
            for other in shown:                    # 회색 선 = 같이 고른 다른 well (비교용)
                if other != wl and len(series[other]) > 1:
                    c.create_line(*[q for tm, v, _ in series[other] for q in (X(tm), Y(v))],
                                  fill=t["border"], width=1)
            xy = [(X(tm), Y(v)) for tm, v, _ in series[wl]]
            if len(xy) > 1:
                c.create_line(*[q for p in xy for q in p], fill=t["accent"], width=2,
                              joinstyle="round", capstyle="round")
            for (x, y), (tm, v, s) in zip(xy, series[wl]):
                c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=t["accent"], outline=t["panel"], width=2)
                self.hits.append((x, y, wl, tm, v, s))

    def _fill_table(self, key, t):
        tr = self.tree
        cols = ["well"] + [f"s{i}" for i in range(len(self.sessions))]
        tr.configure(columns=cols, displaycolumns=cols)
        tr.heading("well", text="well")
        tr.column("well", width=70, anchor="center", stretch=False)
        for i, s in enumerate(self.sessions):
            tr.heading(f"s{i}", text=s["label"])
            tr.column(f"s{i}", width=104, anchor="center", stretch=False)
        tr.delete(*tr.get_children())
        for wl in self.wells_with_data():
            tr.insert("", "end", values=[wl] + [fmt_val(s["wells"].get(wl, {}).get(key)) for s in self.sessions],
                      tags=("sel",) if wl in self.sel else ())
        tr.tag_configure("sel", background=t["sel"])

    def _hover(self, e):
        """가장 가까운 점의 값 표시 (24px 안이면 잡힘)"""
        c = self.canvas
        c.delete("tip")
        if not self.hits:
            return
        x, y, wl, tm, v, s = min(self.hits, key=lambda a: (a[0] - e.x) ** 2 + (a[1] - e.y) ** 2)
        if (x - e.x) ** 2 + (y - e.y) ** 2 > 24 ** 2:
            return
        t = THEMES[self.app.theme]
        unit = TREND_Y[self.y_var.get()][1]
        runs = s["wells"][wl]["runs"]
        lines = [f"{wl}  ·  {tm:%Y-%m-%d %H:%M}",
                 f"마지막 {TAIL_SEC:g}초 평균" + (f"  ·  이 회차 기록 {runs}회 중 마지막" if runs > 1 else ""),
                 "파일: " + ", ".join(s["files"])]
        bw = 300
        tx = x + 14 if x + 14 + bw < c.winfo_width() else x - 14 - bw
        ty = min(max(y - 30, 4), c.winfo_height() - 78)
        c.create_oval(x - 9, y - 9, x + 9, y + 9, outline=t["fg"], tags="tip")
        c.create_rectangle(tx, ty, tx + bw, ty + 74, fill=t["bg"], outline=t["border"], tags="tip")
        c.create_line(tx + 10, ty + 16, tx + 26, ty + 16, fill=t["accent"], width=2, tags="tip")
        c.create_text(tx + 32, ty + 16, anchor="w", text=f"{v:,.1f} {unit}", fill=t["fg"],
                      font=("Segoe UI", 10, "bold"), tags="tip")
        for k, line in enumerate(lines):
            c.create_text(tx + 10, ty + 34 + k * 14, anchor="w", text=line[:50], fill=t["muted"],
                          font=("Segoe UI", 8), tags="tip")

    def export(self, path=None):
        """표(행 = well, 열 = 회차)를 엑셀용 CSV 로 저장. 위 3줄 = 회차 시각 · 첫 회차부터 경과 일수 · 파일"""
        if not self.sessions:
            messagebox.showinfo("표 저장", "저장할 기록이 없습니다.", parent=self)
            return
        key, unit = TREND_Y[self.y_var.get()]
        if path is None:
            path = filedialog.asksaveasfilename(
                parent=self, initialdir=self.folder_var.get(), defaultextension=".csv",
                initialfile=f"{EXPORT_PREFIX}{dt.datetime.now():%Y%m%d_%H%M}.csv", filetypes=[("CSV", "*.csv")])
            if not path:
                return
        t0 = self.sessions[0]["time"]

        def cell(v):
            return "" if v is None else f"{v:.1f}"
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow([f"well \\ {self.y_var.get()}"] + [f"{s['time']:%Y-%m-%d %H:%M}" for s in self.sessions])
                w.writerow(["경과 (일)"] + [f"{(s['time'] - t0).total_seconds() / 86400:.3f}" for s in self.sessions])
                w.writerow(["파일"] + [" / ".join(s["files"]) for s in self.sessions])
                for wl in self.wells_with_data():
                    w.writerow([wl] + [cell(s["wells"].get(wl, {}).get(key)) for s in self.sessions])
        except OSError as e:
            messagebox.showerror("표 저장 실패", f"{path}\n{e}", parent=self)
            return
        self.app.status_var.set(f"시간 추적 표 저장: {path}")


# ----------------------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self, selftest=False):
        super().__init__()
        self.title(f"EVOM2 TEER 측정 (NI USB-6003)   v{APP_VERSION}")
        self.geometry("1160x880")
        self.minsize(1060, 740)

        self.q = queue.Queue()
        self.reader = None
        self.chunks = []           # (mean, sd, amp, is_square) per 0.1 s
        self.last_samples = []
        self.blank = None
        self.blank_cap = None      # blank 측정 진행 상태
        self.rec = None            # 기록 진행 상태
        self.points = {}           # 지금 CSV 의 well 별 기록 요약 (read_runs, 시간순)
        self.trend = None          # 시간 추적 그래프 창
        self.latest = None         # GitHub 에서 확인한 새 버전
        self.updating = False      # [업데이트 확인]부터 교체까지 (그동안 기록·blank 시작 막음)
        self.pending_marks = []    # 아직 CSV 에 못 쓴 중단 표시 [(파일, run_id, 라벨), …] (파일이 잠겨 있었음)
        self.closing = False
        self._last_err = 0.0
        self.auto_update = "--selftest-update" in sys.argv     # 업데이트 자동 시험 (묻지 않고 진행)
        self.settings = load_settings()
        self.cal = self.settings["cal"]
        self.theme = self.settings["theme"]

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._build()
        self.apply_theme(self.theme)
        self.reload_points()
        self.refresh_devices()
        self.after(50, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._cleanup_update()
        if selftest:
            self.after(300, self._selftest)
        elif self.auto_update:
            self.after(500, self.check_update)
        else:
            self.after(3000, self._startup_update_check)

    # ================= 화면 구성
    def _build(self):
        pad = {"padx": 5, "pady": 3}

        # ---- 1. 연결
        top = ttk.LabelFrame(self, text="1. 장비 연결")
        top.pack(fill="x", **pad)
        ttk.Label(top, text="장비 / 채널:").grid(row=0, column=0, sticky="e", **pad)
        self.dev_var = tk.StringVar()
        self.dev_cb = ttk.Combobox(top, textvariable=self.dev_var, width=34, state="readonly")
        self.dev_cb.grid(row=0, column=1, **pad)
        ttk.Button(top, text="새로고침", command=self.refresh_devices).grid(row=0, column=2, **pad)
        ttk.Label(top, text="입력 방식:").grid(row=0, column=3, sticky="e", **pad)
        self.term_var = tk.StringVar(value=self.settings["terminal"])
        ttk.Combobox(top, textvariable=self.term_var, values=["DIFF", "RSE"], width=6,
                     state="readonly").grid(row=0, column=4, **pad)
        self.term_var.trace_add("write", lambda *a: self._term_hint())
        self.conn_btn = ttk.Button(top, text="연결", command=self.toggle_connect, width=10)
        self.conn_btn.grid(row=0, column=5, **pad)
        tb = ttk.Frame(top)
        tb.grid(row=0, column=6, sticky="e", **pad)
        self.upd_btn = ttk.Button(tb, text="업데이트 확인", command=self.check_update, width=15)
        self.upd_btn.pack(side="left", padx=(0, 4))
        self.theme_btn = ttk.Button(tb, text="Claude Code UI", command=self.toggle_theme, width=16)
        self.theme_btn.pack(side="left")
        top.columnconfigure(6, weight=1)

        ttk.Label(top, text="EVOM2 모드:").grid(row=1, column=0, sticky="e", **pad)
        mf = ttk.Frame(top)
        mf.grid(row=1, column=1, columnspan=6, sticky="w")
        self.mode_var = tk.StringVar(value=list(MODES)[0])
        ttk.Combobox(mf, textvariable=self.mode_var, values=list(MODES), width=16,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(mf, text="   신호 처리:").pack(side="left")
        self.proc_var = tk.StringVar(value=PROCS[0])
        ttk.Combobox(mf, textvariable=self.proc_var, values=PROCS, width=11,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(mf, text="   보정 (EVOM2 화면값):").pack(side="left")
        self.calin_var = tk.StringVar(value="1000")
        ttk.Entry(mf, textvariable=self.calin_var, width=7).pack(side="left", padx=4)
        self.cal_btn = ttk.Button(mf, text="보정", command=self.calibrate, state="disabled")
        self.cal_btn.pack(side="left", padx=2)
        ttk.Button(mf, text="해제", command=self.clear_cal).pack(side="left", padx=2)
        self.cal_var = tk.StringVar()
        ttk.Label(mf, textvariable=self.cal_var, style="Muted.TLabel").pack(side="left", padx=6)
        self.mode_var.trace_add("write", lambda *a: self._show_cal())
        self._show_cal()

        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(top, textvariable=self.status_var, style="Accent.TLabel").grid(
            row=2, column=0, columnspan=7, sticky="w", **pad)

        # ---- 2. 실시간 + 플레이트 그림
        row2 = ttk.Frame(self)
        row2.pack(fill="x")
        pl = ttk.LabelFrame(row2, text="Well 선택  (클릭 = 선택,  더블클릭·Enter = 기록 시작)")
        pl.pack(side="right", fill="y", **pad)
        self.plate = tk.Canvas(pl, width=340, height=226, highlightthickness=0)
        self.plate.pack(padx=6, pady=(4, 0))
        self.plate.bind("<Button-1>", self._plate_click)
        self.plate.bind("<Double-Button-1>", lambda e: self.start_record())
        self.plate.bind("<Return>", lambda e: self.start_record())
        ttk.Label(pl, text=f"진한 색 = 오늘 측정 · 연한 색 = 이전 기록 · 숫자 = 마지막 {TAIL_SEC:g}초 평균 TEER",
                  style="Muted.TLabel").pack(anchor="w", padx=6)
        pf = ttk.Frame(pl)
        pf.pack(fill="x", padx=6, pady=3)
        self.advance_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(pf, text="기록이 끝나면 다음 well 로", variable=self.advance_var).pack(side="left")
        ttk.Button(pf, text="시간 추적 그래프", command=self.open_trend).pack(side="right")
        mid = ttk.LabelFrame(row2, text="2. 실시간 값 (최근 1초)")
        mid.pack(side="left", fill="both", expand=True, **pad)
        vf = ttk.Frame(mid)
        vf.pack(fill="x")
        self.value_var = tk.StringVar(value="---")
        ttk.Label(vf, textvariable=self.value_var, style="Big.TLabel").pack(side="left", padx=(20, 10))
        self.teer_var = tk.StringVar(value="")
        ttk.Label(vf, textvariable=self.teer_var, style="Big2.TLabel").pack(side="left", padx=10)
        self.sub_var = tk.StringVar(value="")
        ttk.Label(mid, textvariable=self.sub_var, style="Muted.TLabel").pack(anchor="w", padx=20)
        self.canvas = tk.Canvas(mid, height=150, highlightthickness=1)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=4)

        # ---- 3. blank
        bl = ttk.LabelFrame(self, text="3. blank (세포 없는 insert 의 저항)   →  TEER = (R − blank) × 면적")
        bl.pack(fill="x", **pad)
        bf = ttk.Frame(bl)
        bf.pack(fill="x", **pad)
        self.blank_btn = ttk.Button(bf, text="blank 측정 시작", command=self.toggle_blank,
                                    state="disabled", width=16)
        self.blank_btn.pack(side="left", padx=4)
        ttk.Label(bf, text="측정 시간").pack(side="left")
        self.blank_sec = tk.StringVar(value=str(self.settings["blank_sec"]))
        ttk.Spinbox(bf, from_=1, to=600, textvariable=self.blank_sec, width=5).pack(side="left", padx=3)
        ttk.Label(bf, text="초 평균").pack(side="left")
        ttk.Label(bf, text="      blank 값(Ω):").pack(side="left")
        self.blank_var = tk.StringVar(value="")
        e = ttk.Entry(bf, textvariable=self.blank_var, width=9)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self._blank_typed())
        ttk.Button(bf, text="입력값 적용", command=self._blank_typed).pack(side="left", padx=2)
        ttk.Button(bf, text="지우기", command=self.clear_blank).pack(side="left", padx=2)
        self.blank_info = tk.StringVar()
        ttk.Label(bf, textvariable=self.blank_info, style="Muted.TLabel").pack(side="left", padx=10)
        self.clear_blank()

        # ---- 4. 기록
        rec = ttk.LabelFrame(self, text="4. 기록")
        rec.pack(fill="x", **pad)
        r1 = ttk.Frame(rec)
        r1.pack(fill="x", **pad)
        ttk.Label(r1, text="Insert:").pack(side="left")
        self.well_var = tk.StringVar(value=self.settings["well"])
        ttk.Combobox(r1, textvariable=self.well_var, values=[w for w, _ in WELLS], width=9,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(r1, text="막 면적(cm²):").pack(side="left")
        self.area_var = tk.StringVar(value=f"{self.settings['areas'][self.well_var.get()]:g}")
        ttk.Entry(r1, textvariable=self.area_var, width=7).pack(side="left", padx=4)
        ttk.Label(r1, text="(Corning 기본값, 수정 가능)", style="Muted.TLabel").pack(side="left")
        self.well_var.trace_add("write", lambda *a: self._well_changed())
        self.area_var.trace_add("write", lambda *a: self._area_changed())
        ttk.Label(r1, text="      라벨:").pack(side="left")
        self.label_var = tk.StringVar(value="A1")
        ent = ttk.Entry(r1, textvariable=self.label_var, width=12)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda ev: self.start_record())

        r2 = ttk.Frame(rec)
        r2.pack(fill="x", **pad)
        ttk.Label(r2, text="기록 방식:").pack(side="left")
        self.recmode_var = tk.StringVar(value=REC_MODES[0])
        ttk.Combobox(r2, textvariable=self.recmode_var, values=REC_MODES, width=24,
                     state="readonly").pack(side="left", padx=4)
        self.recmode_var.trace_add("write", lambda *a: self._show_rec_opts())
        self.opt_frame = ttk.Frame(r2)
        self.opt_frame.pack(side="left", padx=6)
        self.rec_btn = ttk.Button(r2, text="기록", command=self.start_record, state="disabled", width=8)
        self.rec_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(r2, text="정지", command=self.stop_record, state="disabled", width=8)
        self.stop_btn.pack(side="left", padx=2)
        self.rec_info = tk.StringVar(value="")
        ttk.Label(r2, textvariable=self.rec_info, style="Rec.TLabel").pack(side="left", padx=8)
        self.semi_sec = tk.StringVar(value="10")
        self.auto_meas = tk.StringVar(value="10")
        self.auto_rest = tk.StringVar(value="5")
        self.auto_count = tk.StringVar(value="6")
        self._show_rec_opts()

        # ---- 5. 저장 위치
        sv = ttk.LabelFrame(self, text="5. 저장 위치")
        sv.pack(fill="x", **pad)
        ttk.Label(sv, text="폴더:").grid(row=0, column=0, sticky="e", **pad)
        self.folder_var = tk.StringVar(value=self.settings["folder"])
        ttk.Entry(sv, textvariable=self.folder_var, width=80).grid(row=0, column=1, sticky="we", **pad)
        ttk.Button(sv, text="폴더 변경...", command=self.choose_folder).grid(row=0, column=2, **pad)
        ttk.Button(sv, text="폴더 열기", command=self.open_folder).grid(row=0, column=3, **pad)
        ttk.Label(sv, text="파일명:").grid(row=1, column=0, sticky="e", **pad)
        default_name = self.settings["filename"] or f"teer_log_{dt.date.today():%Y%m%d}.csv"
        self.file_var = tk.StringVar(value=default_name)
        ttk.Entry(sv, textvariable=self.file_var, width=80).grid(row=1, column=1, sticky="we", **pad)
        ttk.Label(sv, text="(.csv, 같은 이름이면 이어서 기록. 하루 = 파일 하나)", style="Muted.TLabel").grid(
            row=1, column=2, columnspan=2, sticky="w", **pad)
        sv.columnconfigure(1, weight=1)

        # ---- 6. 표
        bot = ttk.LabelFrame(self, text=f"6. 기록된 데이터 (최근 500행, 강조 행 = 기록마다 마지막 {TAIL_SEC:g}초 평균)")
        bot.pack(fill="both", expand=True, **pad)
        cols = ("시간", "경과(s)", "라벨", "R (Ω)", "R−blank", "insert", "면적", "TEER (Ω·cm²)", "방법")
        self.tree = ttk.Treeview(bot, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (140, 60, 100, 80, 80, 70, 60, 110, 90)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(bot, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        sb.pack(side="left", fill="y", pady=4)

        # 라벨(= 선택한 well)이나 저장 파일이 바뀌면 플레이트 그림을 다시 그림
        self.label_var.trace_add("write", lambda *a: self._label_changed())
        for v in (self.folder_var, self.file_var):
            v.trace_add("write", lambda *a: self.reload_points())

    def _show_rec_opts(self):
        for w in self.opt_frame.winfo_children():
            w.destroy()
        m = self.recmode_var.get()
        f = self.opt_frame
        if m == REC_MODES[1]:
            ttk.Label(f, text="기록 시간").pack(side="left")
            ttk.Spinbox(f, from_=1, to=3600, textvariable=self.semi_sec, width=6).pack(side="left", padx=3)
            ttk.Label(f, text="초").pack(side="left")
        elif m == REC_MODES[2]:
            ttk.Label(f, text="측정").pack(side="left")
            ttk.Spinbox(f, from_=1, to=3600, textvariable=self.auto_meas, width=5).pack(side="left", padx=3)
            ttk.Label(f, text="초 → 휴식(전극 이동)").pack(side="left")
            ttk.Spinbox(f, from_=0, to=600, textvariable=self.auto_rest, width=5).pack(side="left", padx=3)
            ttk.Label(f, text="초,").pack(side="left")
            ttk.Spinbox(f, from_=1, to=96, textvariable=self.auto_count, width=4).pack(side="left", padx=3)
            ttk.Label(f, text="개 well").pack(side="left")
        else:
            ttk.Label(f, text=f"초당 {ROWS_PER_SEC}행씩, 정지 누를 때까지", style="Muted.TLabel").pack(side="left")

    # ================= 테마
    def toggle_theme(self):
        self.apply_theme("light" if self.theme == "claude" else "claude")

    def apply_theme(self, name):
        self.theme = name
        self.settings["theme"] = name
        t = THEMES[name]
        s = self.style
        self.configure(bg=t["bg"])
        s.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"],
                    bordercolor=t["border"], lightcolor=t["bg"], darkcolor=t["bg"],
                    troughcolor=t["panel"], font=("Segoe UI", 10))
        s.configure("TFrame", background=t["bg"])
        s.configure("TLabel", background=t["bg"], foreground=t["fg"])
        s.configure("Muted.TLabel", foreground=t["muted"])
        s.configure("Accent.TLabel", foreground=t["accent"])
        s.configure("Rec.TLabel", foreground=t["rec"])
        s.configure("Big.TLabel", foreground=t["accent"], font=("Segoe UI", 34, "bold"))
        s.configure("Big2.TLabel", foreground=t["accent2"], font=("Segoe UI", 24, "bold"))
        s.configure("TLabelframe", background=t["bg"], bordercolor=t["border"])
        s.configure("TLabelframe.Label", background=t["bg"], foreground=t["accent"],
                    font=("Segoe UI", 10, "bold"))
        s.configure("TButton", background=t["btn"], foreground=t["fg"], bordercolor=t["border"],
                    focuscolor=t["accent"])
        s.map("TButton", background=[("active", t["btn_active"]), ("disabled", t["bg"])],
              foreground=[("disabled", t["muted"])])
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(w, fieldbackground=t["field"], foreground=t["fg"], background=t["btn"],
                        arrowcolor=t["fg"], insertcolor=t["fg"], bordercolor=t["border"])
            s.map(w, fieldbackground=[("readonly", t["field"]), ("disabled", t["bg"])],
                  foreground=[("disabled", t["muted"])], selectbackground=[("!focus", t["field"])])
        s.configure("Treeview", background=t["panel"], fieldbackground=t["panel"], foreground=t["fg"],
                    bordercolor=t["border"])
        s.configure("Treeview.Heading", background=t["btn"], foreground=t["fg"], bordercolor=t["border"])
        s.map("Treeview", background=[("selected", t["sel"])])
        s.configure("TScrollbar", background=t["btn"], troughcolor=t["panel"], arrowcolor=t["fg"])
        self.option_add("*TCombobox*Listbox.background", t["field"])
        self.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", t["sel"])
        s.configure("TCheckbutton", background=t["bg"], foreground=t["fg"], indicatorbackground=t["field"],
                    indicatorforeground=t["fg"])
        s.map("TCheckbutton", background=[("active", t["bg"])])
        self.canvas.configure(bg=t["panel"], highlightbackground=t["border"])
        self.plate.configure(bg=t["bg"])
        self.tree.tag_configure("summary", background=t["sel"], foreground=t["fg"],
                                font=("Segoe UI", 10, "bold"))
        self.theme_btn.config(text="기본 UI" if name == "claude" else "Claude Code UI")
        save_settings(self.settings)
        if self.chunks:
            self._draw_chart()
        self._label_changed()
        if self.trend and self.trend.winfo_exists():
            self.trend.redraw()

    # ================= 설정
    def _term_hint(self):
        self.settings["terminal"] = self.term_var.get()
        if self.term_var.get() == "DIFF":
            self.status_var.set("DIFF 배선: 노란 선→2번(AI0+), 검은 선→3번(AI4=AI0−)과 1번(AI GND) 둘 다")
        else:
            self.status_var.set("RSE 배선: 노란 선→2번(AI0), 검은 선→1번(AI GND)")

    def _well_changed(self):
        w = self.well_var.get()
        self.settings["well"] = w
        self.area_var.set(f"{self.settings['areas'].get(w, 0):g}")
        self.draw_plate()

    def _area_changed(self):
        try:
            self.settings["areas"][self.well_var.get()] = float(self.area_var.get())
            save_settings(self.settings)
        except ValueError:
            pass

    def _persist(self):
        self.settings["cal"] = self.cal
        self.settings["folder"] = self.folder_var.get()
        self.settings["filename"] = self.file_var.get()
        self.settings["terminal"] = self.term_var.get()
        try:
            self.settings["blank_sec"] = int(float(self.blank_sec.get()))
        except ValueError:
            pass
        save_settings(self.settings)

    def csv_path(self):
        name = self.file_var.get().strip() or f"teer_log_{dt.date.today():%Y%m%d}.csv"
        if not name.lower().endswith(".csv"):
            name += ".csv"
        return os.path.join(self.folder_var.get().strip() or HERE, name)

    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or HERE, title="CSV 저장 폴더")
        if d:
            self.folder_var.set(os.path.normpath(d))
            self._persist()

    def open_folder(self):
        subprocess.Popen(["explorer", self.folder_var.get() or HERE])

    # ================= 플레이트 그림 · 측정 기록 불러오기
    def well_ids(self):
        return plate_wells(self.well_var.get())

    def reload_points(self):
        # ponytail: 기록이 끝날 때마다 CSV 전체를 다시 읽는다 (수 MB 까지는 수십 ms).
        #           파일이 수십 MB 로 커져 느려지면 새로 쓴 행만 더하는 방식으로 바꿀 것.
        self.points = {}
        for run in read_runs(self.csv_path()):
            self.points.setdefault(run["well"], []).append(run)
        self._label_changed()

    def _label_changed(self):
        self.draw_plate()

    def open_trend(self):
        if self.trend and self.trend.winfo_exists():
            self.trend.lift()
            self.trend.reload()
        else:
            self.trend = TrendWindow(self)

    def draw_plate(self):
        """진한 색 = 오늘 측정한 well, 연한 색 = 이전 기록만 있는 well, 굵은 테두리 = 선택한 well,
        숫자 = 가장 최근 기록의 마지막 3초 평균 TEER (TEER 가 없으면 R)"""
        t, today = THEMES[self.theme], dt.date.today()
        sel = self.label_var.get().strip().upper()

        def look(wid):
            pts = self.points.get(wid)
            done = bool(pts) and pts[-1]["t"] is not None and pts[-1]["t"].date() == today
            sub = None
            if pts:
                v = pts[-1]["teer"] if pts[-1]["teer"] is not None else pts[-1]["r"]
                sub = None if v is None else f"{v:,.0f}"
            return (t["accent"] if done else t["sel"] if pts else t["panel"],
                    t["fg"] if wid == sel else t["border"], 3 if wid == sel else 1,
                    t["on_accent"] if done else t["fg"] if pts else t["muted"], sub)
        paint_plate(self.plate, t, self.well_var.get(), look)

    def _plate_click(self, e):
        hit = plate_hit(self.plate, self.well_var.get(), e.x, e.y)
        if hit and hit[0] == "well" and not self.rec:             # 기록 중에는 well 을 바꾸지 않음
            self.label_var.set(hit[1])
        self.plate.focus_set()                                    # 이어서 Enter 로 기록 시작

    # ================= 장비
    def refresh_devices(self):
        items, err = [], NIDAQMX_ERR
        if NIDAQMX_OK:
            try:
                for d in System.local().devices:
                    for ch in d.ai_physical_chans:
                        items.append(f"{ch.name}  [{d.product_type}]")
            except Exception as e:                 # 드라이버(NI-DAQmx)가 없으면 여기서 난다
                err = f"{type(e).__name__}: {e}"
        items.append(SIM_NAME)
        self.dev_cb["values"] = items
        self.dev_var.set(items[0])
        n = len(items) - 1
        if err:
            self.status_var.set(f"NI-DAQmx 드라이버를 찾지 못했습니다 → 드라이버를 설치하고 재부팅하세요 ({err[:70]})")
        else:
            self.status_var.set(f"NI 채널 {n}개 발견" if n else "NI 장비가 안 보입니다. USB 확인 후 새로고침")

    def toggle_connect(self):
        self.disconnect() if self.reader else self.connect()

    def connect(self):
        sel = self.dev_var.get()
        if not sel:
            messagebox.showwarning("장비", "장비를 선택하세요.")
            return
        simulate = sel == SIM_NAME
        channel = sel.split()[0]
        self.chunks.clear()
        self.last_samples.clear()
        self.reader = Reader(channel, self.term_var.get(), self.q, simulate)
        self.reader.start()
        self.conn_btn.config(text="해제")
        for b in (self.rec_btn, self.blank_btn, self.cal_btn):
            b.config(state="normal")
        self.dev_cb.config(state="disabled")
        self.status_var.set(f"연결됨: {channel} ({self.term_var.get()})" + (" (시뮬레이션)" if simulate else ""))

    def disconnect(self):
        self.stop_record()
        self._blank_finish(cancel=True)
        if self.reader:
            self.reader.stop()
            self.reader.join(timeout=4)
            self.reader = None
        self.conn_btn.config(text="연결")
        for b in (self.rec_btn, self.blank_btn, self.cal_btn, self.stop_btn):
            b.config(state="disabled")
        self.dev_cb.config(state="readonly")
        self.status_var.set("연결 해제")
        self.value_var.set("---")
        self.teer_var.set("")
        self.sub_var.set("")

    # ================= 오류 처리 (창 모드 exe 는 콘솔이 없어서, 오류를 창과 파일로 알린다)
    def report_callback_exception(self, exc, val, tb):
        self._report_error(val, "".join(traceback.format_exception(exc, val, tb)))

    def _report_error(self, e, detail=None):
        detail = detail or "".join(traceback.format_exception(type(e), e, e.__traceback__))
        try:
            with open(os.path.join(HERE, "evom2_error.log"), "a", encoding="utf-8") as f:
                f.write(f"--- {dt.datetime.now():%Y-%m-%d %H:%M:%S}  v{APP_VERSION}\n{detail}\n")
        except OSError:
            pass
        try:
            self.status_var.set(f"오류: {e}  (자세한 내용: evom2_error.log)")
        except tk.TclError:
            return
        if time.time() - self._last_err > 30:     # 같은 오류가 반복돼도 30초에 한 번만 창으로
            self._last_err = time.time()
            messagebox.showerror("오류", f"{e}\n\n측정은 계속됩니다. 자세한 내용은 프로그램 폴더의 evom2_error.log 에 있습니다.")

    # ================= 데이터
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind != "data":
                    self._on_message(kind, payload)
                    if self.closing:
                        return
                    continue
                self.chunks.append(analyze(payload))
                self.last_samples.append(payload)
                if len(self.chunks) > 600:
                    self.chunks = self.chunks[-600:]
                if len(self.last_samples) > WINDOW:
                    self.last_samples = self.last_samples[-WINDOW:]
                if self.blank_cap:
                    self._blank_on_chunk()
                if self.rec:
                    self._rec_on_chunk()
        except queue.Empty:
            pass
        except Exception as e:                     # 처리 중 오류가 나도 측정 루프는 멈추지 않음
            self._report_error(e)
        finally:
            if not self.closing:
                try:
                    if self.reader and self.chunks:
                        self._update_display()
                except Exception as e:
                    self._report_error(e)
                self.after(100, self._poll)

    def _on_message(self, kind, payload):
        if kind == "error":
            r = self.rec
            if r and r["phase"] == "measure":
                self.stop_record(aborted=True)        # 재던 well 은 정상 종료로 치지 않음
            elif r:                                   # 휴식 중: 끝난 well 은 그대로, 다음 well 부터 다시
                self.stop_record()
                self.rec_info.set(f"중단됨: 휴식 중 DAQ 오류 — {self.label_var.get()} 부터 다시 시작하세요"
                                  f" · 끝난 well {r['done']}개")
            self.disconnect()
            messagebox.showerror("DAQ 오류", payload)
        elif kind == "upd_checked":
            self._on_upd_checked(*payload)
        elif kind == "upd_progress":
            n, total = payload
            self.status_var.set(f"업데이트 받는 중… {n / 1e6:.1f}" + (f" / {total / 1e6:.1f}" if total else "") + " MB")
        elif kind == "upd_ready":
            self._on_upd_ready(payload)
        elif kind == "upd_failed":
            self._end_update(f"업데이트 실패: {payload}")
            if self.auto_update:
                self._update_test_report(f"FAIL download: {payload}", close=True)
            else:
                messagebox.showerror("업데이트 실패", str(payload))

    def _scale(self):
        factor, unit = MODES[self.mode_var.get()]
        return factor * self.cal[self.mode_var.get()], unit

    def _pick(self, mean, sd, amp, is_square):
        p = self.proc_var.get()
        use_amp = p == PROCS[1] or (p == PROCS[0] and is_square)
        return (amp, "사각파 진폭") if use_amp else (mean, "평균")

    def _current(self):
        """(사용 전압 V, 표준편차 V, 보정된 환산값, 단위, 방법)"""
        samples = [s for chunk in self.last_samples for s in chunk]
        if not samples:
            return None
        mean, sd, amp, is_square = analyze(samples)
        v, method = self._pick(mean, sd, amp, is_square)
        scale, unit = self._scale()
        return v, sd, v * scale, unit, method

    def _area(self):
        try:
            return float(self.area_var.get())
        except ValueError:
            return 0.0

    def _teer(self, val, area):
        if MODES[self.mode_var.get()][1] != "Ω":
            return None
        return (val - (self.blank or 0.0)) * area

    def _update_display(self):
        cur = self._current()
        if not cur:
            return
        v, sd, val, unit, method = cur
        self.value_var.set(f"{val:,.1f} {unit}")
        area = self._area()
        t = self._teer(val, area)
        self.teer_var.set("" if t is None else f"TEER {t:,.1f} Ω·cm²")
        parts = [f"{v:.4f} V ({method}, 샘플 sd {sd * 1000:.0f} mV)"]
        if self.blank is not None:
            parts.append(f"R−blank = {val - self.blank:,.1f} {unit}")
        if t is not None:
            parts.append(f"TEER = (R − blank) × {area:g} cm² ({self.well_var.get()})"
                         + ("" if self.blank is not None else "  [blank 미차감]"))
        self.sub_var.set("   |   ".join(parts))
        self._draw_chart()

    def _draw_chart(self):
        """왼쪽 축: EVOM 값(Ω 또는 mV), 오른쪽 축: TEER(Ω·cm²). 최근 30초."""
        c = self.canvas
        c.delete("all")
        t = THEMES[self.theme]
        w, h = c.winfo_width(), c.winfo_height()
        pts = self.chunks[-300:]
        if len(pts) < 2 or w < 10:
            return
        scale, unit = self._scale()
        vals = [self._pick(*p)[0] * scale for p in pts]
        area = self._area()
        teers = [self._teer(x, area) for x in vals] if unit == "Ω" else None
        L, R, T, B = 52, 70, 10, 18            # 여백
        pw, ph = w - L - R, h - T - B
        if pw < 20 or ph < 20:
            return

        def rng(a):
            lo, hi = min(a), max(a)
            if hi - lo < 1e-9:
                lo, hi = lo - 1, hi + 1
            m = (hi - lo) * 0.1
            return lo - m, hi + m

        n = len(vals)
        xs = [L + pw * i / (n - 1) for i in range(n)]
        # 격자
        for k in range(5):
            y = T + ph * k / 4
            c.create_line(L, y, L + pw, y, fill=t["grid"])
        c.create_rectangle(L, T, L + pw, T + ph, outline=t["border"])
        # EVOM 값 (왼쪽 축)
        lo, hi = rng(vals)
        ys = [T + ph - ph * (v - lo) / (hi - lo) for v in vals]
        c.create_line(*[xy for p in zip(xs, ys) for xy in p], fill=t["accent"], width=2)
        for k in range(5):
            v = hi - (hi - lo) * k / 4
            c.create_text(L - 4, T + ph * k / 4, anchor="e", text=f"{v:,.0f}", fill=t["accent"],
                          font=("Segoe UI", 8))
        c.create_text(L + 6, T + 4, anchor="nw", text=f"EVOM ({unit})", fill=t["accent"],
                      font=("Segoe UI", 9, "bold"))
        # TEER (오른쪽 축)
        if teers:
            lo2, hi2 = rng(teers)
            ys2 = [T + ph - ph * (v - lo2) / (hi2 - lo2) for v in teers]
            c.create_line(*[xy for p in zip(xs, ys2) for xy in p], fill=t["accent2"], width=2, dash=(5, 3))
            for k in range(5):
                v = hi2 - (hi2 - lo2) * k / 4
                c.create_text(L + pw + 4, T + ph * k / 4, anchor="w", text=f"{v:,.0f}", fill=t["accent2"],
                              font=("Segoe UI", 8))
            c.create_text(L + pw - 6, T + 4, anchor="ne",
                          text="TEER (Ω·cm²)" + ("" if self.blank is not None else " [blank 미차감]"),
                          fill=t["accent2"], font=("Segoe UI", 9, "bold"))
        c.create_text(L + pw / 2, h - 3, anchor="s", text="최근 30초", fill=t["muted"], font=("Segoe UI", 8))

    # ================= 보정
    def _show_cal(self):
        k = self.cal[self.mode_var.get()]
        self.cal_var.set("보정 없음" if abs(k - 1.0) < 1e-6 else f"×{k:.4f} 적용 중")

    def calibrate(self):
        cur = self._current()
        if not cur:
            return
        try:
            target = float(self.calin_var.get())
        except ValueError:
            messagebox.showwarning("보정", "EVOM2 화면에 표시된 숫자를 입력하세요 (예: 1000).")
            return
        factor, unit = MODES[self.mode_var.get()]
        raw = cur[0] * factor
        if abs(raw) < 1e-6:
            messagebox.showwarning("보정", "DAQ 값이 0입니다. 배선과 EVOM2 전원을 확인하세요.")
            return
        k = target / raw
        if not 0.05 < abs(k) < 20:
            messagebox.showwarning("보정", f"계수 {k:.3f}가 비정상적으로 큽니다. 배선/모드를 확인하세요.")
            return
        self.cal[self.mode_var.get()] = k
        self._persist()
        self.clear_blank()
        self._show_cal()
        self.status_var.set(f"보정 완료: DAQ {raw:.1f} → {target:.1f} {unit} (×{k:.4f}). blank 다시 측정하세요.")

    def clear_cal(self):
        self.cal[self.mode_var.get()] = 1.0
        self._persist()
        self._show_cal()
        self.status_var.set("보정 해제 (매뉴얼 스케일 1 Ω = 1 mV)")

    # ================= blank (N초 평균)
    def toggle_blank(self):
        if self.blank_cap:
            self._blank_finish(cancel=True)
        else:
            self.start_blank()

    def _blocked_by_update(self):
        if self.updating:
            self.status_var.set("업데이트 진행 중에는 기록을 시작할 수 없습니다. 업데이트가 끝나거나 취소된 뒤 다시 누르세요.")
        return self.updating

    def _roll_daily_name(self):
        """기본 파일명(teer_log_YYYYMMDD.csv)이면 오늘 날짜로 맞춤 — 프로그램을 켜 둔 채 자정이 지난 경우.
        기록을 시작할 때 한 번만 하므로 자동 기록 한 번은 한 파일에 들어간다."""
        name, today = self.file_var.get().strip(), f"teer_log_{dt.date.today():%Y%m%d}.csv"
        if re.fullmatch(r"teer_log_\d{8}\.csv", name) and name != today:
            self.file_var.set(today)

    def start_blank(self):
        if not self.reader or self.rec or self._blocked_by_update():
            return
        try:
            sec = max(1, int(float(self.blank_sec.get())))
        except ValueError:
            messagebox.showwarning("blank", "측정 시간은 숫자(초)로 입력하세요.")
            return
        self._roll_daily_name()
        self.blank_cap = {"target": sec * 10, "n": 0, "vals": [], "sec": sec}
        self.blank_btn.config(text="blank 측정 취소")
        self.rec_btn.config(state="disabled")
        self.blank_info.set(f"blank 측정 중… 0 / {sec}초")

    def _blank_on_chunk(self):
        b = self.blank_cap
        mean, sd, amp, sq = self.chunks[-1]
        b["vals"].append(self._pick(mean, sd, amp, sq)[0] * self._scale()[0])
        b["n"] += 1
        if b["n"] % 10 == 0:
            self.blank_info.set(f"blank 측정 중… {b['n'] // 10} / {b['sec']}초   "
                                f"(지금까지 평균 {statistics.mean(b['vals']):.1f})")
        if b["n"] >= b["target"]:
            self._blank_finish()

    def _blank_finish(self, cancel=False):
        b, self.blank_cap = self.blank_cap, None
        self.blank_btn.config(text="blank 측정 시작")
        if self.reader:
            self.rec_btn.config(state="normal")
        if not b:
            return
        if cancel or not b["vals"]:
            self.blank_info.set("blank 측정 취소됨")
            return
        value = statistics.mean(b["vals"])
        spread = statistics.pstdev(b["vals"]) if len(b["vals"]) > 1 else 0.0
        self._apply_blank(value)
        self.blank_info.set(f"blank = {value:.1f} Ω ({b['sec']}초 평균, 변동 ±{spread:.1f}) 적용 중")
        cur = self._current()
        if cur:
            cur = (cur[0], spread / self._scale()[0], value, cur[3], cur[4])
            self._write_row(cur, label="blank", elapsed=float(b["sec"]),
                            run_id=dt.datetime.now().strftime("%Y%m%d_%H%M%S.%f")[:-3], rec_mode="blank")
            self.reload_points()

    def _blank_typed(self):
        try:
            self._apply_blank(float(self.blank_var.get()))
            self.blank_info.set(f"blank = {self.blank:.1f} Ω (직접 입력) 적용 중")
        except ValueError:
            messagebox.showwarning("blank", "숫자(Ω)를 입력하세요.")

    def _apply_blank(self, value):
        self.blank = value
        self.blank_var.set(f"{value:.1f}")

    def clear_blank(self):
        self.blank = None
        self.blank_var.set("")
        self.blank_info.set("blank 없음 → TEER 는 R × 면적으로 계산됨 (blank 미차감)")

    # ================= 기록
    # 상태: self.rec = {"mode", "end"(측정 초, 수동은 None), "rest"(휴식 초), "total"(well 수), "done",
    #                   "phase"("measure"/"rest"), "label", "run_id", "n_chunk", "rows", "rest_left", "last"}
    def start_record(self):
        if not self.reader or self.rec or self.blank_cap or self._blocked_by_update():
            return
        m = self.recmode_var.get()
        try:
            if m == REC_MODES[1]:
                end, rest, count = max(1.0, float(self.semi_sec.get())), 0.0, 1
            elif m == REC_MODES[2]:
                end, rest = max(1.0, float(self.auto_meas.get())), max(0.0, float(self.auto_rest.get()))
                count = max(1, int(float(self.auto_count.get())))
            else:
                end, rest, count = None, 0.0, 1
            if not (math.isfinite(end or 0.0) and math.isfinite(rest)) or (end or 0.0) > 86400 or rest > 3600:
                raise ValueError
        except (ValueError, OverflowError):
            messagebox.showwarning("기록", "시간은 1~86400 초, 휴식은 0~3600 초, well 수는 숫자로 입력하세요.")
            return
        self._roll_daily_name()
        self.rec = {"mode": m, "end": end, "rest": rest, "total": count, "done": 0, "last": None, "label": ""}
        self.rec_btn.config(state="disabled")
        self.blank_btn.config(state="disabled")
        self.cal_btn.config(state="disabled")        # 기록 중 보정하면 blank 가 지워지므로 막음
        self.stop_btn.config(state="normal")
        self._begin_run(self.label_var.get())

    def _begin_run(self, label):
        """well 하나의 기록 시작 (well 마다 새 run_id). 1초 이동창을 비워 기록 전(휴식·이전 well) 샘플이 섞이지 않게 함"""
        r = self.rec
        label = label.strip() or r["label"] or "sample"
        if WELL_RE.fullmatch(label.upper()):       # a1 처럼 입력해도 플레이트의 A1 과 같은 well 로 저장
            label = label.upper()
        if label != self.label_var.get():
            self.label_var.set(label)
        self.last_samples = []
        r.update(phase="measure", label=label, n_chunk=0, rows=[], path=self.csv_path(),
                 run_id=dt.datetime.now().strftime("%Y%m%d_%H%M%S.%f")[:-3])
        seq = f"  ({r['done'] + 1}/{r['total']})" if r["total"] > 1 else ""
        self.rec_info.set(f"● {label} 기록 시작{seq}")
        if r["mode"] == REC_MODES[2]:
            self.bell()                            # 손이 바쁠 때 소리로 측정 시작을 알림

    def _rec_on_chunk(self):
        r = self.rec
        if r["phase"] == "rest":
            r["rest_left"] -= 1
            if r["rest_left"] <= 0:
                self._begin_run(self.label_var.get())
            elif r["rest_left"] % 10 == 0:
                self.rec_info.set(f"⏸ 휴식 {r['rest_left'] // 10}초 → 전극을 {self.label_var.get()} 로 옮기세요"
                                  f"  ({r['done']}/{r['total']} 완료)")
            return
        r["n_chunk"] += 1
        if r["n_chunk"] % (10 // ROWS_PER_SEC) == 0:
            self._rec_row()
            if self.rec is not r:                  # 저장 실패로 멈춤
                return
        if r["end"] is not None and r["n_chunk"] >= round(r["end"] * 10):
            self._end_run()

    def _rec_row(self):
        cur = self._current()
        r = self.rec
        if not cur or not r:
            return
        elapsed = r["n_chunk"] / 10.0
        res = self._write_row(cur, label=r["label"], elapsed=elapsed, run_id=r["run_id"],
                              rec_mode=r["mode"].split()[0])
        if res is None:                            # 저장 실패 (엑셀이 파일을 열고 있음 등) -> 중단
            self.stop_record(aborted=True)
            return
        r["rows"].append((elapsed, *res))
        left = "" if r["end"] is None else f" / {r['end']:g}s"
        seq = f"  ({r['done'] + 1}/{r['total']})" if r["total"] > 1 else ""
        self.rec_info.set(f"● {r['label']} 기록 중  {len(r['rows'])}행,  {elapsed:.1f}s{left}{seq}")

    def _end_run(self):
        """정한 시간이 끝난 기록: 마지막 3초 평균을 남기고, 자동이면 다음 well 을 고른 뒤 휴식"""
        r = self.rec
        self._summarize(r)
        ids = self.well_ids()
        if r["mode"] != REC_MODES[2] or r["done"] >= r["total"] or r["label"] == ids[-1]:
            if r["mode"] == REC_MODES[2]:
                self.bell()                        # 자동 기록이 모두 끝남
            self._finish(r, advance=True)
            return
        if r["label"] in ids:
            self.label_var.set(ids[ids.index(r["label"]) + 1])     # 바로 옆 well (A6 다음은 B1)
        if r["rest"] <= 0:
            self._begin_run(self.label_var.get())
            return
        r["phase"], r["rest_left"] = "rest", round(r["rest"] * 10)
        self.bell()
        self.rec_info.set(f"⏸ 휴식 {math.ceil(r['rest'])}초 → 전극을 {self.label_var.get()} 로 옮기세요"
                          f"  ({r['done']}/{r['total']} 완료)")

    def stop_record(self, aborted=False):
        """[정지]: 기록 중이던 well 은 마지막 3초 평균을 남기고 끝냄 (자동이면 남은 well 은 취소).
        aborted=True (저장 실패·DAQ 오류): 그 well 을 다 잰 것으로 치지 않고 다음 well 로도 넘어가지 않음"""
        r = self.rec
        if not r:
            self.stop_btn.config(state="disabled")
            if self.reader:
                self.rec_btn.config(state="normal")
                self.blank_btn.config(state="normal")
                self.cal_btn.config(state="normal")
            return
        measured = r["phase"] == "measure" and bool(r["rows"]) and not aborted
        if measured:
            self._summarize(r)
        if aborted and r["phase"] == "measure" and r["rows"]:
            self.pending_marks.append((r["path"], r["run_id"], r["label"]))
            self._flush_marks()
        self._finish(r, advance=measured, aborted=aborted)

    def _flush_marks(self):
        """중단된 기록마다 같은 run_id 로 rec_mode=ABORT_MARK 행을 하나 덧붙여, 플레이트·시간 추적 그래프가
        그 기록을 빼게 한다 (원래 행은 그대로 남음). 파일이 잠겨 있으면 기억해 두었다가 다음에 다시 시도."""
        keep = []
        for path, run_id, label in self.pending_marks:
            try:
                with open(path, "a", newline="", encoding="utf-8-sig") as f:
                    csv.writer(f).writerow([dt.datetime.now().isoformat(timespec="milliseconds"), run_id, "0.0",
                                            label, "0", MODES[self.mode_var.get()][1], "", "", self.well_var.get(),
                                            "", "", "", "", "", ABORT_MARK, "", self.mode_var.get(),
                                            self.term_var.get(), "abort-marker"])
            except OSError:
                keep.append((path, run_id, label))
        self.pending_marks = keep

    def _summarize(self, r):
        """한 번의 기록 -> 마지막 TAIL_SEC 초 평균. 표에 강조 행으로 넣고 상태줄에 표시"""
        if not r["rows"]:
            return
        s = tail_summary(r["rows"])
        r["done"] += 1
        r["last"] = (r["label"], s)
        self.tree.insert("", "end", tags=("summary",),
                         values=(f"마지막 {TAIL_SEC:g}초 평균", f"{s['n']}행", r["label"], fmt_val(s["r"]),
                                 fmt_val(s["rb"]), self.well_var.get(), f"{self._area():g}", fmt_val(s["teer"]),
                                 "대표값"))
        self.tree.yview_moveto(1.0)
        self.status_var.set(self._summary_text(r["label"], s) + f"  → {self.csv_path()}")

    def _summary_text(self, label, s):
        unit = MODES[self.mode_var.get()][1]
        return (f"{label} = {fmt_val(s['r'])} {unit}" + ("" if s["teer"] is None else f", TEER {fmt_val(s['teer'])} Ω·cm²")
                + f"  (마지막 {TAIL_SEC:g}초 평균, {s['n']}행)")

    def _finish(self, r, advance, aborted=False):
        self.rec = None
        self.stop_btn.config(state="disabled")
        if self.reader:
            self.rec_btn.config(state="normal")
            self.blank_btn.config(state="normal")
            self.cal_btn.config(state="normal")
        ids = self.well_ids()
        if advance and self.advance_var.get() and r["label"] in ids[:-1]:
            self.label_var.set(ids[ids.index(r["label"]) + 1])     # 다음 well (A1→A2→…→A6→B1)
        if r["done"] or r.get("rows"):
            self.reload_points()
            if self.trend and self.trend.winfo_exists():
                self.trend.reload()
        last = r["last"]
        if aborted:
            self.rec_info.set(f"중단됨: {r['label']} ({len(r.get('rows') or [])}행 저장) — 원인을 해결한 뒤 이 well 을 다시 재세요"
                              + (f" · 앞서 끝난 well {r['done']}개" if r["done"] else ""))
        else:
            self.rec_info.set(f"기록 끝: well {r['done']}개" + (f" · 마지막 {self._summary_text(*last)}" if last else ""))

    def _write_row(self, cur, label, elapsed, run_id, rec_mode):
        """CSV 에 한 행 저장 -> (R, R−blank, TEER) 또는 실패하면 None"""
        v, sd, val, unit, method = cur
        area = self._area()
        ts = dt.datetime.now().isoformat(timespec="milliseconds")
        minus = None if self.blank is None else val - self.blank
        teer = self._teer(val, area)
        teer_s = "" if teer is None else f"{teer:.1f}"
        path = self.csv_path()
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "run_id", "elapsed_s", "label", "value", "unit", "blank",
                                "value_minus_blank", "insert", "area_cm2", "TEER_ohm_cm2",
                                "voltage_V", "sample_sd_V", "method", "rec_mode", "cal_factor",
                                "mode", "terminal", "channel"])
                w.writerow([ts, run_id, f"{elapsed:.1f}", label, f"{val:.1f}", unit,
                            "" if self.blank is None else f"{self.blank:.1f}",
                            "" if minus is None else f"{minus:.1f}",
                            self.well_var.get(), f"{area:g}", teer_s, f"{v:.5f}", f"{sd:.5f}",
                            method, rec_mode, f"{self.cal[self.mode_var.get()]:.4f}",
                            self.mode_var.get(), self.term_var.get(), self.dev_var.get().split()[0]])
        except PermissionError:
            messagebox.showerror("저장 실패", f"{path}\n파일이 엑셀 등에서 열려 있습니다. 닫고 다시 시도하세요.")
            return None
        except OSError as e:
            messagebox.showerror("저장 실패", f"{path}\n{e}")
            return None
        self.tree.insert("", "end", values=(ts[11:], f"{elapsed:.1f}", label, f"{val:.1f}",
                                            fmt_val(minus), self.well_var.get(), f"{area:g}",
                                            teer_s or "-", method))
        kids = self.tree.get_children()
        if len(kids) > 500:
            self.tree.delete(*kids[:len(kids) - 500])
        self.tree.yview_moveto(1.0)
        if self.pending_marks:                     # 잠겨 있던 파일이 풀렸으면 밀린 중단 표시를 씀
            self._flush_marks()
        return val, minus, teer

    # ================= 업데이트 (GitHub 릴리스)
    def _cleanup_update(self, tries=20):
        """업데이트 뒤 남은 EVOM2_TEER_update.exe 지우기 (업데이트 프로그램이 닫힐 때까지 몇 번 다시 시도)"""
        if not getattr(sys, "frozen", False):
            return
        p = os.path.join(HERE, UPDATE_EXE)
        if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(sys.executable)):
            return
        for q in (p, p + ".part"):
            try:
                if os.path.exists(q):
                    os.remove(q)
            except OSError:
                if tries:
                    self.after(1000, self._cleanup_update, tries - 1)
                return

    def _startup_update_check(self):
        threading.Thread(target=self._check_worker, args=(False,), daemon=True).start()

    def check_update(self):
        if self.updating:
            return
        if self.rec or self.blank_cap:
            messagebox.showinfo("업데이트", "기록 중에는 업데이트할 수 없습니다. 기록을 끝낸 뒤 다시 누르세요.")
            return
        self.updating = True                       # 끝날 때까지 기록·blank 시작과 버튼 중복 클릭을 막음
        self.upd_btn.config(state="disabled")
        self.status_var.set("업데이트 확인 중…")
        threading.Thread(target=self._check_worker, args=(True,), daemon=True).start()

    def _check_worker(self, interactive):
        try:
            res = latest_release()
        except Exception as e:
            res = e
        self.q.put(("upd_checked", (interactive, res)))

    def _end_update(self, status=None):
        self.updating = False
        self.upd_btn.config(state="normal")
        if status:
            self.status_var.set(status)

    def _on_upd_checked(self, interactive, res):
        if not interactive:                        # 켤 때 조용히 한 번 확인한 결과: 버튼 글자만 바꿈
            if self.updating or isinstance(res, Exception):
                return
            ver, asset, rel = res
            frozen = getattr(sys, "frozen", False)
            if ver and version_tuple(ver) > version_tuple(APP_VERSION) and (asset or not frozen):
                self.latest = res
                self.upd_btn.config(text=f"업데이트 v{ver}")
                self.status_var.set(f"새 버전 v{ver} 이 있습니다 → 오른쪽 위 [업데이트 v{ver}] 버튼")
            return
        if isinstance(res, Exception):
            self._end_update("업데이트 확인 실패 (인터넷 연결 확인)")
            if self.auto_update:
                self._update_test_report(f"FAIL check: {res}", close=True)
            else:
                messagebox.showwarning("업데이트", f"GitHub 에 연결하지 못했습니다.\n{res}")
            return
        ver, asset, rel = res
        frozen = getattr(sys, "frozen", False)
        if not ver or version_tuple(ver) <= version_tuple(APP_VERSION):
            self.upd_btn.config(text="업데이트 확인")
            self._end_update(f"최신 버전입니다 (v{APP_VERSION})")
            if self.auto_update:
                self._update_test_report(f"NO UPDATE latest={ver} current={APP_VERSION}", close=True)
            else:
                messagebox.showinfo("업데이트", f"최신 버전입니다 (v{APP_VERSION}).")
            return
        if frozen and not asset:
            self._end_update(f"v{ver} 릴리스에 exe 가 아직 없습니다 (지금 v{APP_VERSION})")
            if self.auto_update:
                self._update_test_report(f"NO EXE in release {ver}", close=True)
            else:
                messagebox.showinfo("업데이트", f"v{ver} 릴리스에 아직 exe 파일이 올라오지 않았습니다. 잠시 뒤 다시 확인하세요.")
            return
        self.latest = res
        self.upd_btn.config(text=f"업데이트 v{ver}")
        notes = (rel.get("body") or "").strip()[:600]
        if not self.auto_update and not messagebox.askyesno(
                "업데이트", f"새 버전 v{ver} 이 있습니다 (지금 v{APP_VERSION}).\n\n{notes}\n\n"
                           "받아서 바꾼 뒤 프로그램을 다시 시작할까요?"):
            self._end_update()
            return
        self.status_var.set(f"v{ver} 받는 중…")
        threading.Thread(target=self._download_worker, args=(asset, rel), daemon=True).start()

    def _download_worker(self, asset, rel):
        new = None
        try:
            if getattr(sys, "frozen", False):
                dest = os.path.join(HERE, UPDATE_EXE)
                download(asset["browser_download_url"], dest, asset.get("size"), asset.get("digest"),
                         lambda n, total: self.q.put(("upd_progress", (n, total))))
                with open(dest, "rb") as f:
                    if f.read(2) != b"MZ":
                        raise OSError("받은 파일이 Windows 실행 파일이 아닙니다")
            else:
                dest = os.path.abspath(__file__)
                new = dest + ".new"
                url = (f"https://raw.githubusercontent.com/{GITHUB_REPO}/{urllib.parse.quote(rel['tag_name'])}/"
                       f"{urllib.parse.quote(SRC_IN_REPO)}")
                download(url, new)
                with open(new, encoding="utf-8") as f:
                    compile(f.read(), dest, "exec")              # 문법이 깨진 파일이면 바꾸지 않음
                os.replace(new, dest)
            self.q.put(("upd_ready", dest))
        except Exception as e:
            if new and os.path.exists(new):
                try:
                    os.remove(new)
                except OSError:
                    pass
            self.q.put(("upd_failed", e))

    def _on_upd_ready(self, dest):
        if self.rec or self.blank_cap:             # (업데이트 중에는 기록을 못 시작하지만) 혹시 기록 중이면 끝난 뒤에
            self.after(1000, self._on_upd_ready, dest)
            return
        extra = ["--selftest"] if self.auto_update else []
        try:
            if getattr(sys, "frozen", False):
                if self.auto_update:
                    self._update_test_report(f"DOWNLOADED {self.latest[0]} -> applying", close=False)
                subprocess.Popen([dest, "--apply-update", sys.executable, *extra], cwd=HERE,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            elif self.auto_update or messagebox.askyesno("업데이트", "새 코드로 바꿨습니다. 지금 다시 시작할까요?"):
                subprocess.Popen([sys.executable, dest, *extra], cwd=os.path.dirname(dest))
            else:
                self._end_update("새 코드로 바꿨습니다. 다음 실행부터 적용됩니다.")
                return
        except OSError as e:
            self._end_update(f"업데이트 실패: {e}")
            messagebox.showerror("업데이트 실패", f"새 프로그램을 실행하지 못했습니다.\n{e}")
            return
        self._on_close()

    def _update_test_report(self, text, close):
        with open(os.path.join(HERE, "update_result.txt"), "w", encoding="utf-8") as f:
            f.write(f"from={APP_VERSION} {text}\n")
        if close:
            self._on_close()

    def _on_close(self):
        self.closing = True
        self.disconnect()
        if self.pending_marks:
            self._flush_marks()
        self._persist()
        self.destroy()

    # ================= 자체 테스트 (개발용, 시뮬레이션 장비 · 임시 폴더만 씀)
    def _selftest(self):
        global SETTINGS_PATH
        SETTINGS_PATH = os.path.join(HERE, "selftest_settings.json")
        tmp = tempfile.mkdtemp(prefix="evom2_selftest_")
        self.folder_var.set(tmp)
        self.file_var.set("selftest_log.csv")
        self.well_var.set("24-well")
        self.dev_var.set(SIM_NAME)
        self.connect()
        log = [f"version={APP_VERSION} frozen={getattr(sys, 'frozen', False)} nidaqmx={NIDAQMX_OK} "
               f"err={NIDAQMX_ERR!r} devices={list(self.dev_cb['values'])}"]
        fails = []

        def check(name, cond, detail=""):
            log.append(f"{'OK  ' if cond else 'FAIL'} {name}  {detail}")
            if not cond:
                fails.append(name)

        def runs(well):
            return [x for x in read_runs(self.csv_path()) if x["well"] == well]

        def ev(x, y):
            return types.SimpleNamespace(x=x, y=y)

        def step_blank():
            c = self._current()
            check("realtime ~1000 Ω", bool(c) and abs(c[2] - 1000) < 30, self.value_var.get())
            px, py, pw, ph, k, cell, x0, y0 = plate_layout(int(self.plate["width"]), int(self.plate["height"]), 4, 6)
            self._plate_click(ev(x0 + cell * 1.5, y0 + cell * 1.5))
            check("plate click -> B2", self.label_var.get() == "B2", self.label_var.get())
            check("plate hit: row label / col label / chamfer corner",
                  plate_hit(self.plate, "24-well", (px + x0) / 2, y0 + cell * 0.5) == ("row", "A")
                  and plate_hit(self.plate, "24-well", x0 + cell * 2.5, (py + y0) / 2) == ("col", "3")
                  and plate_hit(self.plate, "24-well", px + 1, py + 1) is None)
            self.blank_sec.set("2")
            self.start_blank()
            self.after(2700, step_manual)

        def step_manual():
            check("blank = 2s average", self.blank is not None and abs(self.blank - 1000) < 30, f"{self.blank}")
            self.blank = 100.0
            self.label_var.set("A1")
            self.recmode_var.set(REC_MODES[0])
            self.updating = True
            self.start_record()
            blocked = self.rec is None
            self.updating = False
            check("record blocked while an update is running", blocked)
            self.start_record()
            check("run starts with an empty 1-s window (no pre-run samples)", self.last_samples == [])
            self.after(1500, step_semi)

        def step_semi():
            self.stop_record()
            r = runs("A1")
            check("manual 1.5s -> 1 run, all rows averaged (<3s)",
                  len(r) == 1 and 5 <= r[0]["rows"] <= 9 and r[0]["n"] == r[0]["rows"], f"{[(x['rows'], x['n']) for x in r]}")
            check("summary row in table", any("summary" in self.tree.item(i, "tags") for i in self.tree.get_children()))
            check("advance A1 -> A2", self.label_var.get() == "A2", self.label_var.get())
            self.recmode_var.set(REC_MODES[1])
            self.semi_sec.set("1")
            self.start_record()
            self.after(1700, step_auto)

        def step_auto():
            r = runs("A2")
            check("semi 1s -> stops by itself, 5 rows", self.rec is None and len(r) == 1 and r[0]["rows"] == 5,
                  f"{[x['rows'] for x in r]}")
            check("advance A2 -> A3", self.label_var.get() == "A3", self.label_var.get())
            self.recmode_var.set(REC_MODES[2])
            self.auto_meas.set("1")
            self.auto_rest.set("1")
            self.auto_count.set("3")
            self.start_record()
            self.after(1500, step_auto_mid)                 # A3 측정(0~1초) 뒤 휴식(1~2초) 중

        def step_auto_mid():
            phase = self.rec and self.rec["phase"]
            check("auto: rest phase, next well selected", phase == "rest" and self.label_var.get() == "A4",
                  f"{phase} {self.label_var.get()}")
            self.after(4300, step_auto_end)                 # A4 측정 → 휴식 → A5 측정 끝

        def step_auto_end():
            got = {w: [x["rows"] for x in runs(w)] for w in ("A3", "A4", "A5", "A6")}
            check("auto 1s/1s x3 -> A3,A4,A5 (5 rows each, nothing during rest)",
                  self.rec is None and got == {"A3": [5], "A4": [5], "A5": [5], "A6": []}, f"{got}")
            check("auto end -> next well A6", self.label_var.get() == "A6", self.label_var.get())
            self.label_var.set("D6")
            self.auto_rest.set("0")
            self.start_record()
            self.after(1600, step_plate_end)

        def step_plate_end():
            check("auto stops at plate end (D6)", self.rec is None and len(runs("D6")) == 1
                  and self.label_var.get() == "D6", f"{len(runs('D6'))} {self.label_var.get()}")
            self.label_var.set("B1")
            self.auto_rest.set("5")
            self.start_record()
            self.after(1600, step_stop_in_rest)

        def step_stop_in_rest():
            in_rest = bool(self.rec) and self.rec["phase"] == "rest"
            self.stop_record()
            check("stop during rest: B1 kept, B2 selected, not advanced again",
                  in_rest and self.rec is None and len(runs("B1")) == 1 and not runs("B2")
                  and self.label_var.get() == "B2", f"{in_rest} {self.label_var.get()}")
            self.label_var.set("C2")                       # 저장 실패 -> 중단 (다 잰 것으로 치지 않음)
            self.recmode_var.set(REC_MODES[1])
            self.semi_sec.set("5")
            self.start_record()
            self.after(700, lambda: setattr(self, "_write_row", lambda *a, **k: None))
            self.after(1500, step_abort)

        def step_abort():
            del self._write_row                            # 원래 메서드로 되돌림
            with open(self.csv_path(), encoding="utf-8-sig") as f:
                marked = sum(1 for row in csv.reader(f) if len(row) > 14 and row[14] == ABORT_MARK)
            check("write failure aborts: label stays, says 중단, run marked and left out",
                  self.rec is None and self.label_var.get() == "C2" and self.rec_info.get().startswith("중단")
                  and marked == 1 and not runs("C2"), f"{self.label_var.get()} marks={marked} C2={len(runs('C2'))}")
            self._orig_err = messagebox.showerror          # DAQ 오류 시험 동안 오류 창을 띄우지 않음
            messagebox.showerror = lambda *a, **k: None
            self.label_var.set("D3")                       # 측정 중 DAQ 오류 -> 그 well 중단
            self.start_record()
            self.after(700, lambda: self.q.put(("error", "selftest DAQ error")))
            self.after(1100, step_daq_measure)

        def step_daq_measure():
            check("DAQ error while measuring: aborted, marked, label stays",
                  self.reader is None and self.rec is None and self.label_var.get() == "D3" and not runs("D3")
                  and self.rec_info.get().startswith("중단됨: D3"), f"{self.label_var.get()} / {self.rec_info.get()[:30]}")
            self.connect()
            self.label_var.set("D4")                       # 휴식 중 DAQ 오류 -> 끝난 well 은 유지
            self.recmode_var.set(REC_MODES[2])
            self.auto_meas.set("1")
            self.auto_rest.set("3")
            self.auto_count.set("2")
            self.start_record()
            self.after(1700, lambda: self.q.put(("error", "selftest DAQ error")))
            self.after(2100, step_daq_rest)

        def step_daq_rest():
            messagebox.showerror = self._orig_err
            check("DAQ error during rest: finished well kept, restart from next well",
                  self.rec is None and len(runs("D4")) == 1 and self.label_var.get() == "D5"
                  and self.rec_info.get().startswith("중단됨: 휴식 중"), f"{self.label_var.get()} / {self.rec_info.get()[:30]}")
            self.file_var.set("teer_log_20000101.csv")
            self._roll_daily_name()
            check("daily file name rolls over to today", self.file_var.get() == f"teer_log_{dt.date.today():%Y%m%d}.csv",
                  self.file_var.get())
            self.file_var.set("selftest_log.csv")
            bad = os.path.join(tmp, "excel_cp949.csv")
            fake_csv(bad, [("C1", dt.datetime.now(), 600)], dt.datetime.now())
            with open(bad, "rb") as f:
                raw = f.read()
            with open(bad, "wb") as f:
                f.write(raw.decode("utf-8-sig").encode("cp949") + b"\x00" * 64)
            rr = read_runs(bad)
            check("read_runs: CP949 (re-saved by Excel) + NUL tail", len(rr) == 1 and abs(rr[0]["r"] - 600) < 1e-6,
                  f"{[(x['well'], x['r']) for x in rr]}")
            rows = [(i * 0.2, 500.0 if i <= 35 else 900.0, None, None) for i in range(1, 51)]
            s = tail_summary(rows)
            check("tail_summary = last 3s", s["n"] == 15 and abs(s["r"] - 900) < 1e-9, f"{s}")
            s1 = tail_summary([(30.0, 1005.6, 0.0, 0.0)])
            check("tail_summary single row", s1["n"] == 1 and s1["r"] == 1005.6)
            check("version compare", version_tuple("v1.2.10") == (1, 2, 10)
                  and version_tuple("1.10.0") > version_tuple("1.9.9"))
            step_trend()

        def fake_csv(path, runs_, mtime):
            """시험용 측정 CSV: 기록마다 10초(0.2초 간격), 앞 7초는 엉뚱한 값, 마지막 3초가 R"""
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "run_id", "elapsed_s", "label", "value", "unit", "blank",
                            "value_minus_blank", "insert", "area_cm2", "TEER_ohm_cm2", "voltage_V",
                            "sample_sd_V", "method", "rec_mode", "cal_factor", "mode", "terminal", "channel"])
                for well, start, R in runs_:
                    rid = start.strftime("%Y%m%d_%H%M%S.%f")[:-3]
                    for i in range(1, 51):
                        el = i * 0.2
                        val = R if el > 7.0 else R * 0.5
                        w.writerow([(start + dt.timedelta(seconds=el)).isoformat(timespec="milliseconds"), rid,
                                    f"{el:.1f}", well, f"{val:.1f}", "Ω", "100.0", f"{val - 100:.1f}", "24-well",
                                    "0.33", f"{(val - 100) * 0.33:.1f}", "1", "0.01", "평균", "자동", "1.0000",
                                    "OHMS (저항)", "DIFF", "test"])
            os.utime(path, (mtime.timestamp(), mtime.timestamp()))

        def step_trend():
            base = dt.datetime.combine(dt.date.today(), dt.time(12))
            day, mi = dt.timedelta(days=1), dt.timedelta(minutes=1)
            d = os.path.join(tmp, "trend")
            os.makedirs(d)
            fake_csv(os.path.join(d, "day0.csv"), [("A1", base - 3 * day, 500), ("A2", base - 3 * day + mi, 400),
                                                   ("A3", base - 3 * day + 2 * mi, 300)], base - 3 * day + 9 * mi)
            fake_csv(os.path.join(d, "day1.csv"), [("A1", base - 2 * day, 600), ("A1", base - 2 * day + mi, 700),
                                                   ("A2", base - 2 * day + 2 * mi, 450),
                                                   ("A3", base - 2 * day + 3 * mi, 350)], base - 2 * day + 9 * mi)
            fake_csv(os.path.join(d, "multi.csv"), [("A4", base - 5 * day, 300), ("A4", base - 4 * day, 500)],
                     base - day)
            fake_csv(os.path.join(d, "day3.csv"), [("A1", base, 800), ("A2", base + mi, 500),
                                                   ("A3", base + 2 * mi, 400)], base + 9 * mi)
            with open(os.path.join(d, EXPORT_PREFIX + "old.csv"), "w", encoding="utf-8") as f:
                f.write("well,x\nA1,1\n")
            self.open_trend()
            tw = self.trend
            tw.folder_var.set(d)
            tw.reload()
            self.update()
            S = tw.sessions
            check("trend file mode: 4 sessions ordered by file mtime",
                  [s["files"] for s in S] == [["day0.csv"], ["day1.csv"], ["multi.csv"], ["day3.csv"]],
                  f"{[s['files'] for s in S]}")
            a1 = [s["wells"].get("A1", {}).get("teer") for s in S]
            exp = [400 * 0.33, 600 * 0.33, None, 700 * 0.33]
            check("trend A1 = last-3s TEER of the latest run in each session (2 runs on day1)",
                  all((x is None) == (y is None) and (x is None or abs(x - y) < 0.2) for x, y in zip(a1, exp))
                  and S[1]["wells"]["A1"]["runs"] == 2, f"{a1}")
            check("default selection = wells with data", tw.sel == ["A1", "A2", "A3", "A4"], f"{tw.sel}")
            check("panel points", len(tw.hits) == 10, f"{len(tw.hits)}")
            px, py, pw, ph, k, cell, x0, y0 = plate_layout(int(tw.plate["width"]), int(tw.plate["height"]), 4, 6)
            tw._plate_click(ev((px + x0) / 2, y0 + cell * 0.5))
            off = list(tw.sel)
            tw._plate_click(ev((px + x0) / 2, y0 + cell * 0.5))
            check("row label toggles whole row", off == [] and tw.sel == ["A1", "A2", "A3", "A4"], f"{off} {tw.sel}")
            tw._plate_click(ev(x0 + cell * 1.5, y0 + cell * 0.5))
            check("well click toggles one well", tw.sel == ["A1", "A3", "A4"], f"{tw.sel}")
            out = os.path.join(tmp, "export.csv")
            tw.export(out)
            with open(out, encoding="utf-8-sig") as f:
                ex = list(csv.reader(f))
            check("export matrix (3 header rows + 4 wells)", len(ex) == 7 and ex[0][0].startswith("well")
                  and ex[3][0] == "A1" and ex[3][1] == f"{400 * 0.33:.1f}" and len(ex[0]) == 5, f"{ex[:4]}")
            tw.time_var.set(TREND_TIME[1])
            self.update()
            check("timestamp mode: sessions per recorded date", len(tw.sessions) == 5,
                  f"{[s['label'] for s in tw.sessions]}")
            self.toggle_theme()
            self.update()
            self.toggle_theme()
            check("theme toggle redraws trend", tw.winfo_exists() and len(tw.hits) > 0, f"{len(tw.hits)}")
            finish()

        def finish():
            log.append("SELFTEST " + ("OK" if not fails else "FAIL: " + ", ".join(fails)))
            text = "\n".join(log)
            with open(os.path.join(HERE, "selftest_result.txt"), "w", encoding="utf-8") as f:
                f.write(text)                     # 결과의 정본은 파일 (exe 는 콘솔이 없음)
            try:
                print(text)
            except (UnicodeError, AttributeError, OSError, ValueError):
                pass                              # 콘솔이 없거나 CP949 콘솔이 일부 글자를 못 찍는 경우
            self.disconnect()
            shutil.rmtree(tmp, ignore_errors=True)
            if os.path.exists(SETTINGS_PATH):
                os.remove(SETTINGS_PATH)
            self.destroy()

        self.after(1500, step_blank)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--apply-update":
        apply_update(sys.argv[2], sys.argv[3:])
    else:
        App(selftest="--selftest" in sys.argv).mainloop()
