# BitTug — DePIN Gateway Platform

> **Formerly known as *sensecap-m1-gateway / SenseCap M1 Gateway*.** See [CHANGELOG.md](CHANGELOG.md) for the rename history.

BitTug is an open-source, **hardware-agnostic DePIN gateway for Raspberry Pi**. Flash a Pi, pick
which DePIN network module(s) to run, and go — **nothing beyond the Pi and a network
connection is required** for the modules that need no extra hardware: **Honeygain,
URnetwork, Mysterium, and Anyone Protocol**.

**Helium / LoRaWAN is one optional module among several, not the product's identity.** It is
the original and most mature module and needs a LoRa concentrator board attached to the Pi;
if you don't have one you simply don't run it, and the rest of BitTug works unchanged.
MastChain (AIS) and Wingbits (ADS-B) are likewise optional and need an RTL-SDR dongle.
See [Modules](#modules) for the full list and what each one needs.

Originally built as a replacement firmware for the **Seeed SenseCap M1** LoRaWAN gateway, the
platform has since proven out on bare Raspberry Pi 3B/4 hardware with no SenseCap board and
no concentrator attached at all — which is what drove the shift to a modular,
hardware-agnostic design.

No hidden services. No telemetry. No third-party backdoors. Fully auditable.

---

## Modules

Pick the module(s) that match the hardware you have. Each runs independently — the DePIN
modules as their own Docker-based systemd services, the Helium stack as native systemd
services. Full descriptions follow in the sections below.

| Module | What it is | Extra hardware | Network |
|---|---|---|---|
| **Honeygain** | Bandwidth-sharing network | none — bare Pi | outbound only |
| **URnetwork** | Community bandwidth provider | none — bare Pi | outbound only |
| **Mysterium** | Mysterium Network node | none — bare Pi | publishes TCP `4449` (node API / dashboard) |
| **Anyone Protocol** | Tor relay (non-exit) | none — bare Pi | publishes TCP `9001` (ORPort) + TCP `9030` (DirPort) |
| **MastChain** | AIS ship-tracking (AIS-catcher) | RTL-SDR-class USB dongle (AIS ≈162 MHz) | outbound to `api.mastchain.io` |
| **Wingbits** | ADS-B aircraft tracking | RTL-SDR-class USB dongle (ADS-B 1090 MHz) | outbound; `readsb`↔`wingbits` on localhost |
| **Helium / LoRaWAN** | Helium IoT gateway | LoRa concentrator (SX1302) + ATECC608A secure element | outbound to Helium; EUI derived from `eth0` |

**One dongle = one spectrum:** MastChain (≈162 MHz) and Wingbits (1090 MHz) cannot share a
single RTL-SDR, and a dongle can only be opened by one process at a time — running both on
one device requires **two dongles**. The web UI surfaces this warning when a single-dongle
conflict is detected.

**Tailscale** is an optional, shared remote-access layer that works alongside any module and
runs through the web UI using your own auth key — no config file required. See
[Tailscale](#tailscale).

---

## What This Is NOT

- Not affiliated with Seeed Studio or the Helium Foundation
- Not a cloud service — your gateway, your keys, your data
- Not LoRa-only — the Helium module is optional, not the core requirement

---

## Supported Hardware & OS

The only hardware BitTug actually requires is a Raspberry Pi; everything past that is
module-dependent (see the [Modules](#modules) table).

| | Detail |
|---|---|
| **SBC** | Raspberry Pi 3B / 4 / 5, 64-bit |
| **OS** | Raspberry Pi OS Lite 64-bit (ARM64 / Debian Trixie). The image builder tracks the latest `raspios_lite_arm64` release. |
| **Proven on** | Bare Raspberry Pi 3B and Pi 4, with no concentrator or SenseCap board attached |
| **Helium / LoRaWAN module** | Raspberry Pi 4B path (verified on the SenseCap M1). The concentrator reset (`scripts/reset_lgw.sh`) is hardcoded to the Pi 4B sysfs GPIO layout and is explicitly **not Pi 5** |

Other Helium-class hardware using the same RAK2287/SX1302 concentrator — e.g. Bobcat and
similar miners — should work but is not yet tested. If you run BitTug on other hardware, a
report or PR is welcome.

The split is modular: the web UI, `gateway-rs`, and the Wingbits stack are Pi-portable and
hardware-independent, while `lora_pkt_fwd` and `reset_lgw.sh` are specific to the attached
concentrator and only load if you're running the Helium module.

---

## Helium / LoRaWAN Module (Optional)

Helium is the original and most mature BitTug module, and it is entirely optional. It needs a
LoRa concentrator board attached to the Pi. The module is what the Helium-facing documentation
later in this README — [Band / Region Selection](#band--region-selection), the
[How It Works](#how-it-works) diagram, and [Building from Source](#building-from-source) —
refers to; skip those if you only run the concentrator-free modules.

| Component | Detail |
|-----------|--------|
| SBC | Raspberry Pi 4B path (verified on the Pi 4B inside the SenseCap M1) |
| Concentrator | Helium-class concentrator (originally RAK2287 / SX1302 over SPI `/dev/spidev0.0`) |
| Secure Element | Microchip ATECC608A on I2C-1 (`0x60`) — hardware swarm key storage, no software key files |
| Connectivity | Outbound internet; Gateway EUI derived from `eth0` |
| GPS | None — fake GPS configured in the web UI or `config/` |

The module consists of Semtech **`lora_pkt_fwd`** (reference packet forwarder for the SX1302
concentrator) and Helium **`gateway-rs`** (lightweight network gateway daemon). On a device
with no concentrator both are automatically skipped — no crash-looping — and the Helium parts
of the web UI hide themselves until a concentrator is detected.

---

## Flashing a Pre-Built Image

Pre-built images are available on the [Releases](https://github.com/bitcryptic-gw/bittug/releases) page. The same image works whether or not you have a concentrator attached — Helium simply won't do anything until one is connected and configured.

**Requirements:**
- Raspberry Pi Imager (available for Windows, Mac, Linux)
- A microSD card (8 GB minimum)

**Steps:**

1. Download the latest `.img.xz` from [Releases](https://github.com/bitcryptic-gw/bittug/releases) and verify the SHA256 checksum.
2. Flash it to a microSD card using Raspberry Pi Imager — no Customisation or settings step needed.
3. Insert the card into your Pi (with a concentrator attached if you're using the Helium module — otherwise just the bare Pi) and power on.
4. Wait for first boot to complete — this clones the repo, runs `boot/bootstrap.sh`, and reboots automatically. Takes a few minutes.
5. SSH in using the default account: `ssh sensecap@<hostname-or-ip>`, password `sensecap`. You'll be required to set a new password immediately — this is enforced, not optional.
6. The web UI is now available at `http://<hostname>:8080`. The bearer token (separate from your SSH login — this authenticates the web UI, not SSH) is printed to the console during first boot; recover it any time via `sudo cat /etc/gateway-ui/token`.

**Default credentials:** username `sensecap`, password `sensecap`. Full sudo, SSH enabled. This is a published, well-known default — anyone with the public image knows it. The forced password change on first login (enforced, not optional) is what makes that safe. Don't expose the device to the open internet before changing it.

*(The default account and hostname prefix are named `sensecap` for historical/hardware-identity reasons, independent of the BitTug project rename — see [CHANGELOG.md](CHANGELOG.md).)*

If you'd rather configure your own username, password, or SSH key instead of using the default account, you can do so via Raspberry Pi Imager's Customisation step (the gear icon) — but this is not available when flashing via **"Use custom"** with a custom `.img.xz` file in at least Imager v2.0.10. If your version or method of Imager does support it (e.g. the `rpi-imager` CLI may behave differently), your configured credentials work as normal and the default account is never created (the first-boot script checks for an existing user first).

---

## Configuration

Configuration is done through the web UI.

### Web UI

`http://<hostname>:8080` — accessible via Tailscale (or LAN if you haven't restricted it).

**Authentication:** Bearer token stored at `/etc/gateway-ui/token`. Printed to the console during first boot. Recovery:
```bash
sudo cat /etc/gateway-ui/token
```

**Tabs:**

| Tab | What it shows |
|-----|---------------|
| **Dashboard** | Grouped service status (Wingbits / Tailscale / Web UI, plus Helium when a concentrator is detected), system metrics (CPU / memory / disk) |
| **Applications** | Helium: gateway identity, beacon stats, LoRa region — these appear only when a concentrator is detected, otherwise a one-line note explains that Helium is optional. Wingbits: status and in-browser setup/reconfiguration flow |
| **Network** | Interface cards (eth0 / wlan0 / Tailscale), Tailscale auth + options (subnet routing, SSH toggle), web UI port |
| **Live Log** | Unified journal stream with filter pills: System / Wingbits / Tailscale, plus Helium when a concentrator is detected |
| **Settings** | OTA updates (version check, changelog, smart service restart, SSE stream), bearer token display and regenerate |

The header bar shows the current build version alongside the brand (`BitTug vYYYY.MM.DD`). An amber **⬆ Update available** badge appears when a newer GitHub release is detected; clicking navigates to the Settings OTA section.

*All screenshots taken on desktop. Mobile layout stacks cards vertically.*

![Dashboard](docs/screenshots/dashboard.png)
![Applications](docs/screenshots/applications.png)
![Network](docs/screenshots/network.png)
![Live Log](docs/screenshots/logs.png)
![Settings](docs/screenshots/settings.png)

---

## Band / Region Selection

*(Applies only if you're running the Helium LoRaWAN module — skip this section if you're not using a concentrator.)*

Band is configured from the **Applications** tab in the web UI. To change band after first boot, select the new region and apply — the forwarder restarts automatically.

| Band | Region | Notes |
|------|--------|-------|
| `au_915_928` | AU915 | **FSB2 (ch 8–15 + 65)** — Helium AU default |
| `us_902_928` | US915 | **FSB2 (ch 8–15 + 65)** — Helium US default |
| `eu_863_870` | EU868 | 8 standard TTN/Helium channels, 868.1–868.5 + 867.1–867.9 MHz |
| `as_923_1` | AS923-1 | Singapore, Indonesia, Vietnam; 922.0–923.4 MHz |
| `as_923_2` | AS923-2 | Vietnam (alternate plan); 921.4–922.8 MHz |
| `in_865_867` | IN865 | India; 865.0625, 865.4025, 865.985 MHz (3 mandatory channels) |
| `kr_920_923` | KR920 | South Korea; 922.1–923.5 MHz |
| `ru_864_870` | RU864 | Russia; 864.1–864.9 + 868.7–869.3 MHz |
| `cn_470_510` | CN470 | China; FSB11 (486.3–487.7 MHz uplink, 500.3–509.7 MHz downlink) |

> **Helium note:** Helium AU and Helium US both use FSB2. Using any other FSB will result in zero PoC activity.

---

## How It Works

*(This diagram shows the Helium LoRaWAN module's data path. The concentrator-free modules — Honeygain, URnetwork, Mysterium, Anyone Protocol — run independently as their own systemd services and don't touch this path at all.)*

```
LoRa devices (nodes)
       │  RF
       ▼
Helium-class concentrator (SX1302 via SPI /dev/spidev0.0 — RAK2287 shown; see Hardware Requirements)
       │  UDP 127.0.0.1:1680
       ▼
lora_pkt_fwd  [pktfwd.service]
       │  UDP 127.0.0.1:1680
       ▼
gateway-rs  [gateway-rs.service]
       │  ECC608 swarm key (i2c-1:0x60 slot 0)
       ▼
Helium IoT Network (mainnet)
```

- `pktfwd.service` runs the Semtech packet forwarder, which handles the SX1302-family concentrator hardware and forwards raw LoRa packets as UDP datagrams
- `gateway-rs.service` runs the native `helium_gateway` binary, connecting to the Helium mainnet using the ECC608A secure element for identity
- Tailscale is optional; managed via the web UI Network tab using a setuid wrapper (no sudo required)
- **Docker** is installed on the device and available for operator use, but is not used by any part of the Helium or Wingbits stack

---

## Tailscale

Tailscale is optional and managed entirely from the **Network** tab in the web UI. You supply your own auth key — this project never provides one.

**Setup:**
1. Navigate to the Network tab → Tailscale → Setup/Auth card
2. Paste an auth key from [tailscale.com/settings/keys](https://tailscale.com/settings/keys) — a **reusable, pre-approved** key is recommended (see below)
3. Click **Connect** — the gateway authenticates and the status card updates immediately

**Automatic recovery:** on every successful Connect the auth key is persisted to `/etc/gateway/tailscale.key` (`0600 root:root`). If the device is ever logged out — e.g. its machine record is deleted from the Tailscale admin console — `tailscale-autoconnect.timer` re-authenticates with the saved key automatically (at boot and every 10 minutes), preserving SSH/routes/operator settings. This is a deliberate trade-off: a resting key on disk beats an unreachable remote device. Use a reusable key so recovery works more than once; with a one-time key, recovery falls back to manual re-auth via the UI. To opt out, delete the key file. If Tailscale reports **Needs login** in the UI, a one-click "Re-authenticate in browser" link is shown as a manual fallback.

**Options (available once connected):**
- **Subnet routing** — advertise the gateway's local subnet to your Tailnet
- **SSH** — enable Tailscale SSH access

All Tailscale operations run through a setuid wrapper (`/usr/local/bin/tailscale-wrapper`) — no sudo grants required. The `gateway-ui` user is set as the Tailscale operator at provisioning time.

**To install Tailscale manually** (if not already provisioned by bootstrap):
```bash
sudo /opt/gateway/scripts/install-tailscale.sh
```

---

## OTA Updates

The web UI Settings tab provides over-the-air updates direct from this GitHub repository.

**How it works:**
1. The header polls GitHub releases every 60 seconds. If a newer tag is found, an amber **⬆ Update available** badge appears.
2. Click the badge (or navigate to Settings → OTA) to see the version comparison, collapsible release notes, and a list of changed files.
3. Service group checkboxes are pre-selected based on which files changed. Deselect any groups you don't want restarted.
4. Click **Update** — the gateway runs `git pull` and restarts the selected services. Output streams live via SSE.
5. If the web UI service itself restarts mid-update, the browser auto-reloads when it comes back.

The version is stamped at provisioning time into `/etc/gateway-version` using `git describe --tags --always`. All update operations run through a setuid wrapper (`/usr/local/bin/ota-update-wrapper`) — no sudo required.

---

## Wingbits (Optional)

Wingbits is an optional ADS-B data aggregation service. It runs as native systemd services (`readsb.service` + `wingbits.service`) independent of the Helium stack and does not interfere with LoRaWAN operation.

**Setup via web UI:**
Navigate to the **Applications** tab → Wingbits section → paste the station install URL from your Wingbits dashboard → the setup streams real-time output in the browser.

**Setup via CLI** (equivalent):
```bash
# One-time dependency install (run once at provisioning — bootstrap handles this)
sudo /opt/gateway/scripts/install-wingbits-deps.sh

# Setup or reconfigure
sudo /opt/gateway/scripts/wingbits-setup.sh "https://gitlab.com/wingbits/config/-/raw/install.sh?station_id=..."
```

`wingbits-setup.sh` is idempotent — re-run it for station relocation or ID change.

The `wingbits-setup-wrapper` setuid binary (`/usr/local/bin/wingbits-setup-wrapper`, compiled from `scripts/wingbits-setup-wrapper.c` during deps install) allows the web UI to invoke the setup script as root in a controlled way. Source is in the repo.

---

## MastChain (AIS-catcher, Optional)

MastChain is an optional DePIN network that rewards AIS (ship-tracking) coverage. It runs as a Docker-based DePIN module (`depin-mastchain.service`) consuming the existing `ghcr.io/c-man-the-man/mastchain-ais` image, and is managed from the **DePIN** tab of the web UI — configure your MastChain email + dashboard token, then enable the toggle.

**Hardware:** an RTL-SDR-class USB dongle is required. Without one attached, the MastChain card shows **"No RTL-SDR hardware detected"** and the service stays cleanly stopped (it is condition-skipped rather than crash-looping).

**One dongle = one spectrum (important):** MastChain receives AIS on ~162 MHz; Wingbits/ADS-B receives on 1090 MHz. A single RTL-SDR dongle cannot serve both, and the dongle can only be opened by one process at a time. A **second dongle is required** to run MastChain on a device that is already feeding Wingbits — enabling MastChain while readsb holds the only dongle will fail at device open. The web UI surfaces this warning on the MastChain card when a single-dongle conflict is detected.

Unlike MastChain's own installer (which bakes your email and token into a world-readable system file and runs the receiver as root), BitTug stores the credentials in a root-only file and runs the receiver in an isolated, non-root container.

---

## Building from Source

*(This section covers building the Helium LoRaWAN module. If you're only running the concentrator-free modules — Honeygain, URnetwork, Mysterium, Anyone Protocol — you don't need any of this.)*

The steps below are for the SX1302-family concentrator (e.g. RAK2287), which is the currently tested and documented path. If you're building for a different Helium-class concentrator chipset, the packet-forwarder build process will differ — see the upstream hardware vendor's documentation for the appropriate packet forwarder repository.

The `lora_pkt_fwd` binary must be compiled from the Semtech sx1302_hal repository for the RAK2287 / SX1302 hardware.

```bash
# Install build dependencies
sudo apt-get install -y git build-essential libssl-dev

# Clone sx1302_hal
git clone https://github.com/Lora-net/sx1302_hal.git
cd sx1302_hal

# Build
make all

# Install
sudo cp packet_forwarder/lora_pkt_fwd /usr/local/bin/
sudo mkdir -p /opt/gateway/pktfwd
sudo ln -sf /opt/gateway/scripts/reset_lgw.sh /opt/gateway/pktfwd/reset_lgw.sh
```

> `lora_pkt_fwd` (and `chip_id`) hardcode `./reset_lgw.sh` as a relative path and look for it in their working directory (`/opt/gateway/pktfwd`). `boot/bootstrap.sh` creates this symlink automatically during provisioning. If you are setting up manually, the `ln -sf` line above is required — without it, `pktfwd.service` will fail on start with `sh: ./reset_lgw.sh: not found`.

**helium_gateway** must be installed as a native ARM64 musl binary at `/usr/local/bin/helium_gateway`. Download a release from the [helium-systems/gateway-rs releases page](https://github.com/helium/gateway-rs/releases) — select the `aarch64-unknown-linux-musl` build and extract the binary.

---

## Directory Layout

```
/opt/gateway/               (repo root)
├── boot/
│   ├── bootstrap.sh        # First-time provisioning (run by firstrun.sh on first boot)
│   ├── firstrun.sh         # Injected into image — clones repo and calls bootstrap.sh
│   ├── gateway-provisioning-check.sh
│   ├── config.txt          # Pi boot config
│   ├── build-image.sh      # GitHub Actions image build script
│   └── tag-release.sh      # Mac-side release tagging helper
├── config/
│   ├── settings.toml       # gateway-rs config (ECC608A / i2c-1)
│   ├── global_conf.json    # Active frequency plan
│   └── global_conf.*.json  # Frequency plan templates (one per region)
├── docker/                 # Reserved for future non-Helium/non-Wingbits workloads
├── docs/
│   └── screenshots/        # UI screenshots for README
├── gateway-ui/             # FastAPI web UI source
│   ├── main.py
│   ├── requirements.txt
│   └── static/             # index.html, app.js, style.css
├── pktfwd/
│   └── reset_lgw.sh        # Symlink → scripts/reset_lgw.sh (required by lora_pkt_fwd)
├── scripts/
│   ├── reset_lgw.sh        # SX1302 GPIO reset
│   ├── wingbits-setup.sh   # Wingbits setup / reconfigure (idempotent)
│   ├── install-wingbits-deps.sh
│   ├── install-tailscale.sh
│   ├── tailscale-wrapper.c # Setuid wrapper source (Tailscale ops)
│   ├── wingbits-setup-wrapper.c
│   ├── ota-update-wrapper.c
│   └── udev/
│       └── 99-rtlsdr.rules # RTL-SDR symlink → /dev/rtlsdr0
└── systemd/
    ├── pktfwd.service
    ├── gateway-rs.service
    ├── gateway-ui.service
    ├── depin-*.service          # Honeygain / URnetwork / Mysterium / Anyone / MastChain
    ├── depin-update-check.{service,timer}
    ├── tailscale-autoconnect.{service,timer}
    ├── tailscale-reauth-watchdog.service
    └── readsb-override.conf     # drop-in for readsb.service (readsb itself is installed by the Wingbits installer)

/etc/gateway-ui/token       # Bearer token (owner: gateway-ui, mode 600)
/etc/gateway-version        # Build version stamp (written by bootstrap.sh)
/usr/local/bin/tailscale-wrapper     # Compiled setuid wrapper
/usr/local/bin/wingbits-setup-wrapper
/usr/local/bin/ota-update-wrapper
```

---

## Contributing

This project is hardware-agnostic by design. Contributions welcome for:

- Bug fixes and correctness improvements
- Additional frequency plan configs (verified against Helium network requirements)
- Documentation improvements
- Web UI improvements

**Hardware variant contributions** (e.g. support for other concentrator modules or Pi models) are welcome via PRs — please keep existing hardware behaviour unchanged.

Please open an issue before starting large changes.

---

## License

MIT — see [LICENSE](LICENSE).

Copyright (c) 2026 BitTug Contributors.
