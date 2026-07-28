# Zombie-state watchdog — Recon Findings Report

**Date:** 2026-07-28
**Target:** Perth test unit `sensecap-8397f8.myth-nessie.ts.net`, Tailscale 1.101.284 (unstable)
**Mode:** Read-only recon. No edits, no commits, no tags.

---

## 4.1 Existing health/notifier surface in `gateway-ui`

### Loop interval and structure

`_ntfy_notifier()` at `gateway-ui/main.py:1569` is an `asyncio` task spawned at app startup (line 126). It runs a `while True` loop with a 60-second `asyncio.sleep(60)` at the tail (line 1825). The entire loop body is wrapped in `try/except Exception` (line 1822-1823); any unhandled exception is logged and the loop continues on the next iteration.

### Hysteresis: per-alert state persistence

State is held in the module-level dict `_ntfy_state` (line 1557-1565), which is **in-memory only**. It resets on every `gateway-ui.service` restart. No file backing. State keys and their initial value (`None`):

```python
_ntfy_state = {
    "helium_fault": None,
    "wingbits_fault": None,
    "cpu_temp_alert": None,
    "ram_alert": None,
    "storage_alert": None,
    "last_update_version": None,
    "tailscale_hostname_mismatch": None,
}
```

The hysteresis pattern is the same for every alert: compare current value against saved state, fire on detected transition, update state. Examples:

- **Binary toggle** (e.g. `tailscale_hostname_mismatch`, line 1729): `if ts_mismatch != _ntfy_state["tailscale_hostname_mismatch"]` — fires once on detect and once on resolve.
- **Threshold with hysteresis gap** (e.g. `cpu_temp`, line 1761-1778): trips at `>= 75.0`, recovers at `< 70.0` — a 5°C gap prevents flap.
- **RAM/storage** use the same gap pattern (`>= 90` trip, `< 85` recovery).
- **Version** (line 1672-1682): fires only when the latest version *changes* (not every loop iteration).

### First-run baseline suppression

`_ntfy_first_run` (line 1566) gates the first iteration: on startup, every current alert state is recorded but no notifications are sent (`gateway-ui/main.py:1648-1670`). This prevents a flurry of alerts on service restart. After the first run, `_ntfy_first_run` is set `False` and subsequent iterations follow normal hysteresis.

### Existing alert keys

Defined at `gateway-ui/main.py:67-71`:

```python
ALLOWED_ALERT_KEYS = {
    "update_available", "helium_fault", "wingbits_fault",
    "cpu_temp", "ram", "storage", "reboot", "shutdown",
    "tailscale_hostname_mismatch",
}
```

A new zombie alert key would need to be added here and to the `_ntfy_state` initialization dict.

### Config schema

`/etc/gateway-ui/ntfy.json` shape (from `_load_ntfy_config()` at line 297, `api_ntfy_config_set` at line 1496):

```json
{
  "server": "https://ntfy.example.com",
  "topic": "my-topic",
  "token": "tk_...",
  "enabled_alerts": ["update_available", "helium_fault", ...]
}
```

The `enabled_alerts` field is an array. When a new key is added to `ALLOWED_ALERT_KEYS` and the code is deployed to an already-provisioned device, the loaded `enabled_alerts` set from the saved `ntfy.json` will NOT include the new key. The code that decides whether to include it by default is `api_ntfy_config_get` at line 1484-1492:

```python
"enabled_alerts": cfg.get("enabled_alerts", list(ALLOWED_ALERT_KEYS)),
```

**The default (`list(ALLOWED_ALERT_KEYS)`) applies only when the key is entirely absent from the saved JSON, not when the saved array simply omits the new entry.** An already-provisioned device with a saved `enabled_alerts` list will have the new key absent from that list.

