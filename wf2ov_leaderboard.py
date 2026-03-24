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

    @property
    def state_str(self) -> str:
        if self.status == PARTICIPANT_STATUS_RACING:
            return self.delta_str
        return self.status_str


@dataclass
class LeaderboardSnapshot:
    rows:       list  = field(default_factory=list)
    track_name: str   = ""
    lap_total:  int   = 0
    updated_at: float = 0.0


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


class LeaderboardOverlay(BaseOverlay):
    def __init__(self, cfg_path: str):
        super().__init__(cfg_path, "leaderboard", "WF2 Leaderboard")
        self.show_slot_num = self.ov.get('show_slot_num', False)
        self.start()

    def render(self, snap: LeaderboardSnapshot) -> list:
        seg = self.gen_segment
        lines = [ ]

        def line(*segs):
            lines.append(list(segs))

        track = snap.track_name[:30] if snap.track_name else "---"
        line(seg(f" {track}", "header"))
        slot_num = '  #' if self.show_slot_num else ''
        line(seg(f"Pos  {'Name':<12} {'Car':<8} {'Lap':>5}  {'State':>8}  {'HP':>3}{slot_num}", "header"))

        rows = sorted(snap.rows, key=lambda r: r.position if r.position > 0 else 999)
        for row in rows:
            cur_lap  = row.lap
            if row.status == PARTICIPANT_STATUS_FINISH_SUCCESS or row.status == PARTICIPANT_STATUS_FINISH_ELIMINATED:
                cur_lap = snap.lap_total
            lap_str  = f"{cur_lap}/{snap.lap_total}" if snap.lap_total else str(cur_lap)
            name_str = row.name[:12]    if row.name     else f"P{row.index:02d}"
            car_str  = row.car_name[:8] if row.car_name else ""
            pos_str  = f"{row.position:>2}" if row.position else " ?"
            tag      = "player" if row.is_player else ("dnf" if row.status_str else "")
            slot_num = seg(f'{str(row.index):>3}', tag) if self.show_slot_num and row.index < 100 else None
            line(seg(f" {pos_str}  {name_str:<12}", tag), seg(f" {car_str:<8} {lap_str:>5}  {row.state_str:>8}  {row.health:>3}", tag), slot_num)

        return lines[:self.max_rows]

