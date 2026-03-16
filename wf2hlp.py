#!/usr/bin/env python3
"""
wfhlp2.py
Automatic gear shifting for Wreckfest 2 via keyboard simulation.

Keys sent to the game window:
  A  — upshift  (configurable)
  Z  — downshift (configurable)

Requirements:
  pip install pynput pyyaml
"""

import sys
import os
import glob
import json
import time
import threading
import queue
import ctypes
import ctypes.wintypes

from optparse import OptionParser

print(f"Python version: {sys.version}")

from pynput.keyboard import Controller, KeyCode

from wf2telemetry import *
from wf2overlay import create_overlays, LeaderboardState, AdvInfoState, TailDistState
from win64proc import Win64Process

import yaml

DEFAULT_CONFIG_PATH = "wf2hlp.yaml"

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)

# Gear transition keys: gXY means "from gear X to gear Y"
# Gears are 1-based (1=1st, 2=2nd, ...) matching dl.gear after our -1 offset
_UPSHIFT_KEYS   = ["g12", "g23", "g34", "g45", "g56", "g67", "g78"]
_DOWNSHIFT_KEYS = ["g21", "g32", "g43", "g54", "g65", "g76", "g87"]

def parse_gear_rules(rules_cfg: dict, gear_keys: list[str], default: float) -> list[float]:
    """
    Convert { g12: 0.95, g23: 0.93, ... } into a list indexed by from-gear (1-based).
    Index 0 unused, index 1 = threshold when shifting from gear 1, etc.
    Missing keys fall back to `default`.
    Returns list of length len(gear_keys)+2 so index = from_gear is always valid.
    """
    rpm_map = rules_cfg.get("rpm", { })
    # gear_keys like [ "g12","g23", ... ] — index i -> from_gear = i+1
    result = [default] * (len(gear_keys) + 2)
    for i, key in enumerate(gear_keys):
        result[i + 1] = rpm_map.get(key, default)
    return result


class ShifterConfig:
    """
    Resolved per-gear shift thresholds.
    upshift_thr[gear]   — rpm fraction of rpmRedline to shift UP   from `gear`
    downshift_thr[gear] — rpm fraction of rpmRedline to shift DOWN from `gear`

    Built from global auto_shifter section, then optionally overridden
    by a matching car_settings entry (exact lowercase carName match).
    """
    def __init__(self, cfg: dict, car_name: str = ""):
        base = cfg["auto_shifter"]

        self.cooldown_s = base["cooldown_ms"] / 1000.0
        self.key_hold_s = base["key_hold_ms"] / 1000.0

        up_rules   = base["up_shift_rules"]
        down_rules = base["down_shift_rules"]

        # Per-car override (exact lowercase carName match)
        override = base.get("car_settings", {}).get(car_name.lower(), {})
        if override:
            self.cooldown_s = override.get("cooldown_ms", self.cooldown_s * 1000) / 1000.0
            self.key_hold_s = override.get("key_hold_ms", self.key_hold_s * 1000) / 1000.0
            up_rules   = override.get("up_shift_rules",   up_rules)
            down_rules = override.get("down_shift_rules", down_rules)

        # Default fallback values (in case some gear keys are missing)
        up_def   = next(iter(up_rules  .get("rpm", { }).values()), 0.95)
        down_def = next(iter(down_rules.get("rpm", { }).values()), 0.40)

        self.upshift_thr   = parse_gear_rules(up_rules,   _UPSHIFT_KEYS,   up_def)
        self.downshift_thr = parse_gear_rules(down_rules, _DOWNSHIFT_KEYS, down_def)

        self.landing_settle_s = base.get("landing_settle_ms", 400) / 1000.0

        # air_downshift: dict { from_gear -> target_gear } for shifting in AIR
        # Config format:  airdownshift: { g4: 3, g5: 4 }
        # Meaning: if airborne in gear 4 → shift down to 3, in gear 5 → to 4
        air_ds_raw = base.get("airdownshift", {})
        if override:
            air_ds_raw = override.get("airdownshift", air_ds_raw)
        # Parse "g4" -> 4 as int key, value is target gear int
        self.air_downshift = {
            int(k[1:]): int(v)
            for k, v in air_ds_raw.items()
            if k.startswith("g") and k[1:].isdigit()
        }

        keys_cfg = cfg["keys"]
        self.key_upshift   = KeyCode.from_char(keys_cfg["up_shift"])
        self.key_downshift = KeyCode.from_char(keys_cfg["down_shift"])

    def describe(self) -> str:
        # Show first upshift threshold as representative value
        up_repr   = self.upshift_thr[1]
        down_repr = self.downshift_thr[1]
        return (
            f"upshift@{up_repr:.0%}  downshift@{down_repr:.0%}  "
            f"cooldown={self.cooldown_s*1000:.0f}ms  hold={self.key_hold_s*1000:.0f}ms  "
            f"keys={self.key_upshift.char}/{self.key_downshift.char}"
        )


