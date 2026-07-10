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
APP_VERSION = "5.0.0 (Batch Core + Smart Distribution Edition)"

# ── Absolute Path Definitions ──────────────────────────────────────────────────
# مسیردهی مطلق برای جلوگیری از خطای یافت نشدن فایل هنگام اجرا در محیط‌های مختلف
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

XRAY_DIR = os.path.join(APP_DIR, "xray_core")
SETTINGS_FILE = os.path.join(APP_DIR, "Setting.txt")
SOURCE_FILE = os.path.join(APP_DIR, "Sources.txt")
IP_CACHE_FILE = os.path.join(APP_DIR, "ip_cache.json")
OUTPUT_FILE = os.path.join(APP_DIR, "Morteza_Taheri.txt")
STATS_FILE = os.path.join(APP_DIR, "sources_stats.json")

# ── 10. Default Settings ───────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "MAX_CONFIGS": 30,
    "MAX_TESTCOUNT": 300,
    "MAX_WORKERS": 10,
    "TEST_TIMEOUT": 7,
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
    score = round((valid / total * 100)) if total > 0 else 0
    record = {
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_configs": total,
        "valid_configs": valid,
        "score_percent": score,
        "status": "OK" if reachable and total > 0 else "NotOK",
    }
    history = stats.get(url, [])
    history.append(record)
    stats[url] = history[-MAX_STAT_HISTORY:]


def _latest_record(stats: dict, url: str) -> dict | None:
    history = stats.get(url)
    return history[-1] if history else None


def _score_key(url: str, stats: dict) -> tuple:
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
    if not os.path.exists(SOURCE_FILE):
        return

    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        original_lines = f.readlines()

    cleaned = [ln for ln in original_lines if not ln.strip().startswith("# [Score:")]
    header_lines: list[str] = []
    url_blocks: list[tuple[str, list[str], str]] = []
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

    trailing = pending
    url_blocks.sort(key=lambda b: _score_key(b[0], stats))

    new_lines: list[str] = list(header_lines)
    for url, manual_lines, url_line in url_blocks:
        history = stats.get(url)
        if history:
            for rec in history[-MAX_STAT_HISTORY:]:
                new_lines.append(_format_stat_comment(rec))
        new_lines.extend(manual_lines)
        new_lines.append(url_line)
    new_lines.extend(trailing)

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
    return sorted(raw_links, key=lambda u: _score_key(u, stats))


# ══════════════════════════════════════════════════════════════════════════════
# Graceful stop
# ══════════════════════════════════════════════════════════════════════════════
class GracefulStop:
    def __init__(self):
        self.stop_requested = False
        self._count = 0
        self._global_stop: asyncio.Event | None = None

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
                data = await resp.json()
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
# Country / GeoIP helpers
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


_COUNTRY_KEYWORDS: dict[str, tuple[str, str]] = {
    "germany": ("Germany", "DE"), "deutschland": ("Germany", "DE"),
    "canada": ("Canada", "CA"), "united states": ("United States", "US"), "usa": ("United States", "US"),
    "finland": ("Finland", "FI"), "united kingdom": ("United Kingdom", "GB"), "netherlands": ("Netherlands", "NL"),
    "holland": ("Netherlands", "NL"), "france": ("France", "FR"), "italy": ("Italy", "IT"), "sweden": ("Sweden", "SE"),
    "turkey": ("Turkey", "TR"), "turkiye": ("Turkey", "TR"), "japan": ("Japan", "JP"), "singapore": ("Singapore", "SG"),
    "russia": ("Russia", "RU"), "iran": ("Iran", "IR"), "ukraine": ("Ukraine", "UA"), "poland": ("Poland", "PL"),
    "austria": ("Austria", "AT"), "switzerland": ("Switzerland", "CH"), "spain": ("Spain", "ES"),
    "portugal": ("Portugal", "PT"),
    "czech": ("Czech Republic", "CZ"), "romania": ("Romania", "RO"), "hungary": ("Hungary", "HU"),
    "bulgaria": ("Bulgaria", "BG"),
    "latvia": ("Latvia", "LV"), "estonia": ("Estonia", "EE"), "lithuania": ("Lithuania", "LT"),
    "moldova": ("Moldova", "MD"),
    "serbia": ("Serbia", "RS"), "norway": ("Norway", "NO"), "denmark": ("Denmark", "DK"), "india": ("India", "IN"),
    "china": ("China", "CN"), "hong kong": ("Hong Kong", "HK"), "taiwan": ("Taiwan", "TW"),
    "south korea": ("South Korea", "KR"),
    "korea": ("South Korea", "KR"), "australia": ("Australia", "AU"), "brazil": ("Brazil", "BR"),
    "argentina": ("Argentina", "AR"),
    "mexico": ("Mexico", "MX"), "israel": ("Israel", "IL"),
}

