import os
import sys

import ctypes
from ctypes.wintypes import *

from ctypes import POINTER
from ctypes import (c_byte, c_int, c_ulong, c_long, c_size_t, c_char, c_wchar, c_wchar_p, c_void_p, byref)

from typing import TypeAlias

intN: TypeAlias = int | None
strN: TypeAlias = str | None
listN: TypeAlias = dict | None
dictN: TypeAlias = dict | None

# ----------------------------------------------------------------------
# Windows memory type constants
# ----------------------------------------------------------------------

MEM_IMAGE   = 0x1000000
MEM_MAPPED  = 0x0040000
MEM_PRIVATE = 0x0020000

# Memory state constants
MEM_COMMIT  = 0x1000
MEM_FREE    = 0x10000
MEM_RESERVE = 0x2000

# Page protection modifier flags
PAGE_GUARD        = 0x100
PAGE_NOCACHE      = 0x200
PAGE_WRITECOMBINE = 0x400
PAGE_PROT_MASK    = 0xFF   # mask to strip modifier flags

# Page access constants (without modifier flags)
PAGE_NOACCESS          = 0x01
PAGE_READONLY          = 0x02
PAGE_READWRITE         = 0x04
PAGE_WRITECOPY         = 0x08
PAGE_EXECUTE           = 0x10
PAGE_EXECUTE_READ      = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80

# Process access rights
PROCESS_QUERY_INFORMATION  = 0x0400
PROCESS_QUERY_LIMITED_INFO = 0x1000
PROCESS_VM_READ            = 0x0010

# Toolhelp snapshot flags
TH32CS_SNAPPROCESS  = 0x00000002
TH32CS_SNAPMODULE   = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

INVALID_HANDLE_VALUE = c_void_p(-1).value
STILL_ACTIVE         = 259   # GetExitCodeProcess returns this while process is running


