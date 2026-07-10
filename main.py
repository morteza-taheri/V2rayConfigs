import os
import sys
import json
import time
import asyncio
import aiohttp
import re
import signal
import urllib.parse
import base64
import zipfile
import io
import subprocess
from datetime import datetime
from urllib.parse import unquote
from aiohttp_socks import ProxyConnector
from iso3166 import country_name
# ── 5. App Version ─────────────────────────────────────────────────────────────
APP_VERSION = "4.2.0 (Smart Distribution Edition)"

# ── File & Directory Names ─────────────────────────────────────────────────────
# Absolute paths anchored to this script's own location — so the app works
# correctly no matter where it's launched from (Task Scheduler, a .bat file,
# a different working directory, etc.), not just when run from inside its folder.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

XRAY_DIR      = os.path.join(BASE_DIR, "xray_core")
SETTINGS_FILE = os.path.join(BASE_DIR, "Setting.txt")
SOURCE_FILE   = os.path.join(BASE_DIR, "Sources.txt")
IP_CACHE_FILE = os.path.join(BASE_DIR, "ip_cache.json")
OUTPUT_FILE   = os.path.join(BASE_DIR, "Morteza_Taheri.txt")
STATS_FILE    = os.path.join(BASE_DIR, "sources_stats.json")

# ── 10. Default Settings ───────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "MAX_CONFIGS": 30,
    "MAX_TESTCOUNT": 300,
    "MAX_WORKERS": 14,
    "TEST_TIMEOUT": 10,
    "GEOIP_TIMEOUT": 5,
    "XRAY_STARTUP_TIMEOUT": 3.0,
    "HTTP_TEST_URL": "http://www.gstatic.com/generate_204",
    "GITHUB_LATEST_API": "https://api.github.com/repos/XTLS/Xray-core/releases/latest",
    "GITHUB_DOWNLOAD_BASE": "https://github.com/XTLS/Xray-core/releases/download",
    "XRAY_CURRENT_VERSION": ""
}

# ── 14. Windows Asyncio Policy ─────────────────────────────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "tuic://", "ss://", "ssr://")


def print_log(msg: str):
    print(f"[INFO] {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# Settings / Cache helpers
# ══════════════════════════════════════════════════════════════════════════════
def load_or_create_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=4)
        print_log(f"Created {SETTINGS_FILE} with default values.")
        return dict(DEFAULT_SETTINGS)
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        try:
            s = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                if k not in s:
                    s[k] = v
            return s
        except json.JSONDecodeError:
            print_log(f"Error parsing {SETTINGS_FILE}. Using defaults.")
            return dict(DEFAULT_SETTINGS)


def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=4)


def load_ip_cache() -> dict:
    if os.path.exists(IP_CACHE_FILE):
        try:
            with open(IP_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_ip_cache(cache: dict):
    with open(IP_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=4)


# ══════════════════════════════════════════════════════════════════════════════
# Source Stats  (sources_stats.json)
#
# Format: { url: [ {last_run, total_configs, valid_configs,
#                    score_percent, status}, ... up to MAX_STAT_HISTORY ] }
# History is ordered oldest → newest. Oldest entries auto-dropped past the cap.
# ══════════════════════════════════════════════════════════════════════════════
MAX_STAT_HISTORY = 5


def load_stats() -> dict:
    if not os.path.exists(STATS_FILE):
        return {}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    # Migrate legacy format (single dict per URL) → list-of-history format
    migrated: dict = {}
    for url, val in raw.items():
        if isinstance(val, list):
            migrated[url] = val[-MAX_STAT_HISTORY:]
        elif isinstance(val, dict):
            migrated[url] = [val]
    return migrated


def save_stats(stats: dict):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=4, ensure_ascii=False)


def update_source_stat(stats: dict, url: str, total: int, valid: int, reachable: bool):
    """
    Append a new record to this URL's history, then trim to the
    MAX_STAT_HISTORY most recent entries (oldest dropped automatically).
    """
    score = round((valid / total * 100)) if total > 0 else 0
    record = {
        "last_run":      datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_configs": total,
        "valid_configs": valid,
        "score_percent": score,
        "status":        "OK" if reachable and total > 0 else "NotOK",
    }
    history = stats.get(url, [])
    history.append(record)
    stats[url] = history[-MAX_STAT_HISTORY:]   # keep only the newest N


def _latest_record(stats: dict, url: str) -> dict | None:
    history = stats.get(url)
    return history[-1] if history else None


def _score_key(url: str, stats: dict) -> tuple:
    """Sort key: NotOK last, then by latest score descending, new URLs = middle."""
    rec = _latest_record(stats, url)
    if rec is None:
        return (0, -50)
    if rec["status"] == "NotOK":
        return (1, 0)
    return (0, -rec["score_percent"])


def _format_stat_comment(rec: dict) -> str:
    return (
        f"# [Score: {rec['score_percent']}%"
        f" | Total: {rec['total_configs']}"
        f" | Valid: {rec['valid_configs']}"
        f" | Status: {rec['status']}"
        f" | Last: {rec['last_run']}]\n"
    )