# Tire indices
FL, FR, RL, RR = 0, 1, 2, 3
# SURFACE_TYPE_NOCONTACT from Pino header
SURFACE_NOCONTACT = 1

class SlippageResearcher:
    """
    Research tool: detects and logs moments of traction loss.

    Two distinct causes are tracked:
      AIR  — driven tire(s) left the ground:
                radiusUnloaded < 0  (detached / airborne per Pino docs)
             OR surfaceType == NOCONTACT
             OR loadVertical == 0
      SLIP — driven tire(s) on ground but spinning/locking:
                |slipRatio| > SLIP_RATIO_THRESHOLD
             AND rpm rising faster than RPM_RISE_THRESHOLD rpm/s

    Each event is printed once with a full snapshot so you can study
    patterns and later tune gear-shift logic accordingly.
    """

    SLIP_RATIO_THRESHOLD = 0.15   # |slipRatio| on driven wheel
    RPM_RISE_THRESHOLD   = 800    # rpm/s — guards against false SLIP positives
    PRINT_COOLDOWN_S     = 0.3    # min seconds between printed events

    # Driven wheels by driveline type (0=FWD 1=RWD 2=AWD)
    DRIVE_WHEELS = {
        0: (FL, FR),
        1: (RL, RR),
        2: (FL, FR, RL, RR),
    }
    TIRE_NAMES = ["FL", "FR", "RL", "RR"]

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.prev_rpm    = 0
        self.prev_time   = 0.0
        self.last_print  = 0.0
        self.event_count = 0

    def process(self, frame: PacketMain) -> None:
        eng   = frame.carPlayer.engine
        dl    = frame.carPlayer.driveline
        inp   = frame.carPlayer.input
        tires = frame.carPlayer.tires

        if not eng.running:
            return

        now = time.monotonic()
        dt = now - self.prev_time
        rpm_delta_per_s = (eng.rpm - self.prev_rpm) / dt if dt > 0 else 0.0
        self.prev_rpm = eng.rpm
        self.prev_time = now

        drive_wheels = self.DRIVE_WHEELS.get(dl.type, (FL, FR, RL, RR))

        # AIR: driven tire off ground
        air_wheels = [
            i for i in drive_wheels
            if tires[i].radiusUnloaded < 0.0
            or tires[i].surfaceType == SURFACE_NOCONTACT
            or tires[i].loadVertical == 0.0
        ]

        # SLIP: on ground but high slip ratio + rpm spike
        slip_wheels = [
            i for i in drive_wheels
            if i not in air_wheels
            and abs(tires[i].slipRatio) > self.SLIP_RATIO_THRESHOLD
        ]

        cause = None
        if air_wheels:
            cause = "AIR"
        elif slip_wheels and rpm_delta_per_s > self.RPM_RISE_THRESHOLD:
            cause = "SLIP"

        if cause is None:
            return
        if now - self.last_print < self.PRINT_COOLDOWN_S:
            return
        self.last_print = now
        self.event_count += 1

        self.print_event(cause, frame, tires, drive_wheels, air_wheels, slip_wheels, rpm_delta_per_s)

    def print_event(self, cause, frame, tires, drive_wheels, air_wheels, slip_wheels, rpm_delta_per_s) -> None:
        eng = frame.carPlayer.engine
        dl  = frame.carPlayer.driveline
        inp = frame.carPlayer.input
        n = self.TIRE_NAMES
        print(
            f"[SLIP#{self.event_count:04d}] cause={cause}  "
            f"gear={dl.gear}  spd={dl.speed_kmh:.1f}km/h  "
            f"rpm={eng.rpm}  Δrpm/s={rpm_delta_per_s:+.0f}  "
            f"thr={inp.throttle:.2f}  brk={inp.brake:.2f}\n"
            f"  driven={[n[i] for i in drive_wheels]}  "
            f"air={[n[i] for i in air_wheels]}  "
            f"slip={[n[i] for i in slip_wheels]}"
        )
        for i in drive_wheels:
            t = tires[i]
            print(
                f"  {n[i]}: slipRatio={t.slipRatio:+.3f}  "
                f"load={t.loadVertical:.0f}N  "
                f"surf={t.surfaceType}  "
                f"suspNorm={t.suspensionDispNorm:.2f}  "
                f"rps={t.rps:.1f}"
            )

