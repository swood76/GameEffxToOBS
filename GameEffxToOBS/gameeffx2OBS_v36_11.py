"""GameEffx2OBS V35.7 HIT diagnostics - HP-driven OBS color correction and browser overlay.



Install: python -m pip install obsws-python opencv-python numpy dxcam

OBS: keep raw 'Game Capture' inside 'GameEffx Gameplay' scene.

Apply 'Low Health' Color Correction filter to 'GameEffx Gameplay'.

In 'Stream', put a Browser Source ABOVE the nested gameplay scene:

URL http://127.0.0.1:8765/  Width 1920  Height 1080.

Run this script BEFORE opening/refreshing the Browser Source.

"""

import json
import csv
from datetime import datetime

import threading

import random

import time

from collections import deque

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from urllib.parse import urlsplit



import cv2

import numpy as np

import obsws_python as obs

import dxcam

import ctypes

from ctypes import wintypes



HOST, PORT = 'localhost', 4455

PASSWORD = 'gameeffx'  # Set to your OBS WebSocket password

CAPTURE_SOURCE = 'DXcam window-aware (monitor 0)'

DISPLAY_INDEX = 0  # 0 = primary monitor; change to 1 if game is on second monitor

# Reference coordinates are relative to a 1920x1080 Dark Souls CLIENT area.

# V29 locates the actual game window and translates these coordinates to the desktop.

DESKTOP_CAPTURE_REGION = (0, 0, 1920, 1080)  # Entire game client; death text was missing from cropped frames

GAME_WINDOW_TITLE = 'DARK SOULS'  # Case-insensitive title substring; change if necessary

WINDOW_CHECK_SECONDS = 0.5

# Desktop duplication cannot see the game when another window covers it.



DXCAM_TARGET_FPS = 60

EFFECT_SOURCE = 'GameEffx Gameplay'

FILTER_NAME = 'Low Health'

OVERLAY_HOST, OVERLAY_PORT = '127.0.0.1', 8765

OVERLAY_FILE = Path(__file__).with_name('gameeffx2OBS_overlay_v35.html')

DEATH_TEMPLATE = Path(__file__).with_name('you_died_template_v14.png')



BAR_START_X, BAR_END_X = 263, 977

BAR_TOP_Y, BAR_BOTTOM_Y = 108, 128

COLUMN_RED_THRESHOLD = 0.25

HUD_GREEN_START_X, HUD_GREEN_END_X = 260, 470

HUD_GREEN_TOP_Y, HUD_GREEN_BOTTOM_Y = 125, 142

SAMPLE_INTERVAL = 0.0  # DXcam supplies frames; do not add polling delay

HP_HISTORY = 3

EFFECT_START_HP = 75.0

CRITICAL_START_HP = 40.0

FATAL_FLASH_SECONDS = 1.1

FATAL_FLASH_COOLDOWN = 12.0

HEAL_JUMP = 25.0

# Calibrated on raw OBS diagnostic frames, not a manually cropped screenshot.

DEATH_ROI = (500, 415, 1160, 555)  # x1,y1,x2,y2 at 1920x1080

DEATH_THRESHOLD = 0.55

DEATH_INSTANT_THRESHOLD = 0.75  # Single high-confidence frame confirms death

DEATH_CONFIRM_FRAMES = 4

DEATH_MIN_CONFIRM_SECONDS = 0.18

DEATH_CONFIRM_PEAK = 0.65
# Secondary sustained-text path: real YOU DIED frames in diagnostics scored
# approximately 0.454-0.500. Never confirm on a single weak frame.
DEATH_SECONDARY_THRESHOLD = 0.43
DEATH_SECONDARY_PEAK = 0.48
DEATH_SECONDARY_FRAMES = 10
DEATH_SECONDARY_SECONDS = 0.30

DEATH_INSTANT_THRESHOLD = 1.01  # disable one-frame confirmations; require persistence

DEATH_COOLDOWN_SECONDS = 90

DEATH_WATCH_SECONDS = 10.0

CLIP_DELAY_SECONDS = 7.0

GLITCH_SECONDS = 2.0

DEATH_HOLD_SECONDS = 8.0  # Maintain heartbeat across delayed YOU DIED animation

DISSOLVE_MAX_SECONDS = 12.0  # Hard safety limit, including bonfire transitions

DISSOLVE_RELEASE_SECONDS = 1.5

BLOOD_MIN_INTENSITY = 0.28

HIT_FLASH_SECONDS = 0.60

HIT_MIN_DROP = 0.45  # minimum HP drop in percentage points

# V30: one flash per damage animation. A raw drop without a yellow trail

# requires two consecutive samples to avoid one-frame HUD artifacts.

HIT_REARM_STABLE_SECONDS = 0.85

HIT_STABLE_TOLERANCE = 0.8

HIT_RAW_CONFIRM_FRAMES = 2  # retained for diagnostics; pulses require yellow damage

EARLY_BLOOD_SECONDS = 1.50  # provisional full red filter from first hit before bar settles

EARLY_BLOOD_HP = 40.0  # provisional color strength; does not fake actual measured HP

CRITICAL_MISSING_GRACE = 2.0

POST_HEAL_HIT_GUARD_SECONDS = 2.5

LOW_HP_CONFIRM_SECONDS = 0.35  # sustained low HP without yellow damage

TIMING_LOG_INTERVAL = 3.0  # diagnostics; no per-frame spam  # Visible blood from first sub-75% reading





def red_text_mask(frame):

    x1, y1, x2, y2 = DEATH_ROI

    crop = frame[y1:y2, x1:x2]

    b, g, r = (crop[:, :, i].astype(np.int16) for i in range(3))

    return (((r > 48) & (r > g * 1.65) & (r > b * 1.65) &

             ((r - g) > 25)).astype(np.uint8) * 255)





def death_similarity(mask, template):

    # Binary Dice similarity with a small position tolerance.

    # Much less sensitive to changing backgrounds than RGB matching.

    if cv2.countNonZero(mask) < 350 or template.shape != mask.shape:

        return 0.0

    target = template > 0

    count_target = int(np.count_nonzero(target))

    count_live = int(cv2.countNonZero(mask))

    if not count_target:

        return 0.0

    best = 0.0

    for dy in (-6, -3, 0, 3, 6):

        for dx in (-8, -4, 0, 4, 8):

            # Slice overlap without wrapping edges.

            y0, y1 = max(0, dy), min(mask.shape[0], mask.shape[0] + dy)

            x0, x1 = max(0, dx), min(mask.shape[1], mask.shape[1] + dx)

            if y1 <= y0 or x1 <= x0:

                continue

            shifted = mask[y0 - dy:y1 - dy, x0 - dx:x1 - dx] > 0

            overlap = np.count_nonzero(shifted & target[y0:y1, x0:x1])

            best = max(best, 2.0 * overlap / (count_live + count_target))

    return float(best)





shared = {'hit_remaining': 0.0, 'hp': None, 'intensity': 0.0, 'critical': 0.0, 'active': False, 'updated': 0.0, 'glitch_until': 0.0, 'fatal_until': 0.0, 'dissolve_start': 0.0, 'dissolve_until': 0.0, 'dissolve_release_start': 0.0, 'death_event_id': 0, 'hit_until': 0.0}

shared_lock = threading.Lock()

# V36.4: append-only diagnostics, independent of the gameplay logic.
DIAG_DIR = Path(__file__).with_name('gameeffx2OBS_v36_4_logs')
DIAG_DIR.mkdir(exist_ok=True)
SESSION = datetime.now().strftime('%Y%m%d_%H%M%S')
DIAG_PATH = DIAG_DIR / f'state_{SESSION}.csv'
OVERLAY_DIAG_PATH = DIAG_DIR / f'overlay_{SESSION}.jsonl'
DIAG_FIELDS = ['wall_time','monotonic','hp','hp_state','filter_enabled',
               'intensity','critical','death_event_id','dissolve_start',
               'dissolve_until','dissolve_release_start','dissolve_remaining',
               'dissolve_elapsed','dissolve_release_elapsed','dissolve_opacity',
               'death_confirmed','death_watch_blocked','verified_damage_remaining',
               'watch_remaining','replay_due_remaining']
