#!/usr/bin/env python3
import asyncio
import hmac
import json
import logging
import re
import secrets
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_PATH   = Path("/opt/gateway-ui/config")
TOKEN_PATH    = Path("/etc/gateway-ui/token")
GW_CONFIG_DIR = Path("/opt/gateway/config")
GW_ENV        = Path("/opt/gateway/config.env")
STATIC_DIR    = Path(__file__).parent / "static"
GW_VERSION       = Path("/etc/gateway-version")
GITHUB_TOKEN_PATH = Path("/etc/gateway-ui/github-token")
OTA_LOG = Path("/var/log/gateway-ota.log")

SERVICE_GROUPS = {
    "helium":    {"label": "Helium",    "units": ["pktfwd.service", "gateway-rs.service"]},
    "wingbits":  {"label": "Wingbits",  "units": ["readsb.service", "wingbits.service"]},
    "tailscale": {"label": "Tailscale", "units": ["tailscaled.service"]},
    "web-ui":    {"label": "Web UI",    "units": ["gateway-ui.service"]},
}

OPTIONAL_SERVICES = {"readsb.service", "wingbits.service"}

ALLOWED_OTA_UNITS = [
    "pktfwd.service", "gateway-rs.service", "gateway-ui.service",
    "readsb.service", "wingbits.service", "tailscaled.service",
]

HELIUM_GW     = "/usr/local/bin/helium_gateway"
HELIUM_CONF   = "/etc/helium_gateway/settings.toml"
HELIUM_CONF2  = "/opt/gateway/config/settings.toml"

_SYSTEMCTL    = "/bin/systemctl"
_APPLY_BAND     = "/opt/gateway/scripts/apply-band.sh"
_APPLY_TZ       = "/opt/gateway/scripts/apply-timezone.sh"
_APPLY_HOSTNAME = "/opt/gateway/scripts/apply-hostname.sh"
_TAILSCALE    = "/usr/bin/tailscale"
_TS_WRAPPER   = "/usr/local/bin/tailscale-wrapper"
_OTA_WRAPPER  = "/usr/local/bin/ota-update-wrapper"
_SYSCTL_W     = "/usr/sbin/sysctl"

BAND_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
WRAPPER_BIN = "/usr/local/bin/wingbits-setup-wrapper"

NTFY_PATH = Path("/etc/gateway-ui/ntfy.json")
POWER_WRAPPER = "/usr/local/bin/system-power-wrapper"
NTFY_URL_RE = re.compile(r"^https?://")
NTFY_TOPIC_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
ALLOWED_ALERT_KEYS = {
    "update_available", "helium_fault", "wingbits_fault",
    "cpu_temp", "ram", "storage", "reboot", "shutdown",
    "tailscale_hostname_mismatch",
}
SHELL_META_RE = re.compile(r"[;&|`$()<>\n\r]")
WINGBITS_DOWNLOAD_URL = "https://gitlab.com/wingbits/config/-/raw/master/download.sh"
LOC_RE  = re.compile(r"^(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)$")
ID_RE   = re.compile(r"^[A-Za-z0-9]{8,32}$")
_wingbits_running = False

TS_KEY_RE = re.compile(r"^tskey(-auth)?-[A-Za-z0-9_-]+")
CIDR_RE   = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$")
ALLOWED_TAILSCALE_UNITS = ["readsb", "wingbits", "tailscaled", "kernel", "sshd", "pktfwd", "gateway-rs"]

# ── Config + Token (loaded at startup) ───────────────────────────────────────

def _load_config() -> dict:
    cfg: dict = {"bind_host": "0.0.0.0", "port": "8080"}
    if CONFIG_PATH.exists():
        for raw in CONFIG_PATH.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    try:
        cfg["port"] = int(cfg["port"])
    except (ValueError, KeyError):
        cfg["port"] = 8080
    return cfg


def _load_token() -> str:
    if not TOKEN_PATH.exists():
        print(f"ERROR: {TOKEN_PATH} not found. Run first-boot.sh to generate.", file=sys.stderr)
        sys.exit(1)
    t = TOKEN_PATH.read_text().strip()
    if not t:
        print(f"ERROR: {TOKEN_PATH} is empty.", file=sys.stderr)
        sys.exit(1)
    return t


CONFIG: dict = _load_config()
TOKEN: str   = _load_token()

def _load_gateway_version() -> str:
    try:
        return GW_VERSION.read_text().strip() or "dev"
    except Exception:
        return "dev"

GATEWAY_VERSION: str = _load_gateway_version()

# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    task = asyncio.create_task(_ntfy_notifier())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Gateway UI", docs_url=None, redoc_url=None, lifespan=_app_lifespan)