**Mitigation assessment:** The frontend settings page calls `api_ntfy_config_get` on load (which returns `enabled_alerts` as loaded from disk), renders checkboxes, and on save calls `api_ntfy_config_set` which validates keys against `ALLOWED_ALERT_KEYS`. The user must explicitly check the new checkbox to enable it after an OTA. This is reasonable for an opt-in feature, but if the intent is that the new alert defaults to ON for all devices, a migration step in `sync-provisioning.sh` or explicit handling in `_load_ntfy_config` would be needed.

### `_check_tailscale_hostname_mismatch` call sites

`grep -rn "_check_tailscale_hostname_mismatch("` across the repo found **5 matches** (not 4 — includes the definition):

| Line | Call site | Unpack count |
|------|-----------|-------------|
| 442 | `api_sysinfo` | 4 values |
| 997 | **definition** | — |
| 1109 | `api_network_tailscale` | 4 values |
| 1665 | `_ntfy_notifier` (first-run baseline) | 4 values |
| 1728 | `_ntfy_notifier` (alert check) | 4 values |

**All 4 call sites unpack 4 values** (`ts_mismatch, sys_hostname, ts_hostname_actual, ts_mismatch_type`). No silent breakage risk at this time.

---

## 4.2 How gateway-ui currently reads Tailscale state

### Shell-out invocations

All via `_run()` (synchronous, `subprocess.run`, `shell=False`, 10s default timeout) or `_run_async()` (async equivalent). Every invocation runs as the **`gateway-ui`** system user (the service user in `gateway-ui.service` line 8). No explicit timeout override on tailscale calls.

| Location | Command | Purpose |
|----------|---------|---------|
| `main.py:576` | `tailscale status --json` | `_tailscale_hostname()` helper |
| `main.py:1007` | `tailscale status --json` | `_check_tailscale_hostname_mismatch()` |
| `main.py:1050` | `tailscale status --json` | `api_network_tailscale` — full status |
| `main.py:1062` | `tailscale version` | Version string for display |
| `main.py:1096` | `tailscale debug prefs` | Read SSH/routes prefs |

No direct socket reads. All state comes through the CLI binary.

### Operator access

Perth confirmed (`sync-provisioning.sh:27` / `tailscale debug prefs` output):

```
OperatorUser: "gateway-ui"
```

Verified on Perth:
- `sudo -u gateway-ui tailscale status --json` → **succeeds** unprivileged
- Socket at `/run/tailscale/tailscaled.sock` → **mode 0666** (`srw-rw-rw-`), owned `root:root`
- `sudo -u gateway-ui curl --unix-socket /run/tailscale/tailscaled.sock ...` → reaches the socket successfully

**Conclusion:** `--operator=gateway-ui` grants access to `tailscale status --json` and the socket. No privilege escalation needed for read operations.

---

## 4.3 Is the structured signal readable without a long-lived subscription?

### Option 1: `tailscale status --json` — FAILS

**Confirmed on Perth, build 1.101.284:** The `Health` field is a flat array of human-readable strings, not structured warning objects.

```json
"Health": [
  "This is an unstable version of Tailscale meant for testing and development purposes. Please report any issues to Tailscale."
]
```

The LocalAPI `/status` endpoint returns the identical structure (confirmed via `curl --unix-socket`). **String-matching English warning text is not an acceptable detection primitive.** This option is ruled out.

### Option 2: LocalAPI health-specific endpoint — DOES NOT EXIST on this build

Tested endpoints on the LocalAPI socket (`/run/tailscale/tailscaled.sock`), all returning 404 or "invalid localapi request":

- `/localapi/v0/health` → "invalid localapi request"
- `/localapi/v0/health-check` → 404
- `/localapi/v0/health-check/local` → 404
- `/localapi/v0/warnable` → 404
- `/localapi/v0/health-warnings` → 404

The bus graph at `tailscale debug daemon-bus-graph` confirms the health system publishes on the IPN event bus (track `"Change"`, publisher `"health.Tracker"`) but no LocalAPI endpoint surfaces it as a point-in-time query on this build.