with DIAG_PATH.open('w',newline='',encoding='utf-8') as f:
    csv.DictWriter(f,fieldnames=DIAG_FIELDS).writeheader()
_diag_lock = threading.Lock()

def log_state_snapshot(now, hp, hp_state, enabled, death_confirmed,
                       death_watch_blocked, verified_damage_until, watch_until,
                       replay_due_at):
    with shared_lock:
        s = dict(shared)
    elapsed = max(0.0, now-s['dissolve_start']) if s['dissolve_start'] else 0.0
    remaining = max(0.0,s['dissolve_until']-now)
    release = max(0.0,now-s['dissolve_release_start']) if s['dissolve_release_start'] else 0.0
    x = max(0.0,min(1.0,(elapsed-0.35)/1.25))
    opacity = x*x*(3-2*x) if s['dissolve_start'] and remaining>0 else 0.0
    if elapsed>=6.2 and release>0:
        opacity *= max(0.0,1-release/DISSOLVE_RELEASE_SECONDS)
    row = dict(wall_time=datetime.now().isoformat(timespec='milliseconds'),
               monotonic=round(now,3),hp=hp,hp_state=hp_state,
               filter_enabled=int(enabled),intensity=s['intensity'],
               critical=s['critical'],death_event_id=s['death_event_id'],
               dissolve_start=round(s['dissolve_start'],3),
               dissolve_until=round(s['dissolve_until'],3),
               dissolve_release_start=round(s['dissolve_release_start'],3),
               dissolve_remaining=round(remaining,3),
               dissolve_elapsed=round(elapsed,3),
               dissolve_release_elapsed=round(release,3),
               dissolve_opacity=round(opacity,3),
               death_confirmed=int(death_confirmed),
               death_watch_blocked=int(death_watch_blocked),
               verified_damage_remaining=round(max(0.0,verified_damage_until-now),3),
               watch_remaining=round(max(0.0,watch_until-now),3),
               replay_due_remaining=round(max(0.0,(replay_due_at or 0)-now),3))
    with _diag_lock, DIAG_PATH.open('a',newline='',encoding='utf-8') as f:
        csv.DictWriter(f,fieldnames=DIAG_FIELDS).writerow(row)






class OverlayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if urlsplit(self.path).path != '/overlay_diag':
            self.send_error(404)
            return
        try:
            n = int(self.headers.get('Content-Length','0'))
            if n <= 0 or n > 8192:
                self.send_error(413)
                return
            data = json.loads(self.rfile.read(n))
            if not isinstance(data,dict):
                self.send_error(400)
                return
            record = {'server_time':datetime.now().isoformat(timespec='milliseconds'),
                      'server_monotonic':round(time.monotonic(),3),
                      'overlay':data}
            with _diag_lock, OVERLAY_DIAG_PATH.open('a',encoding='utf-8') as f:
                f.write(json.dumps(record,separators=(',',':'))+'\n')
            self.send_response(204)
            self.send_header('Cache-Control','no-store')
            self.end_headers()
        except Exception as exc:
            print(f'OVERLAY DIAGNOSTIC ERROR: {exc}')
            self.send_error(400)

    def do_GET(self):

        route = urlsplit(self.path).path

        if route == '/state':

            with shared_lock:

                payload = dict(shared)

            payload['glitch_remaining'] = max(0.0, payload['glitch_until'] - time.monotonic())

            payload['fatal_remaining'] = max(0.0, payload['fatal_until'] - time.monotonic())

            payload['hit_remaining'] = max(0.0, payload.get('hit_until', 0.0) - time.monotonic())

            payload['dissolve_elapsed'] = max(0.0, time.monotonic() - payload['dissolve_start']) if payload['dissolve_start'] else 0.0

            payload['dissolve_remaining'] = max(0.0, payload['dissolve_until'] - time.monotonic())

            payload['dissolve_release_elapsed'] = max(0.0, time.monotonic() - payload['dissolve_release_start']) if payload['dissolve_release_start'] else 0.0

            # Compute opacity in Python; browser only displays the provided value.

            elapsed = payload['dissolve_elapsed']

            if payload['dissolve_start'] and payload['dissolve_remaining'] > 0:

                x = max(0.0, min(1.0, (elapsed - 0.35) / 1.25))

                opacity = x * x * (3.0 - 2.0 * x)

                # Do not release until black has been held for at least 2 seconds.

                if elapsed >= 6.2 and payload['dissolve_release_elapsed'] > 0:

                    t = max(0.0, min(1.0, payload['dissolve_release_elapsed'] / DISSOLVE_RELEASE_SECONDS))

                    opacity *= 1.0 - t

            else:

                opacity = 0.0

            payload['dissolve_opacity'] = round(opacity, 4)

            # Fail safe: if the detector stops, the overlay fades out.

            if time.monotonic() - payload['updated'] > 2.5:

                payload.update(intensity=0.0, critical=0.0, active=False, glitch_until=0.0, fatal_until=0.0, glitch_remaining=0.0, fatal_remaining=0.0, dissolve_elapsed=0.0, dissolve_remaining=0.0, dissolve_release_elapsed=0.0, dissolve_opacity=0.0, hit_remaining=0.0)

            body = json.dumps(payload).encode('utf-8')

            mime = 'application/json'

        elif route == '/':

            try:

                body = OVERLAY_FILE.read_bytes()

            except OSError:

                self.send_error(404, 'gameeffx2OBS_overlay.html not found beside script')

                return

            mime = 'text/html; charset=utf-8'

        else:

            self.send_error(404)

            return

        self.send_response(200)

        self.send_header('Content-Type', mime)

        self.send_header('Cache-Control', 'no-store')

        self.send_header('Content-Length', str(len(body)))

        self.end_headers()

        self.wfile.write(body)



    def log_message(self, *args):

        pass





# Win32 APIs are built into Windows; no pywin32 dependency required.

user32 = ctypes.windll.user32

try:

    user32.SetProcessDPIAware()  # Pixel-accurate coordinates at non-100% Windows scaling

except Exception:

    pass





def find_game_client_rect():

    """Return (x, y, width, height, title) of the visible Dark Souls client area."""

    candidates = []

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

    user32.GetWindowTextLengthW.restype = ctypes.c_int

    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    user32.GetWindowTextW.restype = ctypes.c_int

    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]



    def each(hwnd, _):

        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):

            return True

        length = user32.GetWindowTextLengthW(hwnd)

        if not length:

            return True

        buf = ctypes.create_unicode_buffer(length + 1)

        user32.GetWindowTextW(hwnd, buf, length + 1)

        title = buf.value

        if GAME_WINDOW_TITLE.lower() not in title.lower():

            return True

        rect = wintypes.RECT()

        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):

            return True

        pt = wintypes.POINT(0, 0)

        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):

            return True

        w, h = rect.right - rect.left, rect.bottom - rect.top

        if w >= 640 and h >= 360:

            candidates.append((w * h, pt.x, pt.y, w, h, title))

        return True



    user32.EnumWindows(callback_type(each), 0)

    if not candidates:

        return None

    _, x, y, w, h, title = max(candidates)

    return x, y, w, h, title





def monitor_size(index=DISPLAY_INDEX):

    """Read physical monitor bounds; DXcam region coordinates are monitor-local."""

    user32 = ctypes.windll.user32

    # Prevent DPI virtualization from changing desktop coordinate units.

    try:

        user32.SetProcessDPIAware()

    except Exception:

        pass

    if index == 0:

        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    raise RuntimeError('V35.1 currently supports the primary monitor only.')