class TractionState:
    NORMAL    = "NORMAL"
    AIR       = "AIR"       # driven wheels off ground
    SETTLING  = "SETTLING"  # just landed, waiting for physics to stabilise

class TractionMonitor:
    """
    Tracks whether the car is airborne and imposes a settle period after
    landing so RPM and speed reach stable values before shifting is allowed.

    Also detects brake-lockup (negative slipRatio under heavy braking) to
    suppress downshifts while wheels are sliding.

    Thresholds derived from observed telemetry:
      - AIR: loadVertical == 0 OR surfaceType == NOCONTACT on ALL driven wheels
        (single-wheel air events like #0100/#0102 are bumps, not full jumps)
      - BRAKE_LOCK: slipRatio < -LOCK_SLIP_THR with brk > LOCK_BRAKE_THR
    """

    LOCK_SLIP_THR  = 0.20   # |slipRatio| threshold for lockup
    LOCK_BRAKE_THR = 0.50   # brake input threshold

    # Driven wheels by driveline type (0=FWD 1=RWD 2=AWD)
    DRIVE_WHEELS = {
        0: (FL, FR),
        1: (RL, RR),
        2: (FL, FR, RL, RR),
    }

    def __init__(self, landing_settle_s: float = 0.4):
        self.landing_settle_s = landing_settle_s
        self.reset()

    def reset(self) -> None:
        self.state = TractionState.NORMAL
        self._land_time = 0.0
        self.brake_locked = False

    def update(self, frame: PacketMain) -> None:
        tires = frame.carPlayer.tires
        dl    = frame.carPlayer.driveline
        inp   = frame.carPlayer.input
        now   = time.monotonic()

        drive_wheels = self.DRIVE_WHEELS.get(dl.type, (FL, FR, RL, RR))

        # AIR: ALL driven wheels off ground simultaneously
        all_air = all(
            tires[i].loadVertical == 0.0 or tires[i].surfaceType == SURFACE_NOCONTACT
            for i in drive_wheels
        )

        # Brake lockup: heavy braking + wheels sliding backwards
        self.brake_locked = (
            inp.brake > self.LOCK_BRAKE_THR
            and any(tires[i].slipRatio < -self.LOCK_SLIP_THR for i in drive_wheels)
        )

        if self.state == TractionState.NORMAL:
            if all_air:
                self.state = TractionState.AIR

        elif self.state == TractionState.AIR:
            if not all_air:
                # Just landed — start settle timer
                self.state = TractionState.SETTLING
                self._land_time = now
                print(f"[TRACTION] Landed -> settling for {self.landing_settle_s*1000:.0f}ms")

        elif self.state == TractionState.SETTLING:
            if all_air:
                # Bounced back into air
                self.state = TractionState.AIR
            elif now - self._land_time >= self.landing_settle_s:
                self.state = TractionState.NORMAL

    @property
    def is_airborne(self) -> bool:
        return self.state == TractionState.AIR

    @property
    def shift_allowed(self) -> bool:
        """True when shifting is safe."""
        return self.state == TractionState.NORMAL

    @property
    def upshift_allowed(self) -> bool:
        return self.shift_allowed

    @property
    def downshift_allowed(self) -> bool:
        """Downshift also blocked during brake lockup."""
        return self.shift_allowed and not self.brake_locked


