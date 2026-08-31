#!/usr/bin/env python3
import asyncio
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import subprocess
import sys
import time
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

# Written by scripts/helium-hardware-check.sh (ExecCondition on
# pktfwd.service / gateway-rs.service) when Helium concentrator hardware is
# present; removed when absent. Read here to distinguish a hardware-absent
# device (Helium "not configured") from a genuine service fault.
HELIUM_HW_MARKER = Path("/run/gateway/helium-hardware-present")

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
DEPIN_WRAPPER = "/usr/local/bin/depin-config-wrapper"
DEPIN_UNINSTALL = "/opt/gateway/scripts/depin-uninstall.sh"
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

DEPIN_PROJECTS = ["honeygain", "urnetwork", "myst", "anyone", "mastchain"]
DEPIN_PROJECT_RE = re.compile(r"^(honeygain|urnetwork|myst|anyone|mastchain)$")
DEPIN_DEVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")
DEPIN_NICKNAME_RE = re.compile(r"^[a-zA-Z0-9]{1,19}$")
DEPIN_CONFIG_REQUIRED = {"honeygain", "urnetwork", "anyone", "mastchain"}
DEPIN_ENV_DIR = Path("/etc/gateway-ui/depin")
DEPIN_HEALTH_STATE_DIR = Path("/var/lib/gateway-ui")
DEPIN_ANONRC = Path("/var/lib/gateway-ui/anyone/etc/anonrc")
DEPIN_URNETWORK_JWT = Path("/var/lib/gateway-ui/urnetwork/jwt")
DEPIN_UPDATE_STATE = Path("/var/lib/gateway-ui/depin-update-state.json")
DEPIN_AUTO_UPDATE = Path("/var/lib/gateway-ui/depin-auto-update.json")
DEPIN_NOTIFY_PENDING = Path("/var/lib/gateway-ui/depin-notify-pending")

DEPIN_IMAGES = {
    "honeygain": "honeygain/honeygain:latest",
    "urnetwork": "bringyour/community-provider:g4-latest",
    "myst": "mysteriumnetwork/myst:latest",
    "anyone": "ghcr.io/anyone-protocol/ator-protocol:latest",
    "mastchain": "ghcr.io/c-man-the-man/mastchain-ais:latest",
}

# Sysfs RTL-SDR presence probe shared with the unit's ExecCondition= and the
# MastChain card's live no-hardware / one-dongle rendering.
MASTCHAIN_HW_CHECK = "/opt/gateway/scripts/mastchain-hardware-check.sh"

_depin_check_running = False
_depin_restart_running = False

DEPIN_LOG_LINES = 50

DEPIN_HEALTH_PATTERNS = {
    "honeygain": {
        "connected": re.compile(r"successfully connected|connected", re.IGNORECASE),
        "disconnected": re.compile(r"disconnected|connection lost|error", re.IGNORECASE),
    },
    "urnetwork": {
        "active": re.compile(r"client_id:|pool\[\d+\]", re.IGNORECASE),
        "inactive": re.compile(r"init proxy auth failed|fatal|error", re.IGNORECASE),
    },
    "myst": {
        "active": re.compile(r"keepalive ping|Sending P2P message|new session|session established", re.IGNORECASE),
        "inactive": re.compile(r"not registered|error|no sessions|disconnected", re.IGNORECASE),
    },
    "anyone": {
        "healthy": re.compile(r"\[notice\]", re.IGNORECASE),
        "unhealthy": re.compile(r"\[err\]|\[warn\]", re.IGNORECASE),
    },
    "mastchain": {
        # UNVERIFIED CANDIDATES (design Decision #5) — runtime log format has not
        # been confirmed (no hardware/account available). These are best-guess
        # signals from recon + upstream docs, pending the tester's real logs:
        #   active   — fork keep-alive/heartbeat posts + successful HTTP upload
        #              (response printed by default)
        #   inactive — "No devices available" (source-confirmed in the fork), or
        #              an HTTP 401/403 auth failure from the upload endpoint
        # DO NOT treat either as settled until the tester captures real output.
        "active": re.compile(r"keepalive|keep-alive|heartbeat|HTTP/1\.[01] 200", re.IGNORECASE),
        "inactive": re.compile(r"No devices available|HTTP/1\.[01] 40[13]|Unauthorized", re.IGNORECASE),
    },
}

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


# tar1090 map — served on a fixed path/port across deployments (Wingbits
# installs readsb + tar1090 via wiedehopf's scripts; host is the only
# variable part, derived from the request context).
TAR1090_PATH = "/tar1090/"
TAR1090_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)


def _tar1090_url(request: Request) -> str:
    host = (request.url.hostname or "").strip().rstrip(".")
    if not host or len(host) > 253 or not TAR1090_HOST_RE.fullmatch(host):
        return "#"
    return f"http://{host}{TAR1090_PATH}"


