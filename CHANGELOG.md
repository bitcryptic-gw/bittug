# Changelog

## 2026-09-17 — Relax firstrun/platform time-sync dependency; add boot diagnostics

**Why:** The v2026.09.17 real-device acceptance boot (spare Pi 3B, ethernet
unplugged ~4 min) showed `systemd-time-wait-sync.service` FAILED (the
`TimeoutStartSec=90s` drop-in firing as designed), `time-sync.target` reached
OK, and `gateway-firstrun.service` FAILED to start. The working hypothesis was
that `gateway-firstrun.service`'s hard `Requires=time-sync.target` propagated
the member's failure to the dependent unit.

**Verified (not assumed):** A container reproduction on systemd 257.13 (same
major as the device) using the **real vendor units** — `systemd-time-wait-sync`
forced to fail via a short `TimeoutStartSec` drop-in — shows the hypothesis is
**false**: `time-sync.target` reaches `active`, and a dependent with
`Requires=`+`After=` on it starts normally. The vendor unit only declares
`Before=`/`Wants=` the target (the target does not require the service), so
`Requires=time-sync.target` cannot propagate the timeout. The same holds for the
`network-online.target` shape. Failure only propagates when the target *itself*
`Requires=` the failing unit. The observed `gateway-firstrun` failure therefore
has a different, still-unidentified cause; this change is **hardening, not the
confirmed fix**.

**What changed:**
- `systemd/gateway-firstrun.service`, `systemd/gateway-platform.service`: dropped
  `Requires=time-sync.target`, keeping `After=time-sync.target`. The authoritative
  guard against a stale clock is the `git clone` / connectivity retry logic, not
  the target's clean success. `Requires=network-online.target` retained
  (deliberate, documented trade-off).
- `systemd/gateway-firstrun.service`: `StandardOutput=`/`StandardError=` are now
  `journal+console`, so first-run provisioning output is visible on the HDMI
  console even when SSH is closed and console input is dead.
- `boot/firstrun.sh`: an `ERR` trap logs the exact failing line before exit, so a
  failed run names where it died in the persistent log/console.
- `boot/build-image.sh`: drop-in `journald.conf.d/persistent.conf`
  (`Storage=persistent`, `SystemMaxUse=200M`) so the journal survives power-off
  and can be read off the SD card — journal volatility is what made this boot's
  failure reason unrecoverable. Stale comments corrected.

**Follow-up:** Re-flash and re-run the offline-boot acceptance test; with
persistent journal + console output the next failure (if any) will report its
own cause instead of requiring a flash-cycle bisect.

## 2026-09-16 — Bound the time-sync wait; surface boot-wait failures at login

**Why:** Two follow-ups from the fresh-flash saga. (1) Round 5 enabled
`systemd-time-wait-sync.service` to fix a stale-clock/TLS race but left its
vendor `TimeoutStartSec=infinity`, so a device with no working network at boot
blocks at `time-sync.target` forever and provisioning never starts — an
explicitly-accepted trade-off flagged as "revisit later" at the time. (2) The
Docker-race fix (previous entry) writes `/opt/gateway/.docker-pending` when the
install fails, but nothing surfaced that at login — a device stuck in that state
looked normal to a tester.