def region_for_window(window):

    """Clip the game client to the physical monitor, as DXcam requires."""

    x, y, w, h, _ = window

    mw, mh = monitor_size()

    region = (max(0, x), max(0, y), min(mw, x + w), min(mh, y + h))

    if region[2] <= region[0] or region[3] <= region[1]:

        raise RuntimeError(f'Game window has no visible pixels on monitor: {window}')

    return region





def frame_from_dxcam(camera, region, window):

    """Place visible desktop pixels at their correct game-client coordinates.



    Off-screen game pixels are black; we never stretch a clipped screenshot

    across the whole 1920x1080 reference image.

    """

    shot = camera.get_latest_frame()

    if shot is None:

        raise RuntimeError('DXcam returned no frame. Ensure the game window is visible.')

    x, y, w, h, _ = window

    l, t, r, b = region

    # Map screen-visible rectangle into reference-game pixel coordinates.

    dst_l = max(0, round((l - x) * 1920 / w))

    dst_t = max(0, round((t - y) * 1080 / h))

    dst_r = min(1920, round((r - x) * 1920 / w))

    dst_b = min(1080, round((b - y) * 1080 / h))

    canvas = np.zeros((1080, 1920, 3), dtype=np.uint8)

    if dst_r > dst_l and dst_b > dst_t:

        resized = cv2.resize(shot, (dst_r - dst_l, dst_b - dst_t),

                             interpolation=cv2.INTER_LINEAR)

        canvas[dst_t:dst_b, dst_l:dst_r] = resized

    return canvas





def hp_bar_geometry(frame):

    """Only trust a left-anchored, vertically coherent red health bar."""

    bar = frame[BAR_TOP_Y:BAR_BOTTOM_Y, BAR_START_X:BAR_END_X + 1]

    b, g, r = (bar[:, :, i].astype(np.float32) for i in range(3))

    red = (r > 45) & (r > g * 1.38) & (r > b * 1.30) & ((r - g) > 23)

    strengths = np.mean(red, axis=0)

    solid = strengths >= 0.40

    # Require a recognizable left-anchored bar, not isolated UI red pixels.

    if np.mean(solid[8:50]) < 0.65:

        return None, solid

    # Small anti-aliasing gaps are acceptable; large discontinuities aren't.

    smoothed = np.convolve(solid.astype(np.float32), np.ones(9)/9, mode='same')

    valid = smoothed >= 0.55

    gaps = np.where(~valid[8:])[0]

    edge = (8 + int(gaps[0])) if len(gaps) else len(valid)-1

    if edge < 40:

        return None, solid

    # A detached red section after the first gap indicates invalid geometry.

    if edge + 30 < len(valid) and np.mean(valid[edge+18:]) > 0.15:

        return None, solid

    return float(np.clip(edge / (len(valid)-1) * 100, 0, 100)), solid





def detect_hp(frame):

    hp, _ = hp_bar_geometry(frame)

    # Missing or corrupted HUD is unknown, never zero HP.

    return hp





def detect_yellow_damage_tail(frame, red_hp):

    """Only accept yellow trailing a verified, left-anchored red bar."""

    if red_hp is None or red_hp >= 97.0:

        return False

    bar = frame[BAR_TOP_Y:BAR_BOTTOM_Y, BAR_START_X:BAR_END_X + 1]

    hsv = cv2.cvtColor(bar, cv2.COLOR_BGR2HSV)

    yellow = ((hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 43) &

              (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 65))

    columns = np.mean(yellow, axis=0) >= 0.45

    edge = int(round(red_hp / 100 * (len(columns)-1)))

    # Yellow must begin close to the red fill and extend continuously to the right.

    start = edge + 2

    stop = min(len(columns), edge + 90)

    if start >= stop or np.mean(columns[start:min(stop,start+18)]) < 0.55:

        return False

    run = 0

    for value in columns[start:stop]:

        if value:

            run += 1

            if run >= 14:

                return True

        else:

            run = 0

    return False





def effect_settings(hp):

    progress = float(np.clip((EFFECT_START_HP - hp) / EFFECT_START_HP, 0, 1))

    intensity = BLOOD_MIN_INTENSITY + (1.0 - BLOOD_MIN_INTENSITY) * progress

    # The browser overlay animates heartbeat smoothly at display refresh rate.

    # Keep color correction stable to avoid competing pulses or flicker.

    strength = intensity * 0.55

    red, green, blue = 255, round(255 - 125 * strength), round(255 - 140 * strength)

    multiply_color = (255 << 24) | (blue << 16) | (green << 8) | red

    settings = {

        'brightness': -0.035 * strength,

        'contrast': 0.14 * strength,

        'saturation': -0.35 * strength,

        'gamma': -0.08 * strength,

        'multiply_color': multiply_color,

    }

    return intensity, settings





def publish(hp, intensity, active, critical_override=None):

    # Critical effect begins below 40% HP and reaches full strength at 0%.

    critical = (float(np.clip(critical_override, 0, 1)) if critical_override is not None else

                (0.0 if hp is None else float(np.clip((CRITICAL_START_HP - hp) / CRITICAL_START_HP, 0, 1))))

    with shared_lock:

        shared.update(hp=hp, intensity=intensity if active else 0.0,

                      critical=critical if active else 0.0,

                      active=active, updated=time.monotonic())





def save_replay(client):

    """Request replay save on the main thread; do not share ReqClient across threads."""

    try:

        client.save_replay_buffer()

        print('REPLAY SAVE REQUEST ACCEPTED — check OBS: File > Show Recordings.')

    except Exception as exc:

        print(f'REPLAY SAVE FAILED: {exc}')

        print('Make sure OBS Replay Buffer is running (Stop Replay Buffer visible).')





def trigger_death():

    with shared_lock:

        shared['glitch_until'] = 0.0

        shared['fatal_until'] = 0.0

        shared['hit_until'] = 0.0

        shared['dissolve_start'] = time.monotonic()

        shared['dissolve_until'] = time.monotonic() + DISSOLVE_MAX_SECONDS

        shared['dissolve_release_start'] = 0.0

        shared['death_event_id'] += 1

    print('YOU DIED CONFIRMED — black dissolve started; replay save in 7 seconds')

    # Caller schedules replay on main thread to keep WebSocket requests serialized.

    return time.monotonic() + CLIP_DELAY_SECONDS