### Option 3: `tailscale debug watch-ipn` — WORKS as a one-shot with `timeout`

**This is the successful option.** The `tailscale debug watch-ipn --help` reveals:

```
--initial-health, --initial-health=false
    set NotifyInitialHealthState: send current health.State in first message (default false)
```

**Verified on Perth** (exit code 124 = killed by `timeout`, data arrived first):

```
$ timeout 5 tailscale debug watch-ipn --initial --initial-health 2>/dev/null
→ JSON document with structured "Health.Warnings" section
```

The structured data shape (from a healthy device):

```json
"Health": {
  "Warnings": {
    "is-using-unstable-version": {
      "WarnableCode": "is-using-unstable-version",
      "Severity": "low",
      "Title": "Using an unstable version",
      "Text": "This is an unstable version of Tailscale...",
      "BrokenSince": "2026-07-26T13:34:28.6785309+08:00",
      "Args": { "current-version": "1.101.284" },
      "DependsOn": ["warming-up"],
      "ETag": "137998866d36676d87b7e9fe5330f39c56124c70f2dd70c082b3357323ecd57b"
    }
  }
}
```

During zombie state, a `"not-in-map-poll"` entry would appear with its own `WarnableCode`, `BrokenSince`, `Severity`, and `DependsOn`.

**Watchdog invocation:**
```
timeout 5 tailscale debug watch-ipn --initial --initial-health 2>/dev/null
```
- Initial data arrives in <1 second
- Process blocks awaiting events, killed by `timeout` after 5s
- Cost: ~5s per poll in a 60s loop — negligible
- The `State` field (integer, `6` = Running) acts as `BackendState`

**Caveat:** `--initial-health` requires `--initial` to actually deliver the first message (tested: `--initial-health` alone produced no data within the timeout window).

### `Self.Online` as a cheaper alternative

**Confirmed on Perth:** `Self.Online` is present in both `tailscale status --json` and the LocalAPI `/status` response. On a healthy device it is `true`.

Per bcreane's analysis, `Self.Online` reads `health.GetInPollNetMap()`, which is the same health path that produces the `not-in-map-poll` warning. However, the `not-in-map-poll` warning includes `DependsOn: ["network-status", "wantrunning-false", "warming-up"]`, meaning it is suppressed when any of those dependencies are active.

**This dependency chain is what makes `Self.Online` a genuine discriminator, not merely the same signal twice:**
- During a zombie (network fine, map poll broken): `Self.Online` → `false`
- During a genuine outage (network broken): `not-in-map-poll` suppressed by `network-status` dependency → `Self.Online` stays `true`

**Recommendation:** The simplest and most robust detection approach uses `Self.Online == false` with `BackendState == "Running"` from the existing `tailscale status --json` call, paired with a self-maintained consecutive-observation counter (dwell). This avoids `watch-ipn` entirely, requires no additional subprocess calls, and naturally handles the DependsOn suppression chain. `watch-ipn --initial --initial-health` is available as a fallback for reading `BrokenSince` if sub-second precision on dwell start time is ever needed, but it is not required for the initial implementation.

---

## 4.4 Recovery action

### The autoconnect script's recovery path

`scripts/tailscale-autoconnect.sh` uses two distinct trigger conditions:

1. **`BackendState == "NeedsLogin"`** (line 168): Runs `tailscale up --auth-key=file:/etc/gateway/tailscale.key --operator=gateway-ui --ssh=<bool>` directly, preserving prefs. This re-registers in ~5s (measured).

2. **`BackendState == "Running"` + `Self.Online == false` for 3 consecutive observations** (line 172, threshold: `DEGRADED_THRESHOLD=3`): Restarts `tailscaled` via `systemctl restart`, then handles the resulting state. The restart is gated by a 10s `curl` probe of `https://controlplane.tailscale.com/` to avoid restarting during genuine outages.

