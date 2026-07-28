# Zombie watchdog — Gap-closure recon report

**Date:** 2026-07-28
**Context:** Follow-up to `ZOMBIE-WATCHDOG-FINDINGS.md`, closing two load-bearing gaps before fix scoping.
**Tailscale commit:** `a7cb5745a2ee92e9edd4c6b79457e03749347d19` (verified installed on Perth: `tailscale version` returns this commit hash)

---

## 2.1 Reconcile the ~21-minute recovery timing (Gap A)

### Actual counter mechanism

Verified by re-reading `scripts/tailscale-autoconnect.sh` (already done in prior report, confirmed here):

- **Counter location:** `/run/tailscale-autoconnect.count` (tmpfs, line 107)
- **Persistence:** In-memory only (tmpfs). Cleared on reboot. Does NOT persist across reboots, does NOT persist across `gateway-ui` restarts, does NOT persist across `tailscaled` restarts (tmpfs survives daemon restart). The counter file only exists when in degraded state.
- **Current state on Perth:** File does not exist (confirmed: `cat /run/tailscale-autoconnect.count` → "No such file or directory"). Device is healthy, so the counter was reset on the last healthy observation.
- **Read logic** (line 114-119): `cat $COUNT_FILE 2>/dev/null || echo 0`
- **Write logic** (line 177-178): `echo "$count" > "$COUNT_FILE"`
- **Reset conditions:** Called `reset_count()` (line 112, `rm -f "$COUNT_FILE"`) when:
  - `Self.Online == true` (healthy observation, line 173-174)
  - Backend is `Stopped` (respecting operator intent, line 163-165)
  - Successful re-auth completes (line 257, 263)
- **NOT reset:** on `gateway-ui` restart, on `tailscaled` restart, on script failure, on reaching-but-not-exceeding threshold.

### Timer cadence confirmed

`systemctl show tailscale-autoconnect.timer` on Perth:

```
TimersMonotonic={ OnUnitActiveUSec=10min ; next_elapse=6d 14h 48min 14.081817s }
TimersMonotonic={ OnBootUSec=3min ; next_elapse=3min }
```

Journal confirms the ~10-minute cadence is stable in practice (sample from 2026-07-28):

```
05:49:36 → healthy
05:59:44 → healthy  (gap: 10m 8s)
06:09:54 → healthy  (gap: 10m 10s)
06:19:59 → healthy  (gap: 10m 5s)
06:30:04 → healthy  (gap: 10m 5s)
06:40:09 → healthy  (gap: 10m 5s)
...
09:02:28 → healthy  (most recent fire)
```

The small variance (~10-30 seconds) is explained by `settle_state 12` taking ~1-2 seconds on a healthy daemon (the poll completes as soon as tailscaled reports Running) plus systemd scheduling jitter.

### Capture artifacts from 2026-07-26

**Not recoverable.** Journal logs for `tailscale-autoconnect.service` on July 26 return "No entries" — rotated out (journal is 33.1MB total). No capture script output files found in `/tmp`, `/var/log`, `/opt/gateway`, or `/root`. Counter file is long gone (healthy device erased it).

### Concrete explanation for ~21 minutes

**The ~21-minute figure IS an expected outcome of the timer model, not a discrepancy.** The model predicts recovery time = `timer_phase_offset_from_deletion + (DEGRADED_THRESHOLD × 10min) + recovery_duration`.

With `DEGRADED_THRESHOLD=3`:
- Best case (phase ≈ 0 min): recovery at ~21 minutes (0 + 30 + ~1)
- Expected case (phase ≈ 5 min): recovery at ~26 minutes
- Worst case (phase ≈ 10 min): recovery at ~31 minutes

Both capture runs landing at ~21 minutes means the deletion happened very shortly before a timer fire in both cases. This is plausible: both captures were actively monitored (Gary performing the deletion), and the deletion action was likely coordinated with or happened to align with the timer's phase.

