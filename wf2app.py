import os
import sys
import json
import struct
import base64
import winreg

from win64proc import *

# ----------------------------------------------------------------------
# WF2 / PlayFab constants
# ----------------------------------------------------------------------

WF2_STEAM_APP_ID  = 1203190
WF2_PF_TITLE_ID   = "54936"
WF2_EXE_NAME      = "Wreckfest2.exe"
WF2_WND_SUBSTRING = "Wreckfest 2 |"

# Pattern constants used by find_playfab_entity_token_addr
PF_PATTERN_U32_AT_0  = 0x00000010
PF_PATTERN_U32_AT_16 = 0xEA0EA059
PF_PATTERN_U32_AT_24 = 0x40000000
PF_MIN_HEAP_ADDR     = 0x200000000
PF_HEAP_ALIGN        = 16

# Offsets within the located PFInfo block
PF_INFO_HEAP_PTR_OFFSET   = 32
PF_INFO_HEAP_PTR_CONFIRM  = 56
PF_INFO_TOKEN_BASE_OFFSET = 64
PF_TOKEN_INDIRECT_OFFSET  = 16

# wf2mem.json file name
WF2MEM_FILE = "wf2mem.json"

# Prefix for main module address in expressions
MAIN_MODULE_PREFIX = "@"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def read_u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def is_valid_heap_ptr(value: int) -> bool:
    return value > PF_MIN_HEAP_ADDR and (value % PF_HEAP_ALIGN) == 0


def is_base64_str(s: str) -> bool:
    if not s or len(s) < 8:
        return False
    valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=|")
    return all(c in valid_chars for c in s)


def decode_pf_entity_token(token_b64: str) -> list | None:
    """
    Decode a PlayFab EntityToken from base64.
    The decoded content has format: "<ver>|<sig_b64>|<json_b64>"
    Returns [prefix_str, json_dict] or None on failure.
    """
    try:
        padding = "=" * ((4 - len(token_b64) % 4) % 4)
        raw = base64.b64decode(token_b64 + padding).decode("utf-8", errors="replace")
    except Exception:
        return None

    parts = raw.split("|", 2)
    if len(parts) < 3:
        return [ raw, { } ]

    prefix   = parts[0] + "|" + parts[1] + "|"
    json_str = parts[2]
    try:
        obj = json.loads(json_str)
        return [ prefix, obj ]
    except Exception:
        return [ prefix, json_str ]


def load_wf2mem(path: str) -> dict:
    """Load wf2mem.json or return an empty skeleton."""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            pass
    return { "mem_data": { }, "PFEntityToken": { } }


