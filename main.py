"""
Desktop Cat Pet
================
A frameless, always-on-top, click-through-transparent desktop pet built on
PySide6 + pynput.

Setup
-----
1. pip install -r requirements.txt
2. Put this file next to an `assets/` folder containing your sprite PNGs,
   named like:  <State>-<Direction>-<FrameNumber>.png
   e.g. Run-Up-Left-3.png, Lay-Down-2.png, Look-Right-1.png
   One-off sprites with no direction/frame (e.g. Pick.png) are also supported.
3. Run: python main.py

Controls
--------
- Double-click the cat: toggle between WALKING (chases your cursor) and
  SLEEPING (idle nap loop).
- Single-click the cat while it's sleeping: brief "alert" look-up.
- Click-and-drag the cat: pick it up and place it anywhere (shows the
  "Pick" sprite while held).
- Clicking anywhere that ISN'T on an opaque cat pixel passes straight
  through to your desktop -- the window is masked to the sprite's shape.

Design notes
------------
- AssetLibrary scans the assets folder ONCE and indexes every sprite by
  (state, direction), sorted by frame number. This means frame counts can
  differ freely between states/directions (Run-1..8, Walk-1..4, Look-1..5,
  etc.) with no hardcoded assumptions anywhere in the animation logic.
- If a requested (state, direction) pair has no sprites, it automatically
  falls back to the nearest direction in the 8-way ring, then to any
  direction available for that state, so a missing folder entry never
  breaks the animation.
- Global mouse *position* is tracked via pynput (needed to know where the
  cursor is even when it's not over our window). Global mouse *clicks* are
  NOT used -- all click/drag/double-click handling is done through normal
  Qt mouse events on the pet window itself, so clicking elsewhere on the
  screen no longer accidentally toggles the pet.
"""

import sys
import os
import re
import math
import time
from collections import defaultdict

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QIcon, QAction
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QSystemTrayIcon, QMenu,
    QWidget, QVBoxLayout, QPushButton,
)
from pynput import mouse, keyboard


def get_base_dir():
    """Folder to load bundled files (assets, icon) from.

    When running as a plain script this is the script's own folder. When
    packaged with PyInstaller (--onefile), bundled data is unpacked to a
    temporary folder at runtime, whose path PyInstaller exposes as
    sys._MEIPASS -- this makes asset loading work identically either way.
    """
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

# 8-way compass ring, ordered so that adjacent entries are visually adjacent
# directions. Used for "nearest available direction" fallback.
DIRECTIONS = [
    "Right", "Up-Right", "Up", "Up-Left",
    "Left", "Down-Left", "Down", "Down-Right",
]


class AssetLibrary:
    """Indexes every sprite in the assets folder by (state, direction)."""

    # Matches e.g. "Run-Up-Left-3.png" -> state=Run, direction=Up-Left, frame=3
    FRAME_RE = re.compile(
        r"^(?P<state>[A-Za-z]+)-(?P<direction>[A-Za-z]+(?:-[A-Za-z]+)?)-(?P<frame>\d+)\.png$",
        re.IGNORECASE,
    )

    def __init__(self, folder):
        self.folder = folder
        self.frames = {}   # state -> direction -> [sorted frame paths]
        self.singles = {}  # bare name (no direction/frame) -> path
        self._scan()

    def _scan(self):
        if not os.path.isdir(self.folder):
            print(f"[AssetLibrary] WARNING: assets folder not found at '{self.folder}'.")
            return

        raw = defaultdict(lambda: defaultdict(dict))
        for fname in os.listdir(self.folder):
            if not fname.lower().endswith(".png"):
                continue
            path = os.path.join(self.folder, fname)
            match = self.FRAME_RE.match(fname)
            if match:
                state = match.group("state")
                direction = match.group("direction")
                frame_num = int(match.group("frame"))
                raw[state][direction][frame_num] = path
            else:
                self.singles[os.path.splitext(fname)[0]] = path

        for state, by_direction in raw.items():
            self.frames[state] = {
                direction: [paths[k] for k in sorted(paths)]
                for direction, paths in by_direction.items()
            }

        total_frames = sum(len(seq) for d in self.frames.values() for seq in d.values())
        print(f"[AssetLibrary] Indexed {total_frames} frames across states: "
              f"{list(self.frames.keys())}")

    def get_sequence(self, state, direction):
        """Sorted frame list for state+direction, with graceful fallback."""
        by_direction = self.frames.get(state)
        if not by_direction:
            return []
        if direction in by_direction:
            return by_direction[direction]

        # Fall back to the nearest available direction in the 8-way ring.
        if direction in DIRECTIONS:
            idx = DIRECTIONS.index(direction)
            n = len(DIRECTIONS)
            for offset in range(1, n):
                for cand_idx in (idx - offset, idx + offset):
                    cand = DIRECTIONS[cand_idx % n]
                    if cand in by_direction:
                        return by_direction[cand]

        # Last resort: whatever direction exists for this state.
        for seq in by_direction.values():
            return seq
        return []

    def get_single(self, name):
        return self.singles.get(name)


