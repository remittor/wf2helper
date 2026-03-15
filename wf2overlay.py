#!/usr/bin/env python3
"""
wf2overlay.py
Wreckfest 2 HUD overlays (tkinter Canvas, always-on-top, transparent).

Two independent overlay windows:
  leaderboard  — race standings table with delta times
  advinfo      — advanced car info: race timer, inputs, engine, tires, etc.

Text is rendered on a Canvas with a 1px drop-shadow (right+down) for
readability on any background — works with transparent: true mode.

Config (wf2hlp.yaml):

    overlays:
        leaderboard:
            x: 20
            y: 80
            ...
        advinfo:
            x: 20
            y: 600
            ...

Appearance keys (same for both sections):
    x, y, alpha, font, font_size, bg, fg, fg_player, fg_dnf,
    shadow, transparent, chroma_key
"""

import tkinter as tk
import tkinter.font as tkfont
import threading
import queue
import time
from dataclasses import dataclass, field
from copy import deepcopy
import math

from win64proc import Win64Process
from wf2telemetry import *
from wf2playfab import *


OV_CFG_DEFAULTS = dict(
    max_rows    = 16,
    x           = 20,
    y           = 400,
    alpha       = 0.82,
    transparent = True,
    chroma_key  = "#000005",
    bg          = "#000001",
    font        = "Fixedsys",
    font_size   = 9,
    bold        = False,
    shadow      = "#000000",    # drop-shadow colour
    fg          = "#ffffff",
    fg_player   = "#ffdd44",
    fg_dnf      = "#888888",
)

def get_ov_cfg(section: dict) -> dict:
    cfg = dict(OV_CFG_DEFAULTS)
    cfg.update(section)
    if cfg["transparent"]:
        cfg["bg"] = cfg["chroma_key"]
    return cfg

def get_color(ov_cfg, tag: str) -> str:
    if tag.startswith('#'):
        return tag
    TABLE = {
        "":       ov_cfg.get("fg",        "#ffffff"),
        "header": "#b8b8b8",
        "player": ov_cfg.get("fg_player", "#ffdd44"),
        "hi":     ov_cfg.get("fg_player", "#ffdd44"),
        "dnf":    ov_cfg.get("fg_dnf",    "#aaaaaa"),
        "label":  "#f3f3f3",
        "warn":   "#ff6644",
        "good":   "#44dd88",
        "air":    "#44bbff",
    }
    return TABLE.get(tag, ov_cfg.get("fg", "#e0e0e0"))

def fmt_time(time_ms: int, e: int = 0) -> str:
    ms = max(0, time_ms)
    if e == 0 and ms == 0:
        return "--:--.---"
    mins = ms // 60000
    secs = (ms % 60000) // 1000
    frac = ms % 1000
    return f"{mins:02d}:{secs:02d}.{frac:03d}"