@app.middleware("http")
async def enforce_body_limit(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl and int(cl) > 1024:
            return JSONResponse(status_code=413, content={"detail": "Request too large"})
    return await call_next(request)


def _require_auth(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    provided = auth[7:]
    if not hmac.compare_digest(provided.encode(), TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


Auth = Annotated[None, Depends(_require_auth)]

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace("{{ version }}", GATEWAY_VERSION)
    return HTMLResponse(html)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _run_async(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        rc = proc.returncode
        return (rc if rc is not None else -1,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"))
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"not found: {cmd[0]}"
    except Exception as exc:
        return -1, "", str(exc)


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"not found: {cmd[0]}"
    except Exception as exc:
        return -1, "", str(exc)


def _env_value(key: str) -> str:
    if not GW_ENV.exists():
        return ""
    for line in GW_ENV.read_text().splitlines():
        s = line.strip()
        if s.startswith(f"{key}="):
            return s[len(key) + 1:].strip()
    return ""


def _service_info(unit: str) -> dict:
    rc, out, _ = _run(["systemctl", "is-active", unit])
    state = out.strip() or ("active" if rc == 0 else "inactive")
    _, ts_out, _ = _run(
        ["systemctl", "show", unit, "--property=ActiveEnterTimestamp", "--value"]
    )
    return {"unit": unit, "state": state, "since": ts_out.strip()}


def _service_group_status(group_key: str) -> dict:
    g = SERVICE_GROUPS.get(group_key)
    if not g:
        return {"label": group_key, "active": 0, "total": 0, "units": [], "group_state": "optional"}
    units = []
    active_count = 0
    for u in g["units"]:
        if _service_installed(u):
            info = _service_info(u)
            raw = info["state"]
            if raw == "active":
                info["state"] = "active"
            elif u in OPTIONAL_SERVICES:
                info["state"] = "optional"
            else:
                info["state"] = "inactive"
        else:
            info = {"unit": u, "since": ""}
            info["state"] = "optional" if u in OPTIONAL_SERVICES else "inactive"
        if info["state"] == "active":
            active_count += 1
        units.append(info)

    unit_states = [u["state"] for u in units]
    if all(s == "active" for s in unit_states):
        group_state = "active"
    elif any(s == "inactive" for s in unit_states):
        group_state = "fault"
    else:
        group_state = "optional"

    return {
        "label": g["label"],
        "active": active_count,
        "total": len(g["units"]),
        "group_state": group_state,
        "units": units,
    }


def _service_installed(unit: str) -> bool:
    rc, _, _ = _run(["systemctl", "cat", unit])
    return rc == 0


def _write_config(updates: dict) -> None:
    current: dict = {}
    if CONFIG_PATH.exists():
        for raw in CONFIG_PATH.read_text().splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            current[k.strip()] = v.strip()
    current.update({str(k): str(v) for k, v in updates.items()})
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("".join(f"{k}={v}\n" for k, v in current.items()))
    CONFIG.update(updates)
    if "port" in updates:
        CONFIG["port"] = int(str(updates["port"]))


async def _restart_after(unit: str, delay: float = 0.8) -> None:
    await asyncio.sleep(delay)
    _run(["sudo", _SYSTEMCTL, "restart", unit])


# ── NTFY ───────────────────────────────────────────────────────────────────────

def _load_ntfy_config() -> dict:
    if not NTFY_PATH.exists():
        return {}
    try:
        return json.loads(NTFY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


async def send_ntfy(title: str, message: str, priority: str = "default", tags: list[str] | None = None) -> bool:
    config = _load_ntfy_config()
    server = config.get("server", "")
    topic = config.get("topic", "")
    token = config.get("token", "")

    if not server or not topic:
        return False

    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": ",".join(tags) if tags else "",
        "Content-Type": "text/plain",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, content=message, headers=headers)
            r.raise_for_status()
        return True
    except Exception as exc:
        logging.error("NTFY send failed: %s", exc)
        return False


def _current_version() -> str:
    if GW_VERSION.exists():
        return GW_VERSION.read_text().strip() or "unknown"
    return "unknown"


# ── Dashboard / System Info ──────────────────────────────────────────────────

@app.get("/api/identity")
def api_identity(_: Auth):
    result: dict = {"key": "", "name": "", "eui": "", "region": ""}
    for conf in (HELIUM_CONF, HELIUM_CONF2):
        if Path(conf).exists():
            rc, out, _ = _run([HELIUM_GW, "-c", conf, "key", "info"])
            if rc == 0:
                try:
                    info = json.loads(out)
                    result["key"]  = info.get("key", "")
                    result["name"] = info.get("name", "")
                except json.JSONDecodeError:
                    pass
            break
    try:
        mac = Path("/sys/class/net/eth0/address").read_text().strip().replace(":", "").upper()
        result["eui"] = f"{mac[:6]}FFFE{mac[6:]}"
    except Exception:
        pass
    result["region"] = _env_value("BAND") or "unknown"
    return result


@app.get("/api/status")
def api_status(_: Auth):
    services = ["pktfwd", "gateway-rs", "readsb", "wingbits", "tailscaled"]
    result = {}
    for s in services:
        unit = f"{s}.service"
        if _service_installed(unit):
            result[s] = _service_info(unit)
        else:
            result[s] = {"unit": unit, "state": "not-installed", "since": ""}
    return result


@app.get("/api/status/groups")
def api_status_groups(_: Auth):
    return {
        key: _service_group_status(key)
        for key in SERVICE_GROUPS
    }


@app.get("/api/sysinfo")
def api_sysinfo(_: Auth):
    cpu_raw = None
    try:
        raw = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
        cpu = f"{raw / 1000:.1f} °C"
        cpu_raw = round(raw / 1000, 1)
    except Exception:
        cpu = "unavailable"

    uptime = "unavailable"
    uptime_raw = None
    try:
        raw = Path("/proc/uptime").read_text().split()[0]
        uptime_raw = float(raw)
        secs = int(uptime_raw)
        if secs < 60:
            uptime = "just now"
        else:
            mins = secs // 60
            if mins < 60:
                uptime = f"{mins}m"
            else:
                hrs = mins // 60
                days = hrs // 24
                if days:
                    uptime = f"{days}d {hrs % 24}h"
                else:
                    uptime = f"{hrs}h"
    except Exception:
        pass

    _, mem_out, _ = _run(["free", "-m"])
    mem_str = mem_out.strip() or "unavailable"

    mem_pct = None
    m = re.search(r"^Mem:\s+(\d+)\s+(\d+)", mem_str, re.MULTILINE)
    if m:
        total, used = int(m.group(1)), int(m.group(2))
        if total > 0:
            mem_pct = round((used / total) * 100)

    _, disk_out, _ = _run(["df", "-h", "/opt"])
    disk_str = disk_out.strip() or "unavailable"

    disk_pct = None
    lines = disk_str.splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            try:
                disk_pct = int(parts[4].rstrip("%"))
            except (ValueError, TypeError):
                pass

    ts_mismatch, sys_hostname, ts_hostname_actual, ts_mismatch_type = _check_tailscale_hostname_mismatch()
    result = {
        "cpu_temp":      cpu,
        "memory":        mem_str,
        "disk":          disk_str,
        "uptime":        uptime,
        "uptime_raw":    uptime_raw,
        "hostname":      socket.gethostname(),
        "cpu_temp_raw":  cpu_raw,
        "mem_used_pct":  mem_pct,
        "disk_used_pct": disk_pct,
        "tailscale_hostname_mismatch": ts_mismatch,
        "tailscale_hostname_mismatch_type": ts_mismatch_type,
    }
    if ts_mismatch:
        result["tailscale_hostname_actual"] = ts_hostname_actual
    return result


@app.get("/api/beacon")
def api_beacon(_: Auth):
    from datetime import datetime, timezone, timedelta

    rc, out, _ = _run(
        ["journalctl", "-u", "gateway-rs", "-n", "500", "--no-pager", "--output=short-iso"],
        timeout=15,
    )
    lines = out.splitlines() if rc == 0 else []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    last_beacon: dict | None = None
    next_beacon: str | None = None
    witness_count = 0

    for line in reversed(lines):
        ll = line.lower()
        if last_beacon is None and "transmit" in ll and "beacon" in ll:
            m = re.match(r"^(\S+)", line)
            last_beacon = {"timestamp": m.group(1) if m else "", "line": line.strip()}
        if next_beacon is None and "next beacon" in ll:
            m = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[+-]\d{4})?)", line)
            if m:
                next_beacon = m.group(1)

    for line in lines:
        if "witness" in line.lower():
            m = re.match(r"^(\S+)", line)
            if m:
                try:
                    ts = datetime.fromisoformat(m.group(1).replace("+0000", "+00:00"))
                    if ts >= cutoff:
                        witness_count += 1
                except ValueError:
                    pass

    return {
        "last_beacon":       last_beacon,
        "next_beacon":       next_beacon,
        "witness_count_24h": witness_count,
    }


# ── Band / Region ────────────────────────────────────────────────────────────

@app.get("/api/bands")
def api_bands(_: Auth):
    prefix, suffix = "global_conf.", ".json"
    regions = sorted(
        p.name[len(prefix):-len(suffix)]
        for p in GW_CONFIG_DIR.glob(f"{prefix}*{suffix}")
        if BAND_RE.fullmatch(p.name[len(prefix):-len(suffix)])
    )
    return {"regions": regions, "current": _env_value("BAND")}


@app.post("/api/band")
async def api_set_band(_: Auth, request: Request):
    body = await request.json()
    region = str(body.get("region", ""))
    if not BAND_RE.fullmatch(region):
        raise HTTPException(status_code=400, detail="Invalid region name")
    if not (GW_CONFIG_DIR / f"global_conf.{region}.json").exists():
        raise HTTPException(status_code=400, detail="Unknown region")
    rc, out, err = _run(["sudo", _APPLY_BAND, region], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "apply-band failed")
    return {"ok": True, "output": out}


# ── Timezone ──────────────────────────────────────────────────────────────────

TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_/+\-]+$")

_timezone_cache: list[str] | None = None


def _list_timezones() -> list[str]:
    global _timezone_cache
    if _timezone_cache is not None:
        return _timezone_cache
    rc, out, _ = _run(["timedatectl", "list-timezones"], timeout=10)
    if rc != 0:
        return []
    _timezone_cache = sorted(out.strip().splitlines())
    return _timezone_cache


@app.get("/api/timezones")
def api_timezones(_: Auth):
    return {"timezones": _list_timezones(), "current": _env_value("TIMEZONE") or "Etc/UTC"}


@app.post("/api/timezone")
async def api_set_timezone(_: Auth, request: Request):
    body = await request.json()
    tz = str(body.get("timezone", ""))
    if not TZ_RE.fullmatch(tz):
        raise HTTPException(status_code=400, detail="Invalid timezone format")
    if not Path(f"/usr/share/zoneinfo/{tz}").exists():
        raise HTTPException(status_code=400, detail="Unknown timezone")
    rc, out, err = _run(["sudo", _APPLY_TZ, tz], timeout=15)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "apply-timezone failed")
    return {"ok": True, "output": out}


# ── Hostname ──────────────────────────────────────────────────────────────────

HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")


def _tailscale_hostname() -> str | None:
    if not Path(_TAILSCALE).exists():
        return None
    rc, out, _ = _run([_TAILSCALE, "status", "--json"])
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, OSError):
        return None
    self_info = data.get("Self", {})
    if not isinstance(self_info, dict):
        return None
    dns_name = self_info.get("DNSName", "").rstrip(".")
    if not dns_name:
        return None
    dot_idx = dns_name.find(".")
    return dns_name[:dot_idx] if dot_idx != -1 else dns_name


