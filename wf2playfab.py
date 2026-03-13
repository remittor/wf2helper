import os
import sys
import time
import json
import urllib.request
import urllib.error

from wf2app import WF2App, decode_pf_entity_token, is_base64_str


# ----------------------------------------------------------------------
# Known WF2 track leaderboard names
# Discovered from game memory. Pattern: ce-track{NN}_{variant}-class_all
# ----------------------------------------------------------------------

WF2_KNOWN_TRACKS = [
    "ce-track01_1-class_all",
    "ce-track02_1-class_all",
    "ce-track02_2-class_all",
    "ce-track02_2_rev-class_all",
    "ce-track03_1-class_all",
    "ce-track03_1_rev-class_all",
    "ce-track04_1-class_all",
    "ce-track04_1_rev-class_all",
    "ce-track05_1-class_all",
    "ce-track05_1_rev-class_all",
    "ce-track06_1-class_all",
    "ce-track06_1_rev-class_all",
    "ce-track07_1-class_all",
    "ce-track07_1_rev-class_all",
]

# Cache file for discovered track names
WF2_TRACKS_CACHE_FILE = "wf2tracks.json"


# ----------------------------------------------------------------------
# PlayFab client
# ----------------------------------------------------------------------

class PlayFabClient:
    """Minimal PlayFab leaderboard client using Python stdlib only."""

    BASE_URL = "https://{title_id}.playfabapi.com"

    def __init__(self, title_id: str):
        self.title_id     = title_id
        self.base_url     = self.BASE_URL.format(title_id=title_id)
        self.entity_id    : str | None = None
        self.entity_token : str | None = None

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def get_headers(self):
        return {
            "Content-Type":  "application/json",
            "X-EntityToken": self.entity_token or "",
        }

    def post(self, path: str, payload: dict, max_retries: int = 10) -> dict:
        """
        Send POST request with JSON body, return parsed response dict.
        Automatically retries on HTTP 429 TooManyRequests, waiting
        retryAfterSeconds as specified by the server.
        """
        url  = self.base_url + path
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, body, self.get_headers(), method = 'POST')
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body_text = e.read().decode("utf-8", errors="replace")
                if e.code == 429 and attempt < max_retries:
                    wait = 10
                    try:
                        wait = int(json.loads(body_text).get("retryAfterSeconds", 10))
                    except Exception:
                        pass
                    print(f"[WAIT] Rate limited, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {e.code} from {path}: {body_text}") from e
            except urllib.error.URLError as e:
                raise RuntimeError(f"Network error calling {path}: {e.reason}") from e
        raise RuntimeError(f"Max retries exceeded for {path}")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def set_entity_token(self, entity_token: str, entity_id: str):
        self.entity_token = entity_token
        self.entity_id    = entity_id

    def require_auth(self):
        if not self.entity_token or not self.entity_id:
            raise RuntimeError("Not authenticated. Call set_entity_token() first.")

    # ------------------------------------------------------------------
    # Leaderboard API
    # ------------------------------------------------------------------

    def get_leaderboard(self, name: str, max_results: int = 100) -> list[dict]:
        """Fetch top N entries from a named leaderboard."""
        self.require_auth()
        payload = { "LeaderboardName": name, "PageSize": max(1, max_results) }
        resp = self.post("/Leaderboard/GetLeaderboard", payload)
        if resp.get("status") != "OK":
            raise RuntimeError(f"GetLeaderboard failed: {resp}")
        return resp["data"].get("Rankings", [ ])

    def get_leaderboard_around_entity(self, name: str, entity_id: str | None = None, surrounding: int = 0) -> list[dict]:
        """
        Fetch leaderboard entries centered on a specific entity.
        surrounding=0 returns only that entity's own entry.
        """
        self.require_auth()
        eid = entity_id or self.entity_id
        resp = self.post(
            "/Leaderboard/GetLeaderboardAroundEntity",
            {
                "LeaderboardName": name,
                "Entity": {
                    "Id":   eid,
                    "Type": "title_player_account",
                },
                "MaxSurroundingEntries": max(1, surrounding),
            },
        )
        if resp.get("status") != "OK":
            raise RuntimeError(f"GetLeaderboardAroundEntity failed: {resp}")
        return resp["data"].get("Rankings", [ ])

    def post_with_retry(self, path: str, payload: dict, max_retries: int = 5) -> dict:
        """
        Like post(), but handles HTTP 429 TooManyRequests by waiting
        retryAfterSeconds (from response body) and retrying up to max_retries times.
        """
        url  = self.base_url + path
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, body, self.get_headers(), method  = "POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body_text = e.read().decode("utf-8", errors="replace")
                if e.code == 429 and attempt < max_retries:
                    wait = 10  # fallback
                    try:
                        wait = int(json.loads(body_text).get("retryAfterSeconds", 10))
                    except Exception:
                        pass
                    print(f"[WAIT] Rate limited, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {e.code} from {path}: {body_text}") from e
            except urllib.error.URLError as e:
                raise RuntimeError(f"Network error calling {path}: {e.reason}") from e
        raise RuntimeError(f"Max retries exceeded for {path}")

    def probe_leaderboard(self, name: str) -> bool:
        """
        Check if a leaderboard exists.
        Uses GetLeaderboardAroundEntity with MaxSurroundingEntries=1 (minimum valid value).
        Existing leaderboard  -> HTTP 200, status OK.
        Non-existent          -> HTTP 404, errorCode 1567 (LeaderboardNotFound).
        HTTP 429              -> waits retryAfterSeconds and retries automatically.
        Returns True if the leaderboard exists, False otherwise.
        """
        self.require_auth()
        url  = self.base_url + "/Leaderboard/GetLeaderboardAroundEntity"
        body = json.dumps({
            "LeaderboardName": name,
            "Entity": {
                "Id":   self.entity_id,
                "Type": "title_player_account",
            },
            "MaxSurroundingEntries": 1,
        }).encode("utf-8")

        max_retries = 10
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, body, self.get_headers(), method  = "POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read()
                    return True  # HTTP 200 -> leaderboard exists
            except urllib.error.HTTPError as e:
                body_text = e.read().decode("utf-8", errors="replace")
                if e.code == 404:
                    return False  # LeaderboardNotFound
                if e.code == 429 and attempt < max_retries:
                    wait = 10
                    try:
                        wait = int(json.loads(body_text).get("retryAfterSeconds", 10))
                    except Exception:
                        pass
                    print(f"[WAIT] Rate limited on {name}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                # Other errors -> log and treat as exists to avoid false negatives
                print(f"[WARN] probe {name}: HTTP {e.code}: {body_text}")
                return True
            except urllib.error.URLError:
                return True  # network error -> assume exists
        return True  # max retries hit -> assume exists


# ----------------------------------------------------------------------
# WF2PlayFab
# ----------------------------------------------------------------------

class WF2PlayFab:
    """
    High-level helper combining WF2App (token extraction) and
    PlayFabClient (leaderboard API) for Wreckfest 2.
    """

    PF_TITLE_ID = "54936"

    def __init__(self, cache_dir: str | None = None):
        self.app       = WF2App(cache_dir=cache_dir)
        self.client    = PlayFabClient(self.PF_TITLE_ID)
        self.entity_id : str | None = None
        self.cache_dir = cache_dir or os.path.dirname(os.path.abspath(__file__))
        self.tracks_cache_path = os.path.join(self.cache_dir, WF2_TRACKS_CACHE_FILE)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def init_auth(self, attach_game: bool = True) -> bool:
        """
        Initialize PlayFab auth using EntityToken from wf2mem.json
        or live game memory.
        Returns True on success.
        """
        def verify(token: str) -> bool:
            return self.verify_token(token)

        token = self.app.get_entity_token(verify_fn = verify if attach_game else None)
        if not token:
            return False

        entity_id = self.extract_entity_id(token)
        if not entity_id:
            print("[ERROR] Could not extract EntityId from token.")
            return False

        self.entity_id = entity_id
        self.client.set_entity_token(token, entity_id)
        print(f"[OK] Auth initialized. EntityId: {entity_id}")
        return True

    def verify_token(self, token: str) -> bool:
        """
        Verify a token by making a lightweight PlayFab API call.
        Returns True if accepted.
        """
        old_token     = self.client.entity_token
        old_entity_id = self.client.entity_id

        entity_id = self.extract_entity_id(token)
        if not entity_id:
            return False

        self.client.set_entity_token(token, entity_id)
        try:
            self.client.get_leaderboard_around_entity(WF2_KNOWN_TRACKS[0], entity_id = entity_id, surrounding = 0)
            return True
        except RuntimeError as e:
            err = str(e)
            if "401" in err or "Unauthorized" in err or "EntityTokenExpired" in err:
                return False
            return True
        finally:
            if old_token:
                self.client.set_entity_token(old_token, old_entity_id)

    @staticmethod
    def extract_entity_id(token: str) -> str | None:
        """Extract EntityId (ei field) from a PlayFab EntityToken."""
        decoded = decode_pf_entity_token(token)
        if not decoded or len(decoded) < 2:
            return None
        payload = decoded[1]
        if isinstance(payload, dict):
            return payload.get("ei")
        return None

    # ------------------------------------------------------------------
    # Track list management
    # ------------------------------------------------------------------

    def load_track_names(self) -> list[str]:
        """
        Load track leaderboard names from wf2tracks.json cache.
        Falls back to WF2_KNOWN_TRACKS if cache does not exist.
        """
        if os.path.isfile(self.tracks_cache_path):
            try:
                with open(self.tracks_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                tracks = data.get("tracks", [ ])
                if tracks:
                    return tracks
            except Exception:
                pass
        return list(WF2_KNOWN_TRACKS)

    def save_track_names(self, tracks: list[str]):
        """Persist track names to wf2tracks.json."""
        with open(self.tracks_cache_path, "w", encoding="utf-8") as f:
            json.dump({"tracks": sorted(tracks)}, f, indent=4, ensure_ascii=True)
        print(f"[OK] Track list saved to {self.tracks_cache_path}")

    def probe_all_tracks(self, max_track_num: int = 15) -> list[str]:
        """
        Discover all track leaderboards by probing PlayFab.
        Tries all track numbers 01..max_track_num with common variants.
        Saves results to wf2tracks.json.
        Returns confirmed list of track names.
        """
        variants = ["1", "2", "3", "4", "5", "1_rev", "2_rev", "3_rev", "4_rev", "5_rev" ]
        confirmed = []
        total = 0
        print(f"[INFO] Probing tracks ce-track01..{max_track_num:02d} with {len(variants)} variants each...")
        for n in range(1, max_track_num + 1):
            for v in variants:
                name = f"ce-track{n:02d}_{v}-class_all"
                total += 1
                exists = self.client.probe_leaderboard(name)
                if exists:
                    confirmed.append(name)
                    print(f"  [FOUND] {name}")
            pass
        print(f"[INFO] Probed {total} names, found {len(confirmed)} leaderboards")
        self.save_track_names(confirmed)
        return confirmed

    # ------------------------------------------------------------------
    # Leaderboard queries
    # ------------------------------------------------------------------

    def get_my_time(self, leaderboard_name: str, output_file: str | None = None) -> dict | None:
        """Get current player's own entry from a leaderboard."""
        entries = self.client.get_leaderboard_around_entity(leaderboard_name, entity_id = self.entity_id, surrounding = 0)
        entry = entries[0] if entries else None
        if output_file and entry:
            save_json(entry, output_file)
        return entry

    def get_my_times_all_tracks(self, output_file: str | None = None) -> dict[str, dict]:
        """
        Fetch current player's best lap from every known track leaderboard.
        Returns dict: { leaderboard_name: entry_dict }
        """
        tracks = self.load_track_names()
        print(f"[INFO] Fetching times for {len(tracks)} track leaderboards...")

        results = { }
        for name in tracks:
            try:
                entries = self.client.get_leaderboard_around_entity(name, entity_id = self.entity_id, surrounding = 0)
                if entries:
                    results[name] = entries[0]
                    score_ms = entries[0].get("Scores", ["?"])[0]
                    rank     = entries[0].get("Rank", "?")
                    print(f"  {name:44s}  rank={rank:>5}  time={fmt_ms(score_ms)}")
                else:
                    print(f"  {name:44s}  (no entry)")
            except RuntimeError as e:
                err = str(e)
                if "LeaderboardNotFound" in err or '"errorCode":1001' in err:
                    pass  # Board does not exist, skip silently
                else:
                    print(f"  {name:44s}  ERROR: {e}")

        if output_file:
            save_json(results, output_file)

        return results

    def get_top(self, leaderboard_name: str, max_results: int = 100, output_file: str | None = None) -> list[dict]:
        """Fetch top N entries from a leaderboard."""
        entries = self.client.get_leaderboard(leaderboard_name, max_results)
        if output_file:
            save_json(entries, output_file)
        return entries


# ----------------------------------------------------------------------
# Formatting / IO helpers
# ----------------------------------------------------------------------

def fmt_ms(score) -> str:
    """Format milliseconds as mm:ss.mmm string."""
    try:
        ms     = int(score)
        mins   = ms // 60000
        secs   = (ms % 60000) // 1000
        millis = ms % 1000
        return f"{mins:02d}:{secs:02d}.{millis:03d}"
    except (ValueError, TypeError):
        return str(score)


def save_json(data, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f"[OK] Saved to {path}")


def print_entries(entries: list[dict]):
    print(f"  {'Rank':>6}  {'Time':>10}  {'DisplayName':<24}  EntityId")
    print("  " + "-" * 72)
    for e in entries:
        rank  = e.get("Rank", "?")
        score = fmt_ms(e.get("Scores", ["?"])[0])
        name  = (e.get("DisplayName") or "")[:24]
        eid   = e.get("Entity", {}).get("Id", "")
        print(f"  {rank:>6}  {score:>10}  {name:<24}  {eid}")


# ----------------------------------------------------------------------
# CLI commands
# ----------------------------------------------------------------------

def cmd_probe(args, wf2: WF2PlayFab):
    """Probe PlayFab to discover all existing track leaderboards."""
    confirmed = wf2.probe_all_tracks(max_track_num=args.max_track)
    print(f"\nConfirmed tracks ({len(confirmed)}):")
    for t in confirmed:
        print(f"  {t}")


def cmd_my_times(args, wf2: WF2PlayFab):
    """Fetch player's best lap on all known tracks."""
    results = wf2.get_my_times_all_tracks(output_file=args.output)
    print(f"\nTotal tracks with entries: {len(results)}")


def cmd_my_time(args, wf2: WF2PlayFab):
    """Fetch player's entry on a specific leaderboard."""
    entry = wf2.get_my_time(args.name, output_file=args.output)
    if entry:
        print(f"\nLeaderboard: {args.name}")
        print_entries([entry])
    else:
        print(f"[INFO] No entry found for leaderboard: {args.name}")


def cmd_top(args, wf2: WF2PlayFab):
    """Fetch top N entries from a leaderboard."""
    entries = wf2.get_top(args.name, max_results=args.top, output_file=args.output)
    print(f"\nLeaderboard: {args.name}  (top {len(entries)})")
    print_entries(entries)


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Wreckfest 2 PlayFab leaderboard client")
    parser.add_argument("--cache-dir", "-C", default = None, help = "Directory containing wf2mem.json (default: script directory)")
    parser.add_argument("--output", "-o", default = None, help = "Save results to JSON file")
    parser.add_argument("--no-attach", "-@", action = "store_true", help = "Do not attach to game process (use cached token only)")
    sub = parser.add_subparsers(dest="command", required=True)

    # probe
    p_probe = sub.add_parser("probe", help = "Probe PlayFab to discover all existing track leaderboards")
    p_probe.add_argument("--max-track", "-m", type = int, default = 15, help = "Max track number to probe (default: 15)")
    p_probe.set_defaults(func=cmd_probe)

    # my-times
    p_mt = sub.add_parser("my-times", help="Fetch your best lap on all tracks")
    p_mt.set_defaults(func=cmd_my_times)

    # my-time
    p_mto = sub.add_parser("my-time", help="Fetch your entry on a specific leaderboard")
    p_mto.add_argument("name", help="Leaderboard name")
    p_mto.set_defaults(func=cmd_my_time)

    # top
    p_top = sub.add_parser("top", help="Fetch top N entries from a leaderboard")
    p_top.add_argument("name", help="Leaderboard name")
    p_top.add_argument("--top", "-n", type = int, default = 100, help = "Number of entries (default: 100)")
    p_top.set_defaults(func=cmd_top)

    args = parser.parse_args()

    cache_dir = args.cache_dir or os.path.dirname(os.path.abspath(__file__))
    wf2 = WF2PlayFab(cache_dir = cache_dir)

    try:
        print("Initializing PlayFab auth...")
        ok = wf2.init_auth(attach_game = not args.no_attach)
        if not ok:
            print("[ERROR] Failed to initialize auth.")
            sys.exit(1)

        args.func(args, wf2)

    except RuntimeError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
