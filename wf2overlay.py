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
from wf2playfab import *
from wf2app import WF2_EXE_NAME


OV_CFG_DEFAULTS = dict(
    show        = True,
    max_rows    = 16,
    x           = 20,
    y           = 400,
    bg_alpha    = 0.2,
    bg_color    = "#111111",
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
    Always-on-top tkinter text overlay with optional semi-transparent background.

    Two separate Tk windows are used:
      bg_root  — background window, solid colour at bg_alpha opacity.
                 Only shown during SESSION_STATUS_COUNTDOWN / RACING. Controlled via set_race_active().
      root     — text window, transparent background, text at alpha opacity.
                 Always visible when the overlay is shown (set_visible).

    Config keys:
      show        — bool, if False the overlay renders nothing (default True)
      bg_alpha    — opacity of the background window bg_root (default 0.2)
      bg_color    — background fill colour for bg_root (default "#000000")
      alpha       — opacity of the text window (default 0.82)
      transparent — use chroma-key transparency on text window (default True)
      chroma_key  — chroma-key colour (default "#000005")
      bg          — background fill colour (default "#000000")
    """

    SHADOW_DX = 1
    SHADOW_DY = 1
    PAD       = 4   # canvas padding px

    def __init__(self, ov: dict, title: str):
        self.ov     = ov
        self.title  = title
        
        self.show         = bool(ov.get("show", True))
        self.bg_alpha     = float(ov.get("bg_alpha", 0.2))
        self.bg_color     = ov.get("bg_color", "#000000")
        self.alpha        = float(ov.get("bg_alpha", 0.8))
        self.shadow       = ov.get("shadow", "#000000")
        self.transparent  = bool( ov.get("transparent", True))
        self.chroma_key   = ov.get("chroma_key", "#000005")
        self.bg           = self.chroma_key if self.transparent else ov.get("bg", "#000000")
        self.font_family  = ov.get("font", "Fixedsys")
        self.font_size    = int(ov.get("font_size", 9))
        self.font_weight  = "bold" if ov.get("bold", True) else "normal"
        self.max_rows     = ov.get("max_rows", 16)
        self.gap_rows     = ov.get("gap_rows", 1)
        
        self.queue  = queue.Queue(maxsize=8)
        self.cmd_queue = queue.Queue()  # thread-safe window commands
        self.ov_visible = False
        self.race_active = False

        self.last   = None
        self.root   = None
        self.bg_root= None
        self.canvas = None
        self.bg_canvas = None
        self.font   = None   # tkfont.Font
        self.cw     = 0      # char width px
        self.lh     = 0      # line height px
        self.thread = threading.Thread(target = self.run, daemon = True)
        self.thread.start()

    def push(self, data) -> None:
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            pass

    def set_visible(self, visible: bool) -> None:
        """Show/hide both windows. Thread-safe via cmd_queue."""
        self.cmd_queue.put(("visible", visible))

    def set_race_active(self, active: bool) -> None:
        """Show/hide background window. Thread-safe via cmd_queue."""
        self.cmd_queue.put(("race_active", active))

    def is_bitmap_font(self, font):
        font_name = font if isinstance(font, str) else font.actual('family')
        size_dict = { }
        for fsize in [ 11, 12, 13 ]:
            xfont = tkfont.Font(self.root, family = font_name, size = fsize)
            xsize = xfont.metrics("linespace")
            size_dict[xsize] = fsize
            del xfont
        return True if len(size_dict) == 1 else False
    
    def load_font(self, wnd = None):
        if not wnd:
            wnd = self.root
        font_name = self.ov["font"]
        font_size = self.ov["font_size"]
        font = tkfont.Font(
            root   = wnd,
            family = font_name,
            size   = font_size,
            weight = "bold" if self.ov.get("bold", False) else "normal",
        )
        #print(f'OV: "{self.title}"  FONT: {font_name}   SIZE: {font_size}   HEIGHT: {font.metrics("linespace")}')
        if wnd == self.root:
            font_bitmap = self.is_bitmap_font(font_name)
            if not font_bitmap:
                print(f'[WARN] Font "{font_name}" is not a bitmap!')
            elif font_size < 0 and font.metrics("linespace") != abs(font_size):
                size_dict = { }
                for fsize in range(8, 65):
                    xfont = tkfont.Font(self.root, family = font_name, size = fsize)
                    xsize = xfont.metrics("linespace")
                    size_dict[xsize] = fsize
                print(f'[WARN] Bitmap font "{font_name}" does not support size {abs(font_size)} (available sizes: {size_dict.keys()})')
        return font

    def run(self) -> None:
        ov = self.ov
        bg = None
        if self.bg_alpha > 0.0:
            # Background window — solid colour, no transparency, lower z-order
            bg = tk.Tk()
            self.bg_root = bg
            bg.title(self.title + " BG")

        # Text window — transparent background, text rendered on top
        root = tk.Tk()
        self.root = root
        root.title(self.title)

        if bg:
            bg.overrideredirect(True)
            bg.attributes("-topmost", True)
            bg.attributes("-alpha", self.bg_alpha)
            bg.configure(bg=self.bg_color)
            bg.geometry(f"+{ov['x']}+{ov['y']}")
            bg.withdraw()   # hidden until race session becomes active
            self.bg_canvas = tk.Canvas(bg, bg = self.bg_color, highlightthickness = 0, cursor = "arrow")
            self.bg_canvas.pack(fill=tk.BOTH, expand=True)
            self.bg_font = self.load_font(bg)
            self.bg_canvas.configure(width=1, height=1)

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
        self.font = self.load_font()
        self.cw   = self.font.measure("W")
        self.lh   = self.font.metrics("linespace") + self.gap_rows

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

    def ov_show(self, with_bg = True):
        self.root.deiconify()
        if self.bg_root:
            self.bg_root.deiconify() if with_bg else self.bg_root.withdraw()

    def ov_hide(self):
        self.root.withdraw()
        if self.bg_root:
            self.bg_root.withdraw()

    def read_queues(self, process_cmd: bool = True):
        if process_cmd:
            # Process window commands from other threads
            cmd_count = 0
            try:
                while True:
                    cmd, arg = self.cmd_queue.get_nowait()
                    if cmd:
                        cmd_count += 1
                        if cmd == "visible":
                            self.ov_visible = arg
                        elif cmd == "race_active":
                            self.race_active = arg
            except queue.Empty:
                pass
            if cmd_count > 0:
                if self.ov_visible:
                    self.ov_show(self.race_active)
                else:
                    self.ov_hide()
        # data for processing in poll
        data = None
        try:
            while True:
                data = self.queue.get_nowait()
        except (queue.Empty, queue.Full):
            pass
        return data
    
    def poll(self) -> None:
        self.last = self.read_queues()
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
        if self.bg_root:
            self.bg_canvas.delete("all")

        if not self.show:
            # Collapse to 1x1 so the window is invisible but alive
            canvas.configure(width=1, height=1)
            self.root.geometry("1x1")
            if self.bg_root:
                self.bg_root.geometry("1x1")
            return

        pad    = self.PAD
        lh     = self.lh
        cw     = self.cw
        font   = self.font
        shadow = self.shadow
        dx, dy = self.SHADOW_DX, self.SHADOW_DY
        fg_def = self.ov["fg"]
        max_col = 0
        canvas_txt_list = [ ]
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
                txt_shadow = ( x + dx, y + dy, { "text": text, "anchor": 'nw', "fill": shadow } )
                canvas_txt_list.append( txt_shadow )
                txt_main = ( x, y, { "text": text, "anchor": 'nw', "fill": color or fg_def } )
                canvas_txt_list.append( txt_main )
                col += len(text)
                pass
            max_col = max(max_col, col)
            pass
        w = pad * 2 + max_col * cw + dx
        h = pad * 2 + len(lines) * lh + dy
        # Keep bg_root same size and position, mirror text onto bg_canvas
        if self.bg_root and self.race_active:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            self.bg_root.geometry(f"{w}x{h}+{x}+{y}")
            self.bg_canvas.configure(width=w, height=h)
            for txt in canvas_txt_list:
                self.bg_canvas.create_text(txt[0], txt[1], font = self.bg_font, **txt[2])
        # Output all text lines on main canvas
        for txt in canvas_txt_list:
            canvas.create_text(txt[0], txt[1], font = self.font, **txt[2])
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
            # Move bg_root to same position
            if self.bg_root:
                self.bg_root.geometry(f"+{x}+{y}")