class DesktopPet(QMainWindow):
    DRAG_THRESHOLD = 6          # px of movement before a press counts as a drag
    SINGLE_CLICK_DELAY_MS = 220  # wait this long to make sure it's not a double-click

    # What the cat is doing the instant the app launches. "SLEEPING" (lying
    # down, Lay sprites) or "SITTING" (upright, Sitting sprites) both count
    # as "not moving." Change this if you want a different startup pose.
    INITIAL_MODE = "SLEEPING"

    # Ignore mouse clicks for this long after launch. Without this, if your
    # cursor happens to be sitting on top of the window the instant it's
    # created (e.g. right after clicking "Run" in your editor), Qt can
    # deliver that as a click/double-click on the new window and the cat
    # would immediately start running before you ever touched it.
    STARTUP_CLICK_GRACE_SEC = 0.8

    def __init__(self, assets_folder):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # NOTE: we deliberately do NOT set WA_TransparentForMouseEvents on the
        # window -- we want the window itself to receive clicks so the cat
        # can be dragged. Instead, the LABEL is made mouse-transparent so
        # clicks fall through it to the window, and setMask() (applied per
        # frame) restricts the window's clickable/visible shape to the
        # sprite's opaque pixels.

        self.label = QLabel(self)
        self.label.setScaledContents(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.resize(100, 100)
        self.label.setGeometry(0, 0, 100, 100)
        self.move(500, 500)

        self.asset_lib = AssetLibrary(assets_folder)
        self._pixmap_cache = {}

        # --- State machine ---
        self.mode = self.INITIAL_MODE   # SLEEPING, SITTING, WALKING, ALERT, DRAGGING
        self.direction = "Down"
        self.frame_index = 1
        self._launch_time = time.time()

        self.target_x, self.target_y = 500, 500

        # Walking-mode movement tuning: THREE speed tiers, each with its own
        # sprite set and speed range, so the chase decelerates naturally
        # (Run -> Walk -> Sitting) instead of sprinting right up and then
        # abruptly sitting.
        self.MOVE_TUNING = {
            "RUN":  {"state": "Run",  "min_step": 6, "max_step": 14, "ease": 0.18},
            "WALK": {"state": "Walk", "min_step": 2, "max_step": 6,  "ease": 0.12},
        }
        # Hysteresis bands (enter/exit are different on purpose, so the cat
        # doesn't flicker between tiers when hovering near a boundary).
        self.settle_enter_distance = 20   # WALK/RUN -> SETTLE (sit) below this
        self.settle_exit_distance = 45    # SETTLE -> WALK above this
        self.run_enter_distance = 90      # WALK -> RUN above this
        self.run_exit_distance = 60       # RUN -> WALK below this
        self.walk_substate = "WALK"       # RUN, WALK, or SETTLE

        # Smoothed cursor-direction vector, so small jitter in raw mouse
        # movement doesn't cause the sprite to flicker between facings.
        self.smooth_dx = 0.0
        self.smooth_dy = 0.0

        self.alert_ticks = 0
        self.alert_duration_ticks = 20  # 20 * 100ms = 2s

        # Typing reaction: pynput keyboard listener stamps this; if a key
        # was pressed recently, the cat sits and watches you type.
        self.last_keypress_time = -1e9
        self.typing_active_window = 1.2  # seconds of "still typing" grace

        # --- Drag / click bookkeeping ---
        self._press_global = None
        self._press_window_pos = None
        self._dragging_now = False
        self._pre_drag_mode = None
        self._pending_single_click = False

        # --- Timers / input ---
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.engine_heartbeat)
        self.timer.start(100)

        self.start_cursor_tracking()
        self.start_keyboard_tracking()
        self.start_hotkeys()
        self.control_panel = None  # set from main() after both windows exist
        self._init_tray_icon()

    # ------------------------------------------------------------------ #
    # Global cursor position tracking (position only -- no click handling)
    # ------------------------------------------------------------------ #
    def start_cursor_tracking(self):
        def on_move(x, y):
            self.target_x = x
            self.target_y = y

        self.mouse_listener = mouse.Listener(on_move=on_move)
        self.mouse_listener.start()

    def start_keyboard_tracking(self):
        def on_press(key):
            self.last_keypress_time = time.time()

        try:
            self.keyboard_listener = keyboard.Listener(on_press=on_press)
            self.keyboard_listener.start()
        except Exception as exc:
            print(f"[DesktopPet] Keyboard tracking unavailable: {exc}")
            self.keyboard_listener = None

    def start_hotkeys(self):
        """Global keyboard shortcuts, so you can quit/hide the cat even if
        you can't find the tray icon (Windows often buries it in the
        notification-area overflow arrow).

        Ctrl+Alt+Q -> fully quit (stops the app, tray icon disappears too)
        Ctrl+Alt+H -> hide/show the cat (app keeps running in the tray)
        """
        try:
            self.hotkey_listener = keyboard.GlobalHotKeys({
                "<ctrl>+<alt>+q": self._request_quit,
                "<ctrl>+<alt>+h": self._request_toggle_visibility,
            })
            self.hotkey_listener.start()
        except Exception as exc:
            print(f"[DesktopPet] Global hotkeys unavailable: {exc}")
            self.hotkey_listener = None

    def _request_quit(self):
        # Hotkey callbacks fire on pynput's own thread -- hop back onto the
        # Qt/GUI thread before touching the application.
        QTimer.singleShot(0, QApplication.instance().quit)

    def _request_toggle_visibility(self):
        QTimer.singleShot(0, self._tray_toggle_visibility)

    def closeEvent(self, event):
        if hasattr(self, "mouse_listener"):
            self.mouse_listener.stop()
        if getattr(self, "keyboard_listener", None):
            self.keyboard_listener.stop()
        if getattr(self, "hotkey_listener", None):
            self.hotkey_listener.stop()
        self.timer.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # System tray icon -- the "click to control the cat" UI, since the pet
    # itself is a borderless window with no menu bar or buttons.
    # ------------------------------------------------------------------ #
    def _init_tray_icon(self):
        icon_path = self.asset_lib.get_single("Pick") or self._first_available_sprite()
        icon = QIcon(icon_path) if icon_path else self.style().standardIcon(
            self.style().StandardPixmap.SP_ComputerIcon
        )

        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("Desktop Cat")

        menu = QMenu()

        self.toggle_walk_action = QAction("Start Following Cursor")
        self.toggle_walk_action.triggered.connect(self._tray_toggle_walk)
        menu.addAction(self.toggle_walk_action)

        self.toggle_visibility_action = QAction("Hide Cat")
        self.toggle_visibility_action.triggered.connect(self._tray_toggle_visibility)
        menu.addAction(self.toggle_visibility_action)

        self.open_panel_action = QAction("Open Control Panel")
        self.open_panel_action.triggered.connect(self._open_control_panel)
        menu.addAction(self.open_panel_action)

        menu.addSeparator()

        quit_action = QAction("Quit")
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _first_available_sprite(self):
        for by_direction in self.asset_lib.frames.values():
            for seq in by_direction.values():
                if seq:
                    return seq[0]
        return None

    def _on_tray_activated(self, reason):
        # Left-click (or double-click) the tray icon toggles walking too,
        # as a shortcut so you don't have to open the menu every time.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._tray_toggle_walk()

    def _sync_walk_labels(self):
        label = "Stop Following Cursor" if self.mode == "WALKING" else "Start Following Cursor"
        if hasattr(self, "toggle_walk_action"):
            self.toggle_walk_action.setText(label)
        if self.control_panel is not None:
            self.control_panel.walk_btn.setText(label)

    def _tray_toggle_walk(self):
        if self.mode == "WALKING":
            self.mode = self.INITIAL_MODE
        else:
            self.mode = "WALKING"
            self.walk_substate = "WALK"
            self.smooth_dx = 0.0
            self.smooth_dy = 0.0
        self.frame_index = 1
        self._sync_walk_labels()

    def _tray_toggle_visibility(self):
        if self.isVisible():
            self.hide_cat()
        else:
            self.show_cat()

    def _open_control_panel(self):
        if self.control_panel is not None:
            self.control_panel.show()
            self.control_panel.raise_()
            self.control_panel.activateWindow()

    def show_cat(self):
        """Bring the cat back onto the screen (centered, so it's always
        somewhere visible even if it was last dragged off an edge)."""
        if not self.isVisible():
            screen = QApplication.primaryScreen().availableGeometry()
            x = screen.center().x() - self.width() // 2
            y = screen.center().y() - self.height() // 2
            self.move(x, y)
        self.show()
        if hasattr(self, "toggle_visibility_action"):
            self.toggle_visibility_action.setText("Hide Cat")

    def hide_cat(self):
        """Remove the cat from the screen. The app keeps running (tray
        icon + control panel stay available) so you can bring it back
        any time with show_cat()."""
        self.hide()
        if hasattr(self, "toggle_visibility_action"):
            self.toggle_visibility_action.setText("Show Cat")

    # ------------------------------------------------------------------ #
    # Mouse interaction: drag vs single-click vs double-click
    # ------------------------------------------------------------------ #
    def _within_startup_grace(self):
        return (time.time() - self._launch_time) < self.STARTUP_CLICK_GRACE_SEC

    def mousePressEvent(self, event):
        if self._within_startup_grace():
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_window_pos = self.pos()
            self._dragging_now = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._within_startup_grace():
            return
        if (event.buttons() & Qt.MouseButton.LeftButton) and self._press_global is not None:
            delta = event.globalPosition().toPoint() - self._press_global
            if not self._dragging_now and delta.manhattanLength() > self.DRAG_THRESHOLD:
                self._dragging_now = True
                self._pre_drag_mode = self.mode
                self.mode = "DRAGGING"
            if self._dragging_now:
                new_x, new_y = self._clamp_to_screen(
                    self._press_window_pos.x() + delta.x(),
                    self._press_window_pos.y() + delta.y(),
                )
                self.move(new_x, new_y)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._within_startup_grace():
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragging_now:
                # Dropped -- settle back into a resting state.
                self.mode = self.INITIAL_MODE
                self.frame_index = 1
            else:
                # Might be a single click OR the first half of a double
                # click. Wait briefly to find out which.
                self._pending_single_click = True
                QTimer.singleShot(self.SINGLE_CLICK_DELAY_MS, self._resolve_single_click)
            self._dragging_now = False
            self._press_global = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._within_startup_grace():
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._pending_single_click = False  # cancel the pending single-click
            if self.mode == "WALKING":
                self.mode = self.INITIAL_MODE
            else:
                self.mode = "WALKING"
                self.walk_substate = "WALK"
                self.smooth_dx = 0.0
                self.smooth_dy = 0.0
            self.frame_index = 1
            self._sync_walk_labels()
        super().mouseDoubleClickEvent(event)

    def _resolve_single_click(self):
        if not self._pending_single_click:
            return  # a double-click already cancelled this
        self._pending_single_click = False
        if self.mode in ("SLEEPING", "SITTING"):
            self.mode = "ALERT"
            self.alert_ticks = self.alert_duration_ticks

    # ------------------------------------------------------------------ #
    # Direction math
    # ------------------------------------------------------------------ #
    @staticmethod
    def determine_direction(dx, dy):
        angle = math.degrees(math.atan2(-dy, dx))
        if angle < 0:
            angle += 360
        if 22.5 <= angle < 67.5:
            return "Up-Right"
        elif 67.5 <= angle < 112.5:
            return "Up"
        elif 112.5 <= angle < 157.5:
            return "Up-Left"
        elif 157.5 <= angle < 202.5:
            return "Left"
        elif 202.5 <= angle < 247.5:
            return "Down-Left"
        elif 247.5 <= angle < 292.5:
            return "Down"
        elif 292.5 <= angle < 337.5:
            return "Down-Right"
        return "Right"

    # ------------------------------------------------------------------ #
    # Sprite loading / rendering
    # ------------------------------------------------------------------ #
    def _load_scaled(self, path):
        cached = self._pixmap_cache.get(path)
        if cached is not None:
            return cached
        pix = QPixmap(path)
        if pix.isNull():
            print(f"[DesktopPet] Could not load sprite: {path}")
            return QPixmap()
        scaled = pix.scaled(
            self.width(),
            self.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,  # keeps pixel-art crisp
        )
        self._pixmap_cache[path] = scaled
        return scaled

    def _show_frame(self, path):
        if not path:
            return
        pixmap = self._load_scaled(path)
        if pixmap.isNull():
            return
        self.label.setPixmap(pixmap)
        self.setMask(pixmap.mask())  # only opaque pixels are visible/clickable

    def _clamp_to_screen(self, x, y):
        screen = QApplication.primaryScreen().availableGeometry()
        x = max(screen.left(), min(x, screen.right() - self.width()))
        y = max(screen.top(), min(y, screen.bottom() - self.height()))
        return x, y

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def engine_heartbeat(self):
        if self.mode == "DRAGGING":
            self._render_dragging()
            return

        # Typing is an overlay on top of whatever mode we're in -- it
        # doesn't change self.mode, so walking/sleeping resume exactly
        # where they left off once typing stops.
        if self._is_typing_active():
            self._render_typing()
            return

        current_pos = self.pos()
        target_x = self.target_x - self.width() // 2
        target_y = self.target_y - self.height() // 2
        raw_dx = target_x - current_pos.x()
        raw_dy = target_y - current_pos.y()

        if self.mode == "WALKING":
            self._render_walking(raw_dx, raw_dy)
        elif self.mode == "ALERT":
            self._render_alert()
        elif self.mode == "SITTING":
            self._render_sitting_idle()
        else:
            self._render_sleeping()

    def _render_walking(self, raw_dx, raw_dy):
        # Smooth the direction vector so small jitters in cursor movement
        # don't cause the sprite to flicker between facing directions.
        self.smooth_dx = self.smooth_dx * 0.75 + raw_dx * 0.25
        self.smooth_dy = self.smooth_dy * 0.75 + raw_dy * 0.25
        distance = math.hypot(self.smooth_dx, self.smooth_dy)

        # --- Tier transitions (each boundary uses separate enter/exit
        # thresholds so the state can't flicker back and forth right at
        # the edge of a band) ---
        if self.walk_substate == "SETTLE" and distance > self.settle_exit_distance:
            self.walk_substate = "WALK"
            self.frame_index = 1
        elif self.walk_substate in ("WALK", "RUN") and distance < self.settle_enter_distance:
            self.walk_substate = "SETTLE"
            self.frame_index = 1

        if self.walk_substate == "WALK" and distance > self.run_enter_distance:
            self.walk_substate = "RUN"
            self.frame_index = 1
        elif self.walk_substate == "RUN" and distance < self.run_exit_distance:
            self.walk_substate = "WALK"
            self.frame_index = 1

        if self.walk_substate == "SETTLE":
            # Caught up to the pointer -- sit down and hold still. Static
            # pose, no looping animation.
            seq = self.asset_lib.get_sequence("Sitting", self.direction)
            if seq:
                self._show_frame(seq[0])
            return

        # RUN or WALK: move toward the pointer, easing speed with distance
        # within each tier so it accelerates/decelerates smoothly rather
        # than moving at a constant speed and stopping abruptly.
        tuning = self.MOVE_TUNING[self.walk_substate]
        if distance > 1:
            step = max(tuning["min_step"], min(tuning["max_step"], distance * tuning["ease"]))
            move_x = int(self.pos().x() + (self.smooth_dx / distance) * step)
            move_y = int(self.pos().y() + (self.smooth_dy / distance) * step)
            move_x, move_y = self._clamp_to_screen(move_x, move_y)
            self.move(move_x, move_y)

            self.direction = self.determine_direction(self.smooth_dx, self.smooth_dy)
            seq = self.asset_lib.get_sequence(tuning["state"], self.direction)
            if seq:
                self.frame_index = (self.frame_index % len(seq)) + 1
                self._show_frame(seq[self.frame_index - 1])

    def _render_alert(self):
        seq = self.asset_lib.get_sequence("Look", "Up")
        if seq:
            self._show_frame(seq[0])
        self.alert_ticks -= 1
        if self.alert_ticks <= 0:
            self.mode = self.INITIAL_MODE
            self.frame_index = 1

    def _render_sleeping(self):
        # Static -- fully at rest, holds one pose, no looping animation.
        seq = self.asset_lib.get_sequence("Lay", self.direction)
        if seq:
            self._show_frame(seq[0])

    def _render_sitting_idle(self):
        # Static -- fully at rest, holds one pose, no looping animation.
        seq = self.asset_lib.get_sequence("Sitting", self.direction)
        if seq:
            self._show_frame(seq[0])

    def _is_typing_active(self):
        return (time.time() - self.last_keypress_time) < self.typing_active_window

    def _render_typing(self):
        # Static -- sits and holds still while you type, no looping animation.
        seq = self.asset_lib.get_sequence("Sitting", "Down")
        if seq:
            self._show_frame(seq[0])

    def _render_dragging(self):
        path = self.asset_lib.get_single("Pick")
        if not path:
            seq = self.asset_lib.get_sequence("Look", "Up")
            path = seq[0] if seq else None
        self._show_frame(path)


