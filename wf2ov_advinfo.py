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
from wf2playfab import *
from wf2app import WF2_EXE_NAME
from wf2overlay import *


@dataclass
class AdvInfoSnapshot:
    race_inited    : bool  = False
    race_started   : bool  = False
    race_stopped   : bool  = False
    race_finished  : bool  = False
    
    pkt_count_after_finish: int = 0
    
    track_id       : str = ""
    track_name     : str = ""
    car_name       : str = ""
    pb_rank        : int = 0
    pb_rank_new    : int = 0   # estimated new rank after PB, from rank_dict
    pb_time        : int = 0
    pb_time_new    : int = 0
    wr_time        : int = 0
    rank_dict      : dict = field(default_factory=dict)  # rank: { "score_ms": XXXX }  -- dict fetched at race start

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


# states for PlayFabWorker 
PF_WRK_IDLE    = 1
PF_WRK_PENDING = 2
PF_WRK_RUNNING = 3
PF_WRK_DONE    = 4

class PlayFabWorker:
    """
    Executes requests to PlayFab in a separate daemon thread.
    The main thread calls request_pb_wr(track_id) and immediately returns.
    The result is available via get_result(), which returns a dict or None.
    States:
        idle    — doing nothing
        pending — the task has been assigned, waiting for execution
        running — the request is currently being processed
        done    — the result is ready, retrieve it via get_result()
    """
    def __init__(self, daemon: bool = True):
        self.result      : dict | None = None
        self.result_lock : threading.Lock = threading.Lock()
        self.state       : int = PF_WRK_IDLE
        self.errcode     : int = 0
        self.playfab     : WF2PlayFab | None = None
        self.pf_inited   : bool = False
        if daemon:
            self.task_queue  : queue.Queue = queue.Queue(maxsize = 1)
            self.thread = threading.Thread(target = self.worker, daemon = True, name = "PlayFabWorker")
            self.thread.start()

    def request_pb_wr(self, track_id: str, pb_n_wr: bool = True, rank_page: int = 0) -> None:
        with self.result_lock:
            self.result = None
        self.state = PF_WRK_PENDING
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.task_queue.put_nowait( (track_id, pb_n_wr, rank_page) )
        except queue.Full:
            pass

    def get_result(self) -> dict | None:
        if self.state != PF_WRK_DONE:
            return None, None
        with self.result_lock:
            result = self.result
            self.result = None
            errcode = self.errcode
            self.errcode = 0
        self.state = PF_WRK_IDLE
        return errcode, result

    def worker(self) -> None:
        while True:
            try:
                track_id, pb_n_wr, rank_page = self.task_queue.get(timeout = 1.0)
            except queue.Empty:
                continue
            self.state = PF_WRK_RUNNING
            result = self.fetch(track_id, pb_n_wr, rank_page)
            with self.result_lock:
                self.result = result
            self.state = PF_WRK_DONE

    def fetch(self, track_id: str, pb_n_wr: bool = True, rank_page: int = 0) -> dict | None:
        res = { "track_id": track_id }
        if rank_page >= 0:
            res.update( { 'rank_page': [ ] } )
        try:
            if not self.pf_inited:
                if self.playfab is None:
                    self.playfab = WF2PlayFab()
                self.pf_inited = self.playfab.init_auth(attach_game = True)
                if not self.pf_inited:
                    print("[PlayFab] [ERROR] Failed to initialize auth.")
                    self.errcode = -1
                    return res
        except Exception as e:
            print(f"[PlayFab] [ERROR] Fetching <{track_id}>: {e}")
            if "401" in str(e) or "Unauthorized" in str(e) or "EntityTokenExpired" in str(e):
                self.pf_inited = False
            self.errcode = -1
            return res
        if pb_n_wr:
            data = self.fetch_pb_n_wr(track_id)
            if not data:
                return res
            self.errcode = 0
            res.update(data)
        if rank_page >= 0:
            if rank_page == 0:
                if 'pb_rank' not in res or not res['pb_rank']:
                    return res
                max_rank = res['pb_rank']
            else:
                max_rank = rank_page
            data = self.fetch_rank_page(track_id, max_rank)
            if not data:
                self.errcode = 0 if pb_n_wr else -1 
                return res
            self.errcode = 0
            res.update(data)
        return res

    def fetch_pb_n_wr(self, track_id: str) -> dict | None:
        pb_time, pb_rank, wr_time = 0, 0, 0
        try:
            # Personal best
            entry = self.playfab.get_my_time(track_id)
            if entry:
                pb_time = int(entry.get("Scores", [0])[0])
                pb_rank = entry.get("Rank", 0)
            # World record (top-1)
            entry_list = self.playfab.get_top(track_id, max_results = 1)
            if entry_list and entry_list[0]:
                wr_time = int(entry_list[0].get("Scores", [0])[0])
            print(f"[PlayFab] <{track_id}>  WR: {fmt_time(wr_time)}  PB: {fmt_time(pb_time)}  Rank: {pb_rank}")
        except Exception as e:
            print(f"[PlayFab] [ERROR] fetching <{track_id}>: {e}")
            if "401" in str(e) or "Unauthorized" in str(e) or "EntityTokenExpired" in str(e):
                self.pf_inited = False
            self.errcode = -1
            return None
        return { "pb_time": pb_time, "pb_rank": pb_rank, "wr_time": wr_time }
            
    def fetch_rank_page(self, track_id: str, max_rank: int) -> dict | None:
        rank_page = [ ]
        try:
            # Fetch rank page. Single GetLeaderboard call: start=max(1, pb_rank-98), page_size=100.
            # Stored as list of (rank, score_ms), rank ascending, score_ms lower=faster.
            if max_rank > 0:
                lb_name = get_lb_name_by_track_id(track_id)
                start = max(1, max_rank - 95)
                page, _ = self.playfab.client.get_leaderboard_page(lb_name, starting_position = start, page_size = 100)
                rank_page = [ (entry.get("Rank", 0), int(entry.get("Scores", [0])[0])) for entry in page ]
                time_range = fmt_time(rank_page[0][1]) + ' ... ' + fmt_time(rank_page[-1][1]) if rank_page else ""
                print(f"[PlayFab] Rank page: loaded {len(rank_page)} entries from rank {start} to {start+99}  [{time_range}]")
        except Exception as e:
            print(f"[PlayFab] [ERROR] Rank page for <{track_id}> fetch failed: {e}")
            self.errcode = -1
            return None
        return { "rank_page": rank_page }