def rewrite_sources_with_comments(stats: dict):
    """
    Rewrite Sources.txt:
      1. Strip out ALL previously auto-generated "# [Score:...]" lines
         (wherever they are — avoids any stale/duplicate accumulation).
      2. Re-insert up to MAX_STAT_HISTORY fresh history lines directly
         above each URL that has stats (oldest → newest order).
      3. Re-order URL blocks by latest score (best first, NotOK last).
      4. Manual (non-auto) comments and blank lines stay attached to
         whichever URL block they were written above.

    Atomic write (tmp → rename) — a crash never corrupts the file.
    """
    if not os.path.exists(SOURCE_FILE):
        return

    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        original_lines = f.readlines()

    # Step 1 — drop every auto-generated stat comment, wherever it is.
    cleaned = [ln for ln in original_lines if not ln.strip().startswith("# [Score:")]

    # Step 2 — walk cleaned lines, group into header + url_blocks.
    header_lines: list[str] = []
    url_blocks: list[tuple[str, list[str], str]] = []   # (url, manual_lines, url_line)
    pending: list[str] = []
    found_first_url = False

    for raw in cleaned:
        stripped = raw.strip()
        is_url = bool(stripped) and not stripped.startswith("#")

        if is_url:
            found_first_url = True
            url_blocks.append((stripped, pending, raw))
            pending = []
        else:
            if found_first_url:
                pending.append(raw)
            else:
                header_lines.append(raw)

    trailing = pending   # any comments/blanks after the very last URL

    # Step 3 — sort url_blocks by latest score
    url_blocks.sort(key=lambda b: _score_key(b[0], stats))

    # Step 4 — reassemble with fresh history comments
    new_lines: list[str] = list(header_lines)
    for url, manual_lines, url_line in url_blocks:
        history = stats.get(url)
        if history:
            for rec in history[-MAX_STAT_HISTORY:]:
                new_lines.append(_format_stat_comment(rec))
        new_lines.extend(manual_lines)
        new_lines.append(url_line)
    new_lines.extend(trailing)

    # Atomic write
    tmp = SOURCE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.replace(tmp, SOURCE_FILE)
    except Exception as e:
        print_log(f"Warning: could not update {SOURCE_FILE}: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass


def get_sorted_source_links(raw_links: list[str], stats: dict) -> list[str]:
    """Sort active source links using the same key as the file rewriter."""
    return sorted(raw_links, key=lambda u: _score_key(u, stats))



# ══════════════════════════════════════════════════════════════════════════════
# Graceful stop — Ctrl+C saves partial results
# ══════════════════════════════════════════════════════════════════════════════
class GracefulStop:
    """
    Listens for Ctrl+C (SIGINT).
    On first press  → sets stop_requested, workers finish current test then stop.
    On second press → hard exit.
    """
    def __init__(self):
        self.stop_requested = False
        self._count         = 0
        self._global_stop:  asyncio.Event | None = None

    def set_global_event(self, ev: asyncio.Event):
        self._global_stop = ev

    def trigger(self):
        self._count += 1
        if self._count == 1:
            self.stop_requested = True
            print_log("─" * 60)
            print_log("⚠  Stop requested by user — finishing current tests...")
            print_log("   Press Ctrl+C again to force-quit immediately.")
            print_log("─" * 60)
            if self._global_stop:
                self._global_stop.set()
        else:
            print_log("Force-quit.")
            os._exit(1)


_graceful = GracefulStop()


def _sigint_handler(sig, frame):
    _graceful.trigger()


signal.signal(signal.SIGINT, _sigint_handler)


# ══════════════════════════════════════════════════════════════════════════════
# Xray update
# ══════════════════════════════════════════════════════════════════════════════
def safe_extract(zf: zipfile.ZipFile, target_dir: str):
    abs_target = os.path.abspath(target_dir)
    for member in zf.namelist():
        mp = os.path.abspath(os.path.join(target_dir, member))
        if not mp.startswith(abs_target + os.sep) and mp != abs_target:
            raise Exception(f"Unsafe zip path: {member}")
    zf.extractall(target_dir)


async def check_and_update_xray(settings: dict):
    if not os.path.exists(XRAY_DIR):
        os.makedirs(XRAY_DIR)
    xray_exe = os.path.join(XRAY_DIR, "xray.exe")
    print_log("Checking for Xray-core updates...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                settings["GITHUB_LATEST_API"],
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    print_log("Could not reach GitHub API.")
                    return
                data   = await resp.json()
                latest = data.get("tag_name", "")
                if latest != settings.get("XRAY_CURRENT_VERSION") or not os.path.exists(xray_exe):
                    print_log(f"Updating Xray-core to {latest}...")
                    url = f"{settings['GITHUB_DOWNLOAD_BASE']}/{latest}/Xray-windows-64.zip"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as dl:
                        if dl.status == 200:
                            with zipfile.ZipFile(io.BytesIO(await dl.read())) as z:
                                safe_extract(z, XRAY_DIR)
                            settings["XRAY_CURRENT_VERSION"] = latest
                            save_settings(settings)
                            print_log(f"Xray-core updated to {latest}.")
                        else:
                            print_log("Download failed.")
                else:
                    print_log(f"Xray-core up to date: {latest}")
    except Exception as e:
        print_log(f"Xray update error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Country / flag helpers
# ══════════════════════════════════════════════════════════════════════════════
def get_country_flag(code: str) -> str:
    cc = (code or "").upper().strip()
    if len(cc) != 2 or cc == "XX":
        return "🏳️"
    try:
        return chr(ord(cc[0]) + 127397) + chr(ord(cc[1]) + 127397)
    except Exception:
        return "🏳️"


def flag_emoji_to_code(flag: str) -> str | None:
    try:
        chars = list(flag)
        if len(chars) != 2:
            return None
        code = "".join(chr(ord(ch) - 127397) for ch in chars)
        if code.isalpha() and len(code) == 2:
            return code.upper()
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# GeoIP  Priority: 1. remark  2. ip_cache  3. API
# ══════════════════════════════════════════════════════════════════════════════
_COUNTRY_KEYWORDS: dict[str, tuple[str, str]] = {
    "germany": ("Germany","DE"), "deutschland": ("Germany","DE"),
    "canada": ("Canada","CA"),
    "united states": ("United States","US"), "usa": ("United States","US"),
    "finland": ("Finland","FI"),
    "united kingdom": ("United Kingdom","GB"), "netherlands": ("Netherlands","NL"),
    "holland": ("Netherlands","NL"), "france": ("France","FR"),
    "italy": ("Italy","IT"), "sweden": ("Sweden","SE"),
    "turkey": ("Turkey","TR"), "turkiye": ("Turkey","TR"),
    "japan": ("Japan","JP"), "singapore": ("Singapore","SG"),
    "russia": ("Russia","RU"), "iran": ("Iran","IR"),
    "ukraine": ("Ukraine","UA"), "poland": ("Poland","PL"),
    "austria": ("Austria","AT"), "switzerland": ("Switzerland","CH"),
    "spain": ("Spain","ES"), "portugal": ("Portugal","PT"),
    "czech": ("Czech Republic","CZ"), "romania": ("Romania","RO"),
    "hungary": ("Hungary","HU"), "bulgaria": ("Bulgaria","BG"),
    "latvia": ("Latvia","LV"), "estonia": ("Estonia","EE"),
    "lithuania": ("Lithuania","LT"), "moldova": ("Moldova","MD"),
    "serbia": ("Serbia","RS"), "norway": ("Norway","NO"),
    "denmark": ("Denmark","DK"), "india": ("India","IN"),
    "china": ("China","CN"), "hong kong": ("Hong Kong","HK"),
    "taiwan": ("Taiwan","TW"), "south korea": ("South Korea","KR"),
    "korea": ("South Korea","KR"), "australia": ("Australia","AU"),
    "brazil": ("Brazil","BR"), "argentina": ("Argentina","AR"),
    "mexico": ("Mexico","MX"), "israel": ("Israel","IL"),
}

_SHORT_CODES: dict[str, tuple[str, str]] = {
    "de":("Germany","DE"), "ca":("Canada","CA"), "us":("United States","US"),
    "fi":("Finland","FI"), "gb":("United Kingdom","GB"), "uk":("United Kingdom","GB"),
    "nl":("Netherlands","NL"), "fr":("France","FR"), "it":("Italy","IT"),
    "se":("Sweden","SE"), "tr":("Turkey","TR"), "jp":("Japan","JP"),
    "sg":("Singapore","SG"), "ru":("Russia","RU"), "ir":("Iran","IR"),
    "ua":("Ukraine","UA"), "pl":("Poland","PL"), "at":("Austria","AT"),
    "ch":("Switzerland","CH"), "es":("Spain","ES"), "pt":("Portugal","PT"),
    "cz":("Czech Republic","CZ"), "ro":("Romania","RO"), "hu":("Hungary","HU"),
    "bg":("Bulgaria","BG"), "no":("Norway","NO"), "dk":("Denmark","DK"),
    "in":("India","IN"), "cn":("China","CN"), "hk":("Hong Kong","HK"),
    "tw":("Taiwan","TW"), "kr":("South Korea","KR"), "au":("Australia","AU"),
    "br":("Brazil","BR"), "il":("Israel","IL"),
}


def extract_hint_from_remark(remark: str) -> dict | None:
    if not remark:
        return None

    # 1️⃣ تشخیص از روی ایموجی پرچم
    for flag in re.findall(r'[\U0001F1E6-\U0001F1FF]{2}', remark):
        code = flag_emoji_to_code(flag)
        if code:
            return {
                "country": country_name(code),
                "city": "",
                "code": code
            }

    rl = remark.lower()

    # 2️⃣ تشخیص از روی نام کشور (keywords)
    for key, (_, code) in _COUNTRY_KEYWORDS.items():
        if re.search(r'\b' + re.escape(key) + r'\b', rl):
            return {
                "country": country_name(code),
                "city": "",
                "code": code
            }

    # 3️⃣ تشخیص از روی کد کشور (US / DE / FR / ...)
    for key, (_, code) in _SHORT_CODES.items():
        if re.search(r'\b' + re.escape(key) + r'\b', rl):
            return {
                "country": country_name(code),
                "city": "",
                "code": code
            }

    return None

async def fetch_geo_from_api(session: aiohttp.ClientSession, timeout: float) -> dict | None:
    endpoints = [
        ("http://ip-api.com/json",
         lambda d: {"country": d.get("country"), "city": d.get("city"),
                    "code": d.get("countryCode")}),
        ("https://freeipapi.com/api/json",
         lambda d: {"country": d.get("countryName"), "city": d.get("cityName"),
                    "code": d.get("countryCode")}),
        ("https://ipapi.co/json/",
         lambda d: {"country": d.get("country_name"), "city": d.get("city"),
                    "code": d.get("country_code")}),
    ]
    for url, mapper in endpoints:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    mapped = mapper(await r.json(content_type=None))
                    if mapped and mapped.get("country"):
                        # Normalize name via ISO table when we have a code,
                        # so remark/cache/API all agree on the same country name.
                        code = (mapped.get("code") or "").upper().strip()
                        if code:
                            mapped["country"] = country_name(code)
                        return mapped
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Subscription decoder & config extractor
# ══════════════════════════════════════════════════════════════════════════════
def decode_if_base64(raw: str) -> str | None:
    cleaned = re.sub(r'\s+', '', raw)
    cleaned += "=" * ((4 - len(cleaned) % 4) % 4)
    try:
        decoded = base64.b64decode(cleaned).decode("utf-8", errors="ignore")
        if any(s in decoded for s in SUPPORTED_SCHEMES):
            return decoded
    except Exception:
        pass
    return None


def extract_configs_from_text(text: str) -> list[str]:
    pattern = r'(?:vless|vmess|trojan|tuic|ss|ssr)://[^\s<>"\'`]+'
    return [c.strip() for c in re.findall(pattern, text) if c.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# Protocol parsers
# ══════════════════════════════════════════════════════════════════════════════
def _decode_vmess(uri: str) -> dict:
    raw = uri.replace("vmess://", "")
    raw += "=" * ((4 - len(raw) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))


def _ss_host_port_secret(uri: str) -> tuple[str, int, str] | None:
    """
    Decode a ss:// URI (both the '@'-delimited and fully-base64 legacy
    formats) and return (host, port, method:password) — the REAL server
    identity, not the raw base64 blob that generic urlparse would return.
    """
    try:
        raw = uri[len("ss://"):].split("#")[0]
        if "@" in raw:
            userinfo, hostport = raw.rsplit("@", 1)
        else:
            dec = base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4)).decode("utf-8")
            userinfo, hostport = dec.rsplit("@", 1)
        try:
            ui = base64.urlsafe_b64decode(userinfo + "=" * ((4 - len(userinfo) % 4) % 4)).decode("utf-8")
        except Exception:
            ui = userinfo
        if hostport.startswith("["):
            host = hostport[1:hostport.index("]")]
            port = int(hostport.split("]:")[-1])
        else:
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
        return (host.lower(), port, ui)
    except Exception:
        return None


def _ssr_host_port_secret(uri: str) -> tuple[str, int, str] | None:
    """
    Decode an ssr:// URI and return (host, port, password) — the REAL
    server identity, not the raw base64 blob.
    """
    try:
        raw = uri[len("ssr://"):]
        raw += "=" * ((4 - len(raw) % 4) % 4)
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        main, _, _ = decoded.partition("/?")
        parts = main.split(":")
        host = parts[0].lower()
        port = int(parts[1])
        pb   = parts[5] if len(parts) > 5 else ""
        pb  += "=" * ((4 - len(pb) % 4) % 4)
        try:
            password = base64.urlsafe_b64decode(pb).decode("utf-8")
        except Exception:
            password = pb
        return (host, port, password)
    except Exception:
        return None


def get_server_address(uri: str) -> str | None:
    try:
        if uri.startswith("vmess://"):
            return _decode_vmess(uri).get("add")
        if uri.startswith("ss://"):
            r = _ss_host_port_secret(uri)
            return r[0] if r else None
        if uri.startswith("ssr://"):
            r = _ssr_host_port_secret(uri)
            return r[0] if r else None
        return urllib.parse.urlparse(uri).hostname
    except Exception:
        return None


def get_config_identity(base_uri: str) -> str:
    """
    Build a normalized identity key used for deduplication.

    Two config strings can differ in remark, query-param order, or minor
    formatting while still pointing at the exact same server+credentials.
    This key captures (protocol, host, port, secret) so such duplicates
    are correctly recognized as the same config — not just exact-string
    matches.

    ss:// and ssr:// are decoded via their real payload (not generic
    urlparse) since their raw URI is often just an opaque base64 blob
    that urlparse cannot split into host/port correctly.
    """
    try:
        if base_uri.startswith("vmess://"):
            cfg = _decode_vmess(base_uri)
            return f"vmess|{cfg.get('add')}|{cfg.get('port')}|{cfg.get('id')}"

        if base_uri.startswith("ss://"):
            r = _ss_host_port_secret(base_uri)
            if r:
                host, port, secret = r
                return f"ss|{host}|{port}|{secret}"
            return base_uri   # decode failed — fall back to exact string

        if base_uri.startswith("ssr://"):
            r = _ssr_host_port_secret(base_uri)
            if r:
                host, port, secret = r
                return f"ssr|{host}|{port}|{secret}"
            return base_uri

        parsed = urllib.parse.urlparse(base_uri)
        scheme = parsed.scheme
        host   = (parsed.hostname or "").lower()
        port   = parsed.port
        secret = parsed.username or parsed.password or ""
        return f"{scheme}|{host}|{port}|{secret}"
    except Exception:
        # Fallback: use the raw string itself (still correct, just less robust)
        return base_uri


def _stream_settings(query: dict, hostname: str) -> dict:
    network  = query.get("type", "tcp")
    security = query.get("security", "none")
    ss: dict = {"network": network, "security": security}

    if security == "tls":
        ss["tlsSettings"] = {
            "serverName":    query.get("sni") or hostname,
            "allowInsecure": query.get("allowInsecure", "0") == "1",
            "fingerprint":   query.get("fp", "chrome"),
            "alpn":          [a for a in query.get("alpn", "").split(",") if a],
        }
    elif security == "reality":
        ss["realitySettings"] = {
            "serverName":  query.get("sni") or hostname,
            "publicKey":   query.get("pbk", ""),
            "shortId":     query.get("sid", ""),
            "spiderX":     query.get("spx", ""),
            "fingerprint": query.get("fp", "chrome"),
        }

    if network == "ws":
        ss["wsSettings"] = {"path": query.get("path") or "/",
                            "headers": {"Host": query.get("host") or hostname}}
    elif network == "grpc":
        ss["grpcSettings"] = {
            "serviceName": query.get("serviceName") or query.get("path") or "",
            "multiMode":   query.get("mode", "") == "multi"}
    elif network in ("xhttp", "splithttp"):
        ss["xhttpSettings"] = {"path": query.get("path") or "/",
                               "host": query.get("host") or hostname}
    elif network == "httpupgrade":
        ss["httpupgradeSettings"] = {"path": query.get("path") or "/",
                                     "host": query.get("host") or hostname}
    elif network == "http":
        ss["httpSettings"] = {"path": query.get("path") or "/",
                              "headers": {"Host": [query.get("host") or hostname]}}
    elif network == "kcp":
        ss["kcpSettings"] = {"header": {"type": query.get("headerType", "none")},
                             "seed": query.get("seed", "")}
    elif network == "quic":
        ss["quicSettings"] = {"security": query.get("quicSecurity", "none"),
                              "key": query.get("key", ""),
                              "header": {"type": query.get("headerType", "none")}}
    return ss


def parse_vless(uri: str) -> dict | None:
    try:
        p = urllib.parse.urlparse(uri)
        q = dict(urllib.parse.parse_qsl(p.query))
        user: dict = {"id": p.username, "encryption": q.get("encryption", "none")}
        if q.get("flow"):            user["flow"]            = q["flow"]
        if q.get("packetEncoding"): user["packetEncoding"]  = q["packetEncoding"]
        return {"protocol": "vless",
                "settings": {"vnext": [{"address": p.hostname, "port": int(p.port),
                                         "users": [user]}]},
                "streamSettings": _stream_settings(q, p.hostname or "")}
    except Exception:
        return None


def parse_trojan(uri: str) -> dict | None:
    try:
        p = urllib.parse.urlparse(uri)
        q = dict(urllib.parse.parse_qsl(p.query))
        return {"protocol": "trojan",
                "settings": {"servers": [{"address": p.hostname, "port": int(p.port),
                                           "password": unquote(p.username or "")}]},
                "streamSettings": _stream_settings(q, p.hostname or "")}
    except Exception:
        return None


def parse_vmess(uri: str) -> dict | None:
    try:
        c  = _decode_vmess(uri)
        net = c.get("net", "tcp"); tls = c.get("tls", "none")
        host = c.get("host") or c.get("add") or ""; path = c.get("path") or "/"
        stream: dict = {"network": net, "security": tls}
        if tls == "tls":
            stream["tlsSettings"] = {"serverName": c.get("sni") or host,
                                     "allowInsecure": False}
        if net == "ws":
            stream["wsSettings"] = {"path": path, "headers": {"Host": host}}
        elif net == "grpc":
            stream["grpcSettings"] = {"serviceName": c.get("path") or "", "multiMode": False}
        elif net == "http":
            stream["httpSettings"] = {"path": path, "headers": {"Host": [host]}}
        elif net == "kcp":
            stream["kcpSettings"] = {"header": {"type": c.get("type", "none")}}
        elif net == "quic":
            stream["quicSettings"] = {"security": c.get("type", "none"),
                                      "key": "", "header": {"type": "none"}}
        return {"protocol": "vmess",
                "settings": {"vnext": [{"address": c.get("add"),
                                         "port": int(c.get("port", 443)),
                                         "users": [{"id": c.get("id"),
                                                    "alterId": int(c.get("aid", 0)),
                                                    "security": c.get("scy", "auto")}]}]},
                "streamSettings": stream}
    except Exception:
        return None


def parse_shadowsocks(uri: str) -> dict | None:
    try:
        raw = uri[len("ss://"):].split("#")[0]
        if "@" in raw:
            userinfo, hostport = raw.rsplit("@", 1)
        else:
            dec = base64.urlsafe_b64decode(raw + "=" * ((4-len(raw)%4)%4)).decode("utf-8")
            userinfo, hostport = dec.rsplit("@", 1)
        try:
            ui = base64.urlsafe_b64decode(userinfo + "=" * ((4-len(userinfo)%4)%4)).decode("utf-8")
            method, password = ui.split(":", 1)
        except Exception:
            method, password = userinfo.split(":", 1)
        if hostport.startswith("["):
            host = hostport[1:hostport.index("]")]; port = int(hostport.split("]:")[-1])
        else:
            host, port_s = hostport.rsplit(":", 1); port = int(port_s)
        return {"protocol": "shadowsocks",
                "settings": {"servers": [{"address": host, "port": port,
                                           "method": method, "password": password}]},
                "streamSettings": {"network": "tcp"}}
    except Exception:
        return None


def parse_shadowsocksr(uri: str) -> dict | None:
    try:
        raw = uri[len("ssr://"):]; raw += "=" * ((4-len(raw)%4)%4)
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        main, _, _ = decoded.partition("/?")
        parts    = main.split(":")
        host     = parts[0]; port = int(parts[1])
        protocol = parts[2]; method = parts[3]; obfs = parts[4]
        pb       = parts[5] if len(parts) > 5 else ""
        pb      += "=" * ((4-len(pb)%4)%4)
        password = base64.urlsafe_b64decode(pb).decode("utf-8")
        return {"protocol": "shadowsocks",
                "settings": {"servers": [{"address": host, "port": port,
                                           "method": method, "password": password,
                                           "_ssr_protocol": protocol, "_ssr_obfs": obfs}]},
                "streamSettings": {"network": "tcp"}}
    except Exception:
        return None


def parse_tuic(uri: str) -> dict | None:
    try:
        p = urllib.parse.urlparse(uri)
        q = dict(urllib.parse.parse_qsl(p.query))
        return {"protocol": "tuic",
                "settings": {"servers": [{"address": p.hostname, "port": int(p.port),
                                           "uuid": p.username or "",
                                           "password": unquote(p.password or ""),
                                           "congestionControl": q.get("congestion_control","bbr"),
                                           "alpn": [a for a in q.get("alpn","h3").split(",") if a],
                                           "udpRelayMode": q.get("udp_relay_mode","native")}]},
                "streamSettings": {"network": "tcp", "security": "tls",
                                   "tlsSettings": {"serverName": q.get("sni") or p.hostname,
                                                   "allowInsecure": q.get("allow_insecure","0")=="1"}}}
    except Exception:
        return None


def build_xray_outbound(uri: str) -> dict | None:
    if uri.startswith("vless://"):   return parse_vless(uri)
    if uri.startswith("vmess://"):   return parse_vmess(uri)
    if uri.startswith("trojan://"):  return parse_trojan(uri)
    if uri.startswith("tuic://"):    return parse_tuic(uri)
    if uri.startswith("ssr://"):     return parse_shadowsocksr(uri)
    if uri.startswith("ss://"):      return parse_shadowsocks(uri)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Port readiness check
# ══════════════════════════════════════════════════════════════════════════════
async def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            await asyncio.sleep(0.1)
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Core test
# ══════════════════════════════════════════════════════════════════════════════
async def test_config(
    config_string: str,
    worker_id: int,
    settings: dict,
    ip_cache: dict,
    ip_cache_lock: asyncio.Lock,
) -> dict | None:

    if "#" in config_string:
        base, old_remark = config_string.split("#", 1)
        old_remark = unquote(old_remark).strip()
    else:
        base = config_string; old_remark = ""

    geo_hint = extract_hint_from_remark(old_remark)   # Priority 1

    outbound = build_xray_outbound(base)
    if not outbound:
        return None

    server_addr = get_server_address(base)
    port        = 10800 + worker_id
    temp_path   = os.path.join(XRAY_DIR, f"config_worker_{worker_id}.json")

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump({"inbounds": [{"port": port, "listen": "127.0.0.1",
                                  "protocol": "socks", "settings": {"udp": False}}],
                   "outbounds": [outbound]}, f)

    flags    = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    xray_exe = os.path.join(XRAY_DIR, "xray.exe")
    proc     = None

    try:
        proc = await asyncio.create_subprocess_exec(
            xray_exe, "-config", temp_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)

        if not await wait_for_port("127.0.0.1", port, settings["XRAY_STARTUP_TIMEOUT"]):
            return None

        connector  = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}")
        start_time = time.time()

        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(settings["HTTP_TEST_URL"],
                                   timeout=aiohttp.ClientTimeout(
                                       total=settings["TEST_TIMEOUT"])) as resp:
                if resp.status not in (200, 204):
                    return None
                delay = int((time.time() - start_time) * 1000)

            # Priority 2: ip_cache
            geo_data = None
            if server_addr:
                async with ip_cache_lock:
                    geo_data = ip_cache.get(server_addr)

            # Priority 3: API (only when no hint and no cache)
            if not geo_data and not geo_hint:
                geo_data = await fetch_geo_from_api(session, settings["GEOIP_TIMEOUT"])
                if geo_data and server_addr:
                    async with ip_cache_lock:
                        ip_cache[server_addr] = geo_data

        final_geo = (geo_hint or geo_data
                     or {"country": "Unknown", "city": "", "code": "XX"})
        return {"base": base, "delay": delay, "geo": final_geo}

    except Exception:
        return None
    finally:
        if proc is not None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill(); await proc.wait()
            except Exception:
                pass
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Worker  — runs until MAX_TESTCOUNT reached (not MAX_CONFIGS)
# ══════════════════════════════════════════════════════════════════════════════
async def worker(
    queue:         asyncio.Queue,
    all_valid:     list,        # ALL passing configs (no cap during testing)
    worker_id:     int,
    settings:      dict,
    stop_event:    asyncio.Event,
    ip_cache:      dict,
    ip_cache_lock: asyncio.Lock,
    result_lock:   asyncio.Lock,
    run_stats:     dict,        # {"tested": int}
    source_stats:  dict,        # {"valid": int}  for current source
):
    max_test = settings.get("MAX_TESTCOUNT", settings["MAX_CONFIGS"] * 5)

    while not stop_event.is_set():
        try:
            config = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        async with result_lock:
            run_stats["tested"] += 1
            tested_now = run_stats["tested"]
            # Stop when MAX_TESTCOUNT reached
            if tested_now > max_test:
                stop_event.set()
                queue.task_done()
                break

        res = await test_config(config, worker_id, settings, ip_cache, ip_cache_lock)

        if res and not stop_event.is_set():
            code    = (res["geo"].get("code") or "XX").upper().strip()
            country = res["geo"].get("country", "Unknown")

            async with result_lock:
                source_stats["valid"] += 1
                all_valid.append(res)
                found  = len(all_valid)
                tested = run_stats["tested"]

            print_log(
                f"Tested {tested}/{max_test} | Valid so far: {found} | "
                f"✓ {country} ({code}) {res['delay']}ms"
            )

        queue.task_done()


# ══════════════════════════════════════════════════════════════════════════════
# Global deduplication — ensures no duplicate server ends up in the output
# ══════════════════════════════════════════════════════════════════════════════
def dedupe_configs(items: list) -> list:
    """
    Remove duplicate configs across ALL sources.

    Duplicates are detected by normalized identity (protocol+host+port+secret),
    NOT by exact string match — so the same server appearing with a different
    remark, query-param order, or from a different source is still caught.

    When duplicates are found, the fastest one (lowest delay) is kept.
    """
    best: dict[str, dict] = {}
    for item in items:
        key = get_config_identity(item["base"])
        current = best.get(key)
        if current is None or item["delay"] < current["delay"]:
            best[key] = item

    removed = len(items) - len(best)
    if removed > 0:
        print_log(f"Deduplication: removed {removed} duplicate config(s), "
                  f"{len(best)} unique remain.")
    return list(best.values())


# ══════════════════════════════════════════════════════════════════════════════
# Smart distribution — pick MAX_CONFIGS from all valid, spread evenly
# ══════════════════════════════════════════════════════════════════════════════
def distribute_configs(all_valid: list, max_configs: int) -> list:
    """
    From all valid configs, select up to max_configs with even country spread.

    Step 0 — Deduplicate across all sources first (see dedupe_configs).

    Algorithm (round-robin by country, sorted by delay within each country):
      1. Group all valid configs by country code.
      2. Sort each group by delay ascending (fastest first).
      3. Round-robin: take one from each country in turn until max_configs reached.
         Countries with more configs keep appearing in rotation longer.

    This guarantees:
      - If 10 countries found → roughly max_configs/10 each.
      - If one country dominates → others still get fair share first.
      - Within each country → fastest configs selected first.
      - No duplicate server ever appears in the final output.
    """
    if not all_valid:
        return []

    unique = dedupe_configs(all_valid)

    # Group by country code
    groups: dict[str, list] = {}
    for item in unique:
        code = (item["geo"].get("code") or "XX").upper().strip()
        groups.setdefault(code, []).append(item)

    # Sort each group by delay
    for code in groups:
        groups[code].sort(key=lambda x: x["delay"])

    # Print distribution info
    print_log(f"Distribution: {len(unique)} unique valid configs across "
              f"{len(groups)} countries — selecting {max_configs}.")
    for code, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        print_log(f"  {code}: {len(items)} available")

    # Round-robin selection
    selected: list = []
    iters = [iter(items) for items in groups.values()]
    while len(selected) < max_configs and iters:
        next_iters = []
        for it in iters:
            if len(selected) >= max_configs:
                break
            try:
                selected.append(next(it))
                next_iters.append(it)
            except StopIteration:
                pass  # This country exhausted — drop from rotation
        iters = next_iters
        if not iters:
            break  # All countries exhausted

    return selected


# ══════════════════════════════════════════════════════════════════════════════
# Output writer
# ══════════════════════════════════════════════════════════════════════════════
def build_update_info_config() -> str:
    """
    Build a syntactically-valid but non-functional VLESS entry whose remark
    shows the last update date/time. Placed FIRST in the output file so any
    V2Ray client app displays it at the very top of the config list as an
    informational marker — it points to localhost and is never meant to be
    used as a working proxy.
    """
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    remark = urllib.parse.quote(f"🕐 Last Update: {now}")
    return (
        "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1080"
        f"?encryption=none&security=none&type=tcp#{remark}"
    )


def write_output(selected: list):
    """
    Sort by delay, write with formatted remarks. Final dedup safety net.
    The very first line is always a fake "last update" info entry (see
    build_update_info_config) so it shows at the top of any client app.
    """
    selected = dedupe_configs(selected)   # safety net — should already be unique
    selected.sort(key=lambda x: x["delay"])
    counters: dict = {}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(build_update_info_config() + "\n")
        for item in selected:
            geo     = item["geo"]
            country = geo.get("country", "Unknown")
            city    = geo.get("city", "")
            code    = (geo.get("code") or "XX").upper().strip()
            flag    = get_country_flag(code)
            label   = (f"{flag} {country} {city}"
                       if city and city.lower() not in ("", "location")
                       else f"{flag} {country}")
            key = label.strip()
            counters[key] = counters.get(key, 0) + 1
            f.write(f"{item['base']}#{label} - {counters[key]:02d}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Finalize — called on normal end AND on Ctrl+C
# ══════════════════════════════════════════════════════════════════════════════
def finalize(all_valid: list, settings: dict,
             ip_cache: dict, src_stats: dict, interrupted: bool = False):
    save_ip_cache(ip_cache)
    save_stats(src_stats)
    rewrite_sources_with_comments(src_stats)

    if all_valid:
        selected = distribute_configs(all_valid, settings["MAX_CONFIGS"])
        write_output(selected)
        label = "partial" if interrupted else "final"
        print_log(f"{'─'*60}")
        print_log(f"Tested {settings.get('MAX_TESTCOUNT','?')} | "
                  f"Valid pool: {len(all_valid)} | "
                  f"Saved {len(selected)} {label} configs to {OUTPUT_FILE}.")
    else:
        print_log("No valid configurations found.")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    print_log(f"{'═'*60}")
    print_log(f"  V2Ray Config Tester  v{APP_VERSION}")
    print_log(f"{'═'*60}")

    settings      = load_or_create_settings()
    ip_cache      = load_ip_cache()
    src_stats     = load_stats()
    ip_cache_lock = asyncio.Lock()
    result_lock   = asyncio.Lock()

    # Global stop event shared with GracefulStop
    global_stop = asyncio.Event()
    _graceful.set_global_event(global_stop)

    print_log(f"Target: {settings['MAX_CONFIGS']} output configs "
              f"from testing up to {settings.get('MAX_TESTCOUNT', settings['MAX_CONFIGS']*5)} configs.")

    await check_and_update_xray(settings)

    # Read Sources.txt
    if not os.path.exists(SOURCE_FILE):
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write(
                "# Add subscription URLs below\n"
                "# Lines starting with # are ignored\n"
                "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs"
                "/main/subscriptions/v2ray/all_sub.txt\n"
            )
        print_log(f"Created {SOURCE_FILE}. Add sources and run again.")
        sys.exit()

    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        raw_links_all = [
            ln.strip() for ln in f
            if ln.strip() and not ln.strip().startswith("#")
        ]

    # De-duplicate source URLs (case-insensitive) — prevents wasting the
    # MAX_TESTCOUNT budget by testing the exact same source twice.
    seen_norm: set = set()
    raw_links: list = []
    for url in raw_links_all:
        norm = url.strip().lower()
        if norm in seen_norm:
            print_log(f"⚠  Duplicate source URL skipped: {url}")
            continue
        seen_norm.add(norm)
        raw_links.append(url)

    # Sort sources by previous score (best first, NotOK last)
    source_links = get_sorted_source_links(raw_links, src_stats)

    if src_stats:
        print_log("Source order this run (sorted by previous score):")
        for lnk in source_links:
            rec = _latest_record(src_stats, lnk)
            if rec:
                print_log(f"  [{rec['score_percent']}% | {rec['status']}] {lnk}")
            else:
                print_log(f"  [NEW] {lnk}")

    all_valid:    list = []   # ALL passing configs (no cap)
    run_stats = {"tested": 0}

    # Pre-test dedup: tracks every config identity already queued this run
    # (across ALL sources) so the same server is never spawned/tested twice.
    # This is the key optimization — dedup BEFORE xray is launched, not after.
    global_seen_identities: set = set()

    try:
        for link in source_links:
            if global_stop.is_set():
                break

            # No early exit — always test up to MAX_TESTCOUNT

            print_log(f"{'─'*60}")
            print_log(f"Fetching: {link}")
            raw_text  = None
            reachable = False

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        link, timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 200:
                            raw_text  = await resp.text()
                            reachable = True
            except Exception as e:
                print_log(f"Error fetching source: {e}")

            if not raw_text:
                update_source_stat(src_stats, link, 0, 0, False)
                print_log("Unreachable or empty. Marked as NotOK.")
                continue

            decoded = decode_if_base64(raw_text)
            content = decoded if decoded is not None else raw_text
            if decoded is not None:
                print_log("Detected Base64 subscription. Decoding...")

            configs = list(dict.fromkeys(extract_configs_from_text(content)))
            total_in_source = len(configs)   # size of this source (for Score)

            if not configs:
                update_source_stat(src_stats, link, 0, 0, reachable)
                print_log("No valid configs found. Skipping...")
                continue

            # ── Pre-test global dedup ──────────────────────────────────
            # Skip any config whose (protocol, host, port, secret) identity
            # was already queued from an earlier source in this run —
            # avoids spawning xray.exe for a server we've already tested.
            fresh_configs: list = []
            for c in configs:
                base = c.split("#", 1)[0] if "#" in c else c
                ident = get_config_identity(base)
                if ident in global_seen_identities:
                    continue
                global_seen_identities.add(ident)
                fresh_configs.append(c)

            skipped_dupes = total_in_source - len(fresh_configs)

            if not fresh_configs:
                update_source_stat(src_stats, link, total_in_source, 0, reachable)
                print_log(
                    f"All {total_in_source} configs already seen in this run "
                    f"(duplicates of earlier sources). Skipping..."
                )
                continue

            print_log(
                f"Loaded {total_in_source} configs "
                f"({skipped_dupes} already seen, {len(fresh_configs)} new). "
                f"Starting workers..."
            )

            queue        = asyncio.Queue()
            source_valid = {"valid": 0}   # track valid count for this source
            for c in fresh_configs:
                queue.put_nowait(c)

            stop_event = asyncio.Event()
            # Hook global stop into per-source stop
            async def _watch_global():
                await global_stop.wait()
                stop_event.set()
            watcher = asyncio.create_task(_watch_global())

            tasks = [
                asyncio.create_task(
                    worker(queue, all_valid, i, settings, stop_event,
                           ip_cache, ip_cache_lock, result_lock,
                           run_stats, source_valid)
                )
                for i in range(settings["MAX_WORKERS"])
            ]
            await asyncio.gather(*tasks)
            watcher.cancel()

            # Record stats for this source (auto-trims history to last 5)
            update_source_stat(src_stats, link,
                               total_in_source, source_valid["valid"], reachable)
            latest = _latest_record(src_stats, link)
            print_log(
                f"Source done — Total: {total_in_source} | "
                f"Valid: {source_valid['valid']} | "
                f"Score: {latest['score_percent']}%"
            )

            save_ip_cache(ip_cache)

            if global_stop.is_set():
                break

    except Exception as e:
        print_log(f"Unexpected error: {e}")

    finally:
        interrupted = global_stop.is_set()
        finalize(all_valid, settings, ip_cache, src_stats, interrupted)


if __name__ == "__main__":
    asyncio.run(main())