**The prior report's ~30-minute "expected" figure assumed the timer fires from the start of the zombie condition, not from a pre-existing phase.** The timer was already running on a 10-minute cadence before the deletion — recovery doesn't "start a new timer at T+0," it depends on where the existing cycle happens to be.

### What `DEGRADED_THRESHOLD=2` would produce

With `THRESHOLD=2`, recovery = `phase + 10min + ~1min`:

| Phase (min) | Recovery (min) |
|-------------|---------------|
| 0 (timer fires right as deletion happens) | ~11 |
| 5 (average phase) | ~16 |
| 10 (timer just fired before deletion) | ~21 |

**Range: 11–21 minutes. Average: ~16 minutes.**

This is a material improvement from threshold=3 (21–31 min), approximately halving the dark window. The risk is that it doubles the false-positive surface for transient control-plane blips — a 2-observation window at 10-min cadence means any degradation lasting >10 minutes trips recovery. This is still conservative: the `not-in-map-poll` warnable has `TimeToVisible: 8 * time.Minute`, so transient network hiccups shorter than 8 minutes are invisible to `Self.Online`, and the 10-minute observation cadence adds a further buffer.

**Assessment:** `DEGRADED_THRESHOLD=2` is safe and effective. The real safeguard against false positives is the `curl` probe gate (lines 188-197), not the threshold count — the probe gate prevents any restart when the internet is actually down, regardless of threshold.

---

## 2.2 What does `network-status` actually check? (Gap B)

### Precise definition of the `network-status` warnable

From `health/warnings.go` at the exact commit (source citation: `warnings.go:72-82`):

```go
var NetworkStatusWarnable = condRegister(func() *Warnable {
    return &Warnable{
        Code:                tsconst.HealthWarnableNetworkStatus,
        Title:               "Network down",
        Severity:            SeverityMedium,
        Text:                StaticMessage("Tailscale cannot connect because the network is down. Check your Internet connection."),
        ImpactsConnectivity: true,
        TimeToVisible:       5 * time.Second,
    }
})
```

`NetworkStatusWarnable` has **no `DependsOn`** — it's a root-level warnable.

### What sets/clears it

From `health/health.go` `updateBuiltinWarnablesLocked()` (source citation: lines in the `updateBuiltinWarnablesLocked` function):

```go
if v, ok := t.anyInterfaceUp.Get(); ok && !v {
    t.setUnhealthyLocked(NetworkStatusWarnable, nil)
} else {
    t.setHealthyLocked(NetworkStatusWarnable)
}
```

`anyInterfaceUp` is set via `SetAnyInterfaceUp(bool)` which is called by the network monitor (`netmon`). The field is typed `opt.Bool` — when empty (unknown), it defaults to the else branch (healthy). `anyInterfaceUp` tracks **whether any local network interface has link-up**, not whether the internet is reachable and not whether the control plane is reachable.

**Conclusion: `network-status` checks local interface link state only. It does NOT check internet reachability, and it does NOT check control-plane reachability.**

### How `DependsOn` suppression works mechanically

From `health/state.go` `isEffectivelyHealthyLocked()` (full function):

```go
func (t *Tracker) isEffectivelyHealthyLocked(w *Warnable) bool {
    if _, ok := t.warnableVal[w]; !ok {
        // Warnable not found in the tracker. So healthy.
        return true
    }
    for _, d := range w.DependsOn {
        if !t.isEffectivelyHealthyLocked(d) {
            // If one of our deps is unhealthy, we're healthy.
            return true
        }
    }
    // If we have no unhealthy deps and had warnableVal set,
    // we're unhealthy.
    return false
}
```

This is a **recursive chain**: if a dependency is unhealthy, the warnable is treated as "effectively healthy." The dependency check traverses the full dependency tree. The function returns `true` (suppress/healthy) if ANY ancestor in the dependency chain is unhealthy.

**Where it's used:** `CurrentState()` (state.go) — the structured health state exposed via `watch-ipn --initial-health`. This is the same data structure the prior captures obtained. `Strings()` and `stringsLocked()` also use it — the flat human-readable Health array.