@app.get("/api/hostname")
def api_get_hostname(_: Auth):
    return {
        "hostname": socket.gethostname(),
        "tailscale_name": _tailscale_hostname(),
    }


@app.post("/api/hostname")
async def api_set_hostname(_: Auth, request: Request):
    body = await request.json()
    name = str(body.get("hostname", "")).strip()
    if not HOSTNAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid hostname — must be alphanumeric and hyphens, not start or end with a hyphen")
    if len(name) > 63:
        raise HTTPException(status_code=400, detail="Hostname must be 1–63 characters")
    rc, out, err = _run(["sudo", _APPLY_HOSTNAME, name], timeout=15)
    if rc == 0:
        return {"ok": True, "partial": False, "output": out}
    elif rc == 2:
        return JSONResponse(
            status_code=200,
            content={"ok": False, "partial": True, "output": out, "detail": err or "Tailscale rename failed — OS hostname was changed"}
        )
    else:
        raise HTTPException(status_code=500, detail=err or "hostname change failed")


# ── Service Restart ──────────────────────────────────────────────────────────

@app.post("/api/restart/{service}")
def api_restart(_: Auth, service: str):
    allowed = {"pktfwd": "pktfwd.service", "gateway-rs": "gateway-rs.service"}
    if service not in allowed:
        raise HTTPException(status_code=400, detail="Unknown service")
    rc, _, err = _run(["sudo", _SYSTEMCTL, "restart", allowed[service]])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "restart failed")
    return {"ok": True}


# ── Wingbits ─────────────────────────────────────────────────────────────────

CORRUPTION_RE = re.compile(r"--net-beast-reduce-interval=[0-9.]+--")


def _check_readsb_corruption() -> str | None:
    """Check if readsb is crash-looping due to known Wingbits auto-config bug."""
    if not _service_installed("readsb.service"):
        return None
    info = _service_info("readsb.service")
    if info["state"] == "active":
        return None
    rc, out, _ = _run(
        ["journalctl", "-u", "readsb.service", "-n", "50", "--no-pager", "--output=cat"],
        timeout=10,
    )
    if rc != 0:
        return None
    if CORRUPTION_RE.search(out):
        return (
            "readsb is crash-looping due to a known issue in the Wingbits "
            "client's auto-configuration step corrupting its startup arguments. "
            "This is an upstream Wingbits bug, not specific to this device. "
            "ADS-B/Wingbits data is not being transmitted until this is resolved upstream."
        )
    return None


@app.get("/api/wingbits")
def api_wingbits(_: Auth):
    readsb_installed   = _service_installed("readsb.service")
    wingbits_installed = _service_installed("wingbits.service")
    readsb_info   = _service_info("readsb.service")   if readsb_installed   else {"unit": "readsb.service",   "state": "not-installed", "since": ""}
    wingbits_info = _service_info("wingbits.service") if wingbits_installed else {"unit": "wingbits.service", "state": "not-installed", "since": ""}
    readsb_info["diagnostic"] = _check_readsb_corruption()
    return {"readsb": readsb_info, "wingbits": wingbits_info}


def _parse_wingbits_cmd(cmd: str) -> tuple[str, str]:
    if not cmd:
        raise HTTPException(status_code=400, detail="Install command is required")

    if len(cmd) > 4096:
        raise HTTPException(status_code=413, detail="Install command too long")

    cmd = cmd.strip()

    if not cmd:
        raise HTTPException(status_code=400, detail="Install command is required")

    if WINGBITS_DOWNLOAD_URL not in cmd:
        raise HTTPException(
            status_code=400,
            detail="This doesn't look like a Wingbits install command — please paste the full command from your dashboard's Install Station page.",
        )

    loc_m = re.search(r'loc="([^"]*)"', cmd)
    id_m  = re.search(r'id="([^"]*)"', cmd)

    if not loc_m or not id_m:
        raise HTTPException(
            status_code=400,
            detail="Could not find loc=\"...\" and id=\"...\" in the pasted command — please paste the full install command from your dashboard.",
        )

    loc_val = loc_m.group(1)
    id_val  = id_m.group(1)

    if SHELL_META_RE.search(loc_val) or SHELL_META_RE.search(id_val):
        raise HTTPException(status_code=400, detail="Install command contains invalid characters")

    m = LOC_RE.match(loc_val)
    if not m:
        raise HTTPException(status_code=400, detail="Invalid location format — expected loc=\"<lat>, <lon>\" with decimal numbers, e.g. loc=\"-37.6, 143.8\"")
    lat = float(m.group(1))
    lon = float(m.group(2))
    if not (-90 <= lat <= 90):
        raise HTTPException(status_code=400, detail="Latitude out of range (-90 to 90)")
    if not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Longitude out of range (-180 to 180)")

    if not ID_RE.match(id_val):
        raise HTTPException(status_code=400, detail="Invalid station ID format — expected alphanumeric, 8-32 characters")

    return loc_val, id_val


@app.post("/api/wingbits/setup")
async def api_wingbits_setup(_: Auth, request: Request):
    global _wingbits_running

    if not Path(WRAPPER_BIN).exists():
        raise HTTPException(status_code=503, detail="Setup wrapper not installed — run install-wingbits-deps.sh")

    if _wingbits_running:
        raise HTTPException(status_code=409, detail="Setup already in progress")

    body = await request.json()
    cmd = str(body.get("cmd", ""))
    loc_val, id_val = _parse_wingbits_cmd(cmd)

    _wingbits_running = True

    async def event_stream():
        global _wingbits_running
        try:
            proc = await asyncio.create_subprocess_exec(
                WRAPPER_BIN, "--loc", loc_val, "--id", id_val,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                yield f"data: {line.decode('utf-8', errors='replace').rstrip()}\n\n"
            exit_code = await proc.wait()
            yield f"data: {json.dumps({'exit_code': exit_code})}\n\n"
        finally:
            _wingbits_running = False

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Network — Interfaces ─────────────────────────────────────────────────────

@app.get("/api/network/interfaces")
def api_network_interfaces(_: Auth):
    result = {}
    for iface in ("eth0", "wlan0"):
        info: dict = {"name": iface, "link": "Down", "ipv4": "", "ipv6": "", "mac": ""}
        path = Path(f"/sys/class/net/{iface}")
        if not path.exists():
            info["link"] = "N/A"
            result[iface] = info
            continue
        op = path / "operstate"
        if op.exists():
            info["link"] = op.read_text().strip()
            if info["link"] == "up":
                info["link"] = "Up"
            elif info["link"] == "down":
                info["link"] = "Down"
        mac_path = path / "address"
        if mac_path.exists():
            info["mac"] = mac_path.read_text().strip()
        # Get IPs from ip addr
        rc, out, _ = _run(["ip", "addr", "show", iface])
        if rc == 0:
            for line in out.splitlines():
                m4 = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
                if m4 and not line.strip().startswith("valid_lft"):
                    info["ipv4"] = m4.group(1)
                m6 = re.search(r"inet6 ([0-9a-f:]+/\d+)", line)
                if m6:
                    info["ipv6"] = m6.group(1)
        # SSID for wlan0
        if iface == "wlan0":
            rc2, ssid_out, _ = _run(["iwgetid", "-r", iface])
            info["ssid"] = ssid_out.strip() if rc2 == 0 else "N/A"
            rc3, nm_out, _ = _run(["/usr/bin/nmcli", "radio", "wifi"])
            if rc3 == 0:
                info["wifi_enabled"] = nm_out.strip().lower() == "enabled"
            else:
                info["wifi_enabled"] = None
        result[iface] = info
    return result


# ── Network — WiFi Toggle ────────────────────────────────────────────────────

WIFI_WRAPPER = "/usr/local/bin/wifi-toggle-wrapper"
WIFI_CONNECT_WRAPPER = "/usr/local/bin/wifi-connect-wrapper"


@app.post("/api/network/wifi")
async def api_network_wifi(_: Auth, request: Request):
    if not Path(WIFI_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="wifi-toggle-wrapper not installed")
    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="enabled must be a boolean")
    action = "on" if enabled else "off"
    rc, out, err = await _run_async([WIFI_WRAPPER, action], timeout=10)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out.strip() or f"wifi toggle failed")
    return {"wifi_enabled": enabled}