# Wingbits dashboard — per-station URL. The Wingbits install (download.sh)
# writes the station/device ID to /etc/wingbits/device, and its completion
# message links to https://wingbits.com/dashboard/stations/<device_id>. The
# formats accepted are the ones Wingbits' own debug.sh validates: animal-name
# words, an 18-char uppercase hex serial, or a UUID. Missing/unregistered ->
# inert "#". "not-wingbits-device" is the display sentinel wb-config uses when
# no device file exists — it matches the animal-name shape but is not a real
# station ID, so it must also fall back to "#".
WINGBITS_DEVICE_PATH = Path("/etc/wingbits/device")
WINGBITS_DASHBOARD_URL = "https://wingbits.com/dashboard/stations/{id}?active=map"
WINGBITS_DEVICE_RE = re.compile(
    r"^(?:[a-z]+-[a-z]+-[a-z]+"
    r"|[0-9A-F]{18}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
WINGBITS_DEVICE_SENTINELS = {"not-wingbits-device", "none"}


def _wingbits_dashboard_url() -> str:
    try:
        device_id = WINGBITS_DEVICE_PATH.read_text().strip()
    except OSError:
        return "#"
    if (not device_id
            or device_id in WINGBITS_DEVICE_SENTINELS
            or not WINGBITS_DEVICE_RE.fullmatch(device_id)):
        return "#"
    return WINGBITS_DASHBOARD_URL.format(id=device_id)


# Live stats — read-only local values with graceful degradation. Reads are
# guarded against missing files, partial/malformed data, and services that
# have not started yet; any failure yields a "—" display value, never a crash.
# Aircraft count comes from readsb's own JSON output; the satellite count is
# read from the GeoSigner via `wingbits status`, which needs root, so it goes
# through the setuid wingbits-status-wrapper.
READSB_AIRCRAFT_JSON = Path("/run/readsb/aircraft.json")
WINGBITS_STATUS_WRAPPER = "/usr/local/bin/wingbits-status-wrapper"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
WINGBITS_SAT_RE = re.compile(r"Geosigner:\s*OK\s*\((\d+)\s+satellites?\)", re.IGNORECASE)


def _aircraft_tracked_count() -> str:
    try:
        data = json.loads(READSB_AIRCRAFT_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return "—"
    aircraft = data.get("aircraft") if isinstance(data, dict) else None
    if not isinstance(aircraft, list):
        return "—"
    return str(len(aircraft))


def _satellites_in_view_count() -> str:
    if not Path(WINGBITS_STATUS_WRAPPER).exists():
        return "—"
    rc, out, _ = _run([WINGBITS_STATUS_WRAPPER], timeout=10)
    if rc != 0:
        return "—"
    m = WINGBITS_SAT_RE.search(_ANSI_RE.sub("", out or ""))
    if not m:
        return "—"
    return m.group(1)


@app.get("/", include_in_schema=False)
def index(request: Request):
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace("{{ version }}", GATEWAY_VERSION)
    html = html.replace("{{ tar1090_url }}", _tar1090_url(request))
    html = html.replace("{{ wingbits_dashboard_url }}", _wingbits_dashboard_url())
    html = html.replace("{{ aircraft_count }}", _aircraft_tracked_count())
    html = html.replace("{{ satellites_count }}", _satellites_in_view_count())
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

    # Hardware-absent override: on a device with no Helium concentrator
    # (no ATECC608A at i2c-1:0x60), pktfwd/gateway-rs are condition-skipped
    # by ExecCondition and sit inactive forever. Report the Helium group as
    # "not_configured" rather than "fault" so the UI and ntfy do not present
    # a spurious fault for hardware that was never attached. This is an
    # early-exit override only — the hardware-present path below is
    # unchanged.
    if group_key == "helium" and not HELIUM_HW_MARKER.exists():
        units = [
            {"unit": u, "state": "not_configured", "since": ""}
            for u in g["units"]
        ]
        return {
            "label": g["label"],
            "active": 0,
            "total": len(g["units"]),
            "group_state": "not_configured",
            "units": units,
        }

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
        logging.error("NTFY send failed: %s: %r", type(exc).__name__, exc)
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


def _tailscale_fqdn() -> str | None:
    """Return the full Tailscale DNS name (e.g. sensecap-8397f8.myth-nessie.ts.net)."""
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
    return dns_name or None


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


@app.get("/api/wingbits/stats")
def api_wingbits_stats(_: Auth):
    return {
        "aircraft_count":   _aircraft_tracked_count(),
        "satellites_count": _satellites_in_view_count(),
    }


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

    # Whether the device has any active Tailscale auth (tagged key present, or
    # a user-authenticated connection). Drives the Connect-button gate: pasting
    # a key over existing auth is unreliable, so the UI disables Connect (and
    # the backend rejects) whenever this is true.
    auth_active = _tailscale_auth_active()

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
            "auth_active": auth_active,
        }

    if backend_state and backend_state != "Running":
        # NoState/Starting (still dialling control) or Stopped (tailscale down)
        return {
            "status": "starting" if backend_state in ("NoState", "Starting") else "disconnected",
            "backend_state": backend_state,
            "version": version,
            "auth_active": auth_active,
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
        "auth_active": auth_active,
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

    if _tailscale_auth_active():
        raise HTTPException(
            status_code=409,
            detail="This device already has an active Tailscale connection, so pasting a new key over it would be unreliable. Use Clear Tailscale auth first, then paste a fresh key.",
        )

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


# ── Network — Tailscale user re-authentication (interactive/browser login) ───

TS_REAUTH_STATE = Path("/var/lib/gateway-ui/tailscale-reauth.json")
TS_REAUTH_PID   = Path("/var/lib/gateway-ui/tailscale-reauth.pid")
TS_REAUTH_LOG   = Path("/var/lib/gateway-ui/tailscale-reauth.log")
TS_REAUTH_WINDOW_DEFAULT = 480   # seconds (8 min) — tunable, see config
TS_REAUTH_WINDOW_MIN     = 120
TS_REAUTH_WINDOW_MAX     = 3600
TS_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _lan_ipv4_subnets() -> list:
    """IPv4 subnets of the gateway's physical LAN interfaces (eth0/wlan0).

    Deliberately excludes loopback, tailscale0, and virtual bridge/container
    interfaces (docker0, veth*, br-*, virbr*): only a request whose source IP
    falls inside a real physical-LAN subnet is treated as LAN-origin. Returns
    empty list on failure (gate fails closed)."""
    subnets: list = []
    rc, out, _ = _run(["ip", "-4", "-o", "addr", "show"])
    if rc != 0:
        return subnets
    for line in out.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+([0-9.]+/\d+)", line)
        if not m:
            continue
        iface, cidr = m.group(1), m.group(2)
        if iface in ("lo", "tailscale0") or iface.startswith("tailscale"):
            continue
        if iface.startswith(("docker", "veth", "br-", "virbr", "vmbr", "vlan")):
            continue
        try:
            subnets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return subnets


def _request_is_lan(request: Request) -> tuple[bool, str]:
    """LAN-origin gate for the reauth trigger.

    Returns (True, "") when the request source is the physical LAN (or
    loopback), else (False, reason). Any source in Tailscale's CGNAT range
    100.64.0.0/10 is tunneled — blocked even though it is technically an
    RFC1918-like address — and so is any source not in a known LAN subnet.

    Spoofing check (Tailscale subnet routing): with the default
    --snat-subnet-routes=true, a remote tailnet peer reaching the gateway's
    LAN IP via a subnet router is source-NAT'd to the router's own tailnet IP
    (100.x), so it can never present a LAN source to this socket. Traffic from
    a peer is always seen as 100.x here. The one residual gap would be a
    third-party subnet router advertising this exact LAN subnet *and* an
    operator explicitly disabling SNAT — neither applies to this deployment.
    """
    host = request.client.host if request.client else ""
    if not host:
        return False, "no client address"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False, f"unparseable source {host!r}"
    if ip.is_loopback:
        return True, ""
    if ip.version == 4 and ip in TS_TAILSCALE_CGNAT:
        return False, f"source {host} is a Tailscale address (tunneled session)"
    for net in _lan_ipv4_subnets():
        if ip in net:
            return True, ""
    return False, f"source {host} is not on the gateway's LAN"


def _tailscale_auth_url() -> str:
    rc, out, _ = _run([_TAILSCALE, "status", "--json"])
    if rc != 0:
        return ""
    try:
        return str(json.loads(out).get("AuthURL") or "")
    except (json.JSONDecodeError, AttributeError):
        return ""


def _tailscale_status_json() -> dict:
    """Fresh `tailscale status --json` as a dict. Empty on any failure."""
    rc, out, _ = _run([_TAILSCALE, "status", "--json"])
    if rc != 0:
        return {}
    try:
        data = json.loads(out)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, AttributeError):
        return {}


def _tailscale_user_auth_confirmed() -> bool:
    """True when tailscaled reports a running, online, user-owned connection —
    i.e. BackendState==Running, Self.Online==true and no ACL tags, matching the
    watchdog's user_auth_confirmed(). Used to resolve the user-auth-only path
    (no tagged key), which has no watchdog state file to read success from."""
    data = _tailscale_status_json()
    if data.get("BackendState") != "Running":
        return False
    if data.get("Self", {}).get("Online") is not True:
        return False
    if data.get("Self", {}).get("Tags"):
        return False
    return True


TS_TAGGED_KEY = Path("/etc/gateway/tailscale.key")


def _has_tagged_key() -> bool:
    """Whether a persisted tagged auth key exists (and is non-empty)."""
    try:
        return TS_TAGGED_KEY.exists() and TS_TAGGED_KEY.stat().st_size > 0
    except OSError:
        return False


def _tailscale_auth_active() -> bool:
    """Whether the device has any active Tailscale authentication — either a
    persisted tagged key or a currently user-authenticated connection. This is
    the 'has active auth of any kind' predicate used to gate the Connect
    (paste-tagged-key) path: pasting a key over existing auth is unreliable
    (version-dependent `tailscale up --auth-key` behaviour, see the Item 2
    findings), so it is blocked unless the device is in a genuinely clean /
    first-boot state. Deliberately no `--force-reauth` is involved — we make
    the unsafe path unreachable instead of trying to make it safe."""
    if _has_tagged_key():
        return True
    if _tailscale_user_auth_confirmed():
        return True
    return False


def _reauth_log_tail(lines: int = 20) -> str:
    """Tail of the reauth log — used to surface wrapper errors in the API
    response instead of a bare failure."""
    if not TS_REAUTH_LOG.exists():
        return ""
    try:
        return "\n".join(TS_REAUTH_LOG.read_text().splitlines()[-lines:])
    except OSError:
        return ""


def _qr_svg(url: str) -> str | None:
    """Render url as an SVG QR code. Returns None if the optional `qrcode`
    package isn't installed — the UI then falls back to a plain link."""
    if not url:
        return None
    try:
        import qrcode  # lazy: optional dependency, not required to run the UI
        from qrcode.image.svg import SvgPathImage

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        return qr.make_image(image_factory=SvgPathImage).to_string().decode("utf-8")
    except Exception:
        return None


@app.get("/api/network/tailscale/reauth")
def api_tailscale_reauth_status(_: Auth, request: Request):
    """Reauth state, viewable from anywhere (Bearer-auth'd).

    LAN-origin is *reported* so the UI can warn the operator, but only the
    trigger (POST) is gated on it.

    Two regimes share this endpoint:
      * has_tagged_key true  — the watchdog-backed flow: status comes from the
        pending state file (pending / success / fallback / fallback-failed) and
        resolves when the watchdog flips it to a final state.
      * has_tagged_key false — user-auth-only: no tagged key, so no watchdog and
        no state file exist. Status is derived live from tailscaled: 'pending'
        while a login URL is being awaited, 'success' once Running+online with
        no ACL tags, else 'idle'."""
    lan, reason = _request_is_lan(request)
    has_key = _has_tagged_key()

    status = "idle"
    remaining = 0
    window = 0
    if TS_REAUTH_STATE.exists():
        try:
            state = json.loads(TS_REAUTH_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
        status = str(state.get("status", "idle"))
        deadline = int(state.get("deadline", 0) or 0)
        remaining = max(0, deadline - int(time.time())) if status == "pending" else 0
        window = int(state.get("window", 0) or 0)
    elif not has_key:
        auth_url = _tailscale_auth_url()
        if auth_url:
            status = "pending"
        elif _tailscale_user_auth_confirmed():
            status = "success"
        else:
            status = "idle"

    log_tail = ""
    if TS_REAUTH_LOG.exists():
        try:
            log_tail = "\n".join(TS_REAUTH_LOG.read_text().splitlines()[-30:])
        except OSError:
            pass

    auth_url = _tailscale_auth_url() if status == "pending" else ""

    return {
        "status": status,
        "remaining": remaining,
        "window": window,
        "auth_url": auth_url,
        "qr_svg": _qr_svg(auth_url) if auth_url else None,
        "lan_source": lan,
        "lan_reason": reason,
        "log": log_tail,
        "has_tagged_key": has_key,
    }


@app.post("/api/network/tailscale/reauth")
async def api_tailscale_reauth(_: Auth, request: Request):
    """Trigger a user reauthentication (interactive/browser login, no authkey).

    Requires explicit confirmation AND a LAN-origin session. Running the
    atomic `tailscale up --force-reauth` from a tunneled session could sever
    the very connection being used to manage the gateway, so tunneled
    requests are refused here."""
    body = await request.json()
    if body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation required (confirm: true)")

    lan, reason = _request_is_lan(request)
    if not lan:
        raise HTTPException(status_code=403, detail=f"Reauthentication must be initiated from the LAN — {reason}")

    if not Path(_TS_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="tailscale-wrapper not installed — run install-tailscale.sh")

    has_key = _has_tagged_key()
    if has_key and not _service_installed("tailscale-reauth-watchdog.service"):
        raise HTTPException(status_code=503, detail="tailscale-reauth-watchdog.service not installed — re-run sync-provisioning.sh")

    ts_info = _service_info("tailscaled.service")
    if ts_info["state"] != "active":
        raise HTTPException(status_code=503, detail="tailscaled.service is not running — start it before re-authenticating")

    if TS_REAUTH_STATE.exists():
        try:
            st = json.loads(TS_REAUTH_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            st = {}
        if st.get("status") == "pending":
            raise HTTPException(status_code=409, detail="A reauthentication is already in progress")
    elif not has_key and _tailscale_auth_url():
        # No tagged key → no state file/watchdog; a live login URL means a
        # user-auth is still being awaited, so don't stack a second one.
        raise HTTPException(status_code=409, detail="A reauthentication is already in progress")

    try:
        window = int(CONFIG.get("tailscale_reauth_window", TS_REAUTH_WINDOW_DEFAULT))
    except (TypeError, ValueError):
        window = TS_REAUTH_WINDOW_DEFAULT
    window = max(TS_REAUTH_WINDOW_MIN, min(TS_REAUTH_WINDOW_MAX, window))

    # Run the wrapper with its stdout/stderr redirected to the reauth log file
    # (NOT captured as pipes). The wrapper detaches a long-lived `tailscale up
    # --force-reauth` grandchild; if we captured pipes and waited on EOF, that
    # descendant could hold a pipe write-end open and stall the response until
    # the timeout (observed on first use: HTTP 500 {"detail":"timeout"} despite
    # the reauth succeeding in ~0.1s). Waiting on process exit instead — with
    # output going to a file — decouples the response from the detached child.
    #
    # Two wrapper modes:
    #   * has_key  → "reauth": watchdog-backed (pending state file + armed
    #     watchdog that falls back to the saved tagged key on time-out).
    #   * !has_key → "reauth-once": pure user-auth with no tagged key involved;
    #     nothing to revert to, so no watchdog is armed at all.
    subcmd = "reauth" if has_key else "reauth-once"
    TS_REAUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(TS_REAUTH_LOG, "ab")
    try:
        proc = await asyncio.create_subprocess_exec(
            _TS_WRAPPER, subcmd, str(window),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            # The wrapper has 15s to write state + spawn + arm the watchdog;
            # it normally finishes in ~0.1s. If it genuinely stalls, surface
            # whatever it did manage to log rather than a bare timeout.
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=500, detail="tailscale reauth wrapper timed out — see /var/lib/gateway-ui/tailscale-reauth.log")
    finally:
        log_handle.close()

    if rc != 0:
        detail = _reauth_log_tail(20) or "tailscale reauth failed"
        raise HTTPException(status_code=500, detail=detail)

    # Capture the interactive login URL from tailscaled once it drops into
    # NeedsLogin (poll briefly; the known upstream hang is handled by the
    # watchdog when armed, and is otherwise just reported here).
    auth_url = ""
    for _ in range(20):
        auth_url = _tailscale_auth_url()
        if auth_url:
            break
        await asyncio.sleep(0.5)

    return {"ok": True, "auth_url": auth_url, "qr_svg": _qr_svg(auth_url) if auth_url else None,
            "window": window, "status": "pending", "remaining": window,
            "has_tagged_key": has_key}


@app.post("/api/network/tailscale/logout")
async def api_tailscale_logout(_: Auth, request: Request):
    """Clear Tailscale auth — drop the node's authentication locally and reset
    this gateway's auth-tracking state to a genuine first-boot state.

    Requires explicit confirmation AND a LAN-origin session. This is a
    destructive action: it disconnects the device from the tailnet, which
    severs remote access when the UI is being reached over Tailscale itself —
    hence the same LAN-origin gate as the reauth trigger.

    Local-only, reversible logout: the node is deauthenticated locally and the
    persisted tagged key + reauth watchdog state are removed, but the machine
    record is NOT deleted from the tailnet admin console. See the wrapper's
    do_logout() for the rationale (deregistering is an admin-console/API action
    with a larger blast radius)."""
    body = await request.json()
    if body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation required (confirm: true)")

    lan, reason = _request_is_lan(request)
    if not lan:
        raise HTTPException(status_code=403, detail=f"Clearing Tailscale auth must be initiated from the LAN — {reason}")

    if not Path(_TS_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="tailscale-wrapper not installed — run install-tailscale.sh")

    rc, out, err = await _run_async([_TS_WRAPPER, "logout"], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out or "tailscale logout failed")

    return {"ok": True, "output": out}


# ── System / Version ──────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com/repos/bitcryptic-gw/bittug/releases/latest"


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
        logging.warning("GitHub API check failed (api_system_version): %s: %r", type(exc).__name__, exc)
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
                logging.warning("GitHub API check failed (ntfy_notifier): %s: %r", type(exc).__name__, exc)

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

            # ── Check: DePIN image updates ────────────────────────────────
            _depin_check_notifications()

        except Exception as exc:
            logging.error("NTFY notifier error: %s", exc)

        await asyncio.sleep(60)


def _depin_check_notifications() -> None:
    if not DEPIN_NOTIFY_PENDING.exists():
        return
    try:
        DEPIN_NOTIFY_PENDING.unlink()
    except OSError as e:
        logging.warning("Failed to unlink %s: %s", DEPIN_NOTIFY_PENDING, e)
        return

    state = {}
    try:
        if DEPIN_UPDATE_STATE.exists():
            state = json.loads(DEPIN_UPDATE_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return

    hostname = socket.gethostname()
    updated_projects = []
    for proj in DEPIN_PROJECTS:
        pinfo = state.get("projects", {}).get(proj, {})
        if pinfo.get("update_available"):
            digest = pinfo.get("remote_digest", "")
            notified = _ntfy_state.get("depin_notified_digests", {}).get(proj)
            if digest and notified != digest:
                updated_projects.append(proj)
                _ntfy_state.setdefault("depin_notified_digests", {})[proj] = digest

    if not updated_projects:
        return

    label_map = {"honeygain": "Honeygain", "urnetwork": "URnetwork", "myst": "Mysterium", "anyone": "Anyone Protocol"}
    names = [label_map.get(p, p) for p in updated_projects]
    msg = f"DePIN update available: {', '.join(names)}\nRun 'Update' from the DePIN tab on {hostname} to apply."
    asyncio.ensure_future(send_ntfy("DePIN Update Available", msg, "default", ["arrow_up", "docker"]))


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


# ── DePIN ─────────────────────────────────────────────────────────────────────


def _depin_service_unit(project: str) -> str:
    return f"depin-{project}.service"


def _depin_is_configured(project: str) -> bool:
    if project not in DEPIN_CONFIG_REQUIRED:
        return True
    if project == "honeygain":
        return (DEPIN_ENV_DIR / "honeygain.env").exists()
    if project == "urnetwork":
        return DEPIN_URNETWORK_JWT.exists()
    if project == "anyone":
        return DEPIN_ANONRC.exists()
    if project == "mastchain":
        return (DEPIN_ENV_DIR / "mastchain.env").exists()
    return False


def _depin_validate_project(project: str) -> None:
    if not DEPIN_PROJECT_RE.fullmatch(project):
        raise HTTPException(status_code=400, detail=f"Unknown project '{project}' — must be one of: {', '.join(DEPIN_PROJECTS)}")


def _depin_parse_logs(log_lines: list[str], project: str) -> tuple[str, str]:
    patterns = DEPIN_HEALTH_PATTERNS.get(project, {})
    joined = "\n".join(log_lines)
    if not joined.strip():
        # No new log evidence — check persisted last-known-health.
        # Projects like Honeygain are silent when healthy (no periodic
        # log output), so empty logs are normal, not a sign of failure.
        # Persisted state survives gateway-ui restarts and OTA cycles.
        path = DEPIN_HEALTH_STATE_DIR / f"{project}-last-health.json"
        if path.exists():
            try:
                state = json.loads(path.read_text())
                label = state.get("health", "")
                if label in ("connected", "active", "healthy"):
                    return label, ""
            except (json.JSONDecodeError, OSError):
                pass
        return "unknown", ""
    label = "unknown"
    if project == "honeygain":
        if patterns.get("connected") and patterns["connected"].search(joined):
            label = "connected"
        elif patterns.get("disconnected") and patterns["disconnected"].search(joined):
            label = "disconnected"
    elif project == "urnetwork":
        if patterns.get("active") and patterns["active"].search(joined):
            label = "active"
        elif patterns.get("inactive") and patterns["inactive"].search(joined):
            label = "inactive"
    elif project == "myst":
        if patterns.get("active") and patterns["active"].search(joined):
            label = "active"
        elif patterns.get("inactive") and patterns["inactive"].search(joined):
            label = "inactive"
    elif project == "anyone":
        if patterns.get("healthy") and patterns["healthy"].search(joined):
            label = "healthy"
        elif patterns.get("unhealthy") and patterns["unhealthy"].search(joined):
            label = "unhealthy"
    elif project == "mastchain":
        if patterns.get("active") and patterns["active"].search(joined):
            label = "active"
        elif patterns.get("inactive") and patterns["inactive"].search(joined):
            label = "inactive"
    # Persist confirmed healthy states to disk so they survive gateway-ui restarts.
    # Unhealthy/unknown states are never persisted — a broken container will
    # produce log evidence on the next poll, and the absence of persisted state
    # correctly defaults back to "unknown".
    if label in ("connected", "active", "healthy"):
        path = DEPIN_HEALTH_STATE_DIR / f"{project}-last-health.json"
        try:
            path.write_text(json.dumps({"health": label}))
        except OSError as e:
            logging.warning("Failed to write %s: %s", path, e)
            pass
    return label, joined


def _depin_project_status(project: str) -> dict:
    unit = _depin_service_unit(project)
    installed = _service_installed(unit)
    service_info = _service_info(unit) if installed else {"unit": unit, "state": "not-installed", "since": ""}
    enabled = False
    if installed:
        rc, out, _ = _run(["systemctl", "is-enabled", unit])
        enabled = (rc == 0 and out.strip() == "enabled")
    if project == "honeygain":
        # Honeygain's output goes through Docker's json-file log driver, not
        # journald — the unit journal is permanently empty. Read docker logs
        # via the setuid depin-logs-wrapper (gateway-ui has no docker access).
        log_rc, log_out, _ = _run(
            ["/usr/local/bin/depin-logs-wrapper", project],
            timeout=10,
        )
    else:
        log_rc, log_out, _ = _run(
            ["journalctl", "-u", unit, "-n", str(DEPIN_LOG_LINES), "--no-pager", "--output=cat"],
            timeout=10,
        )
    log_lines = log_out.splitlines() if log_rc == 0 else []
    health_label, raw_logs = _depin_parse_logs(log_lines, project)
    extra = {}
    if project == "mastchain":
        extra = _mastchain_hardware_status()
    return {
        "project": project,
        "installed": installed,
        "enabled": enabled,
        "service_state": service_info["state"],
        "since": service_info.get("since", ""),
        "configured": _depin_is_configured(project),
        "config_required": project in DEPIN_CONFIG_REQUIRED,
        "health": health_label,
        "logs": log_lines[-DEPIN_LOG_LINES:],
        "update_available": _depin_update_available(project),
        "update_last_checked": _depin_update_state(project).get("last_checked", ""),
        "update_last_error": _depin_update_state(project).get("last_error", ""),
        "local_digest": _depin_update_state(project).get("local_digest", ""),
        "image_created": _depin_update_state(project).get("image_created", ""),
        "captured_version": _depin_update_state(project).get("captured_version", ""),
        **extra,
    }


def _mastchain_hardware_status() -> dict:
    """Live RTL-SDR presence/count + readsb activity for the MastChain card.

    Drives the no-hardware badge and the one-dongle-one-spectrum warning
    (design §3.4/§3.6). Runs the SAME dependency-free sysfs probe as the unit's
    ExecCondition=, but as the unprivileged gateway-ui user — sysfs is
    world-readable so the probe needs no privileges. Re-run on every status
    poll, so the UI re-renders live when a dongle is plugged/unplugged."""
    count = 0
    rc, out, _ = _run([MASTCHAIN_HW_CHECK, "--count"], timeout=5)
    if rc == 0 and out.strip().isdigit():
        count = int(out.strip())
    rc2, out2, _ = _run(["systemctl", "is-active", "readsb.service"], timeout=5)
    return {
        "hardware_present": count > 0,
        "rtlsdr_count": count,
        "readsb_active": rc2 == 0 and out2.strip() == "active",
    }


def _depin_update_state(project: str) -> dict:
    """Per-project record from the update-check state file. Empty dict on any
    read/permission failure so callers degrade gracefully."""
    if not DEPIN_UPDATE_STATE.exists():
        return {}
    try:
        state = json.loads(DEPIN_UPDATE_STATE.read_text())
        rec = state.get("projects", {}).get(project, {})
        return rec if isinstance(rec, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _depin_update_available(project: str) -> bool:
    return bool(_depin_update_state(project).get("update_available", False))


# Version-capture patterns, kept in sync with capture_version_on_restart in
# scripts/depin-update-check.sh (same 3 projects, same extraction). `\K`-free:
# each regex captures the full phrase and group(1) is the version token.
_DEPIN_VERSION_PATTERNS = {
    "urnetwork": re.compile(r"Provider\s+([0-9][^ ]*?)\s+started"),
    "myst":      re.compile(r"Starting Mysterium Node\s+([0-9]+(?:\.[0-9]+)+)"),
    "anyone":    re.compile(r"Anon version\s+([0-9][^ ]*)"),
    # mastchain: the AIS-catcher startup banner is "AIS-catcher (build <date>)
    # v<version>" — e.g. "AIS-catcher (build Aug 19 2026) v0.00-1-unknown",
    # observed on the actual consumed image (Mac-side no-dongle run 2026-08-31).
    # The build date varies per image rebuild, so the pattern bridges it; the
    # fork reports a rolling "0.00-1-unknown" style version. Keep in sync with
    # version_pattern_for in scripts/depin-version-lib.sh.
    "mastchain": re.compile(r"AIS-catcher\s+.*\sv([0-9][^ ]*)"),
}


def _depin_resolve_honeygain_version() -> None:
    """Resolve the locally-pulled Honeygain :latest digest to a published tag.

    Honeygain's logs carry no version, but its Docker Hub repo publishes
    versioned tags whose index digest can match the current :latest. gateway-ui
    cannot run docker, so this resolves via the hub REST API (anonymous, same
    outbound trust as the existing ntfy httpx calls): the bulk tags-list
    provides index digests for recent tags, and older tags that omit `digest`
    there are backfilled from the per-tag /images endpoint (valid because
    those old single-arch images' first-arch digest equals their index
    digest). COVERAGE PARITY NOTE: with this backfill the Python resolver maps
    the same full set of tags (currently all 7) as the shell resolver in
    depin-update-check.sh (which uses imagetools inspect) — the two paths now
    resolve the same digests to the same versions, so a given local digest
    resolves identically regardless of which trigger (auto-update vs
    Restart/Update) fired. Both write the same honeygain_cache map.

    Caveat: the per-tag /images backfill is only correct for single-arch
    images (its per-arch digest differs from the index digest for multi-arch
    tags). It is used only for tags the bulk tags-list reports as missing a
    digest, which today are the single-arch 0.6.x set. If a future multi-arch
    tag were ever missing from the bulk digests, the backfill could map a
    sub-digest rather than the index — revisit if that scenario appears.

    Cache-first (24h TTL): a manual Restart is now a frequent capture trigger,
    and we must NOT hit Docker Hub's API on every restart — only re-walk when
    the cache is stale or the local digest isn't in it. On no match after a
    real re-walk, leave captured_version unset (fall back to digest+date), no
    retry storm.
    """
    local_digest = _depin_update_state("honeygain").get("local_digest", "")
    if not local_digest:
        return  # not pulled; nothing to resolve (matches day 0)
    now = int(time.time())
    cache = _depin_update_state("honeygain").get("honeygain_cache")
    cache_tags = (cache or {}).get("tags") or {}
    # Fresh cache hit: no outbound call.
    if cache_tags and isinstance(cache, dict):
        ts = cache.get("resolved_at", 0)
        if (now - int(ts)) < 24 * 3600 and local_digest in cache_tags.values():
            tag = next(k for k, v in cache_tags.items() if v == local_digest)
            _depin_set_captured_version("honeygain", tag)
            logging.info("captured honeygain version: %s (cached)", tag)
            return

    # Cache miss or stale: re-walk published tags from the hub REST API.
    # Bulk tags-list covers the recent tags' index digests. Older tags (0.6.x)
    # omit `digest` there, so backfill them from the per-tag /images endpoint,
    # whose first-arch digest equals the index digest for those single-arch
    # images. Combined, this gives full index-digest parity with the shell
    # resolver (imagetools) across every known tag. NOTE: /images per-arch
    # digest is NOT the index digest for multi-arch tags, so only use it as a
    # backfill for tags the bulk list already reports as missing a digest.
    tag_digests = {}
    tag_names = set()
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(
                "https://registry.hub.docker.com/v2/repositories/honeygain/honeygain/tags",
                params={"page_size": 100},
            )
            r.raise_for_status()
            for t in r.json().get("results", []):
                name = t.get("name")
                if not name or name == "latest":
                    continue
                tag_names.add(name)
                digest = t.get("digest")
                if digest:
                    tag_digests[name] = digest
            # Backfill older tags omitted by the bulk digests field.
            for missing in tag_names - set(tag_digests):
                try:
                    ir = client.get(
                        f"https://hub.docker.com/v2/repositories/honeygain/honeygain/tags/{missing}/images",
                    )
                    ir.raise_for_status()
                    entries = ir.json()
                    if isinstance(entries, list) and entries and entries[0].get("digest"):
                        tag_digests[missing] = entries[0]["digest"]
                except Exception:
                    logging.warning("honeygain per-tag /images backfill failed for %s", missing)
    except Exception as e:
        logging.warning("honeygain tag-list fetch failed: %s", e)
        return

    if not tag_digests:
        return

    # Persist the refreshed cache regardless of whether a match is found.
    try:
        state = {}
        if DEPIN_UPDATE_STATE.exists():
            state = json.loads(DEPIN_UPDATE_STATE.read_text())
        p = state.setdefault("projects", {}).setdefault("honeygain", {})
        p["honeygain_cache"] = {"resolved_at": now, "tags": tag_digests}
        DEPIN_UPDATE_STATE.write_text(json.dumps(state, indent=2))
    except (OSError, json.JSONDecodeError) as e:
        logging.warning("failed to write honeygain_cache: %s", e)

    matched = next((t for t, d in tag_digests.items() if d == local_digest), None)
    if matched:
        _depin_set_captured_version("honeygain", matched)
        logging.info("captured honeygain version: %s (matched digest %s)", matched, local_digest)
    else:
        logging.warning(
            "no Honeygain tag matched local digest (%s); keeping prior captured_version",
            local_digest,
        )


def _depin_capture_version_on_restart(project: str) -> None:
    """Capture a project's real version right after a container (re)start.

    The version line lives on a STARTUP log line that scrolls out of any short
    tail window once the container has been up a while, so this is only meant
    to be called from a real restart/start call site (manual Update, enable),
    never from the recurring status poll. Honeygain has no version line and
    instead resolves its version from a published Docker Hub tag/digest match,
    cache-first. Bounded writes only; on a miss we preserve the prior value
    rather than blanking it."""
    if project == "honeygain":
        _depin_resolve_honeygain_version()
        return
    if project not in _DEPIN_VERSION_PATTERNS:
        return
    pat = _DEPIN_VERSION_PATTERNS[project]
    unit = _depin_service_unit(project)
    window = "25s"
    # Bounded readiness wait: retry until the line appears or we time out.
    for _ in range(12):
        rc, out, _ = _run(
            ["journalctl", "-u", unit, "--since", window, "-o", "cat", "--no-pager"],
            timeout=10,
        )
        m = pat.search(out or "") if rc == 0 else None
        if m:
            _depin_set_captured_version(project, m.group(1))
            logging.info("captured %s version: %s", project, m.group(1))
            return
        time.sleep(1)
    logging.warning(
        "no version line captured for %s after restart (pattern %s); preserving prior captured_version",
        project, pat.pattern,
    )


def _depin_set_captured_version(project: str, version: str) -> None:
    """Write only `captured_version`, leaving every other field untouched."""
    try:
        state = {}
        if DEPIN_UPDATE_STATE.exists():
            state = json.loads(DEPIN_UPDATE_STATE.read_text())
        state.setdefault("projects", {}).setdefault(project, {})["captured_version"] = version
        DEPIN_UPDATE_STATE.parent.mkdir(parents=True, exist_ok=True)
        DEPIN_UPDATE_STATE.write_text(json.dumps(state, indent=2))
    except (OSError, json.JSONDecodeError) as e:
        logging.warning("failed to write captured_version for %s: %s", project, e)


def _depin_check_config(project: str) -> None:
    if project in DEPIN_CONFIG_REQUIRED and not _depin_is_configured(project):
        if project == "urnetwork":
            detail = "URnetwork requires authentication before being enabled — enter an auth code from ur.io first"
        else:
            detail = f"{project} requires configuration before being enabled — call /api/depin/{project}/configure first"
        raise HTTPException(status_code=409, detail=detail)


# ── GET /api/depin/status ────────────────────────────────────────────────────

@app.get("/api/depin/status")
def api_depin_status(_: Auth):
    return {
        "hostname": _tailscale_fqdn() or _tailscale_hostname() or socket.gethostname(),
        "projects": {p: _depin_project_status(p) for p in DEPIN_PROJECTS}
    }


# ── POST /api/depin/{project}/configure ──────────────────────────────────────

@app.post("/api/depin/{project}/configure")
async def api_depin_configure(_: Auth, project: str, request: Request):
    _depin_validate_project(project)

    if project in ("urnetwork", "myst"):
        raise HTTPException(status_code=400, detail=f"{project} does not require configuration")

    if not Path(DEPIN_WRAPPER).exists():
        raise HTTPException(status_code=503, detail="depin-config-wrapper not installed — run install-wrappers.sh")

    body = await request.json()

    if project == "honeygain":
        device_name = str(body.get("device_name", "")).strip()
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))

        if not device_name:
            raise HTTPException(status_code=400, detail="device_name is required")
        if not email:
            raise HTTPException(status_code=400, detail="email is required")
        if not password:
            raise HTTPException(status_code=400, detail="password is required")
        if len(device_name) > 64:
            raise HTTPException(status_code=400, detail="device_name too long")
        if len(email) > 320:
            raise HTTPException(status_code=400, detail="email too long")
        if len(password) > 128:
            raise HTTPException(status_code=400, detail="password too long")
        if not DEPIN_DEVICE_RE.fullmatch(device_name):
            raise HTTPException(status_code=400, detail="device_name must be alphanumeric and hyphens only")
        if "@" not in email or "." not in email.split("@")[-1] if "@" in email else True:
            raise HTTPException(status_code=400, detail="invalid email format")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in password):
            raise HTTPException(status_code=400, detail="password contains control characters")

        rc, out, err = await _run_async([DEPIN_WRAPPER, "honeygain", device_name, email, password], timeout=15)

    elif project == "mastchain":
        email = str(body.get("email", "")).strip()
        token = str(body.get("token", "")).strip()

        if not email:
            raise HTTPException(status_code=400, detail="email is required")
        if not token:
            raise HTTPException(status_code=400, detail="token is required")
        if len(email) > 320:
            raise HTTPException(status_code=400, detail="email too long")
        if len(token) > 512:
            raise HTTPException(status_code=400, detail="token too long")
        if "@" not in email or "." not in email.split("@")[-1] if "@" in email else True:
            raise HTTPException(status_code=400, detail="invalid email format")
        # Token must stay a single argv token for `USERPWD <value>` in the
        # unit's ExecStart: printable ASCII, no control chars, no spaces
        # (mirrors is_valid_token in scripts/depin-config-wrapper.c).
        if any(ord(ch) < 0x21 or ord(ch) > 0x7e for ch in token):
            raise HTTPException(status_code=400, detail="token contains invalid characters (printable ASCII, no spaces)")

        rc, out, err = await _run_async([DEPIN_WRAPPER, "mastchain", email, token], timeout=15)

    elif project == "anyone":
        nickname = str(body.get("nickname", "")).strip()
        contact = str(body.get("contact", "")).strip()
        myfamily = str(body.get("myfamily", "")).strip() or None

        if not nickname:
            raise HTTPException(status_code=400, detail="nickname is required")
        if not contact:
            raise HTTPException(status_code=400, detail="contact is required")
        if len(nickname) > 19:
            raise HTTPException(status_code=400, detail="nickname too long (max 19)")
        if len(contact) > 255:
            raise HTTPException(status_code=400, detail="contact too long")
        if not DEPIN_NICKNAME_RE.fullmatch(nickname):
            raise HTTPException(status_code=400, detail="nickname must be alphanumeric only (1-19 chars)")
        if SHELL_META_RE.search(nickname) or SHELL_META_RE.search(contact):
            raise HTTPException(status_code=400, detail="input contains invalid characters")
        if myfamily and SHELL_META_RE.search(myfamily):
            raise HTTPException(status_code=400, detail="myfamily contains invalid characters")

        args = [DEPIN_WRAPPER, "anyone", nickname, contact]
        if myfamily:
            args.append(myfamily)
        rc, out, err = await _run_async(args, timeout=15)

    if rc != 0:
        detail = (err or out).strip() or "config write failed"
        raise HTTPException(status_code=500, detail=detail)

    return {"ok": True, "project": project, "configured": _depin_is_configured(project)}


# ── POST /api/depin/urnetwork/auth ──────────────────────────────────────────

URNETWORK_AUTH_IMAGE = "bringyour/community-provider:g4-latest"
URNETWORK_AUTH_VOLUME = "/var/lib/gateway-ui/urnetwork"


@app.post("/api/depin/urnetwork/auth")
async def api_urnetwork_auth(_: Auth, request: Request):
    body = await request.json()
    code = str(body.get("auth_code", "")).strip()

    if not code:
        raise HTTPException(status_code=400, detail="auth_code is required")
    if len(code) > 2048:
        raise HTTPException(status_code=400, detail="auth_code too long")
    if not re.fullmatch(r'[A-Za-z0-9_+/=-]+', code):
        raise HTTPException(status_code=400, detail="auth_code contains invalid characters")

    cmd = [
        "sudo", "/usr/bin/docker", "run", "--rm",
        "-v", f"{URNETWORK_AUTH_VOLUME}:/root/.urnetwork",
        URNETWORK_AUTH_IMAGE,
        "auth", code, "-f",
    ]
    rc, out, err = await _run_async(cmd, timeout=30)

    if rc != 0:
        detail = (err or out).strip() or "auth failed"
        raise HTTPException(status_code=500, detail=detail)

    return {"ok": True, "output": (out + err).strip()}


# ── POST /api/depin/{project}/enable ─────────────────────────────────────────

@app.post("/api/depin/{project}/enable")
def api_depin_enable(_: Auth, project: str):
    _depin_validate_project(project)
    _depin_check_config(project)
    unit = _depin_service_unit(project)
    if not _service_installed(unit):
        raise HTTPException(status_code=503, detail=f"{unit} not installed — run install-depin-services.sh")
    rc, _, err = _run(["sudo", _SYSTEMCTL, "enable", unit])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "enable failed")
    rc, _, err = _run(["sudo", _SYSTEMCTL, "start", unit])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "start failed — unit is enabled but not running; retry to start")
    # Initial container start: capture the running version now (no-op for
    # Honeygain). Aligned with the auto-update capture in depin-update-check.sh.
    _depin_capture_version_on_restart(project)
    return {"ok": True, "project": project, "enabled": True}