_SHORT_CODES: dict[str, tuple[str, str]] = {
    "de": ("Germany", "DE"), "ca": ("Canada", "CA"), "us": ("United States", "US"), "fi": ("Finland", "FI"),
    "gb": ("United Kingdom", "GB"),
    "uk": ("United Kingdom", "GB"), "nl": ("Netherlands", "NL"), "fr": ("France", "FR"), "it": ("Italy", "IT"),
    "se": ("Sweden", "SE"),
    "tr": ("Turkey", "TR"), "jp": ("Japan", "JP"), "sg": ("Singapore", "SG"), "ru": ("Russia", "RU"),
    "ir": ("Iran", "IR"),
    "ua": ("Ukraine", "UA"), "pl": ("Poland", "PL"), "at": ("Austria", "AT"), "ch": ("Switzerland", "CH"),
    "es": ("Spain", "ES"),
    "pt": ("Portugal", "PT"), "cz": ("Czech Republic", "CZ"), "ro": ("Romania", "RO"), "hu": ("Hungary", "HU"),
    "bg": ("Bulgaria", "BG"),
    "no": ("Norway", "NO"), "dk": ("Denmark", "DK"), "in": ("India", "IN"), "cn": ("China", "CN"),
    "hk": ("Hong Kong", "HK"),
    "tw": ("Taiwan", "TW"), "kr": ("South Korea", "KR"), "au": ("Australia", "AU"), "br": ("Brazil", "BR"),
    "il": ("Israel", "IL"),
}


def extract_hint_from_remark(remark: str) -> dict | None:
    if not remark: return None
    for flag in re.findall(r'[\U0001F1E6-\U0001F1FF]{2}', remark):
        code = flag_emoji_to_code(flag)
        if code: return {"country": country_name(code), "city": "", "code": code}
    rl = remark.lower()
    for key, (_, code) in _COUNTRY_KEYWORDS.items():
        if re.search(r'\b' + re.escape(key) + r'\b', rl):
            return {"country": country_name(code), "city": "", "code": code}
    for key, (_, code) in _SHORT_CODES.items():
        if re.search(r'\b' + re.escape(key) + r'\b', rl):
            return {"country": country_name(code), "city": "", "code": code}
    return None


async def fetch_geo_from_api(session: aiohttp.ClientSession, timeout: float) -> dict | None:
    endpoints = [
        ("http://ip-api.com/json",
         lambda d: {"country": d.get("country"), "city": d.get("city"), "code": d.get("countryCode")}),
        ("https://freeipapi.com/api/json",
         lambda d: {"country": d.get("countryName"), "city": d.get("cityName"), "code": d.get("countryCode")}),
        ("https://ipapi.co/json/",
         lambda d: {"country": d.get("country_name"), "city": d.get("city"), "code": d.get("country_code")}),
    ]
    for url, mapper in endpoints:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    mapped = mapper(await r.json(content_type=None))
                    if mapped and mapped.get("country"):
                        code = (mapped.get("code") or "").upper().strip()
                        if code: mapped["country"] = country_name(code)
                        return mapped
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Config Extraction & Parsing
# ══════════════════════════════════════════════════════════════════════════════
def decode_if_base64(raw: str) -> str | None:
    cleaned = re.sub(r'\s+', '', raw)
    cleaned += "=" * ((4 - len(cleaned) % 4) % 4)
    try:
        decoded = base64.b64decode(cleaned).decode("utf-8", errors="ignore")
        if any(s in decoded for s in SUPPORTED_SCHEMES): return decoded
    except Exception:
        pass
    return None


