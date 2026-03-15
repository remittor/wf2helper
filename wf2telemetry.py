#!/usr/bin/env python3
"""
wf2telemetry.py
Wreckfest 2 UDP telemetry parser (Pino format, Revision 5).
All packet structures are defined via ctypes.Structure with _pack_ = 1.
No struct.unpack_from needed — fields are accessed directly as attributes.
"""

import sys
import time
import socket
from ctypes import Structure
from ctypes import (c_char, c_float, c_uint8, c_uint16, c_uint32, c_int16, c_int32)
from ctypes import sizeof
from typing import Optional


SIGNATURE = b'pkro'

PARTICIPANTS_MAX       = 36
TRACK_ID_LENGTH_MAX    = 64
TRACK_NAME_LENGTH_MAX  = 96
CAR_ID_LENGTH_MAX      = 64
CAR_NAME_LENGTH_MAX    = 96
PLAYER_NAME_LENGTH_MAX = 24
DAMAGE_PARTS_MAX       = 56
DAMAGE_BITS_PER_PART   = 3
DAMAGE_BYTES_PER_PART  = (DAMAGE_PARTS_MAX * DAMAGE_BITS_PER_PART + 7) // 8  # = 21

MAIN_PACKET_TYPE = 0
PARTICIPANTS_LEADERBOARD_PACKET_TYPE    = 1
PARTICIPANTS_TIMING_PACKET_TYPE         = 2
PARTICIPANTS_INFO_PACKET_TYPE           = 5
PARTICIPANTS_TIMING_SECTORS_PACKET_TYPE = 3
PARTICIPANTS_MOTION_PACKET_TYPE         = 4
PARTICIPANTS_DAMAGE_PACKET_TYPE         = 6

SESSION_STATUS_NONE      = 0
SESSION_STATUS_PRE_RACE  = 1
SESSION_STATUS_COUNTDOWN = 2
SESSION_STATUS_RACING    = 3
SESSION_STATUS_ABANDONED = 4
SESSION_STATUS_POST_RACE = 5

GAME_STATUS_PAUSED       = (1 << 0)
GAME_STATUS_REPLAY       = (1 << 1)
GAME_STATUS_SPECTATE     = (1 << 2)
GAME_STATUS_MULTIPLAYER_CLIENT = (1 << 3)
GAME_STATUS_MULTIPLAYER_SERVER = (1 << 4)
GAME_STATUS_IN_RACE      = (1 << 5)

PARTICIPANT_STATUS_INVALID          = 0
PARTICIPANT_STATUS_UNUSED           = 1
PARTICIPANT_STATUS_RACING           = 2
PARTICIPANT_STATUS_FINISH_SUCCESS   = 3
PARTICIPANT_STATUS_DNF_DQ           = 5
PARTICIPANT_STATUS_DNF_RETIRED      = 6
PARTICIPANT_STATUS_DNF_TIMEOUT      = 7
PARTICIPANT_STATUS_DNF_WRECKED      = 8

PLAYER_STATUS_IN_RACE         = (1 << 0)   # Player's car is in race / on track
PLAYER_STATUS_CAR_DRIVABLE    = (1 << 1)   # Player's car is drivable
PLAYER_STATUS_PHYSICS_RUNNING = (1 << 2)   # In race AND not paused
PLAYER_STATUS_CONTROL_PLAYER  = (1 << 3)   # Player is driving player's car
PLAYER_STATUS_CONTROL_AI      = (1 << 4)   # AI is driving player's car


# --- Base: pack=1 mirrors #pragma pack(push, 1) 
class _BaseStruct(Structure):
    _pack_ = 1

class PinoHeader(_BaseStruct):
    _fields_ = [
        ("signature",   c_uint32),  # expect 1869769584 == b'pkro'
        ("packetType",  c_uint8),
        ("statusFlags", c_uint8),
        ("sessionTime", c_int32),   # ms, resets at countdown
        ("raceTime",    c_int32),   # ms, from lights out
    ]