class KeyPresser:
    """Single background thread that executes key press/release actions."""

    def __init__(self, hold_s: float):
        self.hold_s = hold_s
        self.kbd    = Controller()
        self.queue  = queue.Queue()
        self.thread = threading.Thread(target = self.run, daemon = True)
        self.thread.start()

    def press(self, key: KeyCode) -> None:
        self.queue.put(key)

    def run(self) -> None:
        while True:
            key = self.queue.get()
            self.kbd.press(key)
            time.sleep(self.hold_s)
            self.kbd.release(key)


class AutoShifter:
    def __init__(self, scfg: ShifterConfig):
        self.scfg     = scfg
        self.presser  = KeyPresser(scfg.key_hold_s)
        self.traction = TractionMonitor(scfg.landing_settle_s)
        self.reset()

    def reset(self) -> None:
        self.last_shift   = 0.0
        self.pending_gear = None
        self.air_shifted = False
        self.traction.reset()

    def reconfigure(self, scfg: ShifterConfig) -> None:
        """Apply new per-car config. Resets shifting state."""
        self.scfg = scfg
        self.presser.hold_s = scfg.key_hold_s
        self.traction.landing_settle_s = scfg.landing_settle_s
        self.reset()
        print(f"[CFG] {scfg.describe()}")

    def process(self, frame: PacketMain) -> int:
        eng = frame.carPlayer.engine
        dl  = frame.carPlayer.driveline
        lb  = frame.participantPlayerLeaderboard

        if not eng.running:
            return 0
        
        if lb.status not in (PARTICIPANT_STATUS_RACING, PARTICIPANT_STATUS_FINISH_SUCCESS):
            return 0

        # Update traction state every frame regardless of other guards
        was_airborne = self.traction.is_airborne
        self.traction.update(frame)
        # Reset air-shift flag when we leave AIR state (landed or bounced)
        if was_airborne and not self.traction.is_airborne:
            self.air_shifted = False

        now = time.monotonic()
        gear = dl.gear
        gear_max = dl.gearMax

        # Wait for gear confirmation from telemetry after a shift
        if self.pending_gear is not None:
            if gear != self.pending_gear:
                if now - self.last_shift > 1.0:   # timeout — clear stale pending
                    self.pending_gear = None
                return 0
            else:
                self.pending_gear = None

        if now - self.last_shift < self.scfg.cooldown_s:
            return 0
        
        if eng.rpmRedline <= 0:
            return 0

        # ── Air downshift (pre-landing gear selection) ────────────────────────
        # Executed only while fully airborne. Shifts down step-by-step toward
        # the target gear defined in airdownshift config so that at most one
        # key press per cooldown is sent (pending_gear guards double-shifts).
        if self.traction.is_airborne and self.scfg.air_downshift and not self.air_shifted:
            target = self.scfg.air_downshift.get(gear)
            if target is not None and target < gear:
                self.air_shifted = True
                return self.shift(self.scfg.key_downshift, gear, -1, eng.rpm)

        if self.traction.upshift_allowed:
            if eng.rpm >= eng.rpmRedline * self.scfg.upshift_thr[gear] and gear >= 1 and gear < gear_max:
                return self.shift(self.scfg.key_upshift, gear, 1, eng.rpm)

        if self.traction.downshift_allowed:
            if eng.rpm <= eng.rpmRedline * self.scfg.downshift_thr[gear] and gear > 1:
                return self.shift(self.scfg.key_downshift, gear, -1, eng.rpm)

        return 0

    def shift(self, key: KeyCode, gear: int, gear_inc: int, rpm: int) -> None:
        self.pending_gear = gear + gear_inc
        label = 'UP' if gear_inc > 0 else 'DN'
        self.last_shift = time.monotonic()
        print(f"[SHIFT {label}]  gear: {gear} -> {self.pending_gear}  rpm={rpm}")
        self.presser.press(key)
        return 1