def extract_configs_from_text(text: str) -> list[str]:
    pattern = r'(?:vless|vmess|trojan|tuic|ss|ssr)://[^\s<>"\'`]+'
    return [c.strip() for c in re.findall(pattern, text) if c.strip()]


def _decode_vmess(uri: str) -> dict:
    raw = uri.replace("vmess://", "")
    raw += "=" * ((4 - len(raw) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))


def _ss_host_port_secret(uri: str) -> tuple[str, int, str] | None:
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
    try:
        raw = uri[len("ssr://"):]
        raw += "=" * ((4 - len(raw) % 4) % 4)
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        main, _, _ = decoded.partition("/?")
        parts = main.split(":")
        host = parts[0].lower();
        port = int(parts[1])
        pb = parts[5] if len(parts) > 5 else "";
        pb += "=" * ((4 - len(pb) % 4) % 4)
        try:
            password = base64.urlsafe_b64decode(pb).decode("utf-8")
        except Exception:
            password = pb
        return (host, port, password)
    except Exception:
        return None


def get_server_address(uri: str) -> str | None:
    try:
        if uri.startswith("vmess://"): return _decode_vmess(uri).get("add")
        if uri.startswith("ss://"): r = _ss_host_port_secret(uri); return r[0] if r else None
        if uri.startswith("ssr://"): r = _ssr_host_port_secret(uri); return r[0] if r else None
        return urllib.parse.urlparse(uri).hostname
    except Exception:
        return None


def get_config_identity(base_uri: str) -> str:
    try:
        if base_uri.startswith("vmess://"):
            cfg = _decode_vmess(base_uri)
            return f"vmess|{cfg.get('add')}|{cfg.get('port')}|{cfg.get('id')}"
        if base_uri.startswith("ss://"):
            r = _ss_host_port_secret(base_uri)
            if r: return f"ss|{r[0]}|{r[1]}|{r[2]}"
            return base_uri
        if base_uri.startswith("ssr://"):
            r = _ssr_host_port_secret(base_uri)
            if r: return f"ssr|{r[0]}|{r[1]}|{r[2]}"
            return base_uri
        parsed = urllib.parse.urlparse(base_uri)
        return f"{parsed.scheme}|{(parsed.hostname or '').lower()}|{parsed.port}|{parsed.username or parsed.password or ''}"
    except Exception:
        return base_uri


def _stream_settings(query: dict, hostname: str) -> dict:
    network = query.get("type", "tcp");
    security = query.get("security", "none")
    ss: dict = {"network": network, "security": security}
    if security == "tls":
        ss["tlsSettings"] = {"serverName": query.get("sni") or hostname,
                             "allowInsecure": query.get("allowInsecure", "0") == "1",
                             "fingerprint": query.get("fp", "chrome"),
                             "alpn": [a for a in query.get("alpn", "").split(",") if a]}
    elif security == "reality":
        ss["realitySettings"] = {"serverName": query.get("sni") or hostname, "publicKey": query.get("pbk", ""),
                                 "shortId": query.get("sid", ""), "spiderX": query.get("spx", ""),
                                 "fingerprint": query.get("fp", "chrome")}
    if network == "ws":
        ss["wsSettings"] = {"path": query.get("path") or "/", "headers": {"Host": query.get("host") or hostname}}
    elif network == "grpc":
        ss["grpcSettings"] = {"serviceName": query.get("serviceName") or query.get("path") or "",
                              "multiMode": query.get("mode", "") == "multi"}
    elif network in ("xhttp", "splithttp"):
        ss["xhttpSettings"] = {"path": query.get("path") or "/", "host": query.get("host") or hostname}
    elif network == "httpupgrade":
        ss["httpupgradeSettings"] = {"path": query.get("path") or "/", "host": query.get("host") or hostname}
    elif network == "http":
        ss["httpSettings"] = {"path": query.get("path") or "/", "headers": {"Host": [query.get("host") or hostname]}}
    elif network == "kcp":
        ss["kcpSettings"] = {"header": {"type": query.get("headerType", "none")}, "seed": query.get("seed", "")}
    elif network == "quic":
        ss["quicSettings"] = {"security": query.get("quicSecurity", "none"), "key": query.get("key", ""),
                              "header": {"type": query.get("headerType", "none")}}
    return ss