class ParticipantLeaderboard(_BaseStruct):
    _fields_ = [
        ("status",      c_uint8),
        ("trackStatus", c_uint8),
        ("lapCurrent",  c_uint16),
        ("position",    c_uint8),
        ("health",      c_uint8),
        ("wrecks",      c_uint16),
        ("frags",       c_uint16),
        ("assists",     c_uint16),
        ("score",       c_int32),
        ("points",      c_int32),
        ("deltaLeader", c_int32),
        ("lapTiming",   c_uint16),
        ("reserved",    c_char * 6),
    ]

class ParticipantTiming(_BaseStruct):
    _fields_ = [
        ("lapTimeCurrent",        c_uint32),
        ("lapTimePenaltyCurrent", c_uint32),
        ("lapTimeLast",           c_uint32),
        ("lapTimeBest",           c_uint32),
        ("lapBest",               c_uint8),
        ("deltaAhead",            c_int32),
        ("deltaBehind",           c_int32),
        ("lapProgress",           c_float),
        ("reserved",              c_char * 3),
    ]

class ParticipantTimingSectors(_BaseStruct):
    _fields_ = [
        ("sectorTimeCurrentLap1", c_uint32),
        ("sectorTimeCurrentLap2", c_uint32),
        ("sectorTimeLastLap1",    c_uint32),
        ("sectorTimeLastLap2",    c_uint32),
        ("sectorTimeBestLap1",    c_uint32),
        ("sectorTimeBestLap2",    c_uint32),
        ("sectorTimeBest1",       c_uint32),
        ("sectorTimeBest2",       c_uint32),
        ("sectorTimeBest3",       c_uint32),
    ]

class ParticipantInfo(_BaseStruct):
    _fields_ = [
        ("carId",                     c_char * CAR_ID_LENGTH_MAX),
        ("carName",                   c_char * CAR_NAME_LENGTH_MAX),
        ("playerName",                c_char * PLAYER_NAME_LENGTH_MAX),
        ("participantIndex",          c_uint8),
        ("lastNormalTrackStatusTime", c_int32),
        ("lastCollisionTime",         c_int32),
        ("lastResetTime",             c_int32),
        ("reserved",                  c_char * 16),
    ]

class ParticipantMotion(_BaseStruct):
    _fields_ = [
        # Motion::Orientation
        ("positionX",         c_float),
        ("positionY",         c_float),
        ("positionZ",         c_float),
        ("orientationQuatX",  c_float),
        ("orientationQuatY",  c_float),
        ("orientationQuatZ",  c_float),
        ("orientationQuatW",  c_float),
        ("extentsX",          c_uint16),  # car half-width,  cm
        ("extentsY",          c_uint16),  # car half-height, cm
        ("extentsZ",          c_uint16),  # car half-length, cm
        # Motion::VelocityEssential
        ("velocityMagnitude", c_float),   # m/s
    ]

class ParticipantDamage(_BaseStruct):
    _fields_ = [
        ("damageStates", c_uint8 * DAMAGE_BYTES_PER_PART),
    ]

class CarAssists(_BaseStruct):
    _fields_ = [
        ("flags",         c_uint8),
        ("assistGearbox", c_uint8),
        ("levelAbs",      c_uint8),
        ("levelTcs",      c_uint8),
        ("levelEsc",      c_uint8),
        ("reserved",      c_char * 3),
    ]

class CarChassis(_BaseStruct):
    _fields_ = [
        ("trackWidth",              c_float * 2),
        ("wheelBase",               c_float),
        ("steeringWheelLockToLock", c_int32),
        ("steeringLock",            c_float),
        ("cornerWeights",           c_float * 4),
        ("reserved",                c_char * 16),
    ]