# ── Network — WiFi Scan ──────────────────────────────────────────────────────

@app.get("/api/network/wifi/scan")
async def api_network_wifi_scan(_: Auth):
    if not Path("/usr/bin/nmcli").exists():
        return {"available": False, "networks": []}
    rc, radio_out, _ = await _run_async(["/usr/bin/nmcli", "radio", "wifi"], timeout=5)
    wifi_disabled = not (rc == 0 and radio_out.strip().lower() == "enabled")
    rc2, dev_out, _ = await _run_async(["/usr/bin/nmcli", "device", "status"], timeout=5)
    wlan_present = "wlan0" in (dev_out if rc2 == 0 else "")
    if not wlan_present:
        wifi_disabled = True
    if wifi_disabled:
        return {"available": False, "networks": []}

    await _run_async(["/usr/bin/nmcli", "device", "wifi", "rescan"], timeout=15)
    rc3, out, _ = await _run_async(
        ["/usr/bin/nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
        timeout=15,
    )
    if rc3 != 0:
        return {"available": True, "networks": []}

    seen: dict[str, dict] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        ssid = parts[0] if len(parts) > 0 else ""
        if not ssid:
            continue
        try:
            sig = int(parts[1]) if len(parts) > 1 else 0
            security = parts[2] if len(parts) > 2 else ""
            entry = seen.get(ssid)
            if entry is None or sig > entry["signal"]:
                seen[ssid] = {"signal": sig, "open": (security == "")}
        except (ValueError, IndexError):
            pass

    networks = [
        {"ssid": ssid, "signal": e["signal"], "open": e["open"]}
        for ssid, e in sorted(seen.items(), key=lambda x: -x[1]["signal"])
    ]
    return {"available": True, "networks": networks}


# ── Network — WiFi Saved ─────────────────────────────────────────────────────

@app.get("/api/network/wifi/saved")
def api_network_wifi_saved(_: Auth):
    if not Path("/usr/bin/nmcli").exists():
        return {"saved": []}
    rc, out, _ = _run(
        ["/usr/bin/nmcli", "-t", "-f", "NAME,TYPE,TIMESTAMP", "connection", "show"],
        timeout=10,
    )
    if rc != 0:
        return {"saved": []}
    saved = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        name = parts[0] if len(parts) > 0 else ""
        ctype = parts[1] if len(parts) > 1 else ""
        if not ("wireless" in ctype.lower() or "wifi" in ctype.lower()):
            continue
        ts_str = parts[2] if len(parts) > 2 else ""
        try:
            timestamp = int(ts_str) if ts_str else 0
        except ValueError:
            timestamp = 0
        saved.append({"name": name, "type": ctype, "timestamp": timestamp})
    return {"saved": saved}


# ── Network — WiFi Connect ───────────────────────────────────────────────────

WIFI_UNKNOWN_CONNECTION_RE = re.compile(r"(unknown connection|cannot delete unknown)", re.IGNORECASE)


def _friendly_wifi_error(raw: str) -> str:
    if "802-11-wireless-security.psk" in raw and "property is invalid" in raw:
        return ("Password must be 8\u201363 characters (or a 64-character hex key). "
                "Please check the password and try again.")
    if "Secrets were required" in raw and "not provided" in raw:
        return ("Connection failed \u2014 this usually means the password was incorrect, "
                "but it can also happen if the network is out of range or temporarily "
                "unavailable. Please check the password and try again.")
    if "CONNECT:FAILED:profile creation failed" in raw:
        return "Failed to save network settings. Please try again."
    cleaned = raw.strip()
    if len(cleaned) > 300:
        cleaned = cleaned[:300] + "\u2026"
    return f"Connection failed: {cleaned}"


@app.post("/api/network/wifi/connect")
async def api_network_wifi_connect(_: Auth, request: Request):
    if not Path(WIFI_CONNECT_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="wifi-connect-wrapper not installed")
    body = await request.json()
    ssid = str(body.get("ssid", "")).strip()
    password = str(body.get("password", ""))
    if not ssid:
        raise HTTPException(status_code=422, detail="ssid is required")
    if len(ssid) > 128:
        raise HTTPException(status_code=422, detail="ssid too long")
    if len(password) > 128:
        raise HTTPException(status_code=422, detail="password too long")
    rc, out, err = await _run_async(
        [WIFI_CONNECT_WRAPPER, "connect", ssid, password], timeout=35,
    )
    if rc != 0:
        raw = f"{out.strip()} {err.strip()}".strip()
        detail = _friendly_wifi_error(raw or "connection failed")
        raise HTTPException(status_code=500, detail=detail)
    return {"ok": True}


@app.post("/api/network/wifi/connect-saved")
async def api_network_wifi_connect_saved(_: Auth, request: Request):
    if not Path(WIFI_CONNECT_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="wifi-connect-wrapper not installed")
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if len(name) > 128:
        raise HTTPException(status_code=422, detail="name too long")
    rc, out, err = await _run_async(
        [WIFI_CONNECT_WRAPPER, "connect-saved", name], timeout=35,
    )
    if rc != 0:
        raw = f"{out.strip()} {err.strip()}".strip()
        detail = _friendly_wifi_error(raw or "connection failed")
        raise HTTPException(status_code=500, detail=detail)
    return {"ok": True}


@app.post("/api/network/wifi/forget")
async def api_network_wifi_forget(_: Auth, request: Request):
    if not Path(WIFI_CONNECT_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="wifi-connect-wrapper not installed")
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if len(name) > 128:
        raise HTTPException(status_code=422, detail="name too long")
    rc, out, err = await _run_async(
        [WIFI_CONNECT_WRAPPER, "forget", name], timeout=15,
    )
    if rc != 0:
        raw = f"{out.strip()} {err.strip()}".strip()
        detail = _friendly_wifi_error(raw or "forget failed")
        raise HTTPException(status_code=500, detail=detail)
    return {"ok": True}


# ── Network — Tailscale ──────────────────────────────────────────────────────

def _check_tailscale_hostname_mismatch() -> tuple[bool, str, str | None, str]:
    system_hostname = socket.gethostname()
    if not Path(_TAILSCALE).exists():
        return False, system_hostname, None, ""
    if not _service_installed("tailscaled.service"):
        return False, system_hostname, None, ""
    ts_info = _service_info("tailscaled.service")
    if ts_info["state"] != "active":
        return False, system_hostname, None, ""
    try:
        rc, out, _ = _run([_TAILSCALE, "status", "--json"])
        if rc != 0:
            return False, system_hostname, None, ""
        data = json.loads(out)
    except (json.JSONDecodeError, OSError):
        return False, system_hostname, None, ""
    self_info = data.get("Self", {})
    if not isinstance(self_info, dict):
        return False, system_hostname, None, ""
    dns_name = self_info.get("DNSName", "").rstrip(".")
    if not dns_name:
        return False, system_hostname, None, ""
    dot_idx = dns_name.find(".")
    ts_hostname = dns_name[:dot_idx] if dot_idx != -1 else dns_name
    if not ts_hostname:
        return False, system_hostname, None, ""

    # Suffix-collision detection (re-flash duplicate): Tailscale name is
    # "sensecap-abc123-2" where the base "sensecap-abc123" matches system hostname.
    m = re.match(r"^(.+)-\d+$", ts_hostname)
    if m and m.group(1) == system_hostname:
        return True, system_hostname, ts_hostname, "suffix"

    # General drift detection: names simply disagree with no suffix pattern.
    if ts_hostname != system_hostname:
        return True, system_hostname, ts_hostname, "drift"

    return False, system_hostname, None, ""


@app.get("/api/network/tailscale")
def api_network_tailscale(_: Auth):
    if not Path("/usr/bin/tailscale").exists():
        return {"status": "not-installed"}

    ts_installed = _service_installed("tailscaled.service")
    if not ts_installed:
        return {"status": "not-installed"}

    ts_info = _service_info("tailscaled.service")
    if ts_info["state"] != "active":
        return {"status": "stopped", "service": ts_info}

    rc, out, _ = _run([_TAILSCALE, "status", "--json"])
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        if rc != 0:
            return {"status": "error", "detail": out.strip() or "tailscale status failed"}
        return {"status": "error", "detail": "failed to parse tailscale status"}

    backend_state = str(data.get("BackendState") or "")

    # Version — parse first line of `tailscale version`
    version = "unknown"
    rc4, ver_out, _ = _run([_TAILSCALE, "version"])
    if rc4 == 0:
        version = ver_out.splitlines()[0].strip() if ver_out.strip() else "unknown"

    if backend_state == "NeedsLogin":
        # Logged out — typically the machine record was deleted from the
        # admin console. Surface the interactive re-auth URL (preserves all
        # prefs, no key entry) and whether the persisted key exists, so the
        # frontend can explain that tailscale-autoconnect will retry.
        return {
            "status": "needs-login",
            "backend_state": backend_state,
            "version": version,
            "auth_url": str(data.get("AuthURL") or ""),
            "auto_reauth_key_present": Path("/etc/gateway/tailscale.key").exists(),
        }

    if backend_state and backend_state != "Running":
        # NoState/Starting (still dialling control) or Stopped (tailscale down)
        return {
            "status": "starting" if backend_state in ("NoState", "Starting") else "disconnected",
            "backend_state": backend_state,
            "version": version,
        }

    self_info = data.get("Self", {})
    online = self_info.get("Online", False)
    ips = self_info.get("TailscaleIPs", [])
    ip = ips[0] if ips else ""
    hostname = self_info.get("DNSName", "").rstrip(".")

    # Check advertised routes and SSH from debug prefs
    advertised = []
    ssh_enabled = False
    rc3, prefs_out, _ = _run([_TAILSCALE, "debug", "prefs"])
    if rc3 == 0:
        try:
            prefs = json.loads(prefs_out)
            raw = prefs.get("AdvertiseRoutes") or prefs.get("AdvertisedRoutes") or []
            if isinstance(raw, list):
                advertised = [str(r) for r in raw]
            elif isinstance(raw, str) and raw:
                advertised = [raw]
            ssh_enabled = bool(prefs.get("RunSSH", False))
        except (json.JSONDecodeError, AttributeError):
            pass

    ts_mismatch, _, ts_hostname_actual, ts_mismatch_type = _check_tailscale_hostname_mismatch()
    result = {
        "status": "connected",
        "connected": True,
        "backend_state": backend_state,
        "online": online,
        "ip": ip,
        "ips": ips,
        "hostname": hostname,
        "version": version,
        "subnet_routing_enabled": bool(advertised),
        "advertised_routes": advertised,
        "ssh_enabled": ssh_enabled,
        "tailscale_hostname_mismatch": ts_mismatch,
        "tailscale_hostname_mismatch_type": ts_mismatch_type,
    }
    if ts_mismatch:
        result["tailscale_hostname_actual"] = ts_hostname_actual
    return result


TS_KEY_VALID_RE = re.compile(r"^tskey(-auth)?-[A-Za-z0-9_-]+$")


@app.post("/api/network/tailscale/connect")
async def api_tailscale_connect(_: Auth, request: Request):
    body = await request.json()
    key = str(body.get("key", "")).strip()

    if not TS_KEY_VALID_RE.match(key):
        raise HTTPException(status_code=400, detail="Invalid auth key format — must start with tskey- or tskey-auth-")

    if not Path(_TS_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="tailscale-wrapper not installed — run install-tailscale.sh")

    rc, out, err = await _run_async([_TS_WRAPPER, "auth", key], timeout=30)

    # Scrub key from any output
    out_clean = out.replace(key, "[REDACTED]")
    err_clean = err.replace(key, "[REDACTED]")

    if rc != 0:
        raise HTTPException(status_code=500, detail=err_clean or out_clean or "tailscale auth failed")

    return {"ok": True, "output": out_clean}


@app.post("/api/network/tailscale/routes")
async def api_tailscale_routes(_: Auth, request: Request):
    body = await request.json()
    subnets_str = str(body.get("subnets", "")).strip()

    if not Path(_TS_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="tailscale-wrapper not installed — run install-tailscale.sh")

    if subnets_str:
        parts = [s.strip() for s in subnets_str.split(",") if s.strip()]
        for p in parts:
            if not CIDR_RE.match(p):
                raise HTTPException(status_code=400, detail=f"Invalid CIDR: {p}")

    rc, out, err = await _run_async([_TS_WRAPPER, "set-routes", subnets_str], timeout=30)

    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out or "tailscale routes failed")

    # Persist ip_forward setting when routes are set
    if subnets_str:
        sysctl_conf = Path("/etc/sysctl.d/99-tailscale.conf")
        sysctl_conf.write_text("net.ipv4.ip_forward=1\n")
        _run([_SYSCTL_W, "net.ipv4.ip_forward=1"])

    return {"ok": True, "output": out}