**Where it's NOT used:**
- `GetInPollNetMap()` (health.go) — returns `t.inMapPoll` directly, a raw boolean. No DependsOn filtering.
- `SetUnhealthy` — the warnable IS set to unhealthy even if a dependency is unhealthy. Suppression happens at query time, not at set time.

### `not-in-map-poll`'s full dependency chain

From `warnings.go`:
```go
notInMapPollWarnable.DependsOn = []*Warnable{NetworkStatusWarnable, IPNStateWarnable}
```

Plus, implicitly via `unhealthyState()` in `state.go`, the `warming-up` warnable code is appended to `DependsOn` in the displayed `UnhealthyState` (but NOT in the Warnable struct — so it affects display metadata but NOT suppression).

`IPNStateWarnable` (= `wantrunning-false`) is set when `!t.ipnWantRunning` (i.e., operator ran `tailscale down`). It has no dependencies.

### During a Tailscale control-plane-side outage

**Scenario:** Tailscale's control infrastructure is degraded or unreachable. Local internet is fully healthy (all interfaces up, internet reachable).

**What happens:**
1. `anyInterfaceUp` = true (local interfaces are up) → `NetworkStatusWarnable` = **healthy**
2. `ipnWantRunning` = true (operator didn't run `down`) → `IPNStateWarnable` = **healthy**
3. Map poll fails (can't reach control) → `SetOutOfPollNetMap()` called → `inMapPoll` = false
4. After 10-second debounce: `notInMapPollWarnable` = **unhealthy** with `BrokenSince` set
5. `isEffectivelyHealthyLocked(notInMapPollWarnable)` checks dependencies:
   - `NetworkStatusWarnable`: `warnableVal` is nil (healthy) → returns true
   - `IPNStateWarnable`: `warnableVal` is nil (healthy) → returns true
   - All deps healthy → returns **false** (the warnable IS effectively unhealthy)
6. `Self.Online` = `GetInPollNetMap()` = `t.inMapPoll` = **false**
7. `CurrentState().Warnings` includes `not-in-map-poll` (its dependencies are all healthy, so no suppression)

**`Self.Online` is false during a control-plane outage. The DependsOn chain does NOT suppress it, because all dependencies are healthy (local network is fine, Tailscale is not deliberately off).**

### Revisiting false-positive table

The prior report's 4.6 table entry for "control plane maintenance (>12 min)" was marked "possibly — needs verification." The answer is now confirmed:

**During a control-plane-side outage: `Self.Online` IS false, and `BackendState` remains "Running."** The dwell threshold would trip. The only thing preventing a restart is the autoconnect script's `curl` probe gate — `curl https://controlplane.tailscale.com/` would fail during a control-plane outage, skipping the restart.

### Restart-rate-limit guard assessment

**Confirmed necessary.** Without a rate-limit guard, a multi-hour control-plane outage would:
1. Cause `Self.Online == false` continuously
2. Pass the dwell threshold
3. Attempt recovery every dwell period
4. Each attempt would: restart tailscaled, land in NeedsLogin (can't reach control), attempt `tailscale up` with auth key (would fail — can't reach control), exit with error
5. On the next cycle: counter=1 again (because the healthy observation after the previous successful `up` never happens), re-trigger after dwell

**The `curl` probe gate in autoconnect prevents this specific scenario** — the probe to controlplane.tailscale.com fails, skip → no restart. This is why the autoconnect script's probe gate is load-bearing, not just a nice-to-have.

For the gateway-ui alert-only observer: if it only alerts and leaves recovery to autoconnect, no restart-rate limit is needed (no recovery action to rate-limit). If it also performs recovery, the probe gate (or a rate-limit guard) is essential.

### Correction to prior report's claim about `Self.Online`

**The prior report's claim that `Self.Online` is "a genuine discriminator" because `network-status` suppression makes it stay `true` during genuine outages is INCORRECT.**

The source code shows:
1. `GetInPollNetMap()` returns `t.inMapPoll` — a raw boolean, unfiltered by DependsOn
2. `Self.Online` is set from `b.health.GetInPollNetMap()` (`ipn/ipnlocal/local.go:1519`) — the raw boolean
3. `Self.Online` goes `false` whenever the node is not in a map poll, period — regardless of whether `not-in-map-poll` is being suppressed in the Health array

**The actual discriminator is the `curl` probe gate in the autoconnect script**, not `Self.Online`. `Self.Online == false` tells you "something is wrong." The probe gate tells you whether "internet is reachable" — if reachable, the problem is specific to this node (zombie) rather than a site-wide or infrastructure-wide outage.

**This does not invalidate the recommended design,** but it changes the reasoning: the alert-only observer in gateway-ui should NOT claim to distinguish zombie from outage. It should fire an alert that says "Tailscale appears offline (Self.Online = false, BackendState = Running)" without claiming to know the cause. The autoconnect script handles discrimination and recovery correctly via its probe gate.

---

## 3. Closing

### Does either finding change the recommended design?

**The "autoconnect instrumentation + alert-only observer" reshape still holds**, with one adjustment:

1. **The gateway-ui alert should be a symptom alert, not a diagnostic alert.** It fires when `Self.Online == false AND BackendState == "Running" persists beyond dwell`, with a message like "Tailscale is running but reports as offline — autoconnect will attempt recovery." It does NOT claim to distinguish zombie from outage — that's autoconnect's job.

2. **The `curl` probe gate in autoconnect IS the discriminator, not `Self.Online`.** The prior report's confidence in `Self.Online` as a standalone discriminator was misplaced. This doesn't change the architecture — autoconnect already has the probe gate — but it means:
   - Any future change to autoconnect that removes or weakens the probe gate would create a false-positive path
   - The probe gate dependency should be documented in the autoconnect script header as load-bearing, not "nice to have"
   - The gateway-ui alert message should reference that autoconnect handles discrimination, so the operator doesn't panic when they get the alert during a known outage

3. **`DEGRADED_THRESHOLD=2` is safe and recommended**, producing 11-21 minute recovery (average 16 min) instead of 21-31 min. The probe gate prevents false positives.

4. **Restart-rate-limit guard is confirmed necessary** if the gateway-ui observer also initiates recovery. Not needed if it only alerts.

### Additional findings not asked about

**The `warming-up` warnable is implicitly a dependency of all other warnables for display purposes** (added in `unhealthyState()` in `health/state.go`), but NOT for suppression purposes (`isEffectivelyHealthyLocked` only checks the Warnable struct's `DependsOn` field). This means:
- During a `tailscaled` restart recovery, `warming-up` is active for ~5 seconds
- Other warnables (including `not-in-map-poll`) are displayed as depending on `warming-up` in the structured state
- But `Self.Online` and the suppression chain are NOT affected by `warming-up` — they continue to reflect raw state
- During recovery, there is a brief window (~5s) where `Self.Online` might be false AND `warming-up` is active simultaneously. The observer's dwell threshold (12+ minutes) means this transient never trips.

**The autoconnect script's `settle_state` function already handles the `warming-up` transient correctly**: it polls `BackendState` until it reaches one of `Running|NeedsLogin|Stopped`, avoiding any transitional states.

**The `TimeToVisible: 8 * time.Minute` on `notInMapPollWarnable` affects display only, not `Self.Online`**: The `BrokenSince` is set immediately, `inMapPoll` goes false immediately, `Self.Online` goes false immediately. The 8-minute delay only affects when the warning appears in the `Health` array of `tailscale status --json` (the flat strings) and in the `CurrentState().Warnings` map. This means the autoconnect script sees `Self.Online = false` within seconds of the map poll failing, not 8 minutes later. The dwell threshold counts from the first degraded observation, which can start within ≤10 minutes of deletion (the next timer fire after `Self.Online` goes false).
