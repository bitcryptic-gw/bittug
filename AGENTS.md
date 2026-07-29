# sensecap-m1-gateway — Agent Context

## What this is

A self-hosted Helium IoT gateway and ADS-B (Wingbits) station running on Raspberry Pi 4B + RAK2287 (SX1302 LoRa) + ATECC608A ECC chip. Debian Trixie, ARM64. No Docker Hub, no container registry, no CI/CD pipeline — just a live git clone on the Pi.

## Deploy pattern — read this first

- **Repo on Pi:** `/opt/gateway/` (live git clone)
- **Deploy:** commit on Mac → push → `git pull` on Pi → `sudo systemctl restart <service>`
- **Never** run git operations beyond `git pull` on the Pi
- **Never** use `git commit`, `git push`, `git merge`, or `git rebase` on the Pi

## Device access — read this before touching any SSH/SCP command

**Default position: OC must NEVER establish an SSH or SCP session to
any device, for any reason, without the current task's prompt
explicitly naming that device AND explicitly stating that this task
authorizes access to it.**

This applies with no exceptions:

- A device being mentioned, described, or referenced in a prompt is
  **not** the same as being authorized. Authorization must be
  explicit — e.g. "this task grants permission to SSH into
  `root@<hostname>`" — not implied by the device's name simply
  appearing in background/context text.
- A device having been accessed in a **previous** task or session is
  **not** standing approval. Every task starts from zero. Permission
  never carries forward, even to a near-identical follow-up task, even
  later in the same day, even from the same person.
- If a task's background material happens to include a device
  hostname, SSH target, or connection detail, and the task itself does
  not explicitly authorize connecting to it, **do not connect**. Proceed
  with the parts of the task that don't require device access, and
  flag the device-access gap as an open question in your output
  instead of resolving it yourself.
- This applies equally regardless of how low-risk the access seems,
  how read-only the intended commands are, or how confident OC is that
  the person would say yes if asked. Confidence is not authorization.
  Ask.
- If a prompt is ambiguous about whether device access is authorized,
  treat it as **not authorized** and ask, rather than proceeding and
  hoping the ambiguity resolves in OC's favor.

**Before running any command that begins with `ssh` or `scp` (or
invokes them indirectly, e.g. via a script), stop and confirm: does
*this specific task's prompt* explicitly authorize this specific
device? If the answer isn't a clear yes, do not run the command — ask
the person instead, stating the target device and the exact commands
you intend to run, and wait for their response before proceeding.**

This rule exists independently of, and in addition to, any per-prompt
reminder about device access that may also appear in a given task's
instructions. The rule lives here so it cannot be missed by a task
that forgets to restate it.

**Before taking any action in this task, state explicitly: "Devices
this task authorizes me to access: [none, OR list them by hostname]."**
Do this even when the answer is "none" — the explicit statement is
the checkpoint, not a formality.

## Repo layout

```
config/       — lora_pkt_fwd and gateway-rs config files
scripts/      — helper and setup scripts
systemd/      — service unit files (copied to /etc/systemd/system/ on device)
gateway-ui/   — FastAPI web UI source
pktfwd/       — packet forwarder binaries/config
docker/       — Docker configs for non-Helium/non-Wingbits workloads only
boot/         — boot config fragments (config.txt, future bootstrap.sh)
```

## Services

| Service | Binary | Purpose |
|---|---|---|
| `pktfwd.service` | `lora_pkt_fwd` 2.1.0 | LoRa packet forwarder (sx1302_hal) |
| `gateway-rs.service` | `helium_gateway` 1.3.0 | Helium IoT mainnet gateway |
| `gateway-ui.service` | FastAPI (Python) | Web UI, port 8080, Bearer token auth |
| `readsb.service` | readsb | ADS-B decoder (Wingbits) |
| `wingbits.service` | wingbits client | Wingbits data feed + GeoSigner |
| `tailscale-autoconnect.timer` | `scripts/tailscale-autoconnect.sh` | Auto re-auth with saved key (`/etc/gateway/tailscale.key`, 0600 root:root) when tailscaled reports NeedsLogin |