# ── POST /api/depin/{project}/disable ────────────────────────────────────────

@app.post("/api/depin/{project}/disable")
def api_depin_disable(_: Auth, project: str):
    _depin_validate_project(project)
    unit = _depin_service_unit(project)
    rc, _, err = _run(["sudo", _SYSTEMCTL, "stop", unit])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "stop failed")
    rc, _, err = _run(["sudo", _SYSTEMCTL, "disable", unit])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "disable failed — unit is stopped but still enabled; retry to disable")
    return {"ok": True, "project": project, "enabled": False}


# ── POST /api/depin/{project}/uninstall ──────────────────────────────────────

@app.post("/api/depin/{project}/uninstall")
async def api_depin_uninstall(_: Auth, project: str, request: Request):
    _depin_validate_project(project)

    if not Path(DEPIN_UNINSTALL).exists():
        raise HTTPException(status_code=503, detail="depin-uninstall.sh not installed")

    body = await request.json()
    confirm = body.get("confirm")
    if confirm is not True:
        raise HTTPException(status_code=400, detail="uninstall requires 'confirm': true in request body")

    rc, out, err = _run(["sudo", DEPIN_UNINSTALL, project], timeout=60)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out.strip() or "uninstall failed")
    return {"ok": True, "project": project, "uninstalled": True}


