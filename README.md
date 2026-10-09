# GameEffx2OBS — Dark Souls Remastered OBS Effects (V36.17)

Windows-based, local Python + OBS overlay that reacts to **Dark Souls Remastered** gameplay. It reads the visible game window using DXcam, detects HP changes, drives an OBS Color Correction filter for damage effects, animates a critical-health heartbeat vignette, detects the **YOU DIED** screen, transitions to black, and requests an OBS Replay Buffer save after a delay.

> **Tested baseline:** This package contains the V36.17 files from the working setup. It has not been independently tested on another PC. It is tailored to a **1920×1080** Dark Souls client and a Windows desktop; other resolutions, HUD scaling, or display layouts may require code adjustments.

## 1. Install prerequisites

1. Install **OBS Studio** and **Python 3.11+ (64-bit)** on Windows. During Python installation, enable **Add Python to PATH**.
2. Install and launch **Dark Souls Remastered**.
3. In OBS, open **Tools → WebSocket Server Settings**. Enable the WebSocket server, set port **4455**, enable authentication, and set a password. The Python file currently defaults to `gameeffx`; **change `PASSWORD` near the top of `gameeffx_v36_11.py` to match your OBS password before running**. Do not publish a personal password in a public repository.
4. Use a game/client resolution of **1920×1080** for the initial setup. Make sure the game window is visible and not covered by OBS or another window while playing: DXcam captures pixels from the monitor.

## 2. Get the project

On GitHub, click **Code → Download ZIP**, extract it, and open PowerShell inside the extracted folder. Alternatively, after publishing the repo:

```powershell
git clone https://github.com/YOUR-USERNAME/GameEffx.git
cd GameEffx
```

Replace `YOUR-USERNAME` with the actual account name. If you downloaded the ZIP, just open its folder instead.

## 3. Install Python packages

Open PowerShell in the project folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `py` is unavailable, use `python` instead. You do not need to activate the virtual environment when using the commands above.

## 4. Configure OBS scenes and sources (names matter)

Create these **two scenes** in OBS:

### Scene A: `GameEffx Gameplay`

1. Add a **Game Capture** source targeting Dark Souls Remastered.
2. Right-click the **`GameEffx Gameplay` scene** in the Scenes panel, choose **Filters**, and add an **Effect Filter → Color Correction** named **`Low Health`**. The script updates and enables/disables this filter automatically. Initially leave it disabled if desired.
3. Keep the game's audio capture and microphone configured as you normally would.

### Scene B: `Stream`

1. Add the **`GameEffx Gameplay` scene** as a **Scene source** (a nested scene, not a duplicate Game Capture).
2. Add a **Browser** source named **`GameEffx Overlay`** and place it **above** the gameplay scene in the Sources list.
3. Browser source settings:
   - **URL:** `http://127.0.0.1:8765/`
   - **Width:** `1920`
   - **Height:** `1080`
4. Keep **both** the gameplay scene source and the overlay source visible (eye icons enabled).
5. Use the **`Stream`** scene when playing/recording.

**Important:** `GameEffx Gameplay` and `Low Health` must match the strings in the Python file exactly, including spaces and capitalization.

## 5. Enable replay saving

1. In OBS, open **Settings → Output**. Configure a **Replay Buffer** with enough replay duration for your needs (for example, 30–60 seconds), and choose your recording path.
2. Apply settings, then click **Start Replay Buffer** in OBS **before** starting gameplay.
3. When a death is confirmed, GameEffx requests a replay save **7 seconds later**. It does not automatically start the Replay Buffer.
4. Locate clips through **File → Show Recordings** or your configured recording directory.

## 6. Start GameEffx (order matters)

1. Start OBS, confirm the WebSocket server is enabled, and start the Replay Buffer.
2. Start Dark Souls Remastered; keep its window visible.
3. In PowerShell in the project folder, run:

```powershell
.\.venv\Scripts\python.exe gameeffx_v36_11.py
```

4. Confirm the console starts without an exception and shows the local overlay address.
5. **Then** select `Stream` in OBS and right-click `GameEffx Overlay` → **Properties** → **Refresh cache of current page**.
6. Take damage in game and verify the red effect; at low HP verify the heartbeat. After death, check the black dissolve and replay output.

**Only run one copy of the Python script at a time.** Stop it with `Ctrl+C` before restarting. Do not keep old debug/browser tabs open during normal play.

## 7. Settings you may need to change

At the top of `gameeffx_v36_11.py`:

| Setting | Default | Meaning |
|---|---|---|
| `HOST`, `PORT` | `localhost`, `4455` | OBS WebSocket connection |
| `PASSWORD` | `gameeffx` | Must match your OBS WebSocket password; change locally |
| `DISPLAY_INDEX` | `0` | Primary display; use `1` for a second display |
| `GAME_WINDOW_TITLE` | `DARK SOULS` | Substring used to find the game window |
| `EFFECT_SOURCE` | `GameEffx Gameplay` | OBS scene with the Color Correction filter |
| `FILTER_NAME` | `Low Health` | Exact Color Correction filter name |
| `OVERLAY_PORT` | `8765` | Local browser overlay HTTP port |
| `DEATH_TEMPLATE` | `you_died_template_v14.png` | Death-text detection template, supplied in repo |

**Do not rename** `gameeffx_v36_11.py` or `gameeffx_overlay_v35.html` casually: these are the filenames the working V36.17 code uses.

## Troubleshooting

| Problem | What to check |
|---|---|
| Browser overlay shows nothing / no pulse | Start Python first; refresh the Browser Source; close old debug tabs; verify URL and only one Python process is running |
| Red effect works but pulse does not | Verify fresh overlay state; refresh OBS Browser Source. `http://127.0.0.1:8765/?debug` can be used in a separate browser for temporary diagnostics |
| `ConnectionRefusedError` / OBS WebSocket error | OBS open, WebSocket enabled, port 4455, matching password, no other instance |
| Filter not found | Scene named `GameEffx Gameplay`, Color Correction filter named `Low Health` **on that scene** |
| Overlay port already in use | Stop the previous GameEffx Python process or the app using port 8765 |
| HP/death not detected | Use 1920×1080, correct monitor index, correct game window, visible/unobstructed game |
| Replay not saved | Start OBS Replay Buffer; check recording path and console errors |
| Missing template | Ensure `you_died_template_v14.png` sits next to the Python file |

Diagnostics are written to a local `gameeffx_v36_4_logs` folder. These and replay videos are excluded by `.gitignore`.

## Publishing this folder to GitHub (for the owner)

1. Create an empty repository on GitHub, e.g. **GameEffx** (don't initialize with README, since this folder already has one).
2. In PowerShell inside this folder, run:

```powershell
git init
git add .
git commit -m "Initial GameEffx V36.17 release"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/GameEffx.git
git push -u origin main
```


**Privacy note:** Review files before making the repository public. Never commit your actual OBS WebSocket password, local logs, or recordings. The included script's example password is `gameeffx`; replace it locally and keep your real password private.

## Credits and scope

GameEffx is a community-created local OBS effect tool, not affiliated with FromSoftware, Bandai Namco, or OBS Studio. This repository includes only project source and a small text-detection template; it does not include game assets or game binaries beyond that template.