**The critical question: does a bare `systemctl restart tailscaled` suffice, or is the auth-key `tailscale up` call what does the work?**

**Answer: Both. The restart alone lands in `NeedsLogin` (measured at 3s), then trigger-1 applies the auth key.** This is explicitly documented in the script header (lines 32-48) and confirmed by the measured timeline:
```
restart → NeedsLogin: 3s measured at 10:39:35
up → Running: 5s measured at 10:46:50
```

A bare `systemctl restart tailscaled` without a subsequent `tailscale up` with auth key would leave the node in `NeedsLogin` indefinitely — this is **strictly worse** than the ~21-minute autoconnect recovery. The watchdog must either:
- Call the full autoconnect script (which handles both paths), or
- Restart `tailscaled`, then invoke `tailscale-wrapper auth <key>` (which runs `tailscale up --auth-key=file:...`)

### Auth key status

Verified on Perth:

```
File: /etc/gateway/tailscale.key
Size: 62 bytes
Mode: 0600 (-rw-------)
Owner: root:root
```

The key is present (62-byte non-empty file), file-backed, reusable (auth keys are reusable by default), root-owned with 0600 perms. The key was last modified 2026-07-18 and the device has been connected since 2026-07-26 — the key has survived at least one re-registration cycle. **Contents not inspected per brief rules.**

### Wrapper infrastructure

`scripts/install-wrappers.sh` (23 lines) uses **glob auto-discovery**:

```bash
for src in "$SCRIPTS_DIR"/*-wrapper.c; do
```

No hardcoded array. Adding a new `*-wrapper.c` file is automatically picked up on the next OTA (which runs `install-wrappers.sh`).

Existing wrappers:
| Wrapper | Privileged action | Setuid |
|---------|-------------------|--------|
| `tailscale-wrapper` | `tailscale up --auth-key=file:...`, `tailscale set` | yes (root) |
| `system-power-wrapper` | `reboot`, `poweroff` | yes (root) |
| `wifi-toggle-wrapper` | `nmcli radio wifi on/off` | yes (root) |
| `wifi-connect-wrapper` | `nmcli device wifi connect` | yes (root) |
| `wingbits-setup-wrapper` | `wingbits-setup.sh` | yes (root) |
| `ota-update-wrapper` | `git pull`, `systemctl restart` | yes (root) |

**For watchdog recovery:** `tailscale-wrapper auth <key>` already handles the auth step. However, the wrapper validates `argv[0]` against the `gateway-ui` uid (line 375-378), then drops to root (`setuid(0)`) before running `tailscale up`. A `systemctl restart tailscaled` wrapper does not yet exist and would need to be created (or the watchdog could invoke the existing autoconnect script, which runs as root directly from the timer unit).

**Key safety check:** `gateway-ui.service` has `PrivateTmp=yes` (line 19) but **no** `NoNewPrivileges=yes`. This is correct — setuid wrappers require the ability to gain privileges. Any new wrapper invoking `/bin/bash` must use `bash -p` (preserve elevated privileges), as the `ota-update-wrapper.c` does at line 211 (`"/bin/bash", "-p", ...`).

---

## 4.5 Interaction with the existing autoconnect timer

### Timer interval

`systemd/tailscale-autoconnect.timer`:
```
OnBootSec=3min
OnUnitActiveSec=10min
```

10-minute cadence between runs. With `DEGRADED_THRESHOLD=3`, the autoconnect script triggers recovery at roughly **30 minutes** after the zombie condition first appears (3 × 10min observations).

### Reconciling the ~21-minute observed recovery

Both capture runs recovered at ~21 minutes from deletion, not the expected ~30 minutes. This is not fully explained by the timer interval alone. Possible explanations (all inference, not verified):
- The 3min `OnBootSec` on the first timer run after the last boot intersected with the zombie window in a lucky way
- Timer drift or accumulated skew between the deletion timestamp and the timer's phase
- The counter file at `/run/tailscale-autoconnect.count` may have persisted a partial count from a prior transient, reducing the effective threshold for that specific run