@app.post("/api/network/tailscale/ssh")
async def api_tailscale_ssh(_: Auth, request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled", False))

    if not Path(_TS_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="tailscale-wrapper not installed — run install-tailscale.sh")

    val = "on" if enabled else "off"
    rc, out, err = await _run_async([_TS_WRAPPER, "set-ssh", val], timeout=30)

    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out or "tailscale ssh failed")

    return {"ok": True, "output": out}


# ── System / Version ──────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com/repos/bitcryptic-gw/sensecap-m1-gateway/releases/latest"


def _load_github_token() -> str | None:
    try:
        return GITHUB_TOKEN_PATH.read_text().strip() or None
    except Exception:
        return None


VERSION_SUFFIX_RE = re.compile(r"-\d+-g[0-9a-f]+$")


def _normalise_version(v: str) -> str:
    """Strip git describe dirty suffix like -6-g19e8b0f for comparison."""
    return VERSION_SUFFIX_RE.sub("", v)


@app.get("/api/system/version")
async def api_system_version(_: Auth):
    local = "unknown"
    if GW_VERSION.exists():
        local = GW_VERSION.read_text().strip() or "unknown"

    result = {
        "local": local,
        "latest": None,
        "update_available": False,
        "check_failed": False,
        "release_url": None,
        "release_notes": None,
    }

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            gh_headers = {}
            gh_token = _load_github_token()
            if gh_token:
                gh_headers["Authorization"] = f"Bearer {gh_token}"
            r = await client.get(GITHUB_API, headers=gh_headers)
            if r.status_code == 200:
                data = r.json()
                tag = data.get("tag_name", "")
                latest_ver = tag.lstrip("v") if tag else ""
                if latest_ver:
                    result["latest"] = tag
                    result["release_url"] = data.get("html_url")
                    result["release_notes"] = data.get("body", "")[:5000]
                    if local and local != "unknown":
                        normalised = _normalise_version(local)
                        try:
                            local_parts = tuple(int(p) for p in normalised.lstrip("v").split("."))
                            latest_parts = tuple(int(p) for p in latest_ver.split("."))
                            max_len = max(len(local_parts), len(latest_parts))
                            result["update_available"] = (latest_parts + (0,) * (max_len - len(latest_parts))) > (local_parts + (0,) * (max_len - len(local_parts)))
                        except (ValueError, AttributeError):
                            logging.warning("version comparison failed: local=%s latest=%s", local, latest_ver)
                else:
                    logging.warning("GitHub API returned 200 but no usable tag_name (api_system_version)")
                    result["check_failed"] = True
            else:
                logging.warning("GitHub API returned %d (api_system_version)", r.status_code)
                result["check_failed"] = True
    except Exception as exc:
        logging.warning("GitHub API check failed (api_system_version): %s", exc)
        result["check_failed"] = True

    return result