class StatsPrinter:
    def __init__(self, interval_s: float = 1.0):
        self.interval = interval_s
        self.last_time = 0.0

    def reset(self) -> None:
        self.last_time = 0.0

    def show_stat(self, frame: PacketMain, forced: bool = False) -> None:
        now = time.monotonic()
        if not forced:
            if self.interval is None:
                return
            if now - self.last_time < self.interval:
                return
        self.last_time = now
        dl  = frame.carPlayer.driveline
        eng = frame.carPlayer.engine
        gear_str = { -1: "R", 0: "N" }.get(dl.gear, str(dl.gear))
        print(
            f"spd={dl.speed_kmh:5.1f} km/h  "
            f"rpm={eng.rpm:4d}/{eng.rpmRedline:4d}  "
            f"gear={gear_str}/{dl.gearMax}  "
            f"torque={eng.torque:6.1f} N*m  "
            f"pkt/s={frame._pkt_main_stat.speed}"
        )


class SessionMonitor:
    """
    Tracks session and game status changes.
    - Prints one-time notice on pause / unpause.
    - Returns True from update() when a state reset is needed:
        POST_RACE — race finished, clean up
        PRE_RACE  — new race starting
    """

    RESET_STATUSES = {SESSION_STATUS_POST_RACE, SESSION_STATUS_PRE_RACE}

    def __init__(self):
        self.session_status = -1
        self.paused         = False
        self.paused_printed = False

    def reset(self) -> None:
        self.session_status = -1
        self.paused         = False
        self.paused_printed = False

    def update(self, frame: PacketMain) -> bool:
        """
        Returns True if all components should reset their state.
        Prints pause / unpause notices as a side effect.
        """
        paused = bool(frame.header.statusFlags & GAME_STATUS_PAUSED)
        session_status = frame.session.status

        # Pause state change → one-time print
        if paused != self.paused:
            self.paused = paused
            if paused:
                print("[WF2] ** PAUSED **")
            else:
                print("[WF2] Resumed")

        # Session boundary → request reset
        if session_status != self.session_status:
            prev = self.session_status
            self.session_status = session_status
            if session_status in self.RESET_STATUSES:
                label = "POST_RACE" if session_status == SESSION_STATUS_POST_RACE else "PRE_RACE"
                print(f"[WF2] Session -> {label}: resetting state")
                return True

        return False


class ActiveWindowChecker:
    """
    Checks whether Wreckfest2.exe is running AND its window is in the foreground.
    """
    EXE_NAME = "Wreckfest2.exe"

    def __init__(self, keyword: str = "Wreckfest", cache_s: float = 0.5):
        self.keyword = keyword
        self.cache_s = cache_s
        self.last_check  = 0.0
        self.last_result = False
        self.proc = Win64Process()

    def is_active(self) -> bool:
        now = time.monotonic()
        if now - self.last_check < self.cache_s:
            return self.last_result
        self.last_check  = now
        self.last_result = self.check()
        return self.last_result

    def check(self) -> bool:
        if not self.proc.is_alive():
            self.proc.close_process()
            if not self.proc.find_process(self.EXE_NAME):
                return False
        return self.proc.is_foreground()


def find_telemetry_configs() -> list:
    """
    Locate all config.json files under the standard WF2 documents path.
    Pattern: %USERPROFILE%/Documents/My Games/Wreckfest 2/<APP_ID>/savegame/telemetry/config.json
    """
    base = os.path.expandvars(r"%USERPROFILE%\Documents\My Games\Wreckfest 2")
    pattern = os.path.join(base, "*", "savegame", "telemetry", "config.json")
    return glob.glob(pattern)