## Key hardware & identity

- **LoRa:** RAK2287, SPI `/dev/spidev0.0`, AU915
- **ECC:** ATECC608A, i2c-1 0x60 (Helium swarm key slot 0, onboarding slot 15)
- **GPIO:** Reset BCM 17, Power BCM 27, SX1261 BCM 5
- **Helium identity:** `jumpy-carrot-salmon`
- **Fake GPS:** -33.7936 / 151.2489 / 65m (Helium only — Wingbits uses GeoSigner GPS)

## Web UI

- URL: `http://sensecap-m1:8080` (Tailscale only)
- Auth: Bearer token stored at `/etc/gateway-ui/token`
- Runs as: `gateway-ui` user (groups: `systemd-journal`, `i2c`). Privilege escalation via setuid wrappers (`scripts/*-wrapper.c`)
- Tabs: band selection, live logs, restart buttons, token regen, CPU temp, Wingbits status

## Primary user ownership — critical

`/opt/gateway/` must be owned by the primary non-root user, not root. The primary user is whoever was created during OS setup — it is **not** hardcoded and will differ between installs.

**Never hardcode a username** (`bitcryptic` or otherwise) in any provisioning script, systemd unit, or bootstrap code. Always derive the primary user at runtime:

```bash
PRIMARY_USER=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}')
```

The bootstrap script (`boot/bootstrap.sh`, not yet written — roadmap item) is responsible for:
- Deriving the primary user dynamically
- Creating `/opt/gateway/` with correct ownership before cloning
- Cloning the repo as the primary user (not root)
- Installing systemd units and running first-time setup

Until the bootstrap script exists, provisioning is manual. The correct sequence is:

```bash
PRIMARY_USER=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}')
sudo mkdir -p /opt/gateway
sudo chown "$PRIMARY_USER:$PRIMARY_USER" /opt/gateway
git clone https://github.com/bitcryptic-gw/sensecap-m1-gateway /opt/gateway
```

## Docker policy

Docker is installed on the device but is **not used for Helium or Wingbits**. Both stacks run as native systemd services. Docker is reserved for unrelated future workloads. Do not containerise readsb, wingbits, lora_pkt_fwd, helium_gateway, or gateway-ui.

## Wingbits specifics

- Stack: `readsb` (native) + `wingbits` client (native), both managed by systemd
- Beast mode is mandatory — NET_OPTIONS must include:
  `--net-connector localhost,30015,beast_reduce_out --net-beast-reduce-optimize-for-mlat --net-beast-reduce-interval=0.125`
- SDR symlink: `/dev/rtlsdr0` (via udev rule `99-rtlsdr.rules`)
- GeoSigner: USB device, handled by the official wingbits client
- Setup/reconfiguration: `sudo /opt/gateway/scripts/wingbits-setup.sh --loc "<lat>, <lon>" --id "<station-id>"`
- readsb must tolerate absent SDR hardware (retry loop, does not block boot)

## Coding standards

- Shell scripts: `set -euo pipefail`, executable bit committed
- Python: validate all inputs, enforce request body size limits
- Secrets: never in CLI args or env vars visible in process listings — use file mounts (`/etc/gateway-ui/token`, etc.)
- Token comparisons: timing-safe only
- Refuse to start if required secrets are unset
- Principle of least privilege throughout — services run as dedicated users, not root

## What not to do

- Do not hardcode any username — always derive the primary user dynamically
- Do not add dependencies that require internet access at runtime
- Do not introduce a build step that requires the Pi (all compilation happens on Mac or in CI)
- Do not add Docker for anything Helium or Wingbits related
- Do not generate `AGENTS.md` content speculatively — update it only to reflect real changes
- Do not use `git` beyond `git pull` in any script that runs on the Pi