class AdvInfoState:
    def __init__(self):
        self.pf_worker = PlayFabWorker()   # daemon thread, starts immediately
        self.req_track_id: str = ""        # track for which PB/WR was last requested
        self.req_playfab_time: float = 0.0
        self.reset()
        self.curdir = os.path.dirname(os.path.abspath(__file__))

    def reset(self) -> None:
        self.data = AdvInfoSnapshot()
        self.req_track_id = ""
        self.req_playfab_time = 0.0
        return self.data

    def get_data(self):
        return deepcopy(self.data)

    def run_playfab_request(self):
        data = self.data
        min_rank_num = 0 if not data.rank_dict else next(iter(data.rank_dict))
        if data.pb_time > 0 and min_rank_num == 1:
            return  # all data from PlayFab already received
        track_id = data.track_id
        if not track_id:
            return
        if self.pf_worker.state != PF_WRK_IDLE:
            return  # request already active
        now = time.monotonic()
        if now - self.req_playfab_time < 101.0:
            return  # wait before run next request
        self.req_playfab_time = now
        if data.pb_time <= 0:
            self.pf_worker.request_pb_wr(track_id, pb_n_wr = True, rank_page = 0)
        else:
            max_rank = data.pb_rank if not data.rank_dict else next(iter(data.rank_dict))
            self.pf_worker.request_pb_wr(track_id, pb_n_wr = False, rank_page = max_rank)

    def check_playfab_result(self) -> None:
        errcode, result = self.pf_worker.get_result()
        if errcode is None:
            return
        data = self.data
        if result and data.track_id == result['track_id']:
            self.req_track_id = result['track_id']
            if data.pb_time <= 0 and 'pb_time' in result:
                data.pb_time = result["pb_time"]
                data.pb_rank = result["pb_rank"]
            if data.wr_time <= 0 and 'wr_time' in result:
                data.wr_time = result["wr_time"]
            if 'rank_page' in result and result['rank_page']:
                rank_dict = dict((rank, { "score_ms": score_ms }) for rank, score_ms in result['rank_page'])
                data.rank_dict = dict(sorted( (data.rank_dict | rank_dict).items() ))
                #print(f'>>> rank_dict updated: ', next(iter(data.rank_dict)), '...', next(reversed(data.rank_dict)))
                data.pb_rank_new = 0  # needed update for rank

    def check_and_update_pb(self):
        data = self.data    
        if data.lap_time_best <= 0 or data.pb_time <= 0:
            return
        prev_pb_time_new = data.pb_time_new
        if data.lap_time_best < data.pb_time:
            data.pb_time_new = data.lap_time_best
            if data.rank_dict and data.pb_rank_new == 0:
                dict_first_rank = next(iter(data.rank_dict))
                dict_first_time = data.rank_dict[dict_first_rank]['score_ms']
                if data.pb_time_new <= dict_first_time:
                    data.pb_rank_new = 0  # unknown new rank
                else:
                    for rank, rank_val in data.rank_dict.items():
                        if rank_val['score_ms'] > data.pb_time_new:
                            data.pb_rank_new = rank
                            break
        if prev_pb_time_new != data.pb_time_new:
            now_local = datetime.now().astimezone()
            offset_seconds = now_local.utcoffset().total_seconds()
            offset_hours = int(offset_seconds / 3600)
            curtime = now_local.strftime('%Y-%m-%d %H:%M:%S') + f"{offset_hours:+03d}"
            rank_new = str(data.pb_rank_new) if data.pb_rank_new > 0 else "???"
            fn = self.curdir + '/my_pb.txt'
            prev_time = fmt_time(data.pb_time)
            prev_rank = f'({data.pb_rank})'
            new_time = fmt_time(data.pb_time_new)
            new_rank  = f'({rank_new})'
            line = f'{curtime}  {data.track_id:<14}  PREV_TIME: {prev_time} {prev_rank:<6}  NEW_TIME: {new_time} {new_rank:<6}  CAR: {data.car_name}'
            print(f'[WF2] <{data.track_id}>  NEW PB: {new_time}  RANK: {data.pb_rank} -> {rank_new}')
            try:
                with open(fn, 'a', encoding = 'utf-8') as file:
                    file.write(line + '\n')
            except Exception:
                pass

    def renew_from_main(self, pkt, traction_state: str = "") -> int:
        data = self.data
        hdr  = pkt.header
        eng  = pkt.carPlayer.engine
        inp  = pkt.carPlayer.input
        lb   = pkt.participantPlayerLeaderboard
        tm   = pkt.participantPlayerTiming
        tms  = pkt.participantPlayerTimingSectors
        inf  = pkt.participantPlayerInfo
        ses  = pkt.session
        tires = pkt.carPlayer.tires
        
        s_status = ses.status              # see enum SessionStatus
        g_status = hdr.statusFlags         # see enum GameStatusFlags
        p_status = pkt.playerStatusFlags   # see enum PlayerStatusFlags

        self.run_playfab_request()
        self.check_playfab_result()
        self.check_and_update_pb()

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
            data.car_name = inf.carName.decode("utf-8", errors="replace").strip("\x00")
            print('>>> race_inited')

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

        if not data.race_stopped and (data.race_inited or data.race_started) and after_race:
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
            data.lap_time_best = tm.lapTimeBest if tm.lapTimeBest > 1000 else 0
            data.lap_progress = tm.lapProgress

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