class BaseOverlay:
    """
    Always-on-top tkinter Canvas window in its own daemon thread.

    render(data) must return: list of lines.
    Each line is a list of (text, color_hex) tuples — segments placed
    left-to-right in monospace columns.

    For each segment the engine draws:
      1. Shadow copy at (+1, +1) px in self.shadow colour
      2. Main text at (0, 0) in the segment colour
    """

    SHADOW_DX = 1
    SHADOW_DY = 1
    PAD       = 4   # canvas padding px

    def __init__(self, ov: dict, title: str):
        self.ov     = ov
        self.title  = title
        self.shadow = ov.get("shadow", "#000000")
        self.queue  = queue.Queue(maxsize=8)
        self.last   = None
        self.root   = None
        self.canvas = None
        self.font   = None   # tkfont.Font
        self.cw     = 0      # char width px
        self.lh     = 0      # line height px
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def push(self, data) -> None:
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            pass

    def set_visible(self, visible: bool) -> None:
        root = self.root
        if root is None:
            return
        if visible:
            root.after(0, root.deiconify)
        else:
            root.after(0, root.withdraw)

    def run(self) -> None:
        ov   = self.ov
        root = tk.Tk()
        self.root = root

        root.title(self.title)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", ov["alpha"])
        root.configure(bg=ov["bg"])
        if ov["transparent"]:
            root.attributes("-transparentcolor", ov["chroma_key"])
        root.geometry(f"+{ov['x']}+{ov['y']}")

        # Use a named font registered on THIS Tk instance so Canvas in this
        # window always resolves it correctly (avoids cross-Tk font corruption
        # when two Toplevel/Tk windows live in separate threads).
        self.font = tkfont.Font(
            root   = root,
            family = ov["font"],
            size   = ov["font_size"],
            weight = "bold" if ov["bold"] else "normal",
        )
        self.cw   = self.font.measure("W")
        self.lh   = self.font.metrics("linespace")

        self.canvas = tk.Canvas(
            root,
            bg = ov["bg"],
            highlightthickness = 0,
            cursor = "arrow",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        root.bind("<ButtonPress-1>", self.drag_start)
        root.bind("<B1-Motion>",     self.drag_motion)

        self.poll()
        root.mainloop()

    def poll(self) -> None:
        try:
            while True:
                self.last = self.queue.get_nowait()
        except (queue.Empty, queue.Full):
            pass
        if self.last is not None:
            lines = self.render(self.last)
            self.draw(lines)
        if self.root:
            self.root.after(100, self.poll)

    def draw(self, lines: list) -> None:
        canvas = self.canvas
        if canvas is None:
            return
        canvas.delete("all")
        pad    = self.PAD
        lh     = self.lh
        cw     = self.cw
        font   = self.font
        shadow = self.shadow
        dx, dy = self.SHADOW_DX, self.SHADOW_DY
        fg_def = self.ov["fg"]
        max_col = 0
        for row_i, segments in enumerate(lines):
            y = pad + row_i * lh
            col = 0
            for seg in segments:
                if seg is None:
                    continue
                text, color = seg
                if text is None:
                    continue
                x = pad + col * cw
                # 1. shadow
                canvas.create_text(x + dx, y + dy, text=text, fill=shadow, font=font, anchor="nw")
                # 2. main
                canvas.create_text(x, y, text=text, fill=color or fg_def, font=font, anchor="nw")
                col += len(text)
                pass
            max_col = max(max_col, col)
            pass
        w = pad * 2 + max_col * cw + dx
        h = pad * 2 + len(lines) * lh + dy
        canvas.configure(width=w, height=h)
        self.root.geometry(f"{w}x{h}")

    def gen_segment(self, text: str, tag: str = "") -> tuple:
        """Build one segment: (text, colour)."""
        return (text, get_color(self.ov, tag))

    def render(self, data) -> list:
        raise NotImplementedError

    def drag_start(self, event) -> None:
        self.drag_x = event.x
        self.drag_y = event.y

    def drag_motion(self, event) -> None:
        if self.root:
            x = self.root.winfo_x() + (event.x - self.drag_x)
            y = self.root.winfo_y() + (event.y - self.drag_y)
            self.root.geometry(f"+{x}+{y}")


@dataclass
class ParticipantRow:
    index:        int
    position:     int  = 0
    name:         str  = ""
    car_name:     str  = ""
    lap:          int  = 0
    health:       int  = 100
    delta_leader: int  = 0
    delta_ahead:  int  = 0
    delta_behind: int  = 0
    delta_to_player: int = 0   # computed: positive = behind player, negative = ahead
    status:       int  = 0
    is_player:    bool = False

    @property
    def status_str(self) -> str:
        return {
            PARTICIPANT_STATUS_RACING:         "",
            PARTICIPANT_STATUS_FINISH_SUCCESS: "FIN",
            PARTICIPANT_STATUS_DNF_DQ:         "DQ",
            PARTICIPANT_STATUS_DNF_RETIRED:    "RET",
            PARTICIPANT_STATUS_DNF_TIMEOUT:    "T/O",
            PARTICIPANT_STATUS_DNF_WRECKED:    "WRK",
        }.get(self.status, "")

    @property
    def delta_str(self) -> str:
        """Delta relative to the player's car.
        Positive (+) = this participant is behind the player.
        Negative (-) = this participant is ahead of the player.
        Blank for the player's own row."""
        if self.is_player:
            return "  <YOU> "
        ms   = abs(self.delta_to_player)
        sign = "+" if self.delta_to_player >= 0 else "-"
        s    = ms // 1000
        frac = (ms % 1000) // 10
        return f" {sign}{s:3d}.{frac:02d}"


@dataclass
class LeaderboardSnapshot:
    rows:       list  = field(default_factory=list)
    track_name: str   = ""
    lap_total:  int   = 0
    updated_at: float = 0.0


class LeaderboardOverlay(BaseOverlay):
    def __init__(self, ov: dict):
        self.max_rows = ov.get("max_rows", 16)
        super().__init__(ov, "WF2 Leaderboard")

    def render(self, snap: LeaderboardSnapshot) -> list:
        seg = self.gen_segment
        lines = [ ]

        def line(*segs):
            lines.append(list(segs))

        track = snap.track_name[:30] if snap.track_name else "---"
        line(seg(f" {track}", "header"))
        line(seg(f" {'P':>2}  {'Name':<12} {'Car':<8} {'Lap':>5}  {'Δ to you':>8}  {'HP':>3}  St", "header"))

        rows = sorted(snap.rows, key=lambda r: r.position if r.position > 0 else 999)
        for row in rows:
            lap_str  = f"{row.lap}/{snap.lap_total}" if snap.lap_total else str(row.lap)
            name_str = row.name[:12]    if row.name     else f"P{row.index:02d}"
            car_str  = row.car_name[:8] if row.car_name else ""
            pos_str  = f"{row.position:>2}" if row.position else " ?"
            tag      = "player" if row.is_player else ("dnf" if row.status_str else "")
            line(seg(f" {pos_str}  {name_str:<12}", tag), seg(f" {car_str:<8} {lap_str:>5}  {row.delta_str:>8}  {row.health:>3}  {row.status_str}", tag))

        return lines[:self.max_rows]


@dataclass
class AdvInfoSnapshot:
    race_inited    : bool  = False
    race_started   : bool  = False
    race_stopped   : bool  = False
    race_finished  : bool  = False
    
    pkt_count_after_finish: int = 0
    
    track_id       : str = ""
    track_name     : str = ""
    pb_rank        : int = 0
    pb_rank_new    : int = 0   # estimated new rank after PB, from rank_page
    pb_time        : int = 0
    pb_time_new    : int = 0
    wr_time        : int = 0
    rank_page      : list = field(default_factory=list)  # (rank, score_ms) list fetched at race start

    start_TIME_s   : float = 0.0   # real time of race_started set True
    start_time_ms  : int   = 0
    race_TIME_ms   : int   = 0
    race_time_ms   : int   = 0
    
    lap_time_ms    : int   = 0
    lap_time_best  : int   = 0
    lap_progress   : float = 0.0

    throttle:   float = 0.0
    brake:      float = 0.0
    clutch:     float = 0.0
    handbrake:  float = 0.0
    steering:   float = 0.0

    torque:       float = 0.0
    power:        float = 0.0
    temp_block:   float = 0.0
    temp_water:   float = 0.0
    pressure_oil: float = 0.0
    misfiring:    bool  = False

    traction_state: str = ""
    health:         int = 100

    # Sector info
    sector_count   : int   = 0     # 1, 2 or 3
    sector_fract   : tuple = (0.0, 1.0)   # (fract1, fract2) boundaries
    # Current-lap sector times (ms). 0 = not yet completed this lap.
    sect_cur       : tuple = (0, 0, 0)   # S1, S2, S3 for current lap
    # Best-ever sector times across all laps (from telemetry)
    sect_best      : tuple = (0, 0, 0)   # sectorTimeBest1/2/3

    tire_slip: tuple = (0.0, 0.0, 0.0, 0.0)
    tire_temp: tuple = (0.0, 0.0, 0.0, 0.0)
    tire_load: tuple = (0.0, 0.0, 0.0, 0.0)
    tire_surf: tuple = (0,   0,   0,   0)
    susp_norm: tuple = (0.0, 0.0, 0.0, 0.0)


WF2_SURF = { 0: " --- ", 1: " AIR ", 2: " GND ", 3: " GRS ", 4: " GVL ", 5: " MUD ", 6: " SNW ", 7: " ICE ", 8: " ASP " }


class AdvInfoOverlay(BaseOverlay):
    def __init__(self, ov: dict):
        self.max_rows = ov.get("max_rows", 16)
        super().__init__(ov, "WF2 AdvInfo")

    def render(self, s_data: AdvInfoSnapshot) -> list:
        s = self.gen_segment  # segment builder
        d = s_data
        lines  = [ ]             # lines accumulator

        def line(*segs):
            lines.append(list(segs))

        def bar(value: float, width: int = 16, full: str = "█", empty: str = "░", s: int = 0) -> str:
            if s == -1:
                value = max(-1.0, min(1.0, value))
                width = width // 2
                if value >= 0.0:
                    n = round(value * width)
                    return empty * width + full * n + empty * (width - n)
                if value < 0.0:
                    n = round(abs(value) * width)
                    return empty * (width - n) + full * n + empty * width
                
            value = max(0.0, min(1.0, value))
            n = round(value * width)
            return full * n + empty * (width - n)

        def fmt_s(ms: int, n: int) -> str:
            return fmt_time(ms) if ms > 0 else "--:--.---"

        line(s(d.track_name[:34]))

        # Race timer
        fin_tag = "good" if d.race_finished else "hi"
        fin_suffix = "  FINISH" if d.race_finished else ""
        line(s("TIME ", "label"), s(fmt_time(d.race_time_ms), fin_tag), s(fin_suffix, "good"))
        line(s("time ", "label"), s(fmt_time(d.race_TIME_ms), "hi"))

        if d.sector_count > 0:
            # Current sector indicator: mark active sector with tag "hi"
            p = d.lap_progress
            cur_sect = 1 if p < d.sector_fract[0] else (2 if p < d.sector_fract[1] else 3)

            def stag(n: int) -> str:
                return "hi" if n == cur_sect else ""

            n = d.sector_count
            s1c, s2c, s3c = d.sect_cur
            s1b, s2b, s3b = d.sect_best
            if n == 1:
                line(s("SECT ", "label"), s(fmt_s(s1c, 1), stag(1)))
                line(s("BEST ", "label"), s(fmt_s(s1b, 1), "header"))
            elif n == 2:
                line(s("SECT ", "label"), s(fmt_s(s1c, 1), stag(1)),  s("  ", ""), s(fmt_s(s2c, 2), stag(2)))
                line(s("BEST ", "label"), s(fmt_s(s1b, 1), "header"), s("  ", ""), s(fmt_s(s2b, 2), "header"))
            else:  # 3 sectors
                line(s("SECT ", "label"), s(fmt_s(s1c, 1), stag(1)),  s("  ", ""), s(fmt_s(s2c, 2), stag(2)),  s("  ", ""), s(fmt_s(s3c, 3), stag(3)))
                line(s("BEST ", "label"), s(fmt_s(s1b, 1), "header"), s("  ", ""), s(fmt_s(s2b, 2), "header"), s("  ", ""), s(fmt_s(s3b, 3), "header"))

        line(s("LAP TIME ", "label"), s(fmt_time(d.lap_time_ms), "hi"))
        lap_tag = "good" if d.pb_time_new > 0 else ""
        lap_suffix = " [NEW PB]" if d.pb_time_new > 0 else ""
        line(s("LAP BEST ", "label"), s(fmt_time(d.lap_time_best), lap_tag), s(lap_suffix, "good"))
        rank_suffix = ""
        if d.pb_time_new > 0:
            rank_suffix = f' → {d.pb_rank_new}' if d.pb_rank_new > 0 else ' → ???'
        line(s("LAP PB   ", "label"), s(fmt_time(d.pb_time), "good"), s(" RANK ", "label"), s(str(d.pb_rank) if d.pb_rank > 0 else ''), s(rank_suffix))
        line(s("LAP WR   ", "label"), s(fmt_time(d.wr_time), "good"))

        # Inputs
        line(s(" ──────────────────────────────", "label"))
        line(s("THR ", "label"), s(bar(d.throttle), "good"),  s(f" {d.throttle *100:5.1f}%"))
        line(s("BRK ", "label"), s(bar(d.brake   ), "warn"),  s(f" {d.brake    *100:5.1f}%"))
        line(s("CLT ", "label"), s(bar(d.clutch)          ),  s(f" {d.clutch   *100:5.1f}%"))
        line(s("HBR ", "label"), s(bar(d.handbrake), "warn"), s(f" {d.handbrake*100:5.1f}%"))
        line(s("STR ", "label"), s(bar(d.steering, s = -1)),  s( f"{d.steering*100:+6.1f}%"))

        # Engine
        line(s(" ──────────────────────────────", "label"))
        line(s("TRQ ", "label"), s(f"{d.torque:7.1f} N·m"), s("  PWR ", "label"), s(f"{d.power/1000:7.1f} kW"))
        t_eng = d.temp_block - 273.15
        t_wat = d.temp_water - 273.15
        t_eng_col = "warn" if t_eng > 120 else ""
        t_wat_col = "warn" if t_wat > 110 else ""
        extinfo = s("  ⚠  MISFIRE", "warn") if d.misfiring else None
        line(s("ENG ", "label"), s(f"{t_eng:7.1f}°C", t_eng_col), s("    WAT ", "label"), s(f"{t_wat:7.1f}°C", t_wat_col))
        line(s("OIL ", "label"), s(f"{d.pressure_oil:7.1f} kPa", "warn" if d.pressure_oil < 100 else ""), extinfo)

        # Traction
        tr = d.traction_state or "NORMAL"
        tr_tag = "air" if tr in ("AIR", "SETTLING") else "good"
        line(s("TRC ", "label"), s(tr, tr_tag))
        '''
        # ── Tires ─────────────────────────────────────────────────────────────
        line(s("        FL      FR      RL      RR", "label"))

        slip_segs = [s(" SLP  ", "label")]
        for v in d.tire_slip:
            slip_segs.append(s(f" {v:+6.3f}", "warn" if abs(v) > 0.3 else ""))
        line(*slip_segs)

        tmp_segs = [s(" TMP  ", "label")]
        for v in d.tire_temp:
            tmp_segs.append(s(f" {v:6.1f}°", "warn" if v > 100 else ""))
        line(*tmp_segs)

        lod_segs = [s(" LOD  ", "label")]
        for v in d.tire_load:
            lod_segs.append(s(f" {v:6.0f}N", "air" if v == 0 else ""))
        line(*lod_segs)

        srf_segs = [s(" SRF  ", "label")]
        for v in d.tire_surf:
            srf_segs.append(s(WF2_SURF.get(v, f"{v:>5} "), "air" if v == 1 else ""))
        line(*srf_segs)

        sus_segs = [s(" SUS  ", "label")]
        for v in d.susp_norm:
            sus_segs.append(s(f"  {v:5.2f}"))
        line(*sus_segs)

        # ── Health ────────────────────────────────────────────────────────────
        line(s(" ──────────────────────────────", "label"))
        hp_tag = "warn" if d.health < 40 else ("good" if d.health > 80 else "")
        line(s(" HP   ", "label"),
             s(bar(d.health / 100, 12), hp_tag),
             s(f" {d.health:3d}%", hp_tag))
        '''
        return lines[:self.max_rows]


class LeaderboardState:
    def __init__(self):
        self.rows       : dict = { }
        self.player_idx : int  = 255
        self.track_name : str  = ""
        self.lap_total  : int  = 0

    def reset(self) -> None:
        self.rows.clear()
        self.player_idx = 255

    def get(self, idx: int) -> ParticipantRow:
        if idx not in self.rows:
            self.rows[idx] = ParticipantRow(index=idx)
        return self.rows[idx]

    def update_main(self, pkt) -> None:
        self.player_idx = pkt.participantPlayerInfo.participantIndex
        self.track_name = pkt.session.trackName.decode("utf-8", errors="replace").strip("\x00")
        self.lap_total  = pkt.session.laps

    def update_leaderboard(self, pkt) -> None:
        for i, lb in enumerate(pkt.participantsLeaderboard):
            if lb.status == PARTICIPANT_STATUS_UNUSED:
                continue
            row = self.get(i)
            row.status       = lb.status
            row.position     = lb.position
            row.lap          = lb.lapCurrent
            row.health       = lb.health
            row.delta_leader = lb.deltaLeader

    def update_timing(self, pkt) -> None:
        for i, tm in enumerate(pkt.participantsTiming):
            if i not in self.rows:
                continue
            self.rows[i].delta_ahead  = tm.deltaAhead
            self.rows[i].delta_behind = tm.deltaBehind

    def update_info(self, pkt) -> None:
        for i, info in enumerate(pkt.participantsInfo):
            if info.participantIndex == 255:
                continue
            idx = info.participantIndex
            row = self.get(idx)
            row.name  = info.playerName.decode("utf-8", errors="replace").strip("\x00")
            row.car_name = info.carName.decode("utf-8", errors="replace").strip("\x00")

    def snapshot(self) -> LeaderboardSnapshot:
        rows = [ ]
        for idx, row in self.rows.items():
            if row.status == PARTICIPANT_STATUS_UNUSED:
                continue
            row.is_player = (idx == self.player_idx)
            rows.append(row)

        # Compute delta_to_player for each row.
        # delta_leader is ms behind the race leader (0 for leader, >0 for rest).
        # delta_to_player = participant.delta_leader - player.delta_leader:
        #   > 0  participant is further behind the leader than the player → behind player
        #   < 0  participant is closer to the leader than the player → ahead of player
        player_rows = [r for r in rows if r.is_player]
        player_delta = player_rows[0].delta_leader if player_rows else 0
        for row in rows:
            row.delta_to_player = player_delta - row.delta_leader

        return LeaderboardSnapshot(
            rows       = rows,
            track_name = self.track_name,
            lap_total  = self.lap_total,
            updated_at = time.monotonic(),
        )


class PlayFabWorker:
    """
    Executes requests to PlayFab in a separate daemon thread.
    The main thread calls request_pb_wr(track_id) and immediately returns.
    The result is available via get_result(), which returns a dict or None.
    States:
        idle — doing nothing
        pending — the task has been assigned, waiting for execution
        running — the request is currently being processed
        done — the result is ready, retrieve it via get_result()
        error — an error occurred (result is None)
    """
    def __init__(self):
        self.task_queue  : queue.Queue = queue.Queue(maxsize = 1)
        self.result      : dict | None = None
        self.result_lock : threading.Lock = threading.Lock()
        self.state       : str = "idle"   # idle / pending / running / done / error
        self.playfab     : WF2PlayFab | None = None
        self.pf_inited   : bool = False
        self.thread = threading.Thread(target = self.worker, daemon = True, name = "PlayFabWorker")
        self.thread.start()

    def request_pb_wr(self, track_id: str) -> None:
        with self.result_lock:
            self.result = None
        self.state = "pending"
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.task_queue.put_nowait(track_id)
        except queue.Full:
            pass

    def get_result(self) -> dict | None:
        if self.state != "done":
            return None
        with self.result_lock:
            result = self.result
            self.result = None
        self.state = "idle"
        return result

    def worker(self) -> None:
        while True:
            try:
                track_id = self.task_queue.get(timeout = 1.0)
            except queue.Empty:
                continue
            self.state = "running"
            result = self.fetch(track_id)
            with self.result_lock:
                self.result = result
            self.state = "done" if result is not None else "error"

    def fetch(self, track_id: str) -> dict | None:
        pb_time = 0
        pb_rank = 0
        wr_time = 0
        rank_page = [ ]
        try:
            if not self.pf_inited:
                if self.playfab is None:
                    self.playfab = WF2PlayFab()
                self.pf_inited = self.playfab.init_auth(attach_game = True)
                if not self.pf_inited:
                    print("[PlayFab] [ERROR] Failed to initialize auth.")
                    return None
            # Personal best
            entry = self.playfab.get_my_time(track_id)
            if entry:
                pb_time = int(entry.get("Scores", [0])[0])
                pb_rank = entry.get("Rank", 0)
            # World record (top-1)
            entry_list = self.playfab.get_top(track_id, max_results = 1)
            if entry_list and entry_list[0]:
                wr_time = int(entry_list[0].get("Scores", [0])[0])
            print(f"[PlayFab] {track_id}  WR: {fmt_time(wr_time)}  PB: {fmt_time(pb_time)}  Rank: {pb_rank}")
        except Exception as e:
            print(f"[PlayFab] [ERROR] fetching {track_id}: {e}")
            if "401" in str(e) or "Unauthorized" in str(e) or "EntityTokenExpired" in str(e):
                self.pf_inited = False
            return None
        try:
            # Fetch rank page. Single GetLeaderboard call: start=max(1, pb_rank-98), page_size=100.
            # Stored as list of (rank, score_ms), rank ascending, score_ms lower=faster.
            if pb_rank > 0:
                lb_name = self.playfab.normalize_leaderboard_name(track_id)
                start = max(1, pb_rank - 98)
                page, _ = self.playfab.client.get_leaderboard_page(lb_name, starting_position = start, page_size = 100)
                rank_page = [ (entry.get("Rank", 0), int(entry.get("Scores", [0])[0])) for entry in page ]
                time_range = fmt_time(rank_page[0][1]) + ' ... ' + fmt_time(rank_page[-1][1]) if rank_page else ""
                print(f"[PlayFab] Rank page: loaded {len(rank_page)} entries from rank {start}  [{time_range}]")
        except Exception as e:
            print(f"[PlayFab] [ERROR] Rank page fetch failed: {e}")

        return { "pb_time": pb_time, "pb_rank": pb_rank, "wr_time": wr_time, "rank_page": rank_page }


class AdvInfoState:
    def __init__(self):
        self.pf_worker = PlayFabWorker()   # daemon thread, starts immediately
        self.last_track_id: str = ""       # track for which PB/WR was last requested
        self.reset()

    def reset(self) -> None:
        self.data = AdvInfoSnapshot()
        return self.data

    def get_data(self):
        return deepcopy(self.data)

    def check_playfab_result(self) -> None:
        result = self.pf_worker.get_result()
        if result:
            self.data.pb_time   = result["pb_time"]
            self.data.pb_rank   = result["pb_rank"]
            self.data.wr_time   = result["wr_time"]
            self.data.rank_page = result.get("rank_page", [ ])

    def renew_from_main(self, pkt, traction_state: str = "") -> int:
        data = self.data
        hdr  = pkt.header
        eng  = pkt.carPlayer.engine
        inp  = pkt.carPlayer.input
        lb   = pkt.participantPlayerLeaderboard
        tm   = pkt.participantPlayerTiming
        tms  = pkt.participantPlayerTimingSectors
        ses  = pkt.session
        tires = pkt.carPlayer.tires
        
        s_status = ses.status              # see enum SessionStatus
        g_status = hdr.statusFlags         # see enum GameStatusFlags
        p_status = pkt.playerStatusFlags   # see enum PlayerStatusFlags

        self.check_playfab_result()

        race_time_ms = max(0, hdr.raceTime)
        now = time.monotonic()
        need_update_times = False

        if not data.race_inited and s_status == SESSION_STATUS_COUNTDOWN:
            data = self.reset()
            data.race_inited = True
            data.sector_count = ses.sectorCount
            data.sector_fract = (ses.sectorFract1, ses.sectorFract2, 1.0)
            data.track_id = ses.trackId.decode("utf-8", errors="replace").strip("\x00")
            data.track_name = ses.trackName.decode("utf-8", errors="replace").strip("\x00")
            print('>>> race_inited')
            self.last_track_id = data.track_id
            self.pf_worker.request_pb_wr(self.last_track_id)

        if not data.race_started and data.race_inited and not data.race_stopped and race_time_ms > 0:
            data.race_started = True
            data.start_TIME_s = now
            data.start_time_ms = race_time_ms
            print(f'>>> race_started   {race_time_ms} ms  status = 0x{pkt.playerStatusFlags:02X}')

        after_race = False
        if data.race_inited:
            after_race = s_status == SESSION_STATUS_ABANDONED or s_status == SESSION_STATUS_POST_RACE
            if not after_race:
                after_race = (p_status & PLAYER_STATUS_CONTROL_AI) or (g_status & GAME_STATUS_IN_RACE) == 0

        if not data.race_stopped and data.race_started and after_race:
            data.race_stopped = True
            data.race_inited = False
            print('>>> race_stopped')
            need_update_times = True

        if not data.race_finished and lb.status == PARTICIPANT_STATUS_FINISH_SUCCESS:
            data.race_finished = True
            data.race_inited = False
            data.pkt_count_after_finish = 0
            print('>>> race_finished')
            need_update_times = True

        if data.race_started and not data.race_stopped and not data.race_finished:
            need_update_times = True

        if data.race_started and data.race_finished:
            data.pkt_count_after_finish += 1
            if data.pkt_count_after_finish <= 3*60:
                if tm.lapTimeCurrent > 0:
                    need_update_times = True

        if need_update_times:
            if data.pkt_count_after_finish == 0:
                data.race_time_ms = race_time_ms
                data.race_TIME_ms = int((now - data.start_TIME_s) * 1000) + data.start_time_ms

            if data.lap_time_ms > tm.lapTimeCurrent:
                # new lap started
                pass
            
            data.lap_time_ms = tm.lapTimeCurrent
            data.lap_time_best = tm.lapTimeBest
            data.lap_progress = tm.lapProgress

            prev_pb_time_new = data.pb_time_new
            if data.pb_time > 1:
                if tm.lapTimeBest < data.pb_time and tm.lapTimeBest > 0:
                    data.pb_time_new = tm.lapTimeBest
                if tm.lapTimeCurrent < data.pb_time_new and tm.lapTimeCurrent > 8000 and data.race_finished:
                    data.pb_time_new = tm.lapTimeCurrent
                    data.lap_time_best = data.pb_time_new
                if data.pb_time_new > 0 and data.pb_time_new < data.pb_time and data.rank_page:
                    page_first_time = data.rank_page[0][1]  # [ ( rank, score_ms ) ]
                    if data.pb_time_new <= page_first_time:
                        data.pb_rank_new = 0
                    else:
                        for rank, score_ms in data.rank_page:
                            if score_ms > data.pb_time_new:
                                data.pb_rank_new = rank
                                break

            if prev_pb_time_new != data.pb_time_new:
                rank_new = str(data.pb_rank_new) if data.pb_rank_new > 0 else "???"
                print(f'[WF2] NEW PB: {fmt_time(data.pb_time_new)}  RANK: {data.pb_rank} -> {rank_new}')

            s1 = tms.sectorTimeCurrentLap1 
            s2 = tms.sectorTimeCurrentLap2
            if data.sector_count >= 3 and tm.lapProgress >= data.sector_fract[1]:
                s3 = max(0, tm.lapTimeCurrent - s1 - s2)
            else:
                s3 = 0
            data.sect_cur  = ( s1, s2, s3)
            data.sect_best = ( tms.sectorTimeBest1, tms.sectorTimeBest2, tms.sectorTimeBest3 )

        data.throttle       = inp.throttle
        data.brake          = inp.brake
        data.clutch         = inp.clutch
        data.handbrake      = inp.handbrake
        data.steering       = inp.steering

        data.torque         = eng.torque
        data.power          = eng.power
        data.temp_block     = eng.tempBlock
        data.temp_water     = eng.tempWater
        data.pressure_oil   = eng.pressureOil
        data.misfiring      = eng.misfiring

        data.traction_state = traction_state
        data.health         = lb.health

        data.tire_slip = tuple(tires[i].slipRatio                 for i in range(4))
        data.tire_temp = tuple(tires[i].temperatureTread - 273.15 for i in range(4))
        data.tire_load = tuple(tires[i].loadVertical              for i in range(4))
        data.tire_surf = tuple(tires[i].surfaceType               for i in range(4))
        data.susp_norm = tuple(tires[i].suspensionDispNorm        for i in range(4))
        return 1


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

    def __init__(self, radius_m: float = 250.0):
        self.radius_m         = radius_m
        self.player_x         = 0.0
        self.player_z         = 0.0
        self.heading_x        = 0.0
        self.heading_z        = 1.0
        self.motion_positions : dict = {}
        self.lock             = threading.Lock()
        self.ready            = threading.Event()
        self.poll_interval_s  = 0.1   # default 100ms, updated via set_poll_interval()
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
                "motion_positions": dict(self.motion_positions),
            }


