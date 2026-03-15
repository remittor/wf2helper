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
    pb_time        : int = 0
    pb_time_new    : int = 0
    wr_time        : int = 0

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
        rank_suffix = f' → ???' if d.pb_time_new > 0 else ""
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
        try:
            if not self.pf_inited:
                if self.playfab is None:
                    self.playfab = WF2PlayFab()
                self.pf_inited = self.playfab.init_auth(attach_game = True)
                if not self.pf_inited:
                    print("[PlayFab] Failed to initialize auth.")
                    return None
            pb_time = 0
            pb_rank = 0
            wr_time = 0
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
            return { "pb_time": pb_time, "pb_rank": pb_rank, "wr_time": wr_time }
        except Exception as e:
            print(f"[PlayFab] Error fetching {track_id}: {e}")
            if "401" in str(e) or "Unauthorized" in str(e) or "EntityTokenExpired" in str(e):
                self.pf_inited = False
            return None


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
            self.data.pb_time = result["pb_time"]
            self.data.pb_rank = result["pb_rank"]
            self.data.wr_time = result["wr_time"]

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
            data.race_time_ms = race_time_ms
            if data.pkt_count_after_finish == 0:
                data.race_TIME_ms = int((now - data.start_TIME_s) * 1000) + data.start_time_ms

            if data.lap_time_ms > tm.lapTimeCurrent:
                # new lap started
                pass
            
            data.lap_time_ms = tm.lapTimeCurrent
            data.lap_time_best = tm.lapTimeBest
            data.lap_progress = tm.lapProgress

            if data.pb_time > 1:
                if tm.lapTimeBest < data.pb_time and tm.lapTimeBest > 0:
                    data.pb_time_new = tm.lapTimeBest
                if tm.lapTimeCurrent < data.pb_time_new and tm.lapTimeCurrent > 8000 and data.race_finished:
                    data.pb_time_new = tm.lapTimeCurrent
                    data.lap_time_best = data.pb_time_new

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


def create_overlays(cfg: dict):
    ovs = cfg.get("overlays", {})
    lb_ov  = None
    adv_ov = None

    lb_ov_cfg = ovs.get("leaderboard")
    if lb_ov_cfg is not None:
        lb_ov_cfg = get_ov_cfg(lb_ov_cfg)
        lb_ov = LeaderboardOverlay(lb_ov_cfg)

    adv_ov_cfg = ovs.get("advinfo")
    if adv_ov_cfg is not None:
        adv_ov_cfg = get_ov_cfg(adv_ov_cfg)
        adv_ov = AdvInfoOverlay(adv_ov_cfg)

    return lb_ov, adv_ov