# ── System / OTA ──────────────────────────────────────────────────────────────

OTA_CHANGE_MAP = [
    ("gateway-ui/",              "Web UI",   ["gateway-ui.service"]),
    ("pktfwd/",                  "Helium",   ["pktfwd.service"]),
    ("config/global_conf.",      "Helium",   ["pktfwd.service"]),
    ("config/settings.toml",     "Helium",   ["gateway-rs.service"]),
    ("systemd/gateway-rs",       "Helium",   ["gateway-rs.service"]),
    ("systemd/pktfwd",           "Helium",   ["pktfwd.service"]),
    ("scripts/wingbits",         "Wingbits", ["readsb.service", "wingbits.service"]),
    ("systemd/readsb",           "Wingbits", ["readsb.service"]),
    ("systemd/wingbits",         "Wingbits", ["wingbits.service"]),
    ("systemd/tailscale",        "Tailscale", ["tailscaled.service"]),
    ("scripts/tailscale",        "Tailscale", ["tailscaled.service"]),
    ("scripts/install-tailscale","Tailscale", ["tailscaled.service"]),
    ("scripts/ota-update",       "Web UI",   ["gateway-ui.service"]),
]


def _map_changed_files(changed: list[str]) -> tuple[list[dict], list[str]]:
    groups: dict[str, dict] = {}
    boot_changes = []

    for f in changed:
        if f.startswith("boot/"):
            boot_changes.append(f)
            continue

        matched = False
        for prefix, label, services in OTA_CHANGE_MAP:
            if f.startswith(prefix):
                if label not in groups:
                    groups[label] = {"label": label, "services": services, "changed_files": []}
                groups[label]["changed_files"].append(f)
                matched = True
                break

        if not matched:
            # Default to Web UI
            if "Web UI" not in groups:
                groups["Web UI"] = {"label": "Web UI", "services": ["gateway-ui.service"], "changed_files": []}
            groups["Web UI"]["changed_files"].append(f)

    return list(groups.values()), boot_changes


@app.get("/api/system/ota/changes")
async def api_system_ota_changes(_: Auth):
    if not Path(_OTA_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="ota-update-wrapper not installed")

    rc, out, err = await _run_async([_OTA_WRAPPER, "--changes"], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=503, detail=err or out or "git fetch failed — no network?")

    changed = [f.strip() for f in out.splitlines() if f.strip()]

    affected_groups, boot_changes = _map_changed_files(changed)
    latest_ver = _current_version()
    # Try to get latest from GitHub
    latest_tag = "unknown"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            gh_headers = {}
            gh_token = _load_github_token()
            if gh_token:
                gh_headers["Authorization"] = f"Bearer {gh_token}"
            r = await client.get(GITHUB_API, headers=gh_headers)
            if r.status_code == 200:
                latest_tag = r.json().get("tag_name", "unknown")
    except Exception:
        pass

    return {
        "affected_groups": affected_groups,
        "boot_changes": boot_changes,
        "current_version": latest_ver,
        "latest_version": latest_tag,
    }


_ota_running = False


@app.post("/api/system/ota/update")
async def api_system_ota_update(_: Auth, request: Request):
    global _ota_running

    body = await request.json()
    raw = body.get("services", [])
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="services must be a non-empty list")

    services = [str(s).strip() for s in raw]
    for s in services:
        if s not in ALLOWED_OTA_UNITS:
            raise HTTPException(status_code=400, detail=f"Invalid service: {s}")

    if not Path(_OTA_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="ota-update-wrapper not installed")

    if _ota_running:
        raise HTTPException(status_code=409, detail="Update already in progress")

    _ota_running = True

    svc_arg = ",".join(services)

    async def event_stream():
        global _ota_running
        log_fh = None
        try:
            log_fh = open(OTA_LOG, "a", buffering=1)
            log_fh.write(f"\n=== OTA update started: {datetime.now(timezone.utc).isoformat()} ===\n")
            proc = await asyncio.create_subprocess_exec(
                _OTA_WRAPPER, svc_arg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            version = None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                log_fh.write(decoded + "\n")
                if decoded.startswith("VERSION:"):
                    version = decoded[len("VERSION:"):]
                yield f"data: {decoded}\n\n"
            exit_code = await proc.wait()
            log_fh.write(f"=== OTA update finished: {datetime.now(timezone.utc).isoformat()} (exit {exit_code}) ===\n")
            event = {"exit_code": exit_code}
            if version:
                event["version"] = version
            yield f"data: {json.dumps(event)}\n\n"
        finally:
            if log_fh:
                log_fh.close()
            _ota_running = False

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/system/ota/log")
def api_system_ota_log(_: Auth):
    if not OTA_LOG.exists():
        return PlainTextResponse("")
    size = OTA_LOG.stat().st_size
    offset = max(0, size - 51200)
    with open(OTA_LOG) as f:
        f.seek(offset)
        # Skip partial first line if seeking into the middle
        if offset > 0:
            f.readline()
        return PlainTextResponse(f.read())


# ── System / Power ─────────────────────────────────────────────────────────────

@app.post("/api/system/reboot")
async def api_system_reboot(_: Auth):
    if not Path(POWER_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="system-power-wrapper not installed")

    config = _load_ntfy_config()
    if config.get("server") and config.get("topic") and "reboot" in config.get("enabled_alerts", []):
        await send_ntfy(
            "Gateway Rebooting",
            f"{socket.gethostname()} is rebooting. Triggered via web UI.",
            "default",
            ["arrows_counterclockwise", "sensecap"],
        )

    rc, _, err = await _run_async([POWER_WRAPPER, "reboot"])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "reboot failed")
    return {"ok": True}


@app.post("/api/system/shutdown")
async def api_system_shutdown(_: Auth):
    if not Path(POWER_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="system-power-wrapper not installed")

    config = _load_ntfy_config()
    if config.get("server") and config.get("topic") and "shutdown" in config.get("enabled_alerts", []):
        await send_ntfy(
            "Gateway Shutting Down",
            f"{socket.gethostname()} is shutting down. Triggered via web UI.",
            "high",
            ["stop_sign", "sensecap"],
        )

    rc, _, err = await _run_async([POWER_WRAPPER, "poweroff"])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "shutdown failed")
    return {"ok": True}