# ── POST /api/depin/{project}/update ─────────────────────────────────────────

@app.post("/api/depin/{project}/update")
def api_depin_update(_: Auth, project: str):
    _depin_validate_project(project)
    image = DEPIN_IMAGES.get(project)
    if not image:
        raise HTTPException(status_code=500, detail=f"No image defined for {project}")
    rc, out, err = _run(["sudo", "/usr/bin/docker", "pull", image], timeout=120)
    if rc != 0:
        raise HTTPException(status_code=500, detail=(err or out).strip() or "pull failed")
    updated = "Downloaded newer image" in (out + err) or "Status: Downloaded" in (out + err)
    if updated:
        rc2, _, err2 = _run(["sudo", _SYSTEMCTL, "restart", f"depin-{project}.service"], timeout=30)
        if rc2 != 0:
            raise HTTPException(status_code=500, detail=err2 or "restart failed")
        # Fresh image pulled + restarted: capture the new running version
        # (no-op for Honeygain). Aligned with the auto-update capture.
        _depin_capture_version_on_restart(project)
        # Clear update state so status reflects current
        _depin_clear_update_state(project)
    return {"ok": True, "project": project, "updated": updated}


# ── POST /api/depin/{project}/restart ────────────────────────────────────────

@app.post("/api/depin/{project}/restart")
def api_depin_restart(_: Auth, project: str):
    _depin_validate_project(project)
    global _depin_restart_running
    if _depin_restart_running:
        raise HTTPException(status_code=409, detail="Another DePIN project restart is already in progress")
    unit = _depin_service_unit(project)
    if not _service_installed(unit):
        raise HTTPException(status_code=503, detail=f"{unit} not installed — run install-depin-services.sh")
    _depin_restart_running = True
    try:
        rc, _, err = _run(["sudo", _SYSTEMCTL, "restart", unit], timeout=30)
        if rc != 0:
            raise HTTPException(status_code=500, detail=err or "restart failed")
        # A restart is itself a valid version-capture trigger (no-op for
        # Honeygain). This is NOT an update: leave update_available / update
        # state untouched — a plain restart must not clear a pending update.
        _depin_capture_version_on_restart(project)
        return {"ok": True, "project": project, "restarted": True}
    finally:
        _depin_restart_running = False