def save_wf2mem(path: str, data: dict):
    """Persist wf2mem.json with indent=4."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=True)


def build_token_addr_expr(pf_info_offset: int) -> str:
    """
    Build the mem_data address expression string for PFEntityToken.
    Example: "@+0x012B07C0+16"
    The pointer lives at pf_info_offset + PF_INFO_TOKEN_BASE_OFFSET inside
    the main module, and the token string is at *(ptr) + PF_TOKEN_INDIRECT_OFFSET.
    """
    ptr_offset = pf_info_offset + PF_INFO_TOKEN_BASE_OFFSET
    return f"{MAIN_MODULE_PREFIX}+{hex(ptr_offset)}+{PF_TOKEN_INDIRECT_OFFSET}"


def build_unknown_id_expr(pf_info_offset: int) -> str:
    """
    Build the mem_data address expression for PFUnknownId (0xEA0EA059 at +16).
    Example: "@+0x012B0790"
    """
    return f"{MAIN_MODULE_PREFIX}+{hex(pf_info_offset + 16)}"


def parse_addr_expr(expr: str, main_module_addr: int) -> tuple[int, int]:
    """
    Parse an address expression of the form "@+<hex>+<offset>" and return
    (pointer_address, post_dereference_offset).

    "@+0x012B07C0+16" -> (main_module_addr + 0x012B07C0, 16)
    "@+0x012B07C0"    -> (main_module_addr + 0x012B07C0, 0)
    """
    expr = expr.strip()
    expr = expr.replace(MAIN_MODULE_PREFIX, str(main_module_addr))

    tokens = [ ]
    current = ""
    for ch in expr:
        if ch == "+" and current:
            tokens.append(current)
            current = ""
        else:
            current += ch
    if current:
        tokens.append(current)

    values = [ ]
    for t in tokens:
        t = t.strip()
        if t:
            values.append(int(t, 0))

    if len(values) == 0:
        return 0, 0
    if len(values) == 1:
        return values[0], 0
    # Last value is the post-dereference offset
    ptr_addr = sum(values[:-1])
    post_offset = values[-1]
    return ptr_addr, post_offset


# ----------------------------------------------------------------------
# WF2Process
# ----------------------------------------------------------------------

class WF2Process(Win64Process):
    """Wreckfest 2 process accessor, extends Win64Process."""

    def __init__(self):
        super().__init__()
        # WF2Process-specific fields
        self.entity_token_addr : int | None = None
        self.pf_info_offset    : int | None = None
        # WF2-specific WinAPI extensions
        self.init_win_api()

    def init_win_api(self):
        """Initialize any WF2-specific WinAPI extensions (none required yet)."""
        pass

    # ------------------------------------------------------------------
    # Process discovery
    # ------------------------------------------------------------------

    def find_app(self, exe_path_filter: str | None = None) -> bool:
        """Find the running Wreckfest 2 process by exe name."""
        return self.find_process(WF2_EXE_NAME, exe_path_filter)

    def find_process_by_wnd(self) -> bool:
        """Find the Wreckfest 2 process by window title substring."""
        return super().find_process_by_wnd(WF2_WND_SUBSTRING)

    # ------------------------------------------------------------------
    # Memory refresh
    # ------------------------------------------------------------------

    def renew_info(self, exe_path_filter: str | None = None) -> bool:
        """
        Re-find the WF2 process and re-scan all memory regions.
        Returns True if process was found and memory was scanned.
        """
        self.close_process()
        self.mem               = None
        self.entity_token_addr = None
        self.pf_info_offset    = None

        found = self.find_app(exe_path_filter)
        if not found:
            found = self.find_process_by_wnd()
        if not found:
            return False

        self.scan_memory(types = MEM_IMAGE | MEM_MAPPED | MEM_PRIVATE, state = MEM_COMMIT)
        return True

    # ------------------------------------------------------------------
    # Heap region validation helper
    # ------------------------------------------------------------------

    def is_addr_in_private_rw_region(self, addr: int) -> bool:
        """
        Return True if addr falls inside a MEM_COMMIT / PAGE_READWRITE /
        MEM_PRIVATE memory region.
        """
        if self.mem is None:
            return False
        for reg_addr, reg in self.mem["memory"].items():
            if reg["state"] != MEM_COMMIT:
                continue
            if reg["type"]  != MEM_PRIVATE:
                continue
            if reg["access"] != PAGE_READWRITE:
                continue
            if reg_addr <= addr < reg_addr + reg["size"]:
                return True
        return False

    # ------------------------------------------------------------------
    # PFInfo block pattern scanner
    # ------------------------------------------------------------------

    def scan_region_for_pf_info(self, region_data: bytes) -> int | None:
        """
        Scan a memory region buffer for the PFInfo block pattern.
        Alignment: 4 bytes.
        Returns local byte offset of the match or None.

        Pattern checks (offsets relative to candidate start):
          +0   uint32 == PF_PATTERN_U32_AT_0  (0x00000010)
          +16  uint32 == PF_PATTERN_U32_AT_16 (0xEA0EA059)
          +24  uint32 == PF_PATTERN_U32_AT_24 (0x40000000)
          +32  uint64 -> valid heap ptr
          +56  uint64 == value at +32
          +64  uint64 -> valid heap ptr
          ptr at +32 / +56 must be in MEM_PRIVATE / RW / COMMIT region
          ptr at +64 must be in MEM_PRIVATE / RW / COMMIT region
        """
        size = len(region_data)
        # minimum block size to hold all fields: +64 + 8 = 72 bytes
        if size < 72:
            return None

        for offset in range(0, size - 72, 4):
            if read_u32(region_data, offset) != PF_PATTERN_U32_AT_0:
                continue
            if read_u32(region_data, offset + 16) != PF_PATTERN_U32_AT_16:
                continue
            if read_u32(region_data, offset + 24) != PF_PATTERN_U32_AT_24:
                continue

            ptr32 = read_u64(region_data, offset + PF_INFO_HEAP_PTR_OFFSET)
            if not is_valid_heap_ptr(ptr32):
                continue

            ptr56 = read_u64(region_data, offset + PF_INFO_HEAP_PTR_CONFIRM)
            if ptr56 != ptr32:
                continue

            ptr64 = read_u64(region_data, offset + PF_INFO_TOKEN_BASE_OFFSET)
            if not is_valid_heap_ptr(ptr64):
                continue

            if not self.is_addr_in_private_rw_region(ptr32):
                continue

            if not self.is_addr_in_private_rw_region(ptr64):
                continue

            return offset

        return None

    # ------------------------------------------------------------------
    # Two-level pointer dereference to EntityToken address
    # ------------------------------------------------------------------

    def resolve_entity_token_addr(self, pf_info_abs_addr: int) -> int | None:
        """
        Dereference one pointer level from a PFInfo block address,
        then add a constant offset:

        PFEntityToken_base_addr = pf_info_abs_addr + PF_INFO_TOKEN_BASE_OFFSET
        PFEntityToken_st1_addr  = *(uint64)(PFEntityToken_base_addr)
        PFEntityToken_addr      = PFEntityToken_st1_addr + PF_TOKEN_INDIRECT_OFFSET

        Returns the final EntityToken string address or None.
        """
        base_ptr_addr = pf_info_abs_addr + PF_INFO_TOKEN_BASE_OFFSET
        data1 = self.read_mem_reg(base_ptr_addr, 8)
        if not data1 or len(data1) < 8:
            return None
        
        st1_addr = read_u64(data1, 0)
        if not is_valid_heap_ptr(st1_addr):
            return None

        token_addr = st1_addr + PF_TOKEN_INDIRECT_OFFSET
        if not is_valid_heap_ptr(token_addr):
            return None

        return token_addr

    # ------------------------------------------------------------------
    # Main entry point: find EntityToken address
    # ------------------------------------------------------------------

    def find_playfab_entity_token_addr(self) -> int | None:
        """
        Scan main module memory regions for the PFInfo block pattern,
        then dereference to the EntityToken string address.

        Scans regions that are: MEM_IMAGE / MEM_COMMIT /
        (PAGE_EXECUTE_WRITECOPY or PAGE_READWRITE).

        Populates self.entity_token_addr and self.pf_info_offset on success.
        Returns the EntityToken address or None.
        """
        if self.mem is None:
            raise RuntimeError("self.mem is None. Call scan_memory() first.")

        main_addr = self.get_main_module_addr()
        main_mod  = self.get_module_mem(main_addr)
        main_last = main_mod["last_addr"]

        regions = self.get_mem_regs(main_addr, main_last, MEM_IMAGE, MEM_COMMIT, PAGE_EXECUTE_WRITECOPY | PAGE_READWRITE)

        for reg_addr in regions:
            reg  = self.mem["memory"][reg_addr]
            data = self.read_mem_reg(reg_addr, reg["size"])
            if data is None:
                continue

            local_offset = self.scan_region_for_pf_info(data)
            if local_offset is None:
                continue

            pf_info_abs = reg_addr + local_offset
            pf_info_rel = pf_info_abs - main_addr

            token_addr = self.resolve_entity_token_addr(pf_info_abs)
            if token_addr is None:
                continue

            token_data = self.read_mem_reg(token_addr, 1024)
            if not token_data:
                continue

            # Sanity: first byte should be printable ASCII alnum
            if not chr(token_data[0]).isalnum():
                continue

            self.pf_info_offset    = pf_info_rel
            self.entity_token_addr = token_addr
            return token_addr

        return None

    # ------------------------------------------------------------------
    # EntityToken string readers
    # ------------------------------------------------------------------

    def read_entity_token(self, max_len: int = 1024) -> str | None:
        """Read EntityToken null-terminated string from known address."""
        if self.entity_token_addr is None:
            return None
        data = self.read_mem_reg(self.entity_token_addr, max_len)
        if not data:
            return None
        end = data.find(b"\x00")
        if end < 0:
            end = max_len
        token = data[:end].decode("ascii", errors="replace")
        return token or None

    def read_entity_token_via_expr(self, addr_expr: str, main_module_addr: int, max_len: int = 1024) -> str | None:
        """
        Read EntityToken using a stored address expression.
        Expression format: "@+<hex_ptr_offset>+<post_offset>"

        Steps:
          1. ptr_addr   = main_module_addr + hex_ptr_offset
          2. st1_addr   = *(uint64)(ptr_addr)
          3. token_addr = st1_addr + post_offset   (arithmetic only, no dereference)
          4. Read null-terminated string from token_addr.
        """
        ptr_addr, post_offset = parse_addr_expr(addr_expr, main_module_addr)

        ptr_data = self.read_mem_reg(ptr_addr, 8)
        if not ptr_data or len(ptr_data) < 8:
            return None
        
        st1_addr = read_u64(ptr_data, 0)
        if not is_valid_heap_ptr(st1_addr):
            return None

        token_addr = st1_addr + post_offset
        if not is_valid_heap_ptr(token_addr):
            return None

        data = self.read_mem_reg(token_addr, max_len)
        if not data:
            return None
        end = data.find(b"\x00")
        if end < 0:
            end = max_len
        token = data[:end].decode("ascii", errors="replace")
        return token or None


# ----------------------------------------------------------------------
# WF2App
# ----------------------------------------------------------------------

class WF2App:
    """High-level Wreckfest 2 helper with wf2mem.json cache management."""

    STEAM_APP_ID = WF2_STEAM_APP_ID
    PF_TITLE_ID  = WF2_PF_TITLE_ID
    EXE_NAME     = WF2_EXE_NAME

    def __init__(self, cache_dir: str | None = None):
        self.proc      = WF2Process()
        self.game_dir  : str | None = None
        self.exe_path  : str | None = None

        work_dir        = cache_dir or os.path.dirname(os.path.abspath(__file__))
        self.cache_path = os.path.join(work_dir, WF2MEM_FILE)

        self.init_win_api()

    def init_win_api(self):
        """Initialize any WF2App-specific WinAPI functions (none required)."""
        pass

    # ------------------------------------------------------------------
    # Steam / game directory helpers
    # ------------------------------------------------------------------

    def find_steam_root(self) -> str | None:
        """Locate Steam installation directory via Windows registry."""
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Valve\Steam"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Valve\Steam"),
        ]
        for hive, path in keys:
            try:
                with winreg.OpenKey(hive, path) as key:
                    value, _ = winreg.QueryValueEx(key, "InstallPath")
                    if os.path.isdir(value):
                        return value
            except OSError:
                continue
        return None

    def get_steam_library_folders(self, steam_root: str) -> list[str]:
        """Return all Steam library folder paths (steamapps subdirs)."""
        folders = [os.path.join(steam_root, "steamapps")]
        vdf_path = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        if not os.path.isfile(vdf_path):
            return folders
        with open(vdf_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if '"path"' in line.lower():
                    parts = line.split('"')
                    if len(parts) >= 4:
                        path      = parts[-2].replace("\\\\", "\\")
                        candidate = os.path.join(path, "steamapps")
                        if os.path.isdir(candidate):
                            folders.append(candidate)
        return folders

    def find_game_directory(self) -> str | None:
        """Locate Wreckfest 2 installation directory via Steam manifests."""
        steam_root = self.find_steam_root()
        if not steam_root:
            return None
        for library in self.get_steam_library_folders(steam_root):
            manifest = os.path.join(library, f"appmanifest_{self.STEAM_APP_ID}.acf")
            if not os.path.isfile(manifest):
                continue
            with open(manifest, "r", encoding="utf-8") as f:
                for line in f:
                    if '"installdir"' in line.lower():
                        parts = line.split('"')
                        if len(parts) >= 4:
                            install_dir = parts[-2]
                            full_path   = os.path.join(library, "common", install_dir)
                            if os.path.isdir(full_path):
                                self.game_dir = full_path
                                return full_path
        return None

    def find_exe_name(self) -> str | None:
        """Return game exe filename, verifying it exists on disk."""
        game_dir = self.game_dir or self.find_game_directory()
        if not game_dir:
            return None
        candidate = os.path.join(game_dir, self.EXE_NAME)
        if os.path.isfile(candidate):
            self.exe_path = candidate
            return self.EXE_NAME
        return None

    def find_exe_path(self) -> str | None:
        """Return full path to the game exe."""
        if self.exe_path and os.path.isfile(self.exe_path):
            return self.exe_path
        self.find_exe_name()
        return self.exe_path

    # ------------------------------------------------------------------
    # Process attach
    # ------------------------------------------------------------------

    def attach(self) -> bool:
        """Attach to the running WF2 process and scan memory."""
        self.find_game_directory()
        return self.proc.renew_info(exe_path_filter=self.game_dir)

    # ------------------------------------------------------------------
    # wf2mem.json cache management
    # ------------------------------------------------------------------

    def load_cache(self) -> dict:
        return load_wf2mem(self.cache_path)

    def save_cache(self, data: dict):
        save_wf2mem(self.cache_path, data)

    def update_token_in_cache(self, token_b64: str):
        """Update only the PFEntityToken section in cache, preserve the rest."""
        cache   = self.load_cache()
        decoded = decode_pf_entity_token(token_b64)
        cache["PFEntityToken"] = {
            "base64":   token_b64,
            "decoded":  decoded if decoded is not None else [],
        }
        self.save_cache(cache)
        print(f"[OK] PFEntityToken updated in {self.cache_path}")

    def update_mem_data_in_cache(self, pf_info_offset: int):
        """Update only the mem_data section in cache, preserve the rest."""
        cache = self.load_cache()
        cache["mem_data"] = {
            "PFUnknownId":   build_unknown_id_expr(pf_info_offset),
            "PFEntityToken": build_token_addr_expr(pf_info_offset),
        }
        self.save_cache(cache)
        print(f"[OK] mem_data updated in {self.cache_path}")

    # ------------------------------------------------------------------
    # Token retrieval from memory
    # ------------------------------------------------------------------

    def read_fresh_token_from_memory(self) -> str | None:
        """
        Read the EntityToken from game memory.
        Runs full pattern scan if entity_token_addr is not yet known.
        On success updates wf2mem.json mem_data if a new offset was found.
        Returns token string or None.
        """
        if not self.proc.handle:
            raise RuntimeError("Not attached to WF2 process. Call attach() first.")

        if self.proc.entity_token_addr is None:
            print("[INFO] Running PFInfo pattern scan...")
            found_addr = self.proc.find_playfab_entity_token_addr()
            if found_addr is None:
                print("[WARN] PFInfo pattern not found in memory.")
                return None
            print(f"[OK] PFInfo offset:       {hex(self.proc.pf_info_offset)}")
            print(f"[OK] EntityToken address: {hex(found_addr)}")
            self.update_mem_data_in_cache(self.proc.pf_info_offset)

        return self.proc.read_entity_token()

    def read_token_via_cached_expr(self) -> str | None:
        """
        Read EntityToken using the address expression from wf2mem.json.
        Returns token string or None.
        """
        if not self.proc.handle:
            raise RuntimeError("Not attached to WF2 process.")
        cache = self.load_cache()
        token_expr = cache.get("mem_data", {}).get("PFEntityToken")
        if not token_expr:
            return None
        main_addr = self.proc.get_main_module_addr()
        return self.proc.read_entity_token_via_expr(token_expr, main_addr)

    # ------------------------------------------------------------------
    # High-level get_entity_token with full fallback strategy
    # ------------------------------------------------------------------

    def get_entity_token(self, verify_fn=None) -> str | None:
        """
        Return a valid PlayFab EntityToken using a multi-stage strategy.

        Stage 1: Return cached base64 token from wf2mem.json if verify_fn
                 accepts it (or if verify_fn is None).

        Stage 2: Read a fresh token from game memory using the cached
                 address expression. If it differs from the cached token,
                 update the cache and return it.

        Stage 3: Run the full PFInfo pattern scan to re-discover the offset,
                 update cache, and return the fresh token.

        verify_fn: optional callable(token: str) -> bool.
                   Should return True if the token is accepted by PlayFab.

        Returns the token string or None on complete failure.
        """
        cache      = self.load_cache()
        cached_b64 = cache.get("PFEntityToken", { }).get("base64", "")

        # Stage 1: try cached token
        if cached_b64 and is_base64_str(cached_b64):
            if verify_fn is None:
                print("[INFO] Returning cached EntityToken (no verification).")
                return cached_b64
            print("[INFO] Verifying cached EntityToken with PlayFab...")
            if verify_fn(cached_b64):
                print("[OK] Cached EntityToken is valid.")
                return cached_b64
            print("[WARN] Cached EntityToken rejected by PlayFab.")

        # Need live game access for stages 2 and 3
        if not self.proc.handle:
            print("[INFO] Attaching to WF2 process...")
            if not self.attach():
                print("[ERROR] WF2 process not found.")
                return None

        # Stage 2: read via cached address expression
        fresh_token = self.read_token_via_cached_expr()
        if fresh_token and is_base64_str(fresh_token):
            if fresh_token != cached_b64:
                print("[OK] Fresh EntityToken read via cached address expression.")
                self.update_token_in_cache(fresh_token)
                if verify_fn is None or verify_fn(fresh_token):
                    return fresh_token
                print("[WARN] Fresh token (via cached expr) rejected by PlayFab.")
            else:
                print("[WARN] Fresh token matches cached invalid token.")

        # Stage 3: full pattern re-scan
        print("[INFO] Running full PFInfo pattern re-scan...")
        self.proc.entity_token_addr = None
        self.proc.pf_info_offset    = None
        fresh_token = self.read_fresh_token_from_memory()
        if fresh_token and is_base64_str(fresh_token):
            self.update_token_in_cache(fresh_token)
            if verify_fn is None or verify_fn(fresh_token):
                return fresh_token
            print("[ERROR] Token found in memory but rejected by PlayFab.")
            return None

        print("[ERROR] Could not obtain a valid EntityToken.")
        return None


# ----------------------------------------------------------------------
# CLI / self-test entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("WF2App self-test")
    print("=" * 60)

    app = WF2App()

    # Test 1: locate game directory
    print("\n[TEST 1] Finding game directory...")
    game_dir = app.find_game_directory()
    if game_dir:
        print(f"[OK] Game directory: {game_dir}")
    else:
        print("[WARN] Game directory not found via Steam registry")

    # Test 2: locate exe
    print("\n[TEST 2] Finding game exe...")
    exe_name = app.find_exe_name()
    if exe_name:
        print(f"[OK] Exe name: {exe_name}")
        print(f"     Exe path: {app.exe_path}")
    else:
        print("[WARN] Exe not found")

    # Test 3: attach to running process
    print("\n[TEST 3] Attaching to WF2 process...")
    ok = app.attach()
    if not ok:
        print("[WARN] WF2 not running -- skipping memory tests")
        sys.exit(0)
    print(f"[OK] Attached: PID={app.proc.pid}  main={app.proc.mem['main_module_addr_hex']}")

    # Test 4: memory scan summary
    print("\n[TEST 4] Memory scan summary...")
    mem = app.proc.mem
    print(f"     Modules: {len(mem['modules'])}")
    print(f"     Regions: {len(mem['memory'])}")
    print("     Top modules:")
    for mod in list(mem["modules"].values())[:6]:
        print(f"       {mod['addr_hex']}  {mod['name']}")

    # Test 5: find EntityToken address via pattern scan
    print("\n[TEST 5] Searching for PlayFab EntityToken in memory...")
    token_addr = app.proc.find_playfab_entity_token_addr()
    if token_addr is None:
        print("[WARN] EntityToken address not found (game may not be logged in yet)")
    else:
        pf_offset = app.proc.pf_info_offset
        print(f"[OK] PFInfo offset in main module: {hex(pf_offset)}")
        print(f"[OK] EntityToken address:          {hex(token_addr)}")

        # Test 6: read EntityToken string
        print("\n[TEST 6] Reading EntityToken string...")
        token = app.proc.read_entity_token()
        if token:
            print(f"[OK] Token length: {len(token)}")
            print(f"     Token[:80]:  {token[:80]}...")

            decoded = decode_pf_entity_token(token)
            if decoded:
                print(f"[OK] Decoded prefix: {decoded[0][:60]}...")
                if isinstance(decoded[1], dict):
                    print(f"     EntityId (ei): {decoded[1].get('ei', '?')}")
                    print(f"     EntityType:    {decoded[1].get('et', '?')}")
                    print(f"     Expires:       {decoded[1].get('e',  '?')}")

            # Test 7: update cache
            print("\n[TEST 7] Updating wf2mem.json cache...")
            app.update_mem_data_in_cache(pf_offset)
            app.update_token_in_cache(token)
            print(f"[OK] Cache saved: {app.cache_path}")

            # Test 8: verify cache round-trip
            print("\n[TEST 8] Verifying cache round-trip...")
            loaded = app.load_cache()
            cached_token = loaded.get("PFEntityToken", {}).get("base64", "")
            status = "OK" if cached_token == token else "MISMATCH"
            print(f"[{status}] Round-trip check")

            # Test 9: read token via cached address expression
            print("\n[TEST 9] Reading token via cached address expression...")
            expr = loaded.get("mem_data", {}).get("PFEntityToken", "")
            print(f"     Expression: {expr}")
            main_addr = app.proc.get_main_module_addr()
            token2    = app.proc.read_entity_token_via_expr(expr, main_addr)
            if token2:
                status = "OK" if token2 == token else "MISMATCH"
                print(f"[{status}] Token via expr, length={len(token2)}")
            else:
                print("[WARN] Could not read token via expression")

        else:
            print("[WARN] read_entity_token returned None")

    # Test 10: high-level get_entity_token
    print("\n[TEST 10] WF2App.get_entity_token() (no PlayFab verification)...")
    app2 = WF2App()
    tok  = app2.get_entity_token(verify_fn=None)
    if tok:
        print(f"[OK] get_entity_token returned token, length={len(tok)}")
    else:
        print("[WARN] get_entity_token returned None")

    app.proc.close_process()
    print("\n[DONE]")