**What changed:**
- `boot/build-image.sh` (new step 5c-i): deploy a drop-in,
  `/etc/systemd/system/systemd-time-wait-sync.service.d/timeout.conf`, setting
  `TimeoutStartSec=90s`. The binary takes no `--timeout` argument (verified
  against upstream `units/systemd-time-wait-sync.service.in` and its man page),
  and the vendor unit's value is `infinity`, so the drop-in is the correct
  override point. 90s is comfortably above realistic NTP convergence on a flaky
  link (DHCP + wait-online + timesyncd's first exchange, typically <60s) yet
  short enough that a genuinely offline device proceeds in ~1.5 min. On timeout
  the unit fails, `time-sync.target` is still reached (`Before=` ordering, not
  `Requires=`), and provisioning continues — failure is visible, not a stall.
- `boot/gateway-provisioning-check.sh`: the round-4 login notice now reports the
  cause specifically — a time-sync timeout (detected via
  `systemctl is-failed systemd-time-wait-sync.service`) and/or a failed Docker
  install (`/opt/gateway/.docker-pending`), pointing at
  `/var/log/gateway-first-boot.log` and giving the matching remediation. The
  round-4 no-`exit`/`return` guard (profile.d is sourced) is preserved.

**Not done (by design):** the timeout drop-in is image-build-only, not rolled out
via OTA — a device already stuck at `time-sync.target` cannot run
`sync-provisioning.sh` to receive it anyway, and a device that boots with working
network never hits the unbounded wait.

## 2026-09-16 — Fresh-boot Docker install race: retry, no permanent silent success

**Why:** A fresh-flash field device (Birnie's `sensecap-rollin`, 2026-09-12)
came up with Docker entirely absent — no CLI, no apt source, no `/etc/docker`.
`gateway-platform.service` runs `scripts/first-boot.sh` only seconds after boot,
before outbound DNS/HTTPS is reliably usable, and its
`curl -fsSL https://get.docker.com | sh` had no retry. It failed on the first
attempt, the failure was swallowed as a warning, and the provisioning sentinel
was written anyway — so `gateway-platform.service` short-circuited forever and
Docker was never retried. Every `depin-*.service` declares
`Requires=docker.service`, surfacing as `Unit docker.service could not be
found.` This is the same boot-time network/clock race class already fixed for
`git clone` in `boot/firstrun.sh` (round 5) — the Docker install was simply
never given the same treatment.

**What changed:**
- `systemd/gateway-platform.service`: added `Requires=`/`After=time-sync.target`
  (mirroring `gateway-firstrun.service`) and promoted `network-online.target`
  from `Wants=` to `Requires=`. Trade-off documented in the unit: a device that
  never reaches network-online is left unstarted rather than partly provisioned;
  this adds no new failure class because the unit is already gated behind
  `gateway-firstrun.service`'s `Requires=time-sync.target`.
- `scripts/first-boot.sh`: the Docker install is now a 5-attempt / 10s retry loop
  behind a `docker_net_ready` pre-check (DNS resolution **and** an HTTPS fetch to
  `get.docker.com`), so "network not ready" is distinguishable from "installer
  genuinely broken" in the log. The installer's full stdout+stderr is `tee`'d
  into `/var/log/gateway-first-boot.log` (journald on these devices is volatile
  and had already rotated past the original failure).
- `scripts/first-boot.sh` (recovery path): the `.configured` sentinel is now
  written **only when Docker succeeded**. On failure a `/opt/gateway/.docker-pending`
  marker is written and the script exits non-zero, so `gateway-platform.service`
  re-runs the (idempotent) first-boot flow on the next boot instead of
  permanently short-circuiting. `sync-provisioning.sh` branch C is unchanged and
  remains the OTA-time no-op by design — this fix closes the contradiction from
  the other end.

**Not done (by design):** `sync-provisioning.sh` branch C still does not install
Docker when the general provisioned sentinel exists — the two recovery paths are
deliberately not both implemented. Pre-existing affected devices (Birnie's) are
being unblocked manually, not by this change.

## 2026-08-31 — MastChain (AIS-catcher) added as the 5th DePIN module

**Why:** Adds MastChain AIS (ship-tracking) coverage-earning as a Docker-based
DePIN module alongside Honeygain/URnetwork/Myst/Anyone — consuming the existing
`ghcr.io/c-man-the-man/mastchain-ais` image (built from the MastChain mastradar
fork), not a self-hosted build.

**What changed:**
- New `systemd/depin-mastchain.service` wrapping `docker run`, with the
  `ExecCondition=` RTL-SDR hardware gate (exit 77 = skipped, not failed — same
  mechanism as the Helium hardware fix), `--device /dev/bus/usb` passthrough,
  non-root `--user` override, and `--memory=128m --cpus=0.5`.
- Credentials stored file-based at `/etc/gateway-ui/depin/mastchain.env`
  (separate email + token, `640 root:gateway-ui`), combined into `USERPWD
  email:token` only in the unit's `ExecStart` — no world-readable credential
  file and no `User=root`, fixing MastChain's own installer's exposure.
- New `scripts/mastchain-hardware-check.sh` sysfs RTL-SDR presence probe
  (ExecCondition + live UI no-hardware / one-dongle-warning rendering).
- `depin-config-wrapper` gained a `mastchain <email> <token>` subcommand;
  `depin-logs-wrapper` allowlist, update-check IMAGES map, uninstall script, and
  `sync-provisioning.sh` (env-file durability, sudoers pull grant, RTL-SDR
  `MODE=0666` udev rule) all extended for the 5th project.
- gateway-ui: DePIN status/configure support + MastChain card with the
  credential warning and the one-dongle-one-spectrum warning (AIS ~162 MHz vs
  ADS-B 1090 MHz).
- Health/status log patterns are shipped as explicitly **unverified
  candidates** pending confirmation against a real device and account.

**Not done (per design):** no general user-supplied-container feature, no
`mastcontrol` CLI parity, no on-device image build or registry — the image is
consumed.

## 2026-08-30 — README repositioned: LoRa/Helium is optional, not required

**Why:** The README read as though a LoRa/Helium concentrator board was a
requirement to run BitTug. It documented Pi hardware-agnostic SBC support
but didn't make clear that the LoRa/Helium module is entirely optional —
the platform also runs Honeygain, URnetwork, Myst, and Anyone Protocol
with zero concentrator hardware at all.

**What changed:**
- Reframed the intro, "What This Is", and "What This Is NOT" sections
  around a base Pi platform with selectable DePIN modules, rather than
  leading with LoRaWAN/Helium.
- Added a hardware decision table (module → hardware needed) ahead of the
  detailed Helium-specific hardware table.
- Flagged the Band/Region Selection, "How It Works" diagram, and Building
  from Source sections as Helium-module-specific, skippable if you're only
  running the concentrator-free modules.
- Added a note to the Helium hardware table: verified working on the
  SenseCap M1; other Helium-class hardware using the same RAK2287/SX1302
  concentrator (e.g. Bobcat and similar miners) should work but is not yet
  tested.

**Not changed:** No functional/code changes — documentation only. Website
copy (bittug subdomain, not yet built) will carry the same framing once
that work starts.

## 2026-08-29 — Renamed to BitTug
The project has been renamed from **sensecap-m1-gateway / SenseCap M1
Gateway** to **BitTug**.
**Why:** The project is now proven working on bare Raspberry Pi 3B and
Pi 4 hardware, with no SenseCap M1 board and no Helium-class concentrator
attached at all. The SenseCap-specific name no longer reflected what the
software does; BitTug is positioned as a hardware-agnostic DePIN gateway
platform (any Pi 3B/4/5, any Helium-class concentrator, not just
SenseCap/RAK hardware).
**What changed in this release:**
- Project name / branding (web UI title and headers, docs, log messages,
  comments, systemd service *descriptions*) updated to **BitTug**.
- README hardware framing updated from "SenseCap M1 hardware only" to the
  hardware-agnostic positioning (the old claim was superseded by testing
  on bare Pi hardware).
- GitHub references (`REPO_URL` in boot scripts, OTA API constant,
  `Documentation=` URLs, release-body links, release-tag helper echoes)
  point at `bitcryptic-gw/bittug`; GitHub's redirect keeps old links
  working.
- **Release image artifact naming changed.** Built `.img.xz` files are
  now produced as `bittug-<version>.img.xz` (previously
  `sensecap-m1-gateway-<version>.img.xz`), and the CI workflow's
  artifact glob matches the new pattern. Previously published release
  assets under existing tags are not renamed and remain available under
  their original `.img.xz` names.
**Not changed (out of scope for this rename pass):**
- The `gateway-ui` systemd service/user/group name (on-device
  migration implications; flagged separately).
- The `sensecap` default SSH fallback login account.
- Device hostname conventions (`sensecap-<last6mac>` default hostnames).
- The NTFY push-notification tag (`sensecap`) — retained as an accurate
  hardware-category label, not a project-name reference.
- Existing live field devices are not automatically migrated by this
  change; this is a source-repo and fresh-provisioning change only.