**No existing locking or single-instance guard.** The script is a `Type=oneshot` unit triggered by a timer. There is no `flock`, no PID file, no `ExecStartPre` mutual-exclusion check, and no `Conflicts=` in the unit. Two recovery mechanisms operating simultaneously would race:
- If the watchdog restarts `tailscaled` while autoconnect is mid-run: autoconnect's `settle_state` loop would pick up the new daemon state, likely landing in `NeedsLogin` and proceeding with `up` → correct outcome, but the restart itself would be redundant.
- If autoconnect runs its `systemctl restart tailscaled` while the watchdog is mid-check: the watchdog's `tailscale status --json` call would fail or return transitional state → likely safe (the watchdog should handle subprocess failures gracefully).
- If both try to run `tailscale up` with the auth key simultaneously: the second `up` call would fail (daemon already transitioning) or succeed harmlessly → no data loss, but noisy.

### Is the watchdog needed at all?

**Argument FOR:**
- ~21 minutes dark is a long time for a remote fleet device — IoT data, ADS-B data, and SSH access are all lost
- The recovery is incidental (a side effect of a different mechanism), not designed for this purpose. If the autoconnect timer is ever adjusted, disabled, or if its counter logic changes, the recovery goes away with no alert
- No one is notified that recovery happened — the incident is silent
- The watchdog would recover in ~12-15 minutes (dwell threshold) instead of ~21 minutes, saving 6-9 minutes of darkness
- A dedicated WATCHDOG with an NTFY alert provides observability that the incidental autoconnect path does not

**Argument AGAINST:**
- Two mechanisms doing the same thing is a complexity risk. A race between them is unlikely to break anything but adds operational noise
- The autoconnect script is already proven and maintained. Tightening its threshold (e.g. reducing to 2 observations) and adding NTFY alerting to it directly might be a simpler change than a new watchdog in gateway-ui
- The watchdog would need to read the auth key (root:root, 0600), which requires either a new setuid wrapper or invoking the existing autoconnect script — adding surface

**Honest assessment:** The right design is probably **not** a separate watchdog in gateway-ui. Instead, the autoconnect script should be:
1. Tightened: reduce `DEGRADED_THRESHOLD` from 3 to 2 (20 min → effective recovery at ~20 min)
2. Instrumented: add `curl` NTFY calls on detection and recovery, using the same NTFY config file `/etc/gateway-ui/ntfy.json` (which the autoconnect script already has read access to as root)
3. Made observable: fire "Gateway offline — recovering" and "Gateway recovered" NTFY alerts

This approach adds roughly zero new moving parts, uses the already-proven recovery path, and provides the missing observability. The gateway-ui notifier could then add a simpler "tailscale zombie detected" alert that reads `Self.Online` from its existing `tailscale status --json` call and fires purely for alerting — with recovery left to the autoconnect script.

However, if the preference is for gateway-ui to own both detection and recovery (single control plane), the watchdog design as sketched in Section 3 is viable, with the recovery action being: "invoke `tailscale-autoconnect.sh`" (not a bare `systemctl restart tailscaled`).

---

## 4.6 False-positive surface

### DependsOn suppression

Verified on Perth: the `is-using-unstable-version` warning carries `DependsOn: ["warming-up"]`. The `not-in-map-poll` warning (per prior captures) carries `DependsOn: ["network-status", "wantrunning-false", "warming-up"]`.

**What this suppresses in practice:**
- **`warming-up`**: Freshly started daemon, still dialling control. The warning is invisible during boot.
- **`wantrunning-false`**: Operator ran `tailscale down`. The node is deliberately offline — no false positive.
- **`network-status`**: Genuine internet outage at the site. The map poll failing is expected, so the warning is suppressed.