# ── Notifications / NTFY ──────────────────────────────────────────────────────

@app.get("/api/notifications/config")
def api_ntfy_config_get(_: Auth):
    cfg = _load_ntfy_config()
    has_token = bool(cfg.get("token"))
    return {
        "server": cfg.get("server", ""),
        "topic": cfg.get("topic", ""),
        "token": "",
        "token_set": has_token,
        "enabled_alerts": cfg.get("enabled_alerts", list(ALLOWED_ALERT_KEYS)),
    }


@app.post("/api/notifications/config")
async def api_ntfy_config_set(_: Auth, request: Request):
    body = await request.json()
    server = str(body.get("server", "")).strip()
    topic = str(body.get("topic", "")).strip()
    token = str(body.get("token", ""))
    enabled_alerts = body.get("enabled_alerts", list(ALLOWED_ALERT_KEYS))

    if not server:
        raise HTTPException(status_code=422, detail="server is required")
    if not NTFY_URL_RE.match(server):
        raise HTTPException(status_code=422, detail="server must be a valid HTTP/HTTPS URL")
    if not topic:
        raise HTTPException(status_code=422, detail="topic is required")
    if not NTFY_TOPIC_RE.match(topic):
        raise HTTPException(status_code=422, detail="topic must be alphanumeric with hyphens/underscores")
    if not isinstance(enabled_alerts, list):
        raise HTTPException(status_code=422, detail="enabled_alerts must be a list")
    invalid = [k for k in enabled_alerts if k not in ALLOWED_ALERT_KEYS]
    if invalid:
        raise HTTPException(status_code=422, detail=f"unknown alert keys: {invalid}")

    # If token is empty string and we have a saved token, keep the existing one
    current = _load_ntfy_config()
    if not token and current.get("token"):
        token = current["token"]

    NTFY_PATH.parent.mkdir(parents=True, exist_ok=True)
    NTFY_PATH.write_text(json.dumps({
        "server": server,
        "topic": topic,
        "token": token,
        "enabled_alerts": enabled_alerts,
    }, indent=2) + "\n")
    # Ensure permissions
    try:
        NTFY_PATH.chmod(0o640)
    except OSError:
        pass

    return {"ok": True}


@app.post("/api/notifications/test")
async def api_ntfy_test(_: Auth):
    config = _load_ntfy_config()
    if not config.get("server") or not config.get("topic"):
        raise HTTPException(status_code=400, detail="NTFY not configured — set server and topic first")

    ok = await send_ntfy(
        "Gateway Test Notification",
        f"NTFY is configured correctly for {socket.gethostname()}.",
        "default",
        ["white_check_mark", "sensecap"],
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send test notification — check server/topic")
    return {"ok": True}


# ── NTFY background notifier ──────────────────────────────────────────────────

_ntfy_state: dict = {
    "helium_fault": None,
    "wingbits_fault": None,
    "cpu_temp_alert": None,
    "ram_alert": None,
    "storage_alert": None,
    "last_update_version": None,
    "tailscale_hostname_mismatch": None,
}
_ntfy_first_run: bool = True


async def _ntfy_notifier():
    global _ntfy_first_run, _ntfy_state

    while True:
        try:
            config = _load_ntfy_config()
            server = config.get("server", "")
            topic = config.get("topic", "")
            enabled_alerts = set(config.get("enabled_alerts", []))

            if not server or not topic:
                await asyncio.sleep(60)
                continue

            hostname = socket.gethostname()

            # ── Gather sysinfo ─────────────────────────────────────────────
            cpu_raw = None
            try:
                raw = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
                cpu_raw = round(raw / 1000, 1)
            except Exception:
                pass

            _, mem_out, _ = _run(["free", "-m"])
            mem_pct = None
            m = re.search(r"^Mem:\s+(\d+)\s+(\d+)", mem_out or "", re.MULTILINE)
            if m:
                total, used = int(m.group(1)), int(m.group(2))
                if total > 0:
                    mem_pct = round((used / total) * 100)

            _, disk_out, _ = _run(["df", "-h", "/opt"])
            disk_pct = None
            lines = (disk_out or "").splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    try:
                        disk_pct = int(parts[4].rstrip("%"))
                    except (ValueError, TypeError):
                        pass

            # ── Group statuses ────────────────────────────────────────────
            helium_status = _service_group_status("helium")
            wingbits_status = _service_group_status("wingbits")

            # ── Version check ─────────────────────────────────────────────
            update_available = False
            latest_version = None
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    gh_headers = {}
                    gh_token = _load_github_token()
                    if gh_token:
                        gh_headers["Authorization"] = f"Bearer {gh_token}"
                    r = await client.get(GITHUB_API, headers=gh_headers)
                    if r.status_code == 200:
                        data = r.json()
                        tag = data.get("tag_name", "")
                        latest_ver = tag.lstrip("v") if tag else ""
                        if latest_ver:
                            latest_version = tag
                            local = _current_version()
                            if local and local != "unknown":
                                normalised = _normalise_version(local)
                                try:
                                    local_parts = tuple(int(p) for p in normalised.lstrip("v").split("."))
                                    latest_parts = tuple(int(p) for p in latest_ver.split("."))
                                    max_len = max(len(local_parts), len(latest_parts))
                                    update_available = (latest_parts + (0,) * (max_len - len(latest_parts))) > (local_parts + (0,) * (max_len - len(local_parts)))
                                except (ValueError, AttributeError):
                                    pass
                    else:
                        logging.warning("GitHub API returned %d (ntfy_notifier)", r.status_code)
            except Exception as exc:
                logging.warning("GitHub API check failed (ntfy_notifier): %s", exc)

            # ── First run: set baseline, no alerts ────────────────────────
            if _ntfy_first_run:
                if update_available and latest_version:
                    _ntfy_state["last_update_version"] = latest_version
                if cpu_raw is not None:
                    _ntfy_state["cpu_temp_alert"] = cpu_raw >= 75.0
                if mem_pct is not None:
                    _ntfy_state["ram_alert"] = mem_pct >= 90
                if disk_pct is not None:
                    _ntfy_state["storage_alert"] = disk_pct >= 90

                hgs = helium_status.get("group_state")
                _ntfy_state["helium_fault"] = (hgs == "fault")

                wgs = wingbits_status.get("group_state")
                if wgs != "optional":
                    _ntfy_state["wingbits_fault"] = (wgs == "fault")

                ts_mismatch, _, _, _ = _check_tailscale_hostname_mismatch()
                _ntfy_state["tailscale_hostname_mismatch"] = ts_mismatch

                _ntfy_first_run = False
                await asyncio.sleep(60)
                continue

            # ── Check: update_available ───────────────────────────────────
            if "update_available" in enabled_alerts and update_available and latest_version:
                if latest_version != _ntfy_state["last_update_version"]:
                    local = _current_version()
                    await send_ntfy(
                        "Gateway Update Available",
                        f"Version {latest_version} is available. Current: {local}. Open Settings to update.",
                        "default",
                        ["arrow_up", "sensecap"],
                    )
                    _ntfy_state["last_update_version"] = latest_version

            # ── Check: helium_fault ───────────────────────────────────────
            if "helium_fault" in enabled_alerts:
                current_hf = (helium_status.get("group_state") == "fault")
                if current_hf != _ntfy_state["helium_fault"]:
                    if current_hf:
                        await send_ntfy(
                            "Helium Offline",
                            f"Helium services are not running on {hostname}.",
                            "high",
                            ["red_circle", "helium"],
                        )
                    else:
                        await send_ntfy(
                            "Helium Online",
                            f"Helium services restored on {hostname}.",
                            "default",
                            ["green_circle", "helium"],
                        )
                    _ntfy_state["helium_fault"] = current_hf

            # ── Check: wingbits_fault ─────────────────────────────────────
            if "wingbits_fault" in enabled_alerts:
                wgs = wingbits_status.get("group_state")
                if wgs != "optional":
                    current_wf = (wgs == "fault")
                    if current_wf != _ntfy_state["wingbits_fault"]:
                        if current_wf:
                            await send_ntfy(
                                "Wingbits Offline",
                                f"Wingbits services are not running on {hostname}.",
                                "high",
                                ["red_circle", "wingbits"],
                            )
                        else:
                            await send_ntfy(
                                "Wingbits Online",
                                f"Wingbits services restored on {hostname}.",
                                "default",
                                ["green_circle", "wingbits"],
                            )
                        _ntfy_state["wingbits_fault"] = current_wf

            # ── Check: tailscale_hostname_mismatch ─────────────────────────
            if "tailscale_hostname_mismatch" in enabled_alerts:
                ts_mismatch, sys_hostname, ts_hostname, ts_mismatch_type = _check_tailscale_hostname_mismatch()
                if ts_mismatch != _ntfy_state["tailscale_hostname_mismatch"]:
                    if ts_mismatch:
                        if ts_mismatch_type == "suffix":
                            detail = (
                                f"System hostname: {sys_hostname}\n"
                                f"Tailscale hostname: {ts_hostname}\n"
                                f"Device was likely re-flashed — Tailscale auto-renamed it.\n"
                                f"Remove stale duplicate entries: https://login.tailscale.com/admin/machines"
                            )
                        else:
                            detail = (
                                f"System hostname: {sys_hostname}\n"
                                f"Tailscale hostname: {ts_hostname}\n"
                                f"Hostnames do not match — check Tailscale configuration."
                            )
                        await send_ntfy(
                            "Tailscale Hostname Mismatch",
                            detail,
                            "high",
                            ["warning", "tailscale"],
                        )
                    else:
                        await send_ntfy(
                            "Tailscale Hostname Resolved",
                            f"Hostname mismatch resolved on {sys_hostname}.",
                            "default",
                            ["white_check_mark", "tailscale"],
                        )
                    _ntfy_state["tailscale_hostname_mismatch"] = ts_mismatch

            # ── Check: cpu_temp ───────────────────────────────────────────
            if "cpu_temp" in enabled_alerts and cpu_raw is not None:
                current_cpu_alert = cpu_raw >= 75.0
                recovered = cpu_raw < 70.0
                if current_cpu_alert and _ntfy_state["cpu_temp_alert"] is not True:
                    await send_ntfy(
                        "CPU Temperature Alert",
                        f"CPU temp is {cpu_raw}°C on {hostname} (threshold: 75°C).",
                        "high",
                        ["thermometer", "sensecap"],
                    )
                    _ntfy_state["cpu_temp_alert"] = True
                elif recovered and _ntfy_state["cpu_temp_alert"] is not False:
                    await send_ntfy(
                        "CPU Temperature Normal",
                        f"CPU temp has recovered to {cpu_raw}°C on {hostname}.",
                        "default",
                        ["thermometer", "sensecap"],
                    )
                    _ntfy_state["cpu_temp_alert"] = False

            # ── Check: ram ────────────────────────────────────────────────
            if "ram" in enabled_alerts and mem_pct is not None:
                current_ram_alert = mem_pct >= 90
                recovered = mem_pct < 85
                if current_ram_alert and _ntfy_state["ram_alert"] is not True:
                    await send_ntfy(
                        "Memory Alert",
                        f"RAM usage is {mem_pct}% on {hostname} (threshold: 90%).",
                        "high",
                        ["warning", "sensecap"],
                    )
                    _ntfy_state["ram_alert"] = True
                elif recovered and _ntfy_state["ram_alert"] is not False:
                    await send_ntfy(
                        "Memory Normal",
                        f"RAM usage has recovered to {mem_pct}% on {hostname}.",
                        "default",
                        ["white_check_mark", "sensecap"],
                    )
                    _ntfy_state["ram_alert"] = False

            # ── Check: storage ────────────────────────────────────────────
            if "storage" in enabled_alerts and disk_pct is not None:
                current_disk_alert = disk_pct >= 90
                recovered = disk_pct < 85
                if current_disk_alert and _ntfy_state["storage_alert"] is not True:
                    await send_ntfy(
                        "Storage Alert",
                        f"Disk usage is {disk_pct}% on {hostname} (threshold: 90%).",
                        "high",
                        ["warning", "sensecap"],
                    )
                    _ntfy_state["storage_alert"] = True
                elif recovered and _ntfy_state["storage_alert"] is not False:
                    await send_ntfy(
                        "Storage Normal",
                        f"Disk usage has recovered to {disk_pct}% on {hostname}.",
                        "default",
                        ["white_check_mark", "sensecap"],
                    )
                    _ntfy_state["storage_alert"] = False

        except Exception as exc:
            logging.error("NTFY notifier error: %s", exc)

        await asyncio.sleep(60)


# ── Logs ─────────────────────────────────────────────────────────────────────

UNIT_MAP = {
    "system":    [],
    "helium":    ["pktfwd.service", "gateway-rs.service"],
    "wingbits":  ["readsb.service", "wingbits.service"],
    "tailscale": ["tailscaled.service"],
}


@app.get("/api/logs")
def api_logs(_: Auth, units: str = ""):
    if units:
        selected = [u.strip() for u in units.split(",") if u.strip()]
        has_system = "system" in selected
        unit_args = []
        for s in selected:
            if s == "system":
                continue
            if s in UNIT_MAP:
                unit_args.extend(UNIT_MAP[s])
            elif s in ALLOWED_TAILSCALE_UNITS:
                unit_args.append(f"{s}.service")
        if has_system:
            unit_args = []
        if not unit_args:
            unit_args = ["gateway-rs.service"]
    else:
        unit_args = ["gateway-rs.service"]

    cmd = ["journalctl", "-n", "200", "--no-pager", "--output=short-iso"]
    for u in unit_args:
        cmd += ["-u", u]

    rc, out, _ = _run(cmd, timeout=15)
    return {"lines": out.splitlines() if rc == 0 else []}


# ── Settings ─────────────────────────────────────────────────────────────────

@app.get("/api/settings")
def api_get_settings(_: Auth):
    return {
        "port":      CONFIG.get("port", 8080),
        "bind_host": CONFIG.get("bind_host", "0.0.0.0"),
    }


@app.get("/api/settings/token")
def api_get_token(_: Auth):
    t = TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else ""
    masked = (t[:4] + "••••••••" + t[-4:]) if len(t) >= 8 else "••••••••"
    return {"masked": masked, "full": t}


@app.post("/api/settings/token")
async def api_regen_token(_: Auth, bg: BackgroundTasks):
    new_token = secrets.token_hex(32)
    TOKEN_PATH.write_text(new_token + "\n")
    bg.add_task(_restart_after, "gateway-ui")
    return {"ok": True, "token": new_token}


@app.post("/api/settings/port")
async def api_set_port(_: Auth, request: Request, bg: BackgroundTasks):
    body = await request.json()
    try:
        port = int(body.get("port", 8080))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid port")
    if not 1024 <= port <= 65535:
        raise HTTPException(status_code=400, detail="Port must be 1024–65535")
    _write_config({"port": str(port)})
    bg.add_task(_restart_after, "gateway-ui")
    return {"ok": True, "port": port}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=str(CONFIG.get("bind_host", "0.0.0.0")),
        port=int(CONFIG.get("port", 8080)),
        log_level="info",
        timeout_graceful_shutdown=3,
    )