def parse_vless(uri: str) -> dict | None:
    try:
        p = urllib.parse.urlparse(uri);
        q = dict(urllib.parse.parse_qsl(p.query))
        user: dict = {"id": p.username, "encryption": q.get("encryption", "none")}
        if q.get("flow"): user["flow"] = q["flow"]
        if q.get("packetEncoding"): user["packetEncoding"] = q["packetEncoding"]
        return {"protocol": "vless",
                "settings": {"vnext": [{"address": p.hostname, "port": int(p.port), "users": [user]}]},
                "streamSettings": _stream_settings(q, p.hostname or "")}
    except Exception:
        return None


def parse_trojan(uri: str) -> dict | None:
    try:
        p = urllib.parse.urlparse(uri);
        q = dict(urllib.parse.parse_qsl(p.query))
        return {"protocol": "trojan", "settings": {
            "servers": [{"address": p.hostname, "port": int(p.port), "password": unquote(p.username or "")}]},
                "streamSettings": _stream_settings(q, p.hostname or "")}
    except Exception:
        return None


def parse_vmess(uri: str) -> dict | None:
    try:
        c = _decode_vmess(uri);
        net = c.get("net", "tcp");
        tls = c.get("tls", "none")
        host = c.get("host") or c.get("add") or "";
        path = c.get("path") or "/"
        stream: dict = {"network": net, "security": tls}
        if tls == "tls": stream["tlsSettings"] = {"serverName": c.get("sni") or host, "allowInsecure": False}
        if net == "ws":
            stream["wsSettings"] = {"path": path, "headers": {"Host": host}}
        elif net == "grpc":
            stream["grpcSettings"] = {"serviceName": c.get("path") or "", "multiMode": False}
        elif net == "http":
            stream["httpSettings"] = {"path": path, "headers": {"Host": [host]}}
        elif net == "kcp":
            stream["kcpSettings"] = {"header": {"type": c.get("type", "none")}}
        elif net == "quic":
            stream["quicSettings"] = {"security": c.get("type", "none"), "key": "", "header": {"type": "none"}}
        return {"protocol": "vmess", "settings": {"vnext": [{"address": c.get("add"), "port": int(c.get("port", 443)),
                                                             "users": [
                                                                 {"id": c.get("id"), "alterId": int(c.get("aid", 0)),
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
            dec = base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4)).decode("utf-8")
            userinfo, hostport = dec.rsplit("@", 1)
        try:
            ui = base64.urlsafe_b64decode(userinfo + "=" * ((4 - len(userinfo) % 4) % 4)).decode(
                "utf-8"); method, password = ui.split(":", 1)
        except Exception:
            method, password = userinfo.split(":", 1)
        if hostport.startswith("["):
            host = hostport[1:hostport.index("]")]; port = int(hostport.split("]:")[-1])
        else:
            host, port_s = hostport.rsplit(":", 1); port = int(port_s)
        return {"protocol": "shadowsocks",
                "settings": {"servers": [{"address": host, "port": port, "method": method, "password": password}]},
                "streamSettings": {"network": "tcp"}}
    except Exception:
        return None


def parse_shadowsocksr(uri: str) -> dict | None:
    try:
        raw = uri[len("ssr://"):];
        raw += "=" * ((4 - len(raw) % 4) % 4);
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        main, _, _ = decoded.partition("/?");
        parts = main.split(":");
        host = parts[0];
        port = int(parts[1])
        protocol = parts[2];
        method = parts[3];
        obfs = parts[4];
        pb = parts[5] if len(parts) > 5 else "";
        pb += "=" * ((4 - len(pb) % 4) % 4)
        password = base64.urlsafe_b64decode(pb).decode("utf-8")
        return {"protocol": "shadowsocks", "settings": {"servers": [
            {"address": host, "port": port, "method": method, "password": password, "_ssr_protocol": protocol,
             "_ssr_obfs": obfs}]}, "streamSettings": {"network": "tcp"}}
    except Exception:
        return None


def parse_tuic(uri: str) -> dict | None:
    try:
        p = urllib.parse.urlparse(uri);
        q = dict(urllib.parse.parse_qsl(p.query))
        return {"protocol": "tuic", "settings": {"servers": [
            {"address": p.hostname, "port": int(p.port), "uuid": p.username or "",
             "password": unquote(p.password or ""), "congestionControl": q.get("congestion_control", "bbr"),
             "alpn": [a for a in q.get("alpn", "h3").split(",") if a],
             "udpRelayMode": q.get("udp_relay_mode", "native")}]},
                "streamSettings": {"network": "tcp", "security": "tls",
                                   "tlsSettings": {"serverName": q.get("sni") or p.hostname,
                                                   "allowInsecure": q.get("allow_insecure", "0") == "1"}}}
    except Exception:
        return None


def build_xray_outbound(uri: str) -> dict | None:
    if uri.startswith("vless://"): return parse_vless(uri)
    if uri.startswith("vmess://"): return parse_vmess(uri)
    if uri.startswith("trojan://"): return parse_trojan(uri)
    if uri.startswith("tuic://"): return parse_tuic(uri)
    if uri.startswith("ssr://"): return parse_shadowsocksr(uri)
    if uri.startswith("ss://"): return parse_shadowsocks(uri)
    return None


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
# Core Batch Test Function
# ══════════════════════════════════════════════════════════════════════════════
async def test_batch(
        chunk: list[str],
        worker_id: int,
        settings: dict,
        ip_cache: dict,
        ip_cache_lock: asyncio.Lock,
) -> list[dict]:
    inbounds = []
    outbounds = []
    rules = []
    meta_data = []

    # تخصیص بلاک پورت مجزا به هر ورکر برای جلوگیری از تداخل (از پورت 11000 به بعد)
    base_port = 11000 + (worker_id * 100)

    for i, config_string in enumerate(chunk):
        if "#" in config_string:
            base, old_remark = config_string.split("#", 1)
            old_remark = unquote(old_remark).strip()
        else:
            base = config_string
            old_remark = ""

        geo_hint = extract_hint_from_remark(old_remark)
        outbound = build_xray_outbound(base)

        if not outbound:
            continue

        server_addr = get_server_address(base)
        port = base_port + i
        in_tag = f"in_{i}"
        out_tag = f"out_{i}"

        inbounds.append({
            "tag": in_tag,
            "port": port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": False}
        })

        outbound["tag"] = out_tag
        outbounds.append(outbound)

        rules.append({
            "type": "field",
            "inboundTag": [in_tag],
            "outboundTag": out_tag
        })

        meta_data.append({
            "index": i,
            "base": base,
            "port": port,
            "geo_hint": geo_hint,
            "server_addr": server_addr
        })

    if not outbounds:
        return []

    temp_path = os.path.join(XRAY_DIR, f"config_batch_{worker_id}.json")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump({
            "inbounds": inbounds,
            "outbounds": outbounds,
            "routing": {"rules": rules}
        }, f)

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    xray_exe = os.path.join(XRAY_DIR, "xray.exe")
    proc = None
    results = []

    try:
        proc = await asyncio.create_subprocess_exec(
            xray_exe, "-config", temp_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags
        )

        # صبر می‌کنیم تا آخرین پورت بالا بیاید (نشان‌دهنده اجرای کامل)
        last_port = meta_data[-1]["port"]
        if not await wait_for_port("127.0.0.1", last_port, settings["XRAY_STARTUP_TIMEOUT"]):
            return []

        async def check_single(meta):
            connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{meta['port']}")
            start_time = time.time()
            try:
                # استفاده از session ایزوله و امن با مدیریت خودکار سوکت
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(
                            settings["HTTP_TEST_URL"],
                            timeout=aiohttp.ClientTimeout(total=settings["TEST_TIMEOUT"])
                    ) as resp:
                        if resp.status in (200, 204):
                            delay = int((time.time() - start_time) * 1000)

                            geo_data = None
                            if meta['server_addr']:
                                async with ip_cache_lock:
                                    geo_data = ip_cache.get(meta['server_addr'])

                            if not geo_data and not meta['geo_hint']:
                                geo_data = await fetch_geo_from_api(session, settings["GEOIP_TIMEOUT"])
                                if geo_data and meta['server_addr']:
                                    async with ip_cache_lock:
                                        ip_cache[meta['server_addr']] = geo_data

                            final_geo = (meta['geo_hint'] or geo_data or {"country": "Unknown", "city": "",
                                                                          "code": "XX"})
                            return {"base": meta["base"], "delay": delay, "geo": final_geo}
            except Exception:
                pass
            return None

        tasks = [asyncio.create_task(check_single(m)) for m in meta_data]
        res_list = await asyncio.gather(*tasks)

        for r in res_list:
            if r is not None:
                results.append(r)

    except Exception:
        pass
    finally:
        if proc is not None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

    return results


# ══════════════════════════════════════════════════════════════════════════════
# Batch Worker
# ══════════════════════════════════════════════════════════════════════════════
async def worker_batch(
        queue: asyncio.Queue,
        all_valid: list,
        worker_id: int,
        settings: dict,
        stop_event: asyncio.Event,
        ip_cache: dict,
        ip_cache_lock: asyncio.Lock,
        result_lock: asyncio.Lock,
        run_stats: dict,
        source_stats: dict,
):
    max_test = settings.get("MAX_TESTCOUNT", settings["MAX_CONFIGS"] * 5)

    while not stop_event.is_set():
        try:
            chunk = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        async with result_lock:
            run_stats["tested"] += len(chunk)
            tested_now = run_stats["tested"]
            if tested_now > max_test:
                stop_event.set()
                queue.task_done()
                break

        results = await test_batch(chunk, worker_id, settings, ip_cache, ip_cache_lock)

        if results and not stop_event.is_set():
            async with result_lock:
                for res in results:
                    source_stats["valid"] += 1
                    all_valid.append(res)

                found = len(all_valid)
                tested = run_stats["tested"]

            if results:
                best = min(results, key=lambda x: x["delay"])
                code = (best["geo"].get("code") or "XX").upper().strip()
                country = best["geo"].get("country", "Unknown")
                print_log(
                    f"Tested ~{tested}/{max_test} (Batch) | Valid so far: {found} | "
                    f"Best in batch: ✓ {country} ({code}) {best['delay']}ms"
                )

        queue.task_done()


# ══════════════════════════════════════════════════════════════════════════════
# Global deduplication & Distribution
# ══════════════════════════════════════════════════════════════════════════════
def dedupe_configs(items: list) -> list:
    best: dict[str, dict] = {}
    for item in items:
        key = get_config_identity(item["base"])
        current = best.get(key)
        if current is None or item["delay"] < current["delay"]:
            best[key] = item
    removed = len(items) - len(best)
    if removed > 0:
        print_log(f"Deduplication: removed {removed} duplicate config(s), {len(best)} unique remain.")
    return list(best.values())


def distribute_configs(all_valid: list, max_configs: int) -> list:
    if not all_valid:
        return []
    unique = dedupe_configs(all_valid)
    groups: dict[str, list] = {}
    for item in unique:
        code = (item["geo"].get("code") or "XX").upper().strip()
        groups.setdefault(code, []).append(item)
    for code in groups:
        groups[code].sort(key=lambda x: x["delay"])

    print_log(
        f"Distribution: {len(unique)} unique valid configs across {len(groups)} countries — selecting {max_configs}.")
    for code, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        print_log(f"  {code}: {len(items)} available")

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
                pass
        iters = next_iters
        if not iters:
            break
    return selected


def write_output(selected: list):
    selected = dedupe_configs(selected)
    selected.sort(key=lambda x: x["delay"])
    counters: dict = {}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for item in selected:
            geo = item["geo"]
            country = geo.get("country", "Unknown")
            city = geo.get("city", "")
            code = (geo.get("code") or "XX").upper().strip()
            flag = get_country_flag(code)
            label = (
                f"{flag} {country} {city}" if city and city.lower() not in ("", "location") else f"{flag} {country}")
            key = label.strip()
            counters[key] = counters.get(key, 0) + 1
            f.write(f"{item['base']}#{label} - {counters[key]:02d}\n")


def finalize(all_valid: list, settings: dict, ip_cache: dict, src_stats: dict, interrupted: bool = False):
    save_ip_cache(ip_cache)
    save_stats(src_stats)
    rewrite_sources_with_comments(src_stats)

    if all_valid:
        selected = distribute_configs(all_valid, settings["MAX_CONFIGS"])
        write_output(selected)
        label = "partial" if interrupted else "final"
        print_log(f"{'─' * 60}")
        print_log(
            f"Tested {settings.get('MAX_TESTCOUNT', '?')} | Valid pool: {len(all_valid)} | Saved {len(selected)} {label} configs to {OUTPUT_FILE}.")
    else:
        print_log("No valid configurations found.")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    print_log(f"{'═' * 60}")
    print_log(f"  V2Ray Config Tester  v{APP_VERSION}")
    print_log(f"{'═' * 60}")

    settings = load_or_create_settings()
    ip_cache = load_ip_cache()
    src_stats = load_stats()
    ip_cache_lock = asyncio.Lock()
    result_lock = asyncio.Lock()

    global_stop = asyncio.Event()
    _graceful.set_global_event(global_stop)

    print_log(
        f"Target: {settings['MAX_CONFIGS']} output configs from testing up to {settings.get('MAX_TESTCOUNT', settings['MAX_CONFIGS'] * 5)} configs.")

    await check_and_update_xray(settings)

    if not os.path.exists(SOURCE_FILE):
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write(
                "# Add subscription URLs below\n"
                "# Lines starting with # are ignored\n"
                "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/all_sub.txt\n"
            )
        print_log(f"Created {SOURCE_FILE}. Add sources and run again.")
        sys.exit()

    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        raw_links_all = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

    seen_norm: set = set()
    raw_links: list = []
    for url in raw_links_all:
        norm = url.strip().lower()
        if norm in seen_norm:
            print_log(f"⚠  Duplicate source URL skipped: {url}")
            continue
        seen_norm.add(norm)
        raw_links.append(url)

    source_links = get_sorted_source_links(raw_links, src_stats)

    if src_stats:
        print_log("Source order this run (sorted by previous score):")
        for lnk in source_links:
            rec = _latest_record(src_stats, lnk)
            if rec:
                print_log(f"  [{rec['score_percent']}% | {rec['status']}] {lnk}")
            else:
                print_log(f"  [NEW] {lnk}")

    all_valid: list = []
    run_stats = {"tested": 0}
    global_seen_identities: set = set()

    try:
        for link in source_links:
            if global_stop.is_set():
                break

            print_log(f"{'─' * 60}")
            print_log(f"Fetching: {link}")
            raw_text = None
            reachable = False

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(link, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status == 200:
                            raw_text = await resp.text()
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
            total_in_source = len(configs)

            if not configs:
                update_source_stat(src_stats, link, 0, 0, reachable)
                print_log("No valid configs found. Skipping...")
                continue

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
                print_log(f"All {total_in_source} configs already seen in this run (duplicates). Skipping...")
                continue

            print_log(
                f"Loaded {total_in_source} configs "
                f"({skipped_dupes} already seen, {len(fresh_configs)} new). "
                f"Starting batch workers..."
            )

            # دسته‌بندی کانفیگ‌ها به بلاک‌های ۵۰ تایی برای پردازش موازی
            BATCH_SIZE = 50
            chunks = [fresh_configs[i:i + BATCH_SIZE] for i in range(0, len(fresh_configs), BATCH_SIZE)]

            queue = asyncio.Queue()
            source_valid = {"valid": 0}
            for ch in chunks:
                queue.put_nowait(ch)

            stop_event = asyncio.Event()

            async def _watch_global():
                await global_stop.wait()
                stop_event.set()

            watcher = asyncio.create_task(_watch_global())

            # تعداد ورکرها را بهینه می‌کنیم تا از فشار بیش از حد به سیستم جلوگیری شود
            active_workers = min(settings.get("MAX_WORKERS", 10), len(chunks), 20)
            if active_workers == 0: active_workers = 1

            tasks = [
                asyncio.create_task(
                    worker_batch(queue, all_valid, i, settings, stop_event,
                                 ip_cache, ip_cache_lock, result_lock,
                                 run_stats, source_valid)
                )
                for i in range(active_workers)
            ]
            await asyncio.gather(*tasks)
            watcher.cancel()

            update_source_stat(src_stats, link, total_in_source, source_valid["valid"], reachable)
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