# ----------------------------------------------------------------------
# Windows structures
# ----------------------------------------------------------------------

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize",              DWORD),
        ("cntUsage",            DWORD),
        ("th32ProcessID",       DWORD),
        ("th32DefaultHeapID",   POINTER(c_ulong)),
        ("th32ModuleID",        DWORD),
        ("cntThreads",          DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase",      c_long),
        ("dwFlags",             DWORD),
        ("szExeFile",           c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize",        DWORD),
        ("th32ModuleID",  DWORD),
        ("th32ProcessID", DWORD),
        ("GlblcntUsage",  DWORD),
        ("ProccntUsage",  DWORD),
        ("modBaseAddr",   POINTER(c_byte)),
        ("modBaseSize",   DWORD),
        ("hModule",       HMODULE),
        ("szModule",      c_wchar * 256),
        ("szExePath",     c_wchar * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       c_void_p),
        ("AllocationBase",    c_void_p),
        ("AllocationProtect", DWORD),
        ("PartitionId",       WORD),
        ("RegionSize",        c_size_t),
        ("State",             DWORD),
        ("Protect",           DWORD),
        ("Type",              DWORD),
    ]


# ----------------------------------------------------------------------
# Win64Process
# ----------------------------------------------------------------------

class Win64Process:
    """Low-level Windows 64-bit process inspector using ctypes WinAPI."""

    def __init__(self):
        self.pid      : int | None  = None
        self.handle   : int | None  = None
        self.exe_name : str | None  = None
        self.exe_path : str | None  = None
        self.mem      : dict | None = None

        # WinAPI functions — populated by init_win_api()
        self.fn_CreateToolhelp32Snapshot = None
        self.fn_Process32FirstW          = None
        self.fn_Process32NextW           = None
        self.fn_Module32FirstW           = None
        self.fn_Module32NextW            = None
        self.fn_OpenProcess              = None
        self.fn_CloseHandle              = None
        self.fn_VirtualQueryEx           = None
        self.fn_ReadProcessMemory        = None
        self.fn_QueryFullProcessImageNameW = None
        self.fn_EnumWindows              = None
        self.fn_GetWindowThreadProcessId = None
        self.fn_GetWindowTextW           = None
        self.fn_GetWindowTextLengthW     = None
        self.fn_GetForegroundWindow      = None
        self.fn_GetExitCodeProcess       = None
        self.fn_IsWindowVisible          = None
        self.fn_GetWindowRect            = None

        Win64Process.init_win_api(self)

    # ------------------------------------------------------------------
    # WinAPI initialization
    # ------------------------------------------------------------------

    def init_win_api(self):
        """Initialize all required WinAPI functions via ctypes."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32   = ctypes.WinDLL("user32",   use_last_error=True)

        self.fn_CreateToolhelp32Snapshot        = kernel32.CreateToolhelp32Snapshot
        self.fn_CreateToolhelp32Snapshot.restype  = HANDLE
        self.fn_CreateToolhelp32Snapshot.argtypes = [
            DWORD,
            DWORD,
        ]

        self.fn_Process32FirstW        = kernel32.Process32FirstW
        self.fn_Process32FirstW.restype  = BOOL
        self.fn_Process32FirstW.argtypes = [
            HANDLE,
            POINTER(PROCESSENTRY32W),
        ]

        self.fn_Process32NextW        = kernel32.Process32NextW
        self.fn_Process32NextW.restype  = BOOL
        self.fn_Process32NextW.argtypes = [
            HANDLE,
            POINTER(PROCESSENTRY32W),
        ]

        self.fn_Module32FirstW        = kernel32.Module32FirstW
        self.fn_Module32FirstW.restype  = BOOL
        self.fn_Module32FirstW.argtypes = [
            HANDLE,
            POINTER(MODULEENTRY32W),
        ]

        self.fn_Module32NextW        = kernel32.Module32NextW
        self.fn_Module32NextW.restype  = BOOL
        self.fn_Module32NextW.argtypes = [
            HANDLE,
            POINTER(MODULEENTRY32W),
        ]

        self.fn_OpenProcess        = kernel32.OpenProcess
        self.fn_OpenProcess.restype  = HANDLE
        self.fn_OpenProcess.argtypes = [
            DWORD,
            BOOL,
            DWORD,
        ]

        self.fn_CloseHandle        = kernel32.CloseHandle
        self.fn_CloseHandle.restype  = BOOL
        self.fn_CloseHandle.argtypes = [HANDLE]

        self.fn_VirtualQueryEx        = kernel32.VirtualQueryEx
        self.fn_VirtualQueryEx.restype  = c_size_t
        self.fn_VirtualQueryEx.argtypes = [
            HANDLE,
            c_void_p,
            POINTER(MEMORY_BASIC_INFORMATION),
            c_size_t,
        ]

        self.fn_ReadProcessMemory        = kernel32.ReadProcessMemory
        self.fn_ReadProcessMemory.restype  = BOOL
        self.fn_ReadProcessMemory.argtypes = [
            HANDLE,
            c_void_p,
            c_void_p,
            c_size_t,
            POINTER(c_size_t),
        ]

        self.fn_QueryFullProcessImageNameW        = kernel32.QueryFullProcessImageNameW
        self.fn_QueryFullProcessImageNameW.restype  = BOOL
        self.fn_QueryFullProcessImageNameW.argtypes = [
            HANDLE,
            DWORD,
            c_wchar_p,
            POINTER(DWORD),
        ]

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            BOOL,
            HWND,
            LPARAM,
        )

        self.fn_EnumWindows        = user32.EnumWindows
        self.fn_EnumWindows.restype  = BOOL
        self.fn_EnumWindows.argtypes = [WNDENUMPROC, LPARAM]

        self.fn_GetWindowThreadProcessId        = user32.GetWindowThreadProcessId
        self.fn_GetWindowThreadProcessId.restype  = DWORD
        self.fn_GetWindowThreadProcessId.argtypes = [
            HWND,
            POINTER(DWORD),
        ]

        self.fn_GetWindowTextW        = user32.GetWindowTextW
        self.fn_GetWindowTextW.restype  = c_int
        self.fn_GetWindowTextW.argtypes = [
            HWND,
            c_wchar_p,
            c_int,
        ]

        self.fn_GetWindowTextLengthW        = user32.GetWindowTextLengthW
        self.fn_GetWindowTextLengthW.restype  = c_int
        self.fn_GetWindowTextLengthW.argtypes = [HWND]

        self.fn_GetForegroundWindow        = user32.GetForegroundWindow
        self.fn_GetForegroundWindow.restype  = HWND
        self.fn_GetForegroundWindow.argtypes = []

        self.fn_GetExitCodeProcess        = kernel32.GetExitCodeProcess
        self.fn_GetExitCodeProcess.restype  = BOOL
        self.fn_GetExitCodeProcess.argtypes = [HANDLE, POINTER(DWORD)]

        self.fn_IsWindowVisible        = user32.IsWindowVisible
        self.fn_IsWindowVisible.restype  = BOOL
        self.fn_IsWindowVisible.argtypes = [HWND]

        class RECT(ctypes.Structure):
            _fields_ = [ ("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long) ]
        self.RECT = RECT

        self.fn_GetWindowRect        = user32.GetWindowRect
        self.fn_GetWindowRect.restype  = BOOL
        self.fn_GetWindowRect.argtypes = [HWND, POINTER(RECT)]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def open_process(self, pid: int) -> int:
        """Open process handle with read + query rights."""
        access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFO | PROCESS_VM_READ
        handle = self.fn_OpenProcess(access, False, pid)
        if not handle:
            raise RuntimeError(f"OpenProcess failed for pid={pid}, error={ctypes.get_last_error()}")
        return handle

    def close_process(self):
        """Close current process handle."""
        if self.handle:
            self.fn_CloseHandle(self.handle)
            self.handle = None

    def is_alive(self) -> bool:
        """
        Return True if the process opened via find_process() is still running.
        Uses GetExitCodeProcess — returns STILL_ACTIVE (259) while running.
        Returns False if handle is not open or process has exited.
        """
        if not self.handle:
            return False
        exit_code = DWORD(0)
        ok = self.fn_GetExitCodeProcess(self.handle, byref(exit_code))
        return bool(ok) and exit_code.value == STILL_ACTIVE

    def is_foreground(self) -> bool:
        """
        Return True if the foreground window belongs to this process.
        Compares PID of the active window against self.pid via
        GetForegroundWindow + GetWindowThreadProcessId.
        Returns False if process is not found or not in foreground.
        """
        if not self.pid:
            return False
        hwnd = self.fn_GetForegroundWindow()
        if not hwnd:
            return False
        pid_out = DWORD(0)
        self.fn_GetWindowThreadProcessId(hwnd, byref(pid_out))
        return pid_out.value == self.pid

    def get_process_window_rect(self) -> tuple | None:
        """
        Find the first visible window belonging to self.pid and return
        (left, top, width, height) in screen pixels.
        Returns None if no visible window is found or process is not open.
        """
        if not self.pid:
            return None

        found = [None]   # list so the closure can assign to it
        target_pid = self.pid
        pid_buf = DWORD(0)

        WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)

        def enum_cb(hwnd, lparam):
            self.fn_GetWindowThreadProcessId(hwnd, byref(pid_buf))
            if pid_buf.value == target_pid:
                if self.fn_IsWindowVisible(hwnd):
                    found[0] = hwnd
                    return False   # stop enumeration
            return True

        self.fn_EnumWindows(WNDENUMPROC(enum_cb), 0)

        if found[0] is None:
            return None

        rect = self.RECT()
        if not self.fn_GetWindowRect(found[0], byref(rect)):
            return None
        w = rect.right  - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        return (rect.left, rect.top, w, h)

    def get_process_exe_path(self, pid: int) -> str | None:
        """Get full exe path for a given pid."""
        access = PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFO
        h = self.fn_OpenProcess(access, False, pid)
        if not h:
            return None
        try:
            buf  = ctypes.create_unicode_buffer(260)
            size = DWORD(260)
            ok   = self.fn_QueryFullProcessImageNameW(h, 0, buf, byref(size))
            return buf.value if ok else None
        finally:
            self.fn_CloseHandle(h)

    def addr_to_hex(self, addr: int) -> str:
        """Format address as 16-char uppercase hex string."""
        return f"{addr:016X}"

    # ------------------------------------------------------------------
    # Process discovery
    # ------------------------------------------------------------------

    def find_process(self, exe_name: str, exe_path_filter: str | None = None) -> bool:
        """
        Find a running process by exe filename.
        Optionally filter by full exe path substring (case-insensitive).
        Populates self.pid, self.exe_name, self.exe_path on success.
        Returns True if found.
        """
        exe_name_lower        = exe_name.lower()
        exe_path_filter_lower = exe_path_filter.lower() if exe_path_filter else None

        snapshot = self.fn_CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            raise RuntimeError(f"CreateToolhelp32Snapshot failed, error={ctypes.get_last_error()}")

        try:
            entry        = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)

            found = self.fn_Process32FirstW(snapshot, byref(entry))
            while found:
                if entry.szExeFile.lower() == exe_name_lower:
                    pid      = entry.th32ProcessID
                    exe_path = self.get_process_exe_path(pid)

                    if exe_path_filter_lower:
                        if exe_path and exe_path_filter_lower in exe_path.lower():
                            self.pid      = pid
                            self.exe_name = entry.szExeFile
                            self.exe_path = exe_path
                            self.handle   = self.open_process(pid)
                            return True
                    else:
                        self.pid      = pid
                        self.exe_name = entry.szExeFile
                        self.exe_path = exe_path
                        self.handle   = self.open_process(pid)
                        return True

                found = self.fn_Process32NextW(snapshot, byref(entry))
        finally:
            self.fn_CloseHandle(snapshot)

        return False

    def find_process_by_wnd(self, title_substring: str) -> bool:
        """
        Find a running process by window title substring (case-insensitive).
        Populates self.pid, self.exe_name, self.exe_path on success.
        Returns True if found.
        """
        title_lower = title_substring.lower()
        found_pid   = DWORD(0)

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            BOOL,
            HWND,
            LPARAM,
        )

        def enum_callback(hwnd, lparam):
            length = self.fn_GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            self.fn_GetWindowTextW(hwnd, buf, length + 1)
            if title_lower in buf.value.lower():
                pid = DWORD(0)
                self.fn_GetWindowThreadProcessId(hwnd, byref(pid))
                found_pid.value = pid.value
                return False  # stop enumeration
            return True

        callback = WNDENUMPROC(enum_callback)
        self.fn_EnumWindows(callback, 0)

        if found_pid.value == 0:
            return False

        exe_path = self.get_process_exe_path(found_pid.value)
        exe_name = os.path.basename(exe_path) if exe_path else None

        self.pid      = found_pid.value
        self.exe_name = exe_name
        self.exe_path = exe_path
        self.handle   = self.open_process(found_pid.value)
        return True

    # ------------------------------------------------------------------
    # Memory scanning
    # ------------------------------------------------------------------

    def scan_memory(self, types: intN = MEM_IMAGE | MEM_MAPPED, state: intN = MEM_COMMIT, access: intN = None, protect: intN = None) -> dict:
        """
        Scan all virtual memory regions of the process.

        Filters (bitmask OR — any matching bit passes):
          types   — MEM_IMAGE / MEM_MAPPED / MEM_PRIVATE  (None = no filter)
          state   — MEM_COMMIT / MEM_FREE / MEM_RESERVE   (None = no filter)
          access  — PAGE_READONLY / PAGE_READWRITE / ...  (None = no filter)
          protect — PAGE_GUARD / PAGE_NOCACHE / ...       (None = no filter)

        Populates and returns self.mem dict.
        """
        if not self.handle:
            raise RuntimeError("No open process handle. Call find_process() first.")

        result = {
            "main_module_addr":     None,
            "main_module_addr_hex": None,
            "modules":              { },
            "memory":               { },
        }

        # --- enumerate modules ---
        snap = self.fn_CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snap != INVALID_HANDLE_VALUE:
            me        = MODULEENTRY32W()
            me.dwSize = ctypes.sizeof(MODULEENTRY32W)
            ok = self.fn_Module32FirstW(snap, byref(me))
            while ok:
                base = ctypes.cast(me.modBaseAddr, c_void_p).value or 0
                result["modules"][base] = {
                    "addr":         base,
                    "addr_hex":     self.addr_to_hex(base),
                    "last_addr":    base + me.modBaseSize,
                    "last_addr_hex": self.addr_to_hex(base + me.modBaseSize),
                    "name":         me.szModule,
                    "path":         me.szExePath,
                }
                # first module in snapshot = main module (exe)
                if result["main_module_addr"] is None:
                    result["main_module_addr"] = base
                    result["main_module_addr_hex"] = self.addr_to_hex(base)
                ok = self.fn_Module32NextW(snap, byref(me))
            self.fn_CloseHandle(snap)

        # --- walk virtual address space ---
        addr = 0
        mbi  = MEMORY_BASIC_INFORMATION()
        mbi_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)

        while True:
            ret = self.fn_VirtualQueryEx(self.handle, c_void_p(addr), byref(mbi), mbi_size)
            if ret == 0:
                break

            region_addr = mbi.BaseAddress or 0
            region_size = mbi.RegionSize
            mem_type    = mbi.Type
            mem_state   = mbi.State
            mem_protect = mbi.Protect
            mem_access  = mem_protect & PAGE_PROT_MASK
            mem_mods    = mem_protect & ~PAGE_PROT_MASK

            # Apply filters
            type_ok    = (types   is None) or bool(mem_type   & types)
            state_ok   = (state   is None) or bool(mem_state  & state)
            access_ok  = (access  is None) or bool(mem_access & access)
            protect_ok = (protect is None) or bool(mem_mods   & protect)

            if type_ok and state_ok and access_ok and protect_ok:
                result["memory"][region_addr] = {
                    "addr":     region_addr,
                    "addr_hex": self.addr_to_hex(region_addr),
                    "type":     mem_type,
                    "state":    mem_state,
                    "protect":  mem_mods,
                    "access":   mem_access,
                    "size":     region_size,
                }

            next_addr = region_addr + region_size
            if next_addr <= addr:
                break
            addr = next_addr

        self.mem = result
        return result

    # ------------------------------------------------------------------
    # Memory reading
    # ------------------------------------------------------------------

    def read_mem_reg(self, addr: int, size: int | None = None) -> bytes | None:
        """
        Read a memory region into a bytes buffer and return it.
        If size is None, look up the region size from self.mem.
        Returns None on failure.
        """
        if not self.handle:
            raise RuntimeError("No open process handle.")

        if size is None:
            if self.mem is None:
                raise RuntimeError("self.mem is None. Call scan_memory() first.")
            reg = self.mem["memory"].get(addr)
            if reg is None:
                raise KeyError(f"Address 0x{addr:X} not found in self.mem")
            size = reg["size"]

        buf = ctypes.create_string_buffer(size)
        bytes_read = c_size_t(0)
        ok = self.fn_ReadProcessMemory(self.handle, c_void_p(addr), buf, size, byref(bytes_read))
        if not ok:
            return None
        return buf.raw[:bytes_read.value]

    # ------------------------------------------------------------------
    # Memory helpers
    # ------------------------------------------------------------------

    def get_main_module_addr(self) -> int:
        """Return the base address of the main module (exe)."""
        if self.mem is None:
            raise RuntimeError("self.mem is None. Call scan_memory() first.")
        return self.mem["main_module_addr"]

    def get_module_mem(self, addr: int) -> dict:
        """Return the module descriptor dict for the given base address."""
        if self.mem is None:
            raise RuntimeError("self.mem is None. Call scan_memory() first.")
        mod = self.mem["modules"].get(addr)
        if mod is None:
            raise KeyError(f"Module at 0x{addr:X} not found in self.mem")
        return mod

    def get_mem_regs(self, addr_beg: int, addr_end: intN = None, types: intN = MEM_IMAGE, state: intN = MEM_COMMIT, access: intN = PAGE_READONLY | PAGE_READWRITE, protect: intN = None) -> list[int]:
        """
        Return a sorted list of region addresses matching the given masks.

        addr_beg  — start address (module base or region address)
        addr_end  — end address (exclusive); if None, uses last_addr of the module that contains addr_beg
        types     — bitmask filter on Type    (None = no filter)
        state     — bitmask filter on State   (None = no filter)
        access    — bitmask filter on Access  (None = no filter)
        protect   — bitmask filter on Protect (None = no filter)
        """
        if self.mem is None:
            raise RuntimeError("self.mem is None. Call scan_memory() first.")

        # Resolve addr_end from module's last_addr if not provided
        if addr_end is None:
            for mod in self.mem["modules"].values():
                if mod["addr"] <= addr_beg < mod["last_addr"]:
                    addr_end = mod["last_addr"]
                    break
            if addr_end is None:
                # Fallback: use the region itself as the boundary
                reg = self.mem["memory"].get(addr_beg)
                if reg:
                    addr_end = addr_beg + reg["size"]
                else:
                    raise ValueError(f"Cannot determine addr_end for 0x{addr_beg:X}")

        matching = [ ]
        for addr, reg in self.mem["memory"].items():
            if addr < addr_beg or addr >= addr_end:
                continue
            type_ok    = (types   is None) or bool(reg["type"]    & types)
            state_ok   = (state   is None) or bool(reg["state"]   & state)
            access_ok  = (access  is None) or bool(reg["access"]  & access)
            protect_ok = (protect is None) or bool(reg["protect"] & protect)
            if type_ok and state_ok and access_ok and protect_ok:
                matching.append(addr)

        return sorted(matching)


# ----------------------------------------------------------------------
# CLI / self-test entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Win64Process self-test")
    parser.add_argument("exe_name", help="Process exe name to find (e.g. notepad.exe)")
    parser.add_argument("--path-filter", default=None, help="Optional exe path substring filter")
    parser.add_argument("--wnd-title",   default=None, help="Find process by window title substring instead")
    args = parser.parse_args()

    proc = Win64Process()

    # --- find process ---
    if args.wnd_title:
        print(f"Searching for process by window title containing: '{args.wnd_title}'...")
        found = proc.find_process_by_wnd(args.wnd_title)
    else:
        print(f"Searching for process: '{args.exe_name}' (path filter: {args.path_filter})...")
        found = proc.find_process(args.exe_name, args.path_filter)

    if not found:
        print("[ERROR] Process not found.")
        sys.exit(1)

    print(f"[OK] Found process:")
    print(f"     PID:  {proc.pid}")
    print(f"     Name: {proc.exe_name}")
    print(f"     Path: {proc.exe_path}")

    # --- scan memory ---
    print("\nScanning memory (MEM_IMAGE | MEM_MAPPED, MEM_COMMIT)...")
    mem = proc.scan_memory(types  = MEM_IMAGE | MEM_MAPPED, state = MEM_COMMIT)

    print(f"[OK] Main module addr: {mem['main_module_addr_hex']}")
    print(f"[OK] Modules found:    {len(mem['modules'])}")
    print(f"[OK] Regions found:    {len(mem['memory'])}")

    print("\nModules:")
    for mod in list(mem["modules"].values())[:5]:
        print(f"  {mod['addr_hex']}  {mod['name']}")
    if len(mem["modules"]) > 5:
        print(f"  ... and {len(mem['modules']) - 5} more")

    # --- get_mem_regs test ---
    main_addr = proc.get_main_module_addr()
    regs = proc.get_mem_regs(main_addr, None, MEM_IMAGE, MEM_COMMIT, PAGE_READONLY | PAGE_READWRITE)
    print(f"\nget_mem_regs (main module, IMAGE+COMMIT, R/RW): {len(regs)} regions")
    for r in regs[:5]:
        reg = mem["memory"][r]
        print(f"  {reg['addr_hex']}  size={reg['size']:#010x}  access={reg['access']:#04x}")

    # --- read_mem_reg test ---
    if regs:
        test_addr = regs[0]
        test_size = min(64, mem["memory"][test_addr]["size"])
        data = proc.read_mem_reg(test_addr, test_size)
        if data:
            print(f"\nread_mem_reg({mem['memory'][test_addr]['addr_hex']}, {test_size}):")
            print(f"  hex: {data.hex()}")
        else:
            print(f"\nread_mem_reg failed for {mem['memory'][test_addr]['addr_hex']}")

    proc.close_process()
    print("\n[DONE]")