class TailDistOverlay:
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
    POLL_INTERVAL_DEFAULT = 100
    MAX_DELTA_MS_DEFAULT  = 15000

    def __init__(self, ov: dict, exe_name: str = "Wreckfest2.exe"):
        self.ov           = ov
        self.fov          = float(ov.get("fov",           self.FOV_DEFAULT))
        self.canvas_h     = int(  ov.get("canvas_height", self.CANVAS_HEIGHT_DEFAULT))
        self.marker_width = float(ov.get("marker_width",  self.MARKER_WIDTH_DEFAULT))
        self.color_far    = ov.get("color_far",     self.COLOR_FAR_DEFAULT)
        self.color_near   = ov.get("color_near",    self.COLOR_NEAR_DEFAULT)
        self.outline_color= ov.get("outline_color", self.OUTLINE_COLOR_DEFAULT)
        self.name_color   = ov.get("name_color",    self.NAME_COLOR_DEFAULT)
        self.name_shadow  = ov.get("name_shadow",   self.NAME_SHADOW_DEFAULT)
        self.max_rivals   = int(ov.get("max_rivals",    self.MAX_RIVALS_DEFAULT))
        self.poll_interval= int(ov.get("poll_interval", self.POLL_INTERVAL_DEFAULT))
        self.max_delta_ms = int(ov.get("max_delta_ms",  self.MAX_DELTA_MS_DEFAULT))
        self.alpha        = float(ov.get("alpha", 0.5))
        self.transparent  = bool( ov.get("transparent", True))
        self.chroma_key   = ov.get("chroma_key", "#000005")
        self.bg           = self.chroma_key if self.transparent else ov.get("bg", "#000000")
        self.font_family  = ov.get("font", "Terminal")
        self.font_size    = int(ov.get("font_size", 14))

        self.game_rect    = None
        self.rect_age     = 0.0
        self.tail_state   = None   # set via attach() before use
        self.lb_state     = None   # set via attach() before use
        self.root         = None
        self.canvas       = None
        self.tk_font      = None

        self.proc = Win64Process()
        self.proc.find_process(exe_name)

        self.thread = threading.Thread(target = self.run, daemon = True)
        self.thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, tail_state: "TailDistState", lb_state: "LeaderboardState") -> None:
        """Bind data sources. Call once before the overlay thread starts processing."""
        self.tail_state = tail_state
        self.lb_state   = lb_state
        tail_state.set_poll_interval(self.poll_interval)

    def set_visible(self, visible: bool) -> None:
        root = self.root
        if root is None:
            return
        if visible:
            root.after(0, root.deiconify)
        else:
            root.after(0, root.withdraw)

    # ------------------------------------------------------------------
    # tkinter thread
    # ------------------------------------------------------------------

    def run(self) -> None:
        root = tk.Tk()
        self.root = root
        root.title("WF2 TailDist")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.alpha)
        root.configure(bg=self.bg)
        if self.transparent:
            root.attributes("-transparentcolor", self.chroma_key)
        self.tk_font = tkfont.Font(root = root, family = self.font_family, size = self.font_size)
        self.canvas = tk.Canvas(root, bg = self.bg, highlightthickness = 0, cursor = "arrow")
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

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def poll(self) -> None:
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
            self.proc.find_process("Wreckfest2.exe")
        self.game_rect = self.proc.get_process_window_rect()

    def reposition(self) -> None:
        self.refresh_game_rect()
        if self.game_rect is None or self.root is None:
            return
        gl, gt, gw, gh = self.game_rect
        y = gt + gh - self.canvas_h
        self.root.geometry(f"{gw}x{self.canvas_h}+{gl}+{y}")
        if self.canvas:
            self.canvas.configure(width = gw, height = self.canvas_h)

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
                angle_deg = -math.degrees(math.atan2(cross, dot))
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

        if not snap.rivals or self.game_rect is None:
            return

        _, _, canvas_w, _ = self.game_rect
        h        = self.canvas_h
        radius_m = snap.radius_m
        font     = self.tk_font

        # Pass 1: ellipses, farthest first (snap.rivals already sorted)
        for rival in snap.rivals:
            cx = self.angle_to_x(rival.angle_deg, canvas_w)
            vert_r, horiz_r = self.dist_to_radii(rival.dist_m, radius_m)
            color = self.dist_to_color(rival.dist_m, radius_m)
            x0 = cx - horiz_r
            y0 = h  - vert_r
            x1 = cx + horiz_r
            y1 = h  + vert_r    # lower half clipped by canvas bottom
            canvas.create_oval(x0, y0, x1, y1, fill=color, outline=self.outline_color)

        # Pass 2: names, farthest first
        for rival in snap.rivals:
            cx        = self.angle_to_x(rival.angle_deg, canvas_w)
            vert_r, _ = self.dist_to_radii(rival.dist_m, radius_m)
            color     = self.dist_to_color(rival.dist_m, radius_m)
            name      = rival.name[:self.NAME_MAX_LEN]
            ny        = h - vert_r   # top of ellipse = name baseline
            # shadow pass then main pass for readability
            # anchor="n" places text top at ny (top edge of ellipse)
            canvas.create_text(cx + 1, ny + 1, text=name, fill=self.name_shadow, font=font, anchor="n")
            canvas.create_text(cx,     ny,     text=name, fill=self.name_color,  font=font, anchor="n")

# ===========================================================================================

def create_overlays(cfg: dict):
    ovs = cfg.get("overlays", {})
    lb_ov   = None
    adv_ov  = None
    tail_ov = None

    lb_ov_cfg = ovs.get("leaderboard")
    if lb_ov_cfg is not None:
        lb_ov_cfg = get_ov_cfg(lb_ov_cfg)
        lb_ov = LeaderboardOverlay(lb_ov_cfg)

    adv_ov_cfg = ovs.get("advinfo")
    if adv_ov_cfg is not None:
        adv_ov_cfg = get_ov_cfg(adv_ov_cfg)
        adv_ov = AdvInfoOverlay(adv_ov_cfg)

    tail_cfg = ovs.get("taildist")
    if tail_cfg is not None:
        tail_cfg = get_ov_cfg(tail_cfg)
        tail_ov = TailDistOverlay(tail_cfg)

    return lb_ov, adv_ov, tail_ov