def main():

    if not OVERLAY_FILE.is_file():

        raise FileNotFoundError(f'Place {OVERLAY_FILE.name} beside this Python file')

    server = ThreadingHTTPServer((OVERLAY_HOST, OVERLAY_PORT), OverlayHandler)

    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f'GameEffx2OBS V36.4 HIT + death replay diagnostics overlay: http://{OVERLAY_HOST}:{OVERLAY_PORT}/')

    print(f'V36.4 state log: {DIAG_PATH.resolve()}')
    print(f'V36.4 overlay log: {OVERLAY_DIAG_PATH.resolve()}')
    print('Connecting to OBS...')

    client = obs.ReqClient(host=HOST, port=PORT, password=PASSWORD)

    client.set_source_filter_enabled(EFFECT_SOURCE, FILTER_NAME, False)

    print(f'Window-aware DXcam: monitor {DISPLAY_INDEX} | Filtering: {EFFECT_SOURCE}')

    print('IMPORTANT: Dark Souls window must be visible and not covered by OBS.')

    print('V35.1: clipped visible client capture for death recognition; V33 effects/dissolve unchanged')

    print(f'Color below {EFFECT_START_HP:.0f}% HP | Heartbeat/tunnel below {CRITICAL_START_HP:.0f}%')

    print('Automatic YOU DIED detection from captured game window frames | Ctrl+C to quit')

    print('OBS Replay Buffer must already be started to save clips')

    window = find_game_client_rect()

    if window is None:

        raise RuntimeError(f'Cannot find visible Dark Souls window (title contains {GAME_WINDOW_TITLE!r}). Start the game first.')

    region = region_for_window(window)

    print(f'Found game window: {window[4]}x{window[3]} at ({window[0]},{window[1]}) | {window[2]}x{window[3]} | title={window[4]!r}')

    print(f'DXcam capture region: {region}; visible pixels mapped into 1920x1080 game coordinates')

    camera = dxcam.create(output_idx=DISPLAY_INDEX, output_color='BGR')

    if camera is None:

        raise RuntimeError('DXcam could not open selected display. Try DISPLAY_INDEX=1.')

    try:

        camera.start(region=region, target_fps=DXCAM_TARGET_FPS, video_mode=True)

    except Exception:

        server.shutdown()

        server.server_close()

        raise

    last_window_check = time.monotonic()

    window_missing_warned = False

    print('DXcam started. Game may be windowed; keep its HUD unobstructed.')

    history = deque(maxlen=HP_HISTORY)

    previous_raw = None

    previous_damage_raw = None

    previous_visual_raw = None

    last_filter_strength = None

    last_filter_update = 0.0

    last_timing_log = 0.0
    last_state_diag_at = -999.0

    last_frame_at = None

    enabled = False

    previous_log = None

    death_template = cv2.imread(str(DEATH_TEMPLATE), cv2.IMREAD_GRAYSCALE) if DEATH_TEMPLATE.exists() else None

    if death_template is None:

        print('ERROR: Missing V13 death template; extract all files from ZIP.')

    else:

        print(f'Loaded death template: {DEATH_TEMPLATE.name}')

    if death_template is None:

        raise FileNotFoundError('V13 death template missing or unreadable')

    # V34 diagnostics: record what the detector actually sees; no threshold changes.

    diagnostic_dir = Path(__file__).with_name('v35_death_diagnostics')

    diagnostic_dir.mkdir(exist_ok=True)

    last_diagnostic_at = -999.0

    diagnostic_count = 0

    diagnostic_limit = 32

    candidate_dir = Path(__file__).with_name("v35_7_death_candidates")

    candidate_dir.mkdir(exist_ok=True)

    last_candidate_saved_at = -999.0

    print(f'V35 death screenshots: {diagnostic_dir.resolve()} (max {diagnostic_limit})')

    death_streak = 0
    secondary_death_streak = 0
    secondary_death_since = None
    secondary_death_peak = 0.0

    death_streak_since = None

    death_streak_peak = 0.0

    best_recent_match = 0.0

    last_debug_at = 0.0

    last_death_at = -DEATH_COOLDOWN_SECONDS

    last_damaged_at = -DEATH_WATCH_SECONDS

    watch_until = 0.0

    previous_hud_visible = False

    last_fatal_flash_at = -FATAL_FLASH_COOLDOWN

    replay_due_at = None

    respawn_streak = 0

    death_hold_until = 0.0

    held_hp = None

    held_intensity = 0.0

    held_critical = 0.0

    death_confirmed = False

    missing_hud_since = None

    healthy_streak = 0

    last_valid_hp_at = 0.0

    last_hit_at = -10.0

    early_blood_until = 0.0

    hit_armed = True

    hit_quiet_since = None

    hit_last_sample = None

    hit_raw_confirm = 0

    hit_event_min_hp = None

    hit_guard_until = 0.0

    low_candidate_since = None

    verified_damage_until = -999.0
    death_watch_blocked = False  # After death, wait for NEW verified damage.

    low_hp_streak_since = None
    critical_hp_since = None
    critical_hp_samples = 0
    critical_observations = []  # Independent evidence for death watch only.

    last_hp_debug_at = -999.0

    hp_debug_dir = Path(__file__).with_name('v36_1_hp_diagnostics')

    hp_debug_dir.mkdir(exist_ok=True)
    last_good_frame = None
    hit_capture_count = 0
    print(f'HIT screenshot folder: {hp_debug_dir.resolve()}')

    # Suspected hits may produce a short provisional flash after 3 corroborating
    # readings; sustained low-HP effects still require full verification.
    pending_hit = None
    pending_clear_since = None
    confirmed_hit_this_frame = False
    confirmed_hit_baseline = None
    pending_capture = None
    accepted_hp = None
    HIT_SETTLE_SECONDS = 0.45
    HIT_PENDING_TIMEOUT = 4.0
    HIT_MIN_VALID_SAMPLES = 8
    HIT_MIN_VALID_SPAN = 0.55
    HIT_RECOVERY_TOLERANCE = 5.0

    try:

        while True:

            # OBS WebSocket request/response handling must remain on one thread.

            if replay_due_at is not None and time.monotonic() >= replay_due_at:

                save_replay(client)

                replay_due_at = None

            if time.monotonic() - last_window_check >= WINDOW_CHECK_SECONDS:

                last_window_check = time.monotonic()

                new_window = find_game_client_rect()

                if new_window is None:

                    if not window_missing_warned:

                        print('WINDOW LOST / MINIMIZED — waiting; HUD detection paused')

                    window_missing_warned = True

                    time.sleep(0.2)

                    continue

                window_missing_warned = False

                new_region = region_for_window(new_window)

                if new_region != region:

                    camera.stop()

                    camera.start(region=new_region, target_fps=DXCAM_TARGET_FPS, video_mode=True)

                    region = new_region

                    history.clear()

                    previous_raw = None

                    previous_damage_raw = None

                    previous_visual_raw = None

                    hit_armed = True

                    hit_quiet_since = None

                    hit_last_sample = None

                    hit_raw_confirm = 0

                    hit_event_min_hp = None

                    hit_guard_until = time.monotonic() + POST_HEAL_HIT_GUARD_SECONDS
                    pending_hit = None
                    pending_clear_since = None
                    pending_capture = None
                    accepted_hp = None

                    low_candidate_since = None

                    print(f'WINDOW MOVED/RESIZED: client={new_window[2]}x{new_window[3]} at ({new_window[0]},{new_window[1]}); DXcam region={region}')

                window = new_window

            capture_started = time.monotonic()

            frame = frame_from_dxcam(camera, region, window)

            capture_ended = time.monotonic()

            now = capture_ended

            raw_for_watch = detect_hp(frame)
            # Keep original bar measurement for provisional verification.
            # The geometry guard may suppress a real large fall-damage drop.
            raw_hp_observation = raw_for_watch

            yellow_damage = detect_yellow_damage_tail(frame, raw_for_watch)

            # Never allow one suspicious frame to fabricate an HP drop.

            if raw_for_watch is not None and previous_damage_raw is not None:

                abrupt = previous_damage_raw - raw_for_watch

                if abrupt > 18 and not yellow_damage:

                    if now - last_hp_debug_at >= 2.0:

                        last_hp_debug_at = now

                        tag = f'hp_rejected_{int(time.time()*1000)}'

                        marked = frame.copy()

                        cv2.rectangle(marked, (BAR_START_X, BAR_TOP_Y),

                                      (BAR_END_X, BAR_BOTTOM_Y), (0, 255, 255), 2)

                        cv2.imwrite(str(hp_debug_dir / f'{tag}.png'), marked)

                        print(f'HP GEOMETRY GUARD: ignored {previous_damage_raw:.1f}% -> '

                              f'{raw_for_watch:.1f}% without yellow; saved {tag}.png')

                    raw_for_watch = None

                    yellow_damage = False

            # V35.7: track raw red-bar readings even when the old geometry
            # guard suppresses them. A hit requires persistent, consistent
            # lower HP; missing frames neither confirm nor cancel it.
            observed_hp = raw_hp_observation
            observed_yellow = yellow_damage
            confirmed_hit_this_frame = False
            confirmed_hit_baseline = None
            if pending_hit is None and observed_hp is not None:
                baseline = accepted_hp if accepted_hp is not None else previous_damage_raw
                if (baseline is not None and baseline >= 8 and
                        baseline - observed_hp >= 1.5 and now >= hit_guard_until):
                    pending_hit = {'baseline': baseline, 'start': now,
                                   'min': observed_hp, 'low_samples': [],
                                   'recovery_samples': 0, 'fast_samples': [],
                                   'yellow_at_start': observed_yellow, 'flash_sent': False}
                    pending_clear_since = None
                    pending_capture = frame.copy()
                    try:
                        tag = f'pending_{int(time.time()*1000)}'
                        cv2.imwrite(str(hp_debug_dir / f'{tag}_frame.png'), frame)
                    except Exception as exc:
                        print(f'HIT DIAGNOSTIC ERROR: {exc}')
                    print(f'HIT PENDING: {baseline:.1f}% -> {observed_hp:.1f}% '
                          f'| yellow={observed_yellow}; waiting for stable lower HP')

            # V36.10: visual-only flash on the FIRST credible damage frame.
            # The yellow damage trail corroborates the raw HP drop. This does
            # not verify HP, arm death detection, or schedule a replay save.
            # flash_sent prevents a second flash from the 3-sample path.
            if (pending_hit is not None and not pending_hit['flash_sent']
                    and observed_hp is not None and observed_yellow
                    and observed_hp <= pending_hit['baseline'] - 1.5
                    and hit_armed and now >= hit_guard_until
                    and now - last_death_at > 2.0):
                with shared_lock:
                    shared['hit_until'] = time.monotonic() + HIT_FLASH_SECONDS
                # Start the main OBS red effect at the SAME instant as the browser flash.
                # V36.10 omitted this, so red waited for later HP processing.
                early_blood_until = now + EARLY_BLOOD_SECONDS
                pending_hit['flash_sent'] = True
                last_hit_at = now
                hit_armed = False
                hit_quiet_since = None
                print(f'HIT INSTANT FLASH: {pending_hit["baseline"]:.1f}% -> '
                      f'{observed_hp:.1f}% | yellow damage trail; verification pending')

            if pending_hit is not None:
                baseline = pending_hit['baseline']
                if observed_hp is not None:
                    pending_hit['min'] = min(pending_hit['min'], observed_hp)
                    if observed_hp >= baseline - HIT_RECOVERY_TOLERANCE:
                        pending_hit['recovery_samples'] += 1
                        pending_hit['low_samples'].clear()
                    elif observed_hp <= baseline - 1.5:
                        pending_hit['recovery_samples'] = 0
                        pending_hit['low_samples'].append((now, observed_hp))
                        pending_hit['low_samples'] = [
                            (t, hp) for t, hp in pending_hit['low_samples']
                            if now - t <= 1.5]
                    else:
                        pending_hit['low_samples'].clear()
                # V36.0: responsive *visual-only* hit flash. Require three
                # consecutive, mutually consistent low readings plus a yellow
                # trail on at least one of those frames. This uses raw observations
                # because the geometry guard can suppress genuine fall damage.
                # The longer verification remains authoritative for HP and death.
                if observed_hp is not None:
                    fast = pending_hit['fast_samples']
                    if observed_hp <= baseline - 1.5:
                        if fast and (now - fast[-1][0] > 0.10 or
                                     abs(observed_hp - fast[-1][1]) > 3.0):
                            fast.clear()
                        fast.append((now, observed_hp, bool(observed_yellow)))
                        del fast[:-3]
                    else:
                        fast.clear()
                    if (not pending_hit['flash_sent'] and len(fast) == 3 and
                            fast[-1][0] - fast[0][0] <= 0.16 and
                            (pending_hit['yellow_at_start'] or any(y for _, _, y in fast)) and
                            hit_armed and now >= hit_guard_until and
                            now - last_death_at > 2.0):
                        with shared_lock:
                            shared['hit_until'] = now + HIT_FLASH_SECONDS
                        # Provisional red effect starts now; final HP still requires verification.
                        early_blood_until = now + EARLY_BLOOD_SECONDS
                        pending_hit['flash_sent'] = True
                        last_hit_at = now
                        hit_armed = False
                        hit_quiet_since = None
                        print(f'HIT FAST FLASH: {baseline:.1f}% -> {observed_hp:.1f}% '
                              f'| 3 consistent samples; verification pending')

                # N/A frames do not reset the pending evidence or start a
                # new hit; old evidence cannot verify without a fresh sample.
                lows = pending_hit['low_samples']
                stable_lower = (observed_hp is not None and
                                len(lows) >= HIT_MIN_VALID_SAMPLES and
                                lows[-1][0] - lows[0][0] >= HIT_MIN_VALID_SPAN and
                                max(hp for _, hp in lows) - min(hp for _, hp in lows) <= 5.0 and
                                now - lows[-1][0] <= 0.15)
                recovered = pending_hit['recovery_samples'] >= 3
                expired = now - pending_hit['start'] >= HIT_PENDING_TIMEOUT
                if stable_lower:
                    confirmed_hit_this_frame = True
                    # V35.9: a verified fall/enemy hit arms YOU DIED recognition
                    # regardless of whether the surviving HP is below 40%.
                    death_watch_blocked = False
                    verified_damage_until = now + DEATH_WATCH_SECONDS
                    print(f'DEATH WATCH READY: verified hit {baseline:.1f}% -> {observed_hp:.1f}%')
                    confirmed_hit_baseline = baseline
                    # Bypass the guard only after corroborating the drop.
                    raw_for_watch = observed_hp
                    print(f'HIT VERIFIED: {baseline:.1f}% -> {observed_hp:.1f}% '
                          f'| {len(lows)} consistent low-HP samples')
                    try:
                        tag = f'verified_{int(time.time()*1000)}'
                        cv2.imwrite(str(hp_debug_dir / f'{tag}_after.png'), frame)
                        if pending_capture is not None:
                            cv2.imwrite(str(hp_debug_dir / f'{tag}_candidate.png'), pending_capture)
                    except Exception as exc:
                        print(f'HIT DIAGNOSTIC ERROR: {exc}')
                    pending_hit = None
                    pending_capture = None
                    accepted_hp = observed_hp
                elif recovered or expired:
                    reason = ('returned to original HP' if recovered
                              else 'no consistent lower HP before timeout')
                    print(f'HIT REJECTED: {reason}; baseline={baseline:.1f}% '
                          f'observed={observed_hp}')
                    pending_hit = None
                    pending_capture = None
                else:
                    # Hold baseline until the candidate is confirmed/rejected.
                    raw_for_watch = baseline
                    yellow_damage = False

            if confirmed_hit_this_frame:
                yellow_damage = False
            elif pending_hit is None and observed_hp is not None:
                accepted_hp = observed_hp

            # V36.15: Royal Sentinel sometimes produces a stable low red bar
            # without a detectable yellow trail. The geometry guard must still
            # protect the accepted HP and instant-hit behavior. Use these raw
            # observations ONLY to arm text recognition; they cannot trigger a
            # death, replay, blood flash, or dissolve by themselves.
            if not death_watch_blocked and observed_hp is not None and 0 < observed_hp < 40:
                critical_observations.append((now, observed_hp))
                critical_observations = [(t, h) for t, h in critical_observations
                                         if now - t <= 1.25]
                if (len(critical_observations) >= 12 and
                        critical_observations[-1][0] - critical_observations[0][0] >= 0.5 and
                        max(h for _, h in critical_observations) -
                        min(h for _, h in critical_observations) <= 6.0):
                    if now >= verified_damage_until:
                        print('DEATH WATCH READY: sustained raw critical HP (text confirmation required)')
                    verified_damage_until = now + DEATH_WATCH_SECONDS
            elif observed_hp is not None and observed_hp >= 40:
                critical_observations.clear()
            else:
                critical_observations = [(t, h) for t, h in critical_observations
                                         if now - t <= 1.25]

            # A verified, sustained critical HP bar is also valid death-watch
            # context. The old <15% requirement missed deaths from ~29% HP.
            # Do NOT trigger a death from low HP alone; YOU DIED must still
            # match for multiple frames before the dissolve/replay is invoked.
            if (not death_watch_blocked and raw_for_watch is not None and
                    0 < raw_for_watch < 40 and pending_hit is None):
                if critical_hp_since is None:
                    critical_hp_since = now
                    critical_hp_samples = 0
                critical_hp_samples += 1
                if critical_hp_samples >= 8 and now - critical_hp_since >= 0.5:
                    if now >= verified_damage_until:
                        print(f'DEATH WATCH READY: stable critical HP={raw_for_watch:.1f}%')
                    verified_damage_until = now + DEATH_WATCH_SECONDS
            else:
                critical_hp_since = None
                critical_hp_samples = 0

            if not death_watch_blocked and raw_for_watch is not None and raw_for_watch < 15:

                if low_hp_streak_since is None:

                    low_hp_streak_since = now

                if now - low_hp_streak_since >= 0.35:

                    verified_damage_until = now + DEATH_WATCH_SECONDS

            else:

                low_hp_streak_since = None

            if not death_watch_blocked and yellow_damage and raw_for_watch is not None and raw_for_watch < 20:

                verified_damage_until = now + DEATH_WATCH_SECONDS

            # Save the most recent verified healthy frame BEFORE the next hit.
            if raw_for_watch is not None and raw_for_watch >= 95.0:
                last_good_frame = frame.copy()

            detection_ended = time.monotonic()

            frame_gap_ms = (now - last_frame_at) * 1000 if last_frame_at is not None else 0.0

            last_frame_at = now

            hit_detected_this_frame = False

            # V30 damage-event state machine. The first yellow trail triggers

            # immediately; raw red-bar drops need two frames of confirmation.

            # After firing, keep the pulse locked out until the damage animation

            # disappears AND the HP reading settles for a continuous interval.

            if raw_for_watch is not None:

                damage_drop = (previous_damage_raw is not None and

                               previous_damage_raw >= 8.0 and

                               previous_damage_raw - raw_for_watch >= HIT_MIN_DROP)

                if damage_drop:

                    hit_raw_confirm += 1

                elif hit_last_sample is not None and raw_for_watch < hit_last_sample - HIT_MIN_DROP:

                    hit_raw_confirm += 1

                else:

                    hit_raw_confirm = 0

                # Raw bar drops alone are unreliable during bonfire/HUD transitions.

                # A pulse requires the independently detected yellow damage trail.

                candidate = (yellow_damage and raw_for_watch < 95.0 and

                             previous_damage_raw is not None and

                             previous_damage_raw - raw_for_watch >= 1.5)

                if (hit_armed and candidate and now >= hit_guard_until

                        and now - last_death_at > 2.0):

                    with shared_lock:

                        shared['hit_until'] = now + HIT_FLASH_SECONDS

                    early_blood_until = now + EARLY_BLOOD_SECONDS

                    last_hit_at = now

                    hit_detected_this_frame = True

                    hit_armed = False

                    hit_quiet_since = None

                    hit_event_min_hp = raw_for_watch

                    # Always capture the exact triggering frame, even when yellow=True.
                    # Preserve the preceding verified-HP frame for comparison.
                    try:
                        hit_capture_count += 1
                        tag = f'hit_{hit_capture_count:03d}_{int(time.time()*1000)}'
                        annotated = frame.copy()
                        cv2.rectangle(annotated, (BAR_START_X, BAR_TOP_Y),
                                      (BAR_END_X, BAR_BOTTOM_Y), (0, 255, 255), 2)
                        cv2.putText(annotated,
                                    f'HP {previous_damage_raw:.1f} -> {raw_for_watch:.1f} yellow={yellow_damage}',
                                    (200, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                        bar_crop = frame[max(0, BAR_TOP_Y-25):BAR_BOTTOM_Y+30,
                                         max(0, BAR_START_X-30):BAR_END_X+30]
                        outputs = {
                            'frame': cv2.imwrite(str(hp_debug_dir / f'{tag}_frame.png'), frame),
                            'annotated': cv2.imwrite(str(hp_debug_dir / f'{tag}_annotated.png'), annotated),
                            'hp_crop': cv2.imwrite(str(hp_debug_dir / f'{tag}_hp_crop.png'), bar_crop),
                        }
                        if last_good_frame is not None:
                            outputs['before'] = cv2.imwrite(
                                str(hp_debug_dir / f'{tag}_before.png'), last_good_frame)
                        (hp_debug_dir / f'{tag}_info.txt').write_text(
                            f'previous_hp={previous_damage_raw}\ncurrent_hp={raw_for_watch}\n'
                            f'yellow={yellow_damage}\nwindow={window!r}\nregion={region!r}\n'
                            f'capture_shape={frame.shape!r}\n', encoding='utf-8')
                        if not all(outputs.values()):
                            print(f'HIT DIAGNOSTIC WRITE FAILED: {outputs}')
                        else:
                            print(f'HIT DIAGNOSTIC SAVED: {tag} in {hp_debug_dir}')
                    except Exception as hit_diag_error:
                        print(f'HIT DIAGNOSTIC ERROR: {hit_diag_error}')
                    print(f'HIT EDGE: {previous_damage_raw if previous_damage_raw is not None else -1:.1f}% -> {raw_for_watch:.1f}% | '

                          f'yellow={yellow_damage} | capture={1000*(capture_ended-capture_started):.0f}ms | '

                          f'detect={1000*(detection_ended-capture_ended):.1f}ms')

                if not hit_armed:

                    hit_event_min_hp = (raw_for_watch if hit_event_min_hp is None

                                        else min(hit_event_min_hp, raw_for_watch))

                    # Only rearm when there is no yellow trail and HP has been

                    # steady. A transient dip resets the settling timer.

                    settled = (not yellow_damage and hit_last_sample is not None and

                               abs(raw_for_watch - hit_last_sample) <= HIT_STABLE_TOLERANCE)

                    if settled:

                        if hit_quiet_since is None:

                            hit_quiet_since = now

                        elif now - hit_quiet_since >= HIT_REARM_STABLE_SECONDS:

                            hit_armed = True

                            hit_quiet_since = None

                            hit_raw_confirm = 0

                            print(f'HIT REARMED: stable HP={raw_for_watch:.1f}%')

                    else:

                        hit_quiet_since = None

                hit_last_sample = raw_for_watch

                previous_damage_raw = raw_for_watch

            else:

                previous_damage_raw = None

                hit_last_sample = None

                hit_raw_confirm = 0

                hit_quiet_since = None

            # Arm when damage has recently been observed, then keep watching

            # through the delayed death animation and hidden HUD.

            if now < verified_damage_until:

                last_damaged_at = now

            hud_visible = raw_for_watch is not None

            if (not death_watch_blocked and previous_hud_visible and not hud_visible
                    and now - last_damaged_at < 3.0):

                watch_until = now + DEATH_WATCH_SECONDS

                # Watching is diagnostic only; never hold or black out at a bonfire.

                print('DEATH WATCH ARMED — looking for delayed YOU DIED text')

            # A missing HUD after recent damage is only a *possible* fatal hit.

            # Give a brief blood flash, but reserve the main glitch and replay for confirmed text.

            possible_fatal = (not death_watch_blocked and previous_hud_visible and not hud_visible and

                              now - last_damaged_at < 3.0 and

                              now - last_fatal_flash_at >= FATAL_FLASH_COOLDOWN and

                              now - last_death_at >= DEATH_COOLDOWN_SECONDS)

            if possible_fatal:

                # Don't add a red fatal flash while YOU DIED is about to appear.

                last_fatal_flash_at = now

            previous_hud_visible = hud_visible

            watching = (not death_watch_blocked and now < verified_damage_until and

                        (now < watch_until or now - last_damaged_at < 3.0))

            if watching and now - last_death_at >= DEATH_COOLDOWN_SECONDS:

                mask = red_text_mask(frame)

                match = death_similarity(mask, death_template)

                # Save one annotated frame/0.5 seconds while watching, limited to 32.

                # Files show the EXACT full 1920x1080 normalized frame fed to the detector.

                if now - last_diagnostic_at >= 0.5 and diagnostic_count < diagnostic_limit:

                    try:

                        diagnostic_count += 1

                        last_diagnostic_at = now

                        tag = f'{diagnostic_count:02d}_score_{match:.3f}'

                        marked = frame.copy()

                        x1, y1, x2, y2 = DEATH_ROI

                        cv2.rectangle(marked, (x1, y1), (x2 - 1, y2 - 1), (0, 255, 255), 2)

                        cv2.putText(marked, f'DEATH ROI score={match:.3f}', (max(4, x1), max(25, y1 - 12)),

                                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)

                        cv2.imwrite(str(diagnostic_dir / f'{tag}_frame.png'), marked)

                        cv2.imwrite(str(diagnostic_dir / f'{tag}_red_mask.png'), mask)

                        (diagnostic_dir / f'{tag}_info.txt').write_text(

                            f'window={window!r}\nDXcam_region={region!r}\n'

                            f'normalized_frame_shape={frame.shape!r}\nDEATH_ROI={DEATH_ROI!r}\n'

                            f'template_shape={death_template.shape!r}\nscore={match:.6f}\n'

                            f'red_mask_pixels={cv2.countNonZero(mask)}\n'

                            f'watch_remaining_seconds={max(0, watch_until-now):.2f}\n',

                            encoding='utf-8')

                        print(f'V35.1 DIAGNOSTIC SAVED: {tag} | window={window[2]}x{window[3]} '

                              f'| ROI={DEATH_ROI} | frame={frame.shape[1]}x{frame.shape[0]}')

                    except Exception as diagnostic_error:

                        print(f'V34 diagnostic save failed: {diagnostic_error}')

                best_recent_match = max(best_recent_match, match)

                if now - last_debug_at >= 1.0:

                    print(f'DEATH MATCH: {match:.3f} | best: {best_recent_match:.3f} | primary: {DEATH_THRESHOLD:.2f} | sustained: {DEATH_SECONDARY_THRESHOLD:.2f}')

                    best_recent_match = 0.0

                    last_debug_at = now

                # Use the actual score on EACH frame for confirmation.

                # best_recent_match is diagnostic only and resets when logged.

                if match >= DEATH_THRESHOLD:

                    if death_streak == 0:

                        death_streak_since = now

                        death_streak_peak = 0.0

                    death_streak += 1

                    death_streak_peak = max(death_streak_peak, match)

                    # Capture candidate frames without the 32-image diagnostic limit.

                    if now - last_candidate_saved_at >= 0.12:

                        last_candidate_saved_at = now

                        tag = f"candidate_{int(time.time()*1000)}_score_{match:.3f}"

                        cv2.imwrite(str(candidate_dir / f"{tag}_frame.png"), frame)

                        cv2.imwrite(str(candidate_dir / f"{tag}_red_mask.png"), mask)

                        print(f"DEATH CANDIDATE SAVED: {tag}")

                else:

                    death_streak = 0

                    death_streak_since = None

                    death_streak_peak = 0.0

                # Sustained secondary match for dimmer YOU DIED lettering.
                if match >= DEATH_SECONDARY_THRESHOLD:
                    if secondary_death_streak == 0:
                        secondary_death_since = now
                        secondary_death_peak = 0.0
                    secondary_death_streak += 1
                    secondary_death_peak = max(secondary_death_peak, match)
                else:
                    secondary_death_streak = 0
                    secondary_death_since = None
                    secondary_death_peak = 0.0

                instant_match = False

                sustained_match = (death_streak >= DEATH_CONFIRM_FRAMES and

                                   death_streak_since is not None and

                                   now - death_streak_since >= DEATH_MIN_CONFIRM_SECONDS and

                                   death_streak_peak >= DEATH_CONFIRM_PEAK)

                secondary_match = (secondary_death_streak >= DEATH_SECONDARY_FRAMES and
                                   secondary_death_since is not None and
                                   now - secondary_death_since >= DEATH_SECONDARY_SECONDS and
                                   secondary_death_peak >= DEATH_SECONDARY_PEAK)
                if sustained_match or secondary_match:

                    # Always save the precise confirmation frame and red mask.

                    tag = f"CONFIRMED_{int(time.time()*1000)}_score_{match:.3f}"

                    cv2.imwrite(str(candidate_dir / f"{tag}_frame.png"), frame)

                    cv2.imwrite(str(candidate_dir / f"{tag}_red_mask.png"), mask)

                    print(f"DEATH CONFIRMATION IMAGES: {tag}")

                    confirmation = ('single strong frame' if instant_match else

                                    f'{secondary_death_streak} secondary frames' if secondary_match and not sustained_match else f'{death_streak} consecutive frames')

                    print(f'YOU DIED MATCHED: {match:.3f} ({confirmation})')

                    replay_due_at = trigger_death()

                    death_confirmed = True

                    death_hold_until = now + DEATH_HOLD_SECONDS

                    last_death_at = now

                    death_streak = 0

                    death_streak_since = None

                    death_streak_peak = 0.0
                    secondary_death_streak = 0
                    secondary_death_since = None
                    secondary_death_peak = 0.0

                    # A confirmed death consumes all preceding damage evidence.
                    # Prevent the old low-HP/HUD transition from arming again
                    # during respawn or a subsequent bonfire interaction.
                    watch_until = 0.0
                    verified_damage_until = -999.0
                    last_damaged_at = -DEATH_WATCH_SECONDS
                    death_watch_blocked = True
                    low_hp_streak_since = None
                    critical_hp_since = None
                    critical_hp_samples = 0
                    critical_observations.clear()
                    previous_hud_visible = False
                    print('DEATH WATCH CLEARED — fresh verified hit required')

            else:

                death_streak = 0

                death_streak_since = None

                death_streak_peak = 0.0
                secondary_death_streak = 0
                secondary_death_since = None
                secondary_death_peak = 0.0

            if death_confirmed and now >= death_hold_until:

                death_confirmed = False

                held_hp = None

            # Release on a stable visible HUD after the death transition.

            # A 90% requirement was unsafe for windowed/cropped captures.

            with shared_lock:

                dissolve_started = shared['dissolve_start']

                dissolve_ends = shared['dissolve_until']

            if dissolve_started and now < dissolve_ends and now - dissolve_started >= 6.2:

                respawn_streak = respawn_streak + 1 if raw_for_watch is not None and 90.0 <= raw_for_watch <= 100.0 else 0

                if respawn_streak >= 8:

                    with shared_lock:

                        if not shared['dissolve_release_start']:

                            shared['dissolve_release_start'] = now

                            print('RESPAWN DETECTED — releasing black overlay back to gameplay')
                            verified_damage_until = -999.0
                            last_damaged_at = -DEATH_WATCH_SECONDS
                            watch_until = 0.0
                            death_watch_blocked = True

                    respawn_streak = 0

            else:

                respawn_streak = 0

            # Red edge is the new HP immediately; yellow is the trailing old-HP

            # animation. Do NOT wait for yellow to disappear before activating.

            projected_hp = None

            if yellow_damage and raw_for_watch is not None:

                projected_hp = raw_for_watch

            # Ignore brief missing HUD samples while combat effects are active.

            # Preserve raw_for_watch for death detection above; only smooth visual state.

            raw = raw_for_watch

            if raw is None:

                if missing_hud_since is None:

                    missing_hud_since = now

                # V36.17: Keep the last confirmed critical red pulse through a
                # missing-HUD death animation, but ONLY while a recent verified
                # damage event has armed the YOU DIED text watcher. Never use
                # missing HUD alone to confirm death or trigger a black screen.
                death_transition_watch = (
                    not death_watch_blocked
                    and held_hp is not None
                    and held_hp <= CRITICAL_START_HP
                    and now < verified_damage_until
                    and (now < watch_until or now - last_damaged_at < 3.0)
                )
                grace = (5.0 if death_transition_watch else
                         CRITICAL_MISSING_GRACE if held_hp is not None and held_hp <= CRITICAL_START_HP
                         else 0.75)

                if (held_hp is not None and now - missing_hud_since < grace

                        and not death_confirmed):

                    raw = held_hp

            else:

                missing_hud_since = None

                last_valid_hp_at = now



            # V35.2: do not let a transient false low bar at a bonfire activate

            # the filter. Real yellow damage remains immediate. A sustained low

            # reading without yellow is accepted after a short confirmation.

            if raw_for_watch is not None and raw_for_watch >= 98.0 and previous_raw is not None and previous_raw < 75.0:

                hit_guard_until = max(hit_guard_until, now + POST_HEAL_HIT_GUARD_SECONDS)

            if raw is not None and raw < EFFECT_START_HP and not yellow_damage and not death_confirmed:

                if low_candidate_since is None:

                    low_candidate_since = now

                if now < hit_guard_until or now - low_candidate_since < LOW_HP_CONFIRM_SECONDS:

                    if not enabled:

                        # Keep the previous healthy display state; do not manufacture

                        # a missing HUD, which could be mistaken for a death transition.

                        raw = previous_raw if previous_raw is not None and previous_raw >= EFFECT_START_HP else None

            else:

                low_candidate_since = None



            # Require stable healthy readings before disabling an active blood effect.

            if raw is not None and raw >= EFFECT_START_HP and enabled and not death_confirmed:

                healthy_streak += 1

                # One spurious full bar must not disable blood at 1 HP.

                if healthy_streak < 5 and held_hp is not None:

                    raw = held_hp

            else:

                healthy_streak = 0

            if raw is None:

                history.clear()

                previous_raw = None

                if death_confirmed and now < death_hold_until and held_hp is not None:

                    hp, intensity, active = held_hp, held_intensity, True

                    state = 'DEATH TRANSITION'

                else:

                    hp, intensity, active, state = None, 0.0, False, 'HUD NOT FOUND'

                    held_hp = None

                    death_confirmed = False

            else:

                if previous_raw is not None and raw - previous_raw >= HEAL_JUMP:

                    history.clear()

                    hit_guard_until = now + POST_HEAL_HIT_GUARD_SECONDS

                    early_blood_until = 0.0

                    with shared_lock:

                        shared['hit_until'] = 0.0

                    print(f'HEAL / RESPAWN: {previous_raw:.1f}% -> {raw:.1f}%')

                previous_raw = raw

                history.append(raw)

                hp = float(np.median(history))

                # Attack edge: don't wait for the rolling median or the bar animation.

                if raw < hp:

                    hp = float(raw)

                if projected_hp is not None and projected_hp < EFFECT_START_HP:

                    hp = min(hp, projected_hp)

                # A visible, healthy HUD means gameplay resumed: cancel provisional hold.

                if hp >= EFFECT_START_HP and not death_confirmed:

                    death_hold_until = 0.0

                    held_hp = None

                elif death_confirmed and now < death_hold_until:

                    # Ignore transient HP-like pixels during the death screen.

                    hp = held_hp if held_hp is not None else hp

                # Zero HP alone is not reliable enough to indicate death.

                # 0-1% is the most critical state, not a reason to turn effects off.

                active = 0.0 <= hp < EFFECT_START_HP

                intensity = 0.0

                if active:

                    intensity, settings = effect_settings(hp)

                state = 'NEAR ZERO' if hp <= 1 else ('CRITICAL' if hp <= CRITICAL_START_HP else ('LOW' if active else 'NORMAL'))

                if active and not death_confirmed:

                    held_hp, held_intensity = hp, intensity

                    held_critical = float(np.clip((CRITICAL_START_HP - hp) / CRITICAL_START_HP, 0, 1))

                if death_confirmed and now < death_hold_until and held_hp is not None:

                    hp, intensity, active, state = held_hp, held_intensity, True, 'DEATH TRANSITION'

            # A hit or yellow trail activates the MAIN OBS color filter immediately,

            # before the red bar has completed its drain animation. The measured

            # HP is left untouched for logging, death recognition, and healing.

            early_blood = (now < early_blood_until and not death_confirmed and

                           raw_for_watch is not None and raw_for_watch > 0)

            if early_blood and not death_confirmed:
                provisional_intensity = effect_settings(EARLY_BLOOD_HP)[0]
                if not active or intensity < provisional_intensity:
                    active = True
                    intensity = provisional_intensity
                    state = 'EARLY HIT'

            # Publish browser effect BEFORE slow OBS filter WebSocket commands.

            # Yellow drain indicates damage; red edge is already the new HP.

            publish(hp, intensity, active, held_critical if state == 'DEATH TRANSITION' else None)

            filter_started = time.monotonic()

            if active:

                # IMPORTANT V26 BUG FIX: provisional early-hit filter must use EARLY_BLOOD_HP,

                # not the still-healthy measured HP (which produced near-zero color).

                settings_hp = EARLY_BLOOD_HP if state == 'EARLY HIT' else hp

                _, settings = effect_settings(settings_hp)

                # Avoid a filter-settings WebSocket round trip every screenshot.

                # First activation always sets strength immediately.

                if (not enabled or last_filter_strength is None or

                    (abs(intensity - last_filter_strength) >= 0.045 and

                     now - last_filter_update >= 0.12)):

                    client.set_source_filter_settings(EFFECT_SOURCE, FILTER_NAME, settings)

                    last_filter_strength = intensity

                    last_filter_update = now

            if active != enabled:

                client.set_source_filter_enabled(EFFECT_SOURCE, FILTER_NAME, active)

                enabled = active

                if not active:

                    last_filter_strength = None

                print('LOW HEALTH EFFECT ' + ('ON' if enabled else 'OFF'))

            filter_ended = time.monotonic()

            if hit_detected_this_frame:

                print(f'HIT APPLIED: capture={1000*(capture_ended-capture_started):.0f}ms '

                      f'| analysis+logic={1000*(filter_started-capture_ended):.1f}ms '

                      f'| OBS filter calls={1000*(filter_ended-filter_started):.0f}ms '

                      f'| active={active} | state={state}')

            if now - last_timing_log >= TIMING_LOG_INTERVAL:

                print(f'LATENCY: frame gap={frame_gap_ms:.0f}ms | DXcam capture={1000*(capture_ended-capture_started):.0f}ms '

                      f'| HP detection={1000*(detection_ended-capture_ended):.1f}ms '

                      f'| OBS filter calls={1000*(filter_ended-filter_started):.0f}ms')

                last_timing_log = now

            log = (None if hp is None else round(hp, 1), state, enabled)

            if log != previous_log:

                shown = 'N/A' if hp is None else f'{hp:.1f}%'

                print(f'HP: {shown:>6} | {state:<13} | FX: {"ON" if enabled else "OFF"} | Strength: {intensity * 100:5.1f}%')

                previous_log = log

            # Capture state every 250ms even if no console messages change.
            if now - last_state_diag_at >= 0.25:
                last_state_diag_at = now
                try:
                    log_state_snapshot(now, hp, state, enabled, death_confirmed,
                                       death_watch_blocked, verified_damage_until,
                                       watch_until, replay_due_at)
                except Exception as exc:
                    print(f'STATE DIAGNOSTIC ERROR: {exc}')

            if SAMPLE_INTERVAL > 0:

                time.sleep(SAMPLE_INTERVAL)

    except KeyboardInterrupt:

        print('\nStopping GameEffx2OBS...')

    finally:

        publish(None, 0.0, False)

        try:

            client.set_source_filter_enabled(EFFECT_SOURCE, FILTER_NAME, False)

        except Exception as exc:

            print(f'Could not disable filter: {exc}')

        try:

            camera.stop()

        except Exception as exc:

            print(f'DXcam shutdown: {exc}')

        server.shutdown()

        server.server_close()

        print('Stopped.')





if __name__ == '__main__':

    main()