class ControlPanel(QWidget):
    """A small, ordinary titled window (shows in the taskbar, can be
    minimized) with buttons to bring the cat onto the screen or remove it,
    toggle chase mode, and quit -- the explicit "click a button" UI, as an
    alternative to the tray icon and hotkeys."""

    def __init__(self, pet):
        super().__init__()
        self.pet = pet
        self.setWindowTitle("Desktop Cat Control")
        self.setFixedWidth(220)

        layout = QVBoxLayout(self)

        self.show_btn = QPushButton("Show Cat")
        self.show_btn.clicked.connect(self.pet.show_cat)
        layout.addWidget(self.show_btn)

        self.hide_btn = QPushButton("Hide Cat")
        self.hide_btn.clicked.connect(self.pet.hide_cat)
        layout.addWidget(self.hide_btn)

        self.walk_btn = QPushButton("Start Following Cursor")
        self.walk_btn.clicked.connect(self._toggle_walk)
        layout.addWidget(self.walk_btn)

        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(QApplication.instance().quit)
        layout.addWidget(quit_btn)

    def _toggle_walk(self):
        self.pet._tray_toggle_walk()  # also updates this panel's + tray's label

    def closeEvent(self, event):
        # Closing the panel (the X button) just hides it -- the app keeps
        # running. Reopen it via the tray icon's "Open Control Panel."
        event.ignore()
        self.hide()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # keep running when hidden via tray

    assets_path = os.path.join(get_base_dir(), "assets")
    pet = DesktopPet(assets_path)
    pet.show()

    panel = ControlPanel(pet)
    pet.control_panel = panel
    panel.show()

    sys.exit(app.exec())