def _depin_clear_update_state(project: str) -> None:
    if not DEPIN_UPDATE_STATE.exists():
        return
    try:
        state = json.loads(DEPIN_UPDATE_STATE.read_text())
        if "projects" in state and project in state["projects"]:
            state["projects"][project]["update_available"] = False
        DEPIN_UPDATE_STATE.write_text(json.dumps(state, indent=2))
    except json.JSONDecodeError:
        pass
    except OSError as e:
        logging.warning("Failed to write %s: %s", DEPIN_UPDATE_STATE, e)


# ── GET /api/depin/auto-update ───────────────────────────────────────────────

@app.get("/api/depin/auto-update")
def api_depin_auto_update_get(_: Auth):
    try:
        if DEPIN_AUTO_UPDATE.exists():
            state = json.loads(DEPIN_AUTO_UPDATE.read_text())
            return {"projects": state}
    except (json.JSONDecodeError, OSError):
        pass
    return {"projects": {}}


# ── POST /api/depin/{project}/auto-update ─────────────────────────────────────

@app.post("/api/depin/{project}/auto-update")
async def api_depin_auto_update(_: Auth, project: str, request: Request):
    _depin_validate_project(project)
    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    try:
        state = {}
        if DEPIN_AUTO_UPDATE.exists():
            state = json.loads(DEPIN_AUTO_UPDATE.read_text())
        state[project] = enabled
        DEPIN_AUTO_UPDATE.parent.mkdir(parents=True, exist_ok=True)
        DEPIN_AUTO_UPDATE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        logging.warning("Failed to write %s: %s", DEPIN_AUTO_UPDATE, e)
        raise HTTPException(status_code=500, detail=f"Failed to save auto-update state: {e}")
    return {"ok": True, "project": project, "auto_update": enabled}


# ── POST /api/depin/update-check/run-now ─────────────────────────────────────
# Force an immediate update-check, independent of the timer schedule. The unit is
# a Type=oneshot, so `systemctl start` blocks until the full check (registry
# digests + any auto-update pull/restart) completes. The frontend re-polls
# /api/depin/status afterwards to pick up fresh last_checked/update_available.

def _run_depin_update_check() -> None:
    global _depin_check_running
    try:
        _run(["sudo", _SYSTEMCTL, "start", "depin-update-check.service"], timeout=300)
    finally:
        _depin_check_running = False


@app.post("/api/depin/update-check/run-now")
def api_depin_update_check_run_now(_: Auth, bg: BackgroundTasks):
    global _depin_check_running
    if _depin_check_running:
        raise HTTPException(status_code=409, detail="Update check already in progress")
    _depin_check_running = True
    bg.add_task(_run_depin_update_check)
    return {"ok": True, "started": True}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=str(CONFIG.get("bind_host", "0.0.0.0")),
        port=int(CONFIG.get("port", 8080)),
        log_level="info",
        timeout_graceful_shutdown=3,
    )