def is_game_running() -> bool:
    """Return True if Wreckfest2.exe process is running."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq Wreckfest2.exe", "/NH"],
            text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW — no console flash
        )
        return "Wreckfest2.exe" in out
    except Exception:
        return False

def check_and_patch_telemetry_config(udp_port: int) -> bool:
    """
    Find game telemetry config.json, check that UDP is enabled and port matches.
    If not — prompt user to patch. Returns True if everything is OK to proceed.
    """
    configs = find_telemetry_configs()
    if not configs:
        print(
            "[CFG] Could not find Wreckfest 2 telemetry config.json\n"
            "      Expected location:\n"
            "      %USERPROFILE%\\Documents\\My Games\\Wreckfest 2\\<STEAM_APP_ID>\\savegame\\telemetry\\config.json\n"
            "      Please create or check it manually."
        )
        return True   # don't block startup — maybe user knows what they're doing
    ok = True
    for path in configs:
        print(f"[CFG] Checking {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[CFG] Cannot read config: {e}")
            continue
        udp_section = data.get("udp", [{}])[0]
        enabled     = int(udp_section.get("enabled", 0))
        cfg_port    = int(udp_section.get("port", 0))
        issues = [ ]
        if not enabled:
            issues.append(("enabled", 0, 1))
        if cfg_port != udp_port:
            issues.append(("port", cfg_port, udp_port))
        if not issues:
            print(f"[CFG] OK — UDP enabled, port {cfg_port}")
            continue
        # Describe what's wrong
        print(f"[CFG] Issues found:")
        for field, cur, want in issues:
            print(f"      {field}: current={cur!r}  expected={want!r}")
            pass
        answer = input("[CFG] Patch this file automatically? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            for field, _, want in issues:
                udp_section[field] = str(want) if field == "port" else want
                pass
            data["udp"][0] = udp_section
            try:
                with open(path, "w", encoding="utf-8") as file:
                    json.dump(data, file, indent=4)
                print(f"[CFG] Patched OK.")
            except Exception as e:
                print(f"[CFG] Failed to write: {e}")
                ok = False
                continue
            if is_game_running():
                print(
                    "[CFG] Wreckfest 2 is currently running.\n"
                    "      Please restart the game for telemetry changes to take effect,\n"
                    "      then run this script again."
                )
                ok = False
        else:
            print("[CFG] Skipped. Telemetry may not work correctly.")
            ok = False
    return ok


def main():
    op = OptionParser()
    op.add_option("-c", "--config", dest="config",  default=DEFAULT_CONFIG_PATH, help=f"Config YAML file (default: {DEFAULT_CONFIG_PATH})")
    op.add_option("-g", "--gearauto", dest="gearauto", action="store_true", default=False, help="AutoShifter")
    op.add_option("",   "--slippage", dest="slippage", action="store_true", default=False, help="Research traction loss events")
    op.add_option("-S", "--stat", dest="stat", action="store_true", default=False, help="Show stat info every second")
    op.add_option("-i", "--info", dest="info", action="store_true", default=False, help="Show overlay with info")
    op.add_option("-v", "--verbose", dest="verbose", action="store_true", default=False, help="Extra debug output")
    opt, _ = op.parse_args()

    cfg      = load_config(opt.config)
    scfg     = ShifterConfig(cfg)
    shifter  = AutoShifter(scfg)
    stats    = StatsPrinter(interval_s = 1.0 if opt.stat else None)
    monitor  = SessionMonitor()
    wnd_chk  = ActiveWindowChecker(keyword="Wreckfest", cache_s = 0.2)
    slip_res = SlippageResearcher() if opt.slippage else None

    lb_ov, adv_ov, tail_ov = create_overlays(cfg) if opt.info else (None, None, None)
    taildist_cfg = cfg.get("overlays", {}).get("taildist", {})
    radius_m     = float(taildist_cfg.get("max_view_radius", 250.0))
    lb_state     = LeaderboardState() if lb_ov  else None
    adv_state    = AdvInfoState()     if adv_ov else None
    tail_state   = TailDistState(radius_m) if tail_ov else None
    if tail_ov and tail_state and lb_state:
        tail_ov.attach(tail_state, lb_state)

    if slip_res:
        print(
            f"[WF2] Slippage research ON\n"
            f"      slip_ratio threshold : {SlippageResearcher.SLIP_RATIO_THRESHOLD}\n"
            f"      rpm rise threshold   : {SlippageResearcher.RPM_RISE_THRESHOLD} rpm/s\n"
            f"      print cooldown       : {SlippageResearcher.PRINT_COOLDOWN_S}s\n"
        )

    udp_port = cfg.get("udp_port", 23123)
    if not check_and_patch_telemetry_config(udp_port):
        print("[WF2] Fix telemetry config and restart. Exiting.")
        return

    receiver = WF2TelemetryReceiver(port=udp_port)
    print(f"[WF2] Auto-gear active  {scfg.describe()}")
    print(f"[WF2] Ctrl+C to quit\n")
    
    current_car = ""
    game_was_active = False
    race_was_active = False
    
    def check_game_active(frame):
        nonlocal game_was_active
        game_active = wnd_chk.is_active()
        if game_active != game_was_active:
            game_was_active = game_active
            if game_active:
                print("[WF2] Game window active +++++")
            else:
                print("[WF2] Game window inactive")
            for ov in (lb_ov, adv_ov, tail_ov):
                if ov:
                    ov.set_visible(game_active)
        return game_active
    
    def handle_main(frame):
        nonlocal current_car, game_was_active, race_was_active
        if monitor.update(frame):
            shifter.reset()
            stats.reset()
            if slip_res:
                slip_res.reset()
            if lb_state:
                lb_state.reset()
            #if adv_state:
            #    adv_state.reset()
            current_car = ""

        hdr = frame.header
        ses = frame.session
        ses_active = (ses.status == SESSION_STATUS_COUNTDOWN) or (ses.status == SESSION_STATUS_RACING)
        race_active = ses_active and (hdr.statusFlags & GAME_STATUS_IN_RACE) != 0 and not monitor.paused
        if race_active != race_was_active:
            race_was_active = race_active
            for ov in (lb_ov, adv_ov):
                if ov:
                    ov.set_race_active(race_active)

        if monitor.paused:
            return

        car_name = frame.participantPlayerInfo.carName.decode("utf-8", errors="replace").strip("\x00")
        if car_name != current_car:
            current_car = car_name
            shifter.reconfigure(ShifterConfig(cfg, car_name))
            print(f"[WF2] Car: {car_name}")

        game_active = check_game_active(frame)
        shifted = 0
        if game_active and opt.gearauto:
            shifted = shifter.process(frame)

        if slip_res:
            slip_res.process(frame)

        if lb_state:
            lb_state.update_main(frame)

        if adv_ov and adv_state:
            tr = shifter.traction.state if opt.gearauto else ""
            adv_state.renew_from_main(frame, traction_state = tr)
            adv_ov.push(adv_state.get_data())

        if tail_state:
            tail_state.update_main(frame)

        stats.show_stat(frame, forced = shifted > 0)

    try:
        if lb_ov or adv_ov:
            # Use recv_any() to receive all packet types
            while True:
                pkt_type, pkt = receiver.recv_any()
                if pkt is None:
                    check_game_active(None)
                    continue
                if pkt_type == MAIN_PACKET_TYPE:
                    handle_main(pkt)
                elif pkt_type == PARTICIPANTS_LEADERBOARD_PACKET_TYPE:
                    if lb_state and lb_ov:
                        lb_state.update_leaderboard(pkt)
                        lb_ov.push(lb_state.snapshot())
                elif pkt_type == PARTICIPANTS_TIMING_PACKET_TYPE:
                    if lb_state:
                        lb_state.update_timing(pkt)
                elif pkt_type == PARTICIPANTS_MOTION_PACKET_TYPE:
                    if tail_state is not None and pkt is not None:
                        tail_state.update_motion(pkt)
                elif pkt_type == PARTICIPANTS_INFO_PACKET_TYPE:
                    if lb_state:
                        lb_state.update_info(pkt)
        else:
            for frame in receiver:
                handle_main(frame)

    except KeyboardInterrupt:
        print("\n[WF2] Stopped.")
        if slip_res:
            print(f"[SLIP] Total events logged: {slip_res.event_count}")
    finally:
        receiver.close()


if __name__ == "__main__":
    main()

