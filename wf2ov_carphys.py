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
# CarPhysOverlay
# =============================================================================
# Displays three dial gauges showing orientation and velocity vectors.
#
# Dial 1: orientation vector in XZ plane (from orientationQuatX / QuatZ)
# Dial 2: velocityLocal vector in XZ plane (velocityLocalX / velocityLocalZ)
# Dial 3: camera heading angle (reserved, always 0 until memory read added)
#
# Angle convention (matches task description):
#   0 deg  = vector along +X axis
#   90 deg = vector along +Z axis
#   formula: atan2(z, x)
#
# bg_alpha is set to 0.0 for this overlay so bg_root is not created.
# Graphics (ovals, lines) are drawn directly on self.canvas via draw_graphic().
# Text segments go through the standard BaseOverlay text pipeline.
# =============================================================================

@dataclass
class CarPhysSnapshot:
    ori_x   : float = 0.0   # orientationQuatX
    ori_z   : float = 0.0   # orientationQuatZ
    vel_x   : float = 0.0   # velocityLocalX
    vel_z   : float = 0.0   # velocityLocalZ

class CarPhysState:
    def __init__(self):
        self.data = CarPhysSnapshot()

    def update(self, pkt) -> None:
        ori = pkt.carPlayer.orientation
        vel = pkt.carPlayer.velocity
        d   = self.data
        forwardX = 2*(ori.orientationQuatX * ori.orientationQuatZ + ori.orientationQuatW * ori.orientationQuatY)
        forwardY = 2*(ori.orientationQuatY * ori.orientationQuatZ - ori.orientationQuatW * ori.orientationQuatX)
        forwardZ = 1 - 2*(ori.orientationQuatX * ori.orientationQuatX + ori.orientationQuatY * ori.orientationQuatY)
        d.ori_x = forwardX
        d.ori_z = forwardZ
        d.vel_x = vel.velocityLocalX
        d.vel_z = vel.velocityLocalZ

    def get_data(self) -> CarPhysSnapshot:
        return CarPhysSnapshot(
            ori_x = self.data.ori_x,
            ori_z = self.data.ori_z,
            vel_x = self.data.vel_x,
            vel_z = self.data.vel_z,
        )

class CarPhysOverlay(BaseOverlay):
    """
    Shows orientation, velocityLocal and camera angle as dial gauges.
    bg_alpha must be 0.0 (no background window needed).
    Graphics are drawn on self.canvas alongside the text.
    """
    DIAL_RADIUS_DEFAULT = 40

    def __init__(self, cfg_path: str):
        super().__init__(cfg_path, "car_phys", "WF2 CarPhys")
        ov = self.ov
        # Force bg_alpha to 0 so BaseOverlay skips bg_root creation
        ov["bg_alpha"] = 0.0
        self.dial_radius = int(ov.get("dial_radius", self.DIAL_RADIUS_DEFAULT))
        self.start()

    # BaseOverlay.render() must return list-of-lines for text.
    # We return empty list — all drawing is done in draw_graphic().
    def render(self, snap: CarPhysSnapshot) -> list:
        return [ ]

    # Override draw() to handle both text (none here) and graphics.
    def draw(self, lines: list) -> None:
        canvas = self.canvas
        if canvas is None:
            return
        canvas.delete("all")
        if not self.show:
            canvas.configure(width=1, height=1)
            self.root.geometry("1x1")
            return
        snap = self.last   # set by poll() before calling draw()
        if not isinstance(snap, CarPhysSnapshot):
            return
        self.draw_graphic(canvas, snap)

    def draw_graphic(self, canvas, snap: CarPhysSnapshot) -> None:
        r     = self.dial_radius
        pad   = self.PAD
        font  = self.font
        fg    = self.ov.get("fg", "#ffffff")
        color_needle = self.ov.get("needle_color", "#00ff88")
        color_circle = self.ov.get("circle_color", "#aaaaaa")
        lh    = self.lh if self.lh else 16

        # Three dials stacked vertically, each dial takes 2*r height + gap
        gap      = pad * 2
        dial_h   = r * 2 + gap
        text_x   = pad + r * 2 + gap    # X where text starts (right of dial)
        text_w   = 16                   # approximate text column width in chars
        dials = [
            ("orientation", snap.ori_x, snap.ori_z, 0.0),
            ("velocityLocal", snap.vel_x, snap.vel_z, 0.0),
            ("camera", 0.0, 0.0, 0.0),   # reserved — always 0
        ]
        total_h = pad + len(dials) * dial_h
        total_w = text_x + text_w * self.cw + pad

        for i, (label, vx, vz, cam_angle) in enumerate(dials):
            cy = pad + r + i * dial_h   # dial centre Y
            cx = pad + r                # dial centre X
            # Draw circle
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color_circle, width=1, fill="")
            # Compute angle: 0 deg = along +X, 90 deg = along +Z
            if label == "camera":
                angle_deg = cam_angle
                angle_rad = math.radians(angle_deg)
                nx = math.cos(angle_rad)
                nz = math.sin(angle_rad)
            else:
                mag = math.sqrt(vx * vx + vz * vz)
                if mag > 1e-6:
                    nx = vx / mag
                    nz = vz / mag
                else:
                    nx, nz = 1.0, 0.0
                angle_deg = math.degrees(math.atan2(vx, vz))
            # Draw needle: angle 0=right, 90=down in screen coords
            # Screen X = right = +X axis, screen Y = down = +Z axis
            needle_x = cx + int(nx * r)
            needle_y = cy - int(nz * r)
            canvas.create_line(cx, cy, needle_x, needle_y, fill=color_needle, width=2)
            # Centre dot
            canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=color_needle, outline="")
            # Text rows to the right of dial
            ty = cy - r   # top align with dial top
            rows = [
                f"{label}",
                f"angle: {angle_deg:+8.2f} deg",
                f"    X: {vx:+8.6f}",
                f"    Z: {vz:+8.6f}",
            ]
            for j, row in enumerate(rows):
                canvas.create_text(text_x, ty + j * lh, text=row, fill=fg, font=font, anchor="nw")

        canvas.configure(width=total_w, height=total_h)
        self.root.geometry(f"{total_w}x{total_h}")

