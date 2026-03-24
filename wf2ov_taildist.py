#!/usr/bin/env python3

import os
import tkinter as tk
import tkinter.font as tkfont
import threading
import queue
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from copy import deepcopy
import math

from win64proc import Win64Process
from wf2telemetry import *
from wf2app import WF2_EXE_NAME
from wf2overlay import *


# =============================================================================
# TailDist overlay
# =============================================================================
# Graphical canvas fixed to the bottom of the game window.
# Each rival behind the player is drawn as a filled ellipse.
# Ellipse centre X = lateral angle to rival mapped to canvas width.
# Ellipse vertical radius = proportional to how close the rival is.
# Rival name is drawn above the ellipse top edge.
#
# Coordinate system (left-handed, per Pino spec): X=right, Y=up, Z=forward.
# Heading from quaternion: fx=2*(qx*qz+qw*qy), fz=1-2*(qx*qx+qy*qy).
# =============================================================================

def lerp_color(c0: str, c1: str, t: float) -> str:
    """Linear interpolation between two hex colours. t=0 -> c0, t=1 -> c1."""
    t = max(0.0, min(1.0, t))
    def parse(c):
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    r0, g0, b0 = parse(c0)
    r1, g1, b1 = parse(c1)
    r = int(r0 + (r1 - r0) * t)
    g = int(g0 + (g1 - g0) * t)
    b = int(b0 + (b1 - b0) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


class RivalEntry:
    """One rival detected behind the player within range."""
    __slots__ = ("index", "name", "dist_m", "angle_deg")

    def __init__(self, index: int, name: str, dist_m: float, angle_deg: float):
        self.index     = index
        self.name      = name
        self.dist_m    = dist_m
        self.angle_deg = angle_deg


class TailDistSnapshot:
    """Snapshot pushed to the overlay thread each frame."""
    __slots__ = ("rivals", "radius_m")

    def __init__(self, rivals: list, radius_m: float):
        self.rivals   = rivals    # sorted farthest first
        self.radius_m = radius_m


class TailDistState:
    """
    Raw data collector — only stores data arriving from UDP packets.

    The UDP thread calls update_main() and update_motion() freely.
    After update_motion() the ready event is set to signal TailDistOverlay
    that new data is available for processing.

    TailDistOverlay calls take_snapshot() from its own thread when ready
    is set. take_snapshot() atomically copies the current state, clears
    ready, and returns a raw data bundle for geometry computation.
    """
    NAME_MAX_LEN = 12

    def __init__(self, cfg: dict = { }):
        self.radius_m = float(cfg.get("max_view_radius", 250.0))
        self.player_x         = 0.0
        self.player_z         = 0.0
        self.heading_x        = 0.0
        self.heading_z        = 1.0
        self.velloc_x         = 0.0
        self.velloc_z         = 0.0
        self.motion_positions : dict = {}
        self.lock             = threading.Lock()
        self.ready            = threading.Event()
        self.poll_interval_s  = 0.02   # default 20ms, updated via set_poll_interval()
        self.last_signal_t    = 0.0

    def update_main(self, pkt) -> None:
        """Store player position and heading. Called from UDP thread."""
        ori = pkt.carPlayer.orientation
        qx  = ori.orientationQuatX
        qy  = ori.orientationQuatY
        qz  = ori.orientationQuatZ
        qw  = ori.orientationQuatW
        with self.lock:
            self.player_x  = ori.positionX
            self.player_z  = ori.positionZ
            self.heading_x = 2.0 * (qx * qz + qw * qy)
            self.heading_z = 1.0 - 2.0 * (qx * qx + qy * qy)
            self.velloc_x = pkt.carPlayer.velocity.velocityLocalX
            self.velloc_z = pkt.carPlayer.velocity.velocityLocalZ

    def set_poll_interval(self, interval_ms: int) -> None:
        """Configure minimum interval between ready signals, in milliseconds."""
        self.poll_interval_s = interval_ms / 1000.0

    def update_motion(self, pkt) -> None:
        """Store participant positions and signal overlay. Called from UDP thread."""
        positions = {}
        for i, m in enumerate(pkt.participantsMotion):
            positions[i] = (m.positionX, m.positionZ)
        with self.lock:
            self.motion_positions = positions
        # Throttle: only signal overlay at most once per poll_interval_s
        now = time.monotonic()
        if now - self.last_signal_t >= self.poll_interval_s:
            self.last_signal_t = now
            self.ready.set()

    def take_snapshot(self) -> dict:
        """
        Atomically copy current raw state and clear ready flag.
        Returns a plain dict so the overlay thread can work without holding lock.
        """
        self.ready.clear()
        with self.lock:
            return {
                "radius_m":         self.radius_m,
                "player_x":         self.player_x,
                "player_z":         self.player_z,
                "heading_x":        self.heading_x,
                "heading_z":        self.heading_z,
                "velloc_x":         self.velloc_x,
                "velloc_z":         self.velloc_z,
                "motion_positions": dict(self.motion_positions),
            }


class TailDistOverlay(BaseOverlay):
    """
    Graphical overlay fixed to the bottom of the game window.

    Rivals are drawn as filled ellipses on a transparent canvas.
    Ellipse geometry:
      - Centre X: mapped from lateral angle via FOV, clamped to canvas edges
      - Centre Y: bottom of canvas (canvas_height)
      - Vertical radius: proportional to closeness
          dist >= radius * 0.8  ->  vert_r = canvas_h * 0.20
          dist = 0              ->  vert_r = canvas_h * 1.00
          linear between those two anchor points
      - Horizontal radius: vert_r * marker_width

    Colour: linearly interpolated between color_far (dist=radius) and
    color_near (dist=0) based on distance fraction.

    Draw order: ellipses farthest first, then names farthest first.
    """
    FOV_DEFAULT           = 90.0
    CANVAS_HEIGHT_DEFAULT = 200
    MARKER_WIDTH_DEFAULT  = 0.5
    COLOR_FAR_DEFAULT     = "#e0e0e0"
    COLOR_NEAR_DEFAULT    = "#0055ff"
    OUTLINE_COLOR_DEFAULT = "#000000"
    NAME_COLOR_DEFAULT    = "#ffffff"
    NAME_SHADOW_DEFAULT   = "#000000"
    NAME_MAX_LEN          = 12
    RECT_TTL              = 2.0
    MAX_RIVALS_DEFAULT    = 8
    POLL_INTERVAL_DEFAULT = 20
    MAX_DELTA_MS_DEFAULT  = 15000

    def __init__(self, cfg_path: str):
        super().__init__(cfg_path, "taildist", "WF2 TailDist")
        ov = self.ov

        self.fov          = float(ov.get("fov",           self.FOV_DEFAULT))
        self.canvas_h     = int(  ov.get("canvas_height", self.CANVAS_HEIGHT_DEFAULT))
        self.marker_width = float(ov.get("marker_width",  self.MARKER_WIDTH_DEFAULT))
        self.color_far    = ov.get("color_far",     self.COLOR_FAR_DEFAULT)
        self.color_near   = ov.get("color_near",    self.COLOR_NEAR_DEFAULT)
        self.outline_color= ov.get("outline_color", self.OUTLINE_COLOR_DEFAULT)

        self.name_color   = ov.get("fg",       self.NAME_COLOR_DEFAULT)
        self.name_shadow  = ov.get("shadow",   self.NAME_SHADOW_DEFAULT)
        self.max_rivals   = int(ov.get("max_rivals",    self.MAX_RIVALS_DEFAULT))
        self.poll_interval= int(ov.get("poll_interval", self.POLL_INTERVAL_DEFAULT))
        self.max_delta_ms = int(ov.get("max_delta_ms",  self.MAX_DELTA_MS_DEFAULT))

        self.game_rect    = None
        self.rect_age     = 0.0
        self.tail_state   = None   # set via attach() before use
        self.lb_state     = None   # set via attach() before use

        self.proc = Win64Process()
        self.proc.find_process(WF2_EXE_NAME)
        
        self.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, tail_state: "TailDistState", lb_state: "LeaderboardState") -> None:
        """Bind data sources. Call once before the overlay thread starts processing."""
        self.tail_state = tail_state
        self.lb_state   = lb_state
        tail_state.set_poll_interval(self.poll_interval)

    def set_visible(self, visible: bool) -> None:
        self.cmd_queue.put(("visible", visible))

    def set_race_active(self, race_active: bool) -> None:
        self.cmd_queue.put(("race_active", race_active))

    # ------------------------------------------------------------------
    # tkinter thread
    # ------------------------------------------------------------------

    def run(self) -> None:
        # Ellipse window — drawn at ellipse_alpha
        bg = tk.Tk()
        self.bg_root = bg
        bg.title("WF2 TailDist BG")
        bg.overrideredirect(True)
        bg.attributes("-topmost", True)
        bg.attributes("-alpha", self.bg_alpha)
        bg.configure(bg=self.chroma_key)
        if self.transparent:
            bg.attributes("-transparentcolor", self.chroma_key)
        self.bg_font = self.load_font(bg)
        self.bg_canvas = tk.Canvas(bg, bg = self.chroma_key, highlightthickness = 0, cursor = "arrow")
        self.bg_canvas.pack(fill=tk.BOTH, expand=True)
        # Text window — transparent bg, names at self.alpha
        root = tk.Tk()
        self.root = root
        root.title("WF2 TailDist")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.alpha)
        root.configure(bg=self.chroma_key)
        if self.transparent:
            root.attributes("-transparentcolor", self.chroma_key)
        self.font = self.load_font(root)
        self.canvas = tk.Canvas(root, bg = self.chroma_key, highlightthickness = 0, cursor = "arrow")
        self.canvas.pack(fill = tk.BOTH, expand = True)
        root.bind("<ButtonPress-1>", self.drag_start)
        root.bind("<B1-Motion>",     self.drag_motion)
        self.poll()
        root.mainloop()

    def drag_start(self, event) -> None:
        self.drag_x = event.x
        self.drag_y = event.y

    def drag_motion(self, event) -> None:
        if self.root:
            x = self.root.winfo_x() + (event.x - self.drag_x)
            y = self.root.winfo_y() + (event.y - self.drag_y)
            self.root.geometry(f"+{x}+{y}")
            if self.bg_root:
                self.bg_root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def poll(self) -> None:
        self.read_queues()
        if self.tail_state is not None and self.lb_state is not None:
            if self.tail_state.ready.is_set():
                raw = self.tail_state.take_snapshot()
                snap = self.build_snapshot(raw, self.lb_state)
                self.draw(snap)
                # Data was processed — check again immediately on next event loop tick
                if self.root:
                    self.root.after(0, self.poll)
                return
        # No new data yet — yield and check again after a short sleep
        if self.root:
            self.root.after(self.poll_interval, self.poll)

    # ------------------------------------------------------------------
    # Game window tracking
    # ------------------------------------------------------------------

    def refresh_game_rect(self) -> None:
        now = time.monotonic()
        if now - self.rect_age < self.RECT_TTL and self.game_rect is not None:
            return
        self.rect_age = now
        if not self.proc.is_alive():
            self.proc.close_process()
            self.proc.find_process(WF2_EXE_NAME)
        self.game_rect = self.proc.get_process_window_rect()

    def reposition(self) -> None:
        self.refresh_game_rect()
        if self.game_rect is None or self.root is None:
            return
        gl, gt, gw, gh = self.game_rect
        y = gt + gh - self.canvas_h
        geom = f"{gw}x{self.canvas_h}+{gl}+{y}"
        self.root.geometry(geom)
        if self.bg_root:
            self.bg_root.geometry(geom)
        if self.canvas:
            self.canvas.configure(width=gw, height=self.canvas_h)
        if self.bg_canvas:
            self.bg_canvas.configure(width=gw, height=self.canvas_h)

    # ------------------------------------------------------------------
    # Snapshot computation (runs in overlay thread)
    # ------------------------------------------------------------------

    def build_snapshot(self, raw: dict, lb_state: "LeaderboardState") -> "TailDistSnapshot":
        """
        Compute RivalEntry list from raw state dict and leaderboard.
        All math runs here in the overlay thread, not in the UDP thread.
        """
        radius_m  = raw["radius_m"]
        px        = raw["player_x"]
        pz        = raw["player_z"]
        hx        = raw["heading_x"]
        hz        = raw["heading_z"]
        positions = raw["motion_positions"]
        r2        = radius_m * radius_m

        vx = raw['velloc_x']
        vz = raw['velloc_z']
        player_v_mag = math.sqrt(vx * vx + vz * vz)
        player_v_rad = math.atan2(vx, vz) if player_v_mag > 0.04 else 0.0 

        name_map   = { }
        behind_set = set()
        player_idx = lb_state.player_idx
        for idx, row in lb_state.rows.items():
            n = row.name or f"P{idx:02d}"
            name_map[idx] = n[:TailDistState.NAME_MAX_LEN]
            # delta_to_player < 0: rival is behind player (lap-time metric)
            # abs(delta_to_player) <= 15000 ms: not more than 15 seconds behind
            if not row.is_player and -self.max_delta_ms <= row.delta_to_player < 0:
                behind_set.add(idx)

        rivals = [ ]
        for idx, (rx, rz) in positions.items():
            if idx == player_idx:
                continue
            if idx not in behind_set:
                continue
            dx = rx - px
            dz = rz - pz
            dist2 = dx * dx + dz * dz
            if dist2 > r2:
                continue
            dist_m = math.sqrt(dist2)
            if dist_m > 0.01:
                inv   = 1.0 / dist_m
                nx    = dx * inv
                nz    = dz * inv
                cross = hx * nz - hz * nx
                dot   = hx * nx + hz * nz
                # dot < 0 means rival is geometrically AHEAD of player
                # (vector from player to rival points opposite to heading).
                # Exclude such rivals regardless of lap-time delta.
                if dot >= 0.0:
                    continue
                angle_deg = -math.degrees(math.atan2(cross, dot) + player_v_rad)
            else:
                angle_deg = 0.0
            name = name_map.get(idx, f"P{idx:02d}")
            rivals.append(RivalEntry(idx, name, dist_m, angle_deg))

        rivals.sort(key=lambda e: e.dist_m, reverse=True)
        # Keep only the closest max_rivals entries.
        # List is farthest-first, so take the tail, then re-sort.
        if len(rivals) > self.max_rivals:
            rivals = rivals[-self.max_rivals:]
        return TailDistSnapshot(rivals=rivals, radius_m=radius_m)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def angle_to_x(self, angle_deg: float, canvas_w: int) -> int:
        """
        Map rival lateral angle to canvas X pixel.
        Angles outside FOV are clamped to canvas edges (not discarded).
        Reference: angle=+/-180 -> centre, +90 -> left, -90 -> right.
        """
        half_fov = self.fov / 2.0
        a = angle_deg - 180.0
        if a < -180.0: a += 360.0
        if a >  180.0: a -= 360.0
        frac = (-a + half_fov) / self.fov
        frac = max(0.0, min(1.0, frac))
        return int(frac * canvas_w)

    def dist_to_radii(self, dist_m: float, radius_m: float) -> tuple:
        """
        Compute (vert_r, horiz_r) in pixels.
        dist >= radius*0.8  ->  vert_r = canvas_h * 0.20  (minimum, far)
        dist = 0            ->  vert_r = canvas_h * 1.00  (maximum, close)
        Linear between those two anchor points.
        """
        h = self.canvas_h
        frac = dist_m / radius_m if radius_m > 0 else 0.0
        frac = max(0.0, min(frac, 0.8))    # clamp top range to 0.8
        t = 1.0 - frac / 0.8               # 0.0=far, 1.0=close
        vert_r  = int(h * (0.20 + t * 0.80))
        horiz_r = max(1, int(vert_r * self.marker_width))
        return vert_r, horiz_r

    def dist_to_color(self, dist_m: float, radius_m: float) -> str:
        """Interpolate from color_far (dist=radius) to color_near (dist=0)."""
        frac = dist_m / radius_m if radius_m > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        return lerp_color(self.color_far, self.color_near, 1.0 - frac)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def draw(self, snap: TailDistSnapshot) -> None:
        self.reposition()
        canvas = self.canvas
        if canvas is None:
            return
        canvas.delete("all")
        self.bg_canvas.delete("all")

        if not snap.rivals or self.game_rect is None:
            return

        _, _, canvas_w, _ = self.game_rect
        h        = self.canvas_h
        radius_m = snap.radius_m

        txt_shadow_list = [ ]
        txt_main_list = [ ]

        # Pass 1: ellipses on bg_canvas (or canvas if no bg window) and prepare name text list
        for rival in reversed(snap.rivals):
            cx = self.angle_to_x(rival.angle_deg, canvas_w)
            vert_r, horiz_r = self.dist_to_radii(rival.dist_m, radius_m)
            color = self.dist_to_color(rival.dist_m, radius_m)
            x0 = cx - horiz_r
            y0 = h  - vert_r
            x1 = cx + horiz_r
            y1 = h  + vert_r    # lower half clipped by canvas bottom
            self.bg_canvas.create_oval(x0, y0, x1, y1, fill=color, outline=self.outline_color)
            ny = h - vert_r   # top of ellipse = name baseline
            name = rival.name[:self.NAME_MAX_LEN]
            txt_shadow_list.append( (cx + 1, ny + 1, { "text": name, "fill": self.name_shadow, "anchor": 'n' } ) )
            txt_main_list.append(   (cx    , ny    , { "text": name, "fill": self.name_color , "anchor": 'n' } ) )

        # Pass 2: Output text shadows on bg_root and root
        for txt in reversed(txt_shadow_list):
            self.bg_canvas.create_text( txt[0], txt[1], font = self.bg_font, **txt[2] )
            self.canvas.create_text(    txt[0], txt[1], font = self.font, **txt[2] )

        # Pass 3: Output text on bg_root
        for txt in reversed(txt_main_list):
            self.bg_canvas.create_text( txt[0], txt[1], font = self.bg_font, **txt[2] )

        # Pass 4: Output text on root
        for txt in reversed(txt_main_list):
            self.canvas.create_text( txt[0], txt[1], font = self.font, **txt[2] )