class CarDriveline(_BaseStruct):
    _fields_ = [
        ("type",     c_uint8),  # 0=FWD 1=RWD 2=AWD
        ("_gear",    c_uint8),  # 0=R 1=N 2=1st 3=2nd...
        ("gearMax",  c_uint8),  #         1=1st 2=2nd...
        ("speed",    c_float),  # m/s
        ("reserved", c_char * 17),
    ]
    @property
    def speed_kmh(self) -> float:
        return self.speed * 3.6
    @property
    def gear(self) -> int:
        return self._gear - 1


class CarEngine(_BaseStruct):
    _fields_ = [
        ("flags",            c_uint8),  # EngineFlags bitmask
        ("rpm",              c_int32),
        ("rpmMax",           c_int32),
        ("rpmRedline",       c_int32),
        ("rpmIdle",          c_int32),
        ("torque",           c_float),  # N*m
        ("power",            c_float),  # W
        ("tempBlock",        c_float),  # Kelvin
        ("tempWater",        c_float),  # Kelvin
        ("pressureManifold", c_float),  # kPa
        ("pressureOil",      c_float),  # kPa
        ("reserved",         c_char * 15),
    ]
    @property
    def running(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def misfiring(self) -> bool:
        return bool(self.flags & 0x04)


class CarInput(_BaseStruct):
    _fields_ = [
        ("throttle",  c_float),
        ("brake",     c_float),
        ("clutch",    c_float),
        ("handbrake", c_float),
        ("steering",  c_float),
    ]

class MotionOrientation(_BaseStruct):
    _fields_ = [
        ("positionX",        c_float),
        ("positionY",        c_float),
        ("positionZ",        c_float),
        ("orientationQuatX", c_float),
        ("orientationQuatY", c_float),
        ("orientationQuatZ", c_float),
        ("orientationQuatW", c_float),
        ("extentsX",         c_uint16),
        ("extentsY",         c_uint16),
        ("extentsZ",         c_uint16),
    ]

class MotionVelocity(_BaseStruct):
    _fields_ = [
        ("velocityLocalX",     c_float),
        ("velocityLocalY",     c_float),
        ("velocityLocalZ",     c_float),
        ("angularVelocityX",   c_float),
        ("angularVelocityY",   c_float),
        ("angularVelocityZ",   c_float),
        ("accelerationLocalX", c_float),
        ("accelerationLocalY", c_float),
        ("accelerationLocalZ", c_float),
    ]

class CarTire(_BaseStruct):
    _fields_ = [
        ("rps",                   c_float),
        ("camber",                c_float),
        ("slipRatio",             c_float),
        ("slipAngle",             c_float),
        ("radiusUnloaded",        c_float),
        ("loadVertical",          c_float),
        ("forceLat",              c_float),
        ("forceLong",             c_float),
        ("temperatureInner",      c_float),
        ("temperatureTread",      c_float),
        ("suspensionVelocity",    c_float),
        ("suspensionDisplacement",c_float),
        ("suspensionDispNorm",    c_float),
        ("positionVertical",      c_float),
        ("surfaceType",           c_uint8),
        ("reserved",              c_char * 15),
    ]

class InputExtended(_BaseStruct):
    _fields_ = [
        ("flags",    c_uint8),
        ("ffbForce", c_float),
        ("reserved", c_char * 15),
    ]

class CarFull(_BaseStruct):
    _fields_ = [
        ("assists",     CarAssists),
        ("chassis",     CarChassis),
        ("driveline",   CarDriveline),
        ("engine",      CarEngine),
        ("input",       CarInput),
        ("orientation", MotionOrientation),
        ("velocity",    MotionVelocity),
        ("tires",       CarTire * 4),
        ("reserved",    c_char * 14),
    ]

class SessionData(_BaseStruct):
    _fields_ = [
        ("trackId",           c_char * TRACK_ID_LENGTH_MAX),
        ("trackName",         c_char * TRACK_NAME_LENGTH_MAX),
        ("trackLength",       c_float),
        ("laps",              c_int16),
        ("eventLength",       c_int16),
        ("gridSize",          c_uint8),
        ("gridSizeRemaining", c_uint8),
        ("sectorCount",       c_uint8),
        ("sectorFract1",      c_float),
        ("sectorFract2",      c_float),
        ("gameMode",          c_uint8),
        ("damageMode",        c_uint8),
        ("status",            c_uint8),
        ("reserved",          c_char * 26),
    ]

class PacketMain(_BaseStruct):
    _fields_ = [
        ("header",                         PinoHeader),
        ("marshalFlagsPlayer",             c_uint16),
        ("participantPlayerLeaderboard",   ParticipantLeaderboard),
        ("participantPlayerTiming",        ParticipantTiming),
        ("participantPlayerTimingSectors", ParticipantTimingSectors),
        ("participantPlayerInfo",          ParticipantInfo),
        ("participantPlayerDamage",        ParticipantDamage),
        ("carPlayer",                      CarFull),
        ("session",                        SessionData),
        ("playerStatusFlags",              c_uint16),
        ("inputExtended",                  InputExtended),
        ("reserved",                       c_char * 106),
    ]

class PacketParticipantsLeaderboard(_BaseStruct):
    _fields_ = [
        ("header",                  PinoHeader),
        ("participantVisibility",   c_uint8),
        ("participantsLeaderboard", ParticipantLeaderboard * PARTICIPANTS_MAX),
        ("reserved",                c_char * 64),
    ]

class PacketParticipantsTiming(_BaseStruct):
    _fields_ = [
        ("header",                PinoHeader),
        ("participantVisibility", c_uint8),
        ("participantsTiming",    ParticipantTiming * PARTICIPANTS_MAX),
        ("reserved",              c_char * 64),
    ]

class PacketParticipantsTimingSectors(_BaseStruct):
    _fields_ = [
        ("header",                       PinoHeader),
        ("participantVisibility",        c_uint8),
        ("participantsTimingSectors",    ParticipantTimingSectors * PARTICIPANTS_MAX),
        ("reserved",                     c_char * 64),
    ]

class PacketParticipantsMotion(_BaseStruct):
    _fields_ = [
        ("header",                PinoHeader),
        ("participantVisibility", c_uint8),
        ("participantsMotion",    ParticipantMotion * PARTICIPANTS_MAX),
        # No reserved bytes in this packet per spec
    ]

class PacketParticipantsInfo(_BaseStruct):
    _fields_ = [
        ("header",                PinoHeader),
        ("participantVisibility", c_uint8),
        ("participantsInfo",      ParticipantInfo * PARTICIPANTS_MAX),
        ("reserved",              c_char * 512),
    ]


class WF2PktStat:
    def __init__(self):
        self.count = 0
        self.time = 0.0
        self.speed = 0
        
    def add_pkt(self, now = None):
        self.count += 1
        if now is None:
            now = time.monotonic()
        if now - self.time > 0.99:
            self.time = now
            self.speed = self.count
            self.count = 0


class WF2TelemetryReceiver:
    PKT_MAIN_SIZE = sizeof(PacketMain)

    def __init__(self, host: str = "0.0.0.0", port: int = 23123):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2*1024*1024)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2*1024*1024)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)
        print(f"[WF2] Listening on UDP {host}:{port}  (PacketMain = {self.PKT_MAIN_SIZE} bytes)")
        self.pkt_main_stat = WF2PktStat() 

    def close(self):
        self.sock.close()

    PKT_LEADERBOARD_SIZE     = sizeof(PacketParticipantsLeaderboard)
    PKT_TIMING_SIZE          = sizeof(PacketParticipantsTiming)
    PKT_TIMING_SECTORS_SIZE  = sizeof(PacketParticipantsTimingSectors)
    PKT_MOTION_SIZE          = sizeof(PacketParticipantsMotion)
    PKT_INFO_SIZE            = sizeof(PacketParticipantsInfo)

    def parse_main(self, data: bytes) -> Optional[PacketMain]:
        if not data.startswith(SIGNATURE):
            return None
        if len(data) < self.PKT_MAIN_SIZE:
            return None
        if data[4] != MAIN_PACKET_TYPE:   # packetType at byte offset 4
            return None
        self.pkt_main_stat.add_pkt()
        return PacketMain.from_buffer_copy(data[:self.PKT_MAIN_SIZE])

    def parse_leaderboard(self, data: bytes) -> Optional[PacketParticipantsLeaderboard]:
        if not data.startswith(SIGNATURE):
            return None
        if len(data) < self.PKT_LEADERBOARD_SIZE:
            return None
        if data[4] != PARTICIPANTS_LEADERBOARD_PACKET_TYPE:
            return None
        return PacketParticipantsLeaderboard.from_buffer_copy(data[:self.PKT_LEADERBOARD_SIZE])

    def parse_timing(self, data: bytes) -> Optional[PacketParticipantsTiming]:
        if not data.startswith(SIGNATURE):
            return None
        if len(data) < self.PKT_TIMING_SIZE:
            return None
        if data[4] != PARTICIPANTS_TIMING_PACKET_TYPE:
            return None
        return PacketParticipantsTiming.from_buffer_copy(data[:self.PKT_TIMING_SIZE])

    def parse_timing_sectors(self, data: bytes) -> Optional[PacketParticipantsTimingSectors]:
        if not data.startswith(SIGNATURE):
            return None
        if len(data) < self.PKT_TIMING_SECTORS_SIZE:
            return None
        if data[4] != PARTICIPANTS_TIMING_SECTORS_PACKET_TYPE:
            return None
        return PacketParticipantsTimingSectors.from_buffer_copy(data[:self.PKT_TIMING_SECTORS_SIZE])

    def parse_motion(self, data: bytes) -> Optional[PacketParticipantsMotion]:
        if not data.startswith(SIGNATURE):
            return None
        if len(data) < self.PKT_MOTION_SIZE:
            return None
        if data[4] != PARTICIPANTS_MOTION_PACKET_TYPE:
            return None
        return PacketParticipantsMotion.from_buffer_copy(data[:self.PKT_MOTION_SIZE])

    def parse_info(self, data: bytes) -> Optional[PacketParticipantsInfo]:
        if not data.startswith(SIGNATURE):
            return None
        if len(data) < self.PKT_INFO_SIZE:
            return None
        if data[4] != PARTICIPANTS_INFO_PACKET_TYPE:
            return None
        return PacketParticipantsInfo.from_buffer_copy(data[:self.PKT_INFO_SIZE])

    def recv_any(self):
        """
        Receive one UDP packet and return parsed result as
        (packet_type, packet_object) or (None, None) on timeout/unknown.
        """
        try:
            data, _ = self.sock.recvfrom(65535)
        except socket.timeout:
            return None, None
        if not data.startswith(SIGNATURE):
            return None, None
        pkt_type = data[4]
        if pkt_type == MAIN_PACKET_TYPE:
            pkt = self.parse_main(data)
            if pkt is not None:
                pkt._pkt_main_stat = self.pkt_main_stat
            return pkt_type, pkt
        if pkt_type == PARTICIPANTS_LEADERBOARD_PACKET_TYPE:
            return pkt_type, self.parse_leaderboard(data)
        if pkt_type == PARTICIPANTS_TIMING_PACKET_TYPE:
            return pkt_type, self.parse_timing(data)
        if pkt_type == PARTICIPANTS_TIMING_SECTORS_PACKET_TYPE:
            return pkt_type, self.parse_timing_sectors(data)
        if pkt_type == PARTICIPANTS_MOTION_PACKET_TYPE:
            return pkt_type, self.parse_motion(data)
        if pkt_type == PARTICIPANTS_INFO_PACKET_TYPE:
            return pkt_type, self.parse_info(data)
        return pkt_type, None

    def __iter__(self):
        return self

    def __next__(self) -> PacketMain:
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            frame = self.parse_main(data)
            if frame is not None:
                frame._pkt_main_stat = self.pkt_main_stat
                return frame