Since `Self.Online` reads `health.GetInPollNetMap()`, it inherits this dependency chain. `Self.Online == false` only surfaces when the network is fine but the map poll is specifically broken — the zombie fingerprint.

### Legitimate states that present as Running + not-in-map-poll for >12 min

| State | Would watchdog trip? | Harm from restart? |
|-------|---------------------|-------------------|
| **Site-wide internet outage** | No — `network-status` dependency suppresses `not-in-map-poll`, `Self.Online` stays `true` | N/A (won't trip) |
| **DERP-only degraded connectivity** | Possibly — DERP reachable means control reachable, but map poll could fail if DERP relay drops the specific control session. **Low confidence, needs verification** | Restart would re-establish control session; no auth key consumed if machine record intact. Minor (~15s) bounce. Low risk. |
| **Mid-OTA state** | Unlikely — OTA restarts services sequentially. If `tailscaled` is the service being restarted, `_service_info` would show non-"active". If gateway-ui is being restarted, the watchdog is not running. | N/A |
| **Device reboot/shutdown path** | No — `systemctl` reports transitional state, not "active"/"Running" | N/A |
| **Control plane maintenance (>12 min)** | Possibly — map poll fails, network is fine. `not-in-map-poll` would fire. **This is the most concerning false positive.** | Tailscaled restart would re-dial control; if control is still down, post-restart settle would time out or land in a transitional state. Could cycle-restart every dwell period until control returns. **Rate-limiting essential.** |
| **`--force-reauth` race condition** (the bug documented in autoconnect header lines 33-40) | Would present as Running + Online=false after `--force-reauth`. Dwell threshold would catch it. | A daemon restart fixes this bug (as documented). Triggered correctly — this is a true positive, not a false positive. |

### Restart-rate limit

**Strong recommendation:** A "recover at most once per N hours" guard must be part of the design. Without it, an extended control plane outage would trigger a daemon restart every dwell period (every ~15 minutes) indefinitely. For a 6-hour outage this is 24 unnecessary daemon restarts — noisy, wasteful, and contributes nothing.

A simple guard: maintain a `last_recovery_attempt` timestamp (in the in-memory `_ntfy_state` dict, or in `/run` like autoconnect's counter). If a recovery was attempted within the last 2 hours, skip recovery but continue alerting. The alert still provides value (operator knows something is wrong), and the autoconnect timer provides a redundant recovery path on its own cadence.

---

## 4.7 Blast radius and rollout

### Files a full implementation would touch

**If watchdog lives in gateway-ui (alert only, recovery delegated to autoconnect):**
1. `gateway-ui/main.py` — add `"tailscale_zombie"` to `ALLOWED_ALERT_KEYS`, `_ntfy_state`, and a new check block in `_ntfy_notifier()`
2. `scripts/tailscale-autoconnect.sh` — optionally add NTFY alerting on degraded detection and recovery
3. `systemd/tailscale-autoconnect.timer` — optionally tighten `DEGRADED_THRESHOLD`

**If watchdog in gateway-ui with full recovery:**
4. New file: `scripts/tailscale-restart-wrapper.c` — setuid wrapper for `systemctl restart tailscaled`
5. `scripts/sync-provisioning.sh` — add sudoers entry for the new wrapper OR add ntfy.json migration for new alert key

**If autoconnect-only approach:**
1. `scripts/tailscale-autoconnect.sh` — add NTFY alerting, optionally tighten threshold
2. (Nothing else — autoconnect already has full recovery logic)

### Deploy model compatibility

All approaches are compatible with `git pull` on Pi, no other git operations on device, tags cut from Mac. Nothing requires build infrastructure on the Pi beyond what already exists (gcc for wrapper compilation, handled by `install-wrappers.sh` during OTA).

### Provisioning sync

For already-provisioned devices (Birnie's unit):
- If a new `ALLOWED_ALERT_KEYS` entry is added but the device already has a saved `enabled_alerts` array in `/etc/gateway-ui/ntfy.json`, the new key will be absent from that device's enabled list (see Section 4.1). A `sync-provisioning.sh` migration step would be needed to append the new key to existing `enabled_alerts` arrays, OR the frontend must explicitly prompt the user to enable the new alert type after OTA.
- If a new setuid wrapper is added, `install-wrappers.sh` handles compilation automatically (glob-based). No sync-provisioning change needed for the binary.
- If a new sudoers entry is needed, `sync-provisioning.sh` must be updated.

---

## 8. Recommendations

### Go / no-go / reshape

**No-go on the standalone watchdog as designed in Section 3. Reshape to: autoconnect instrumentation + gateway-ui alert-only observer.**

**Reasoning:**

1. The autoconnect script already recovers the device correctly. It has 300+ lines of battle-tested logic covering edge cases (false-positive restart, control-plane probing, prefs preservation, timeout budgeting). Duplicating any part of this in gateway-ui is unnecessary risk.

2. What's missing is observability: nobody knows the zombie happened or that recovery occurred. This can be fixed by adding ~20 lines of NTFY alerting to `tailscale-autoconnect.sh` itself, reading from the same `/etc/gateway-ui/ntfy.json` config.

3. The gateway-ui notifier can add a lightweight alert-only observer: in its existing 60-second loop, check `Self.Online == false AND BackendState == "Running"` from the already-called `tailscale status --json`, maintain consecutive-observation count, fire one NTFY alert on detection, one on resolution. No recovery action — leave that to autoconnect.

4. If faster recovery is desired, reduce `DEGRADED_THRESHOLD` in autoconnect from 3 to 2. This gives ~20-minute recovery instead of ~30-minute, without adding any new code paths.

### The simplest path to production

1. Add NTFY alerting to `tailscale-autoconnect.sh` (alert on degraded detection, alert on successful re-auth, alert if recovery fails)
2. Add `"tailscale_zombie"` to `ALLOWED_ALERT_KEYS` and implement a read-only observer in `_ntfy_notifier()` that fires an alert when `Self.Online == false AND BackendState == "Running"` persists beyond dwell
3. Optionally tighten `DEGRADED_THRESHOLD=2` in autoconnect

### Open questions not answerable from recon

1. **What exactly caused the ~21-minute recovery in capture runs?** The timer interval + threshold predicts ~30 minutes, not 21. This discrepancy is unexplained. A non-invasive experiment: log the exact wall-clock time of each autoconnect timer fire and each `read_count` increment during a controlled zombie window to reconstruct the counter progression.

2. **Does `Self.Online` genuinely suppress during extended internet outages on this build?** We have the dependency-chain analysis but no live observation of a real multi-hour outage. This should be verified before relying on `Self.Online` as the sole discriminator.

3. **What is the `not-in-map-poll` `TimeToVisible` on the unstable track?** The stable track uses 8 minutes; unstable may differ. This affects the minimum safe dwell threshold. Check `health/warnings.go` in the Tailscale source at the specific commit (`a7cb5745a2ee92e9edd4c6b79457e03749347d19`).

4. **Can `watch-ipn` initial health data be obtained without `--initial`?** The help text says `--initial-health` sends health in the first message, but testing showed it needs `--initial` as well. May be a build-specific behavior. If Tailscale fixes this, the invocation could be simplified.

### Additional finding not in the brief

**The `watch-ipn` approach reveals a data path for ANY structured health check, not just `not-in-map-poll`.** If future Tailscale versions add new warnable codes for other conditions (DERP degradation, disk pressure, etc.), the same `--initial --initial-health` invocation provides access to all of them as structured objects with `WarnableCode`, `BrokenSince`, and `Severity`. This is a general-purpose health telemetry path that is currently unused by the project. It could eventually feed a richer monitoring dashboard or a more sophisticated alerting pipeline that doesn't require polling the Tailscale admin API.
