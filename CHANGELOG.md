# Changelog

## 2026-09-30 — Anyone card layout: stop the Change button dropping onto its own line

The Nickname, Contact, and Rewards wallet rows now use a stacked layout: the
label and its action button share the first line (`Contact  [Change]`) and the
value follows at full width, wrapping anywhere (`min-width:0`,
`overflow-wrap:anywhere`) so a long email+wallet can never push the button onto
an isolated line. The action-on-first-line pattern is applied consistently to
Nickname, Contact, and Rewards wallet. The Fingerprint row is unchanged (still
inline; only a `min-width:0` shrink guard so it can't overflow). No behavioural
changes. Also fixed a stale selector left by the round-4 rename —
`.anyone-rename-form` → `.anyone-edit-form` — which had silently dropped the
edit-input `max-width` cap.

## 2026-09-30 — Static caching: revalidate index.html so a post-OTA reload can't reuse old assets

Investigated whether a page can keep running stale JavaScript after an OTA. The
device serves `index.html` with **no** `Cache-Control`/`ETag`/`Last-Modified`,
and `app.js`/`style.css` with `ETag` + `Last-Modified` but no `Cache-Control`;
both assets are referenced as `/static/…?v=v<GATEWAY_VERSION>`. Because the
version changes on every OTA commit (`git describe --tags --always`), a fresh
index always points at a new asset URL — stale JS is unlikely. The residual gap
was the document itself: without any cache directive a browser *could* reuse a
cached `index.html` and thus keep the old version query. The `index()` response
now sets `Cache-Control: no-cache` (revalidate) — a one-line, no-dependency fix
that guarantees a reload always fetches the current version. Static asset
caching is unchanged (versioned URLs make it safe; `ETag` still allows 304s).
Note: the round-4 symptom that prompted this was actually a CSS bug (see the
wallet-button entry), not stale JS.

## 2026-09-30 — Anyone card: wallet button states, the real `.hidden` CSS bug, caution hint

Gary saw the **Rewards wallet** row showing a valid wallet *and* an **Add
wallet** button. Root cause found by testing: **not** stale JS but a CSS
specificity bug — `.hidden { display:none }` is defined before
`.btn { display:inline-flex }`, same specificity, so the later `.btn` rule won
and a `class="btn … hidden"` element stayed visible. Round-4's JS toggled the
class correctly; the CSS lost. Fixed `.hidden` to `display:none !important`
(also corrects the NTFY "Clear" button and the OTA "View OTA log" button, which
had the same latent bug).

Per decision, the button is now always visible on a configured card and
labelled by state via the single shared parser `parseAnyoneWallet(contact)`:
**Add wallet** (no valid wallet) / **Change wallet** (valid wallet).
- **Add wallet**: opens the shared form, focuses Contact, appends ` @anon: `
  only when no `@anon:` token exists (else caret at end).
- **Change wallet**: opens the form, focuses Contact, and selects **exactly the
  42-character `0x…` address** (`setSelectionRange` from the regex match range),
  so typing replaces the address and leaves the email + `@anon:` prefix intact.
- The new `anyoneWalletMatch()` returns `{wallet,start,end}`; `parseAnyoneWallet`
  wraps it, so the advisory, the card display, and the selection share one
  implementation.
- Clicking the wallet button while the form is already open does **not** close
  it — it re-applies focus/selection. Nickname/Contact Change-Cancel labels are
  unaffected; the wallet label updates on save/poll (add → change) without a
  reload and without clobbering an in-progress edit.
- Added a non-blocking caution hint (`#any-edit-wallet-hint`, its own element)
  shown only when the **saved** contact holds a valid wallet.

## 2026-09-30 — DePIN logs: trim docker-logs sudoers grants to Honeygain only

Round 4 replaced the setuid `depin-logs-wrapper` with five exact-command sudoers
lines mirroring the wrapper's allowlist. Only Honeygain actually calls
`docker logs` (`_depin_project_status`); the other four projects read journald,
so four of the five grants were unused privilege. Removed them:
`sync-provisioning.sh`'s sudoers heredoc now carries the single
`gateway-ui ALL=(root) NOPASSWD: /usr/bin/docker logs --tail 50 honeygain`
line (fingerprint line unchanged). The whole file is still regenerated from the
heredoc through the existing atomic path (temp → `visudo -c -f` → `mv`, 0440
root:root), so devices that already had all five lines lose the four unused ones
on the next run rather than accumulating stale grants. No code path referenced
the removed grants.

## 2026-09-30 — Anyone card: make the Change form discoverable for Contact; inline Add wallet

**Why:** only the Nickname row had a Change button and opening the form focused
the Nickname field, so the control read as nickname-only and the Contact field
(where the reward wallet goes) was easy to miss.

**What changed (frontend only: `index.html`, `app.js`, `style.css`):**
- A second **Change** button beside **Contact** opens the *same* single form.
  Opening from Contact focuses the contact field with the **caret at the end**
  (not select-all, so an existing email is not overwritten); opening from
  Nickname keeps the original focus+select. Both buttons stay in sync (both
  read "Cancel" while open) and Cancel resets both to "Change".
- The form has a clear heading **"Edit nickname & contact"**.
- The **Rewards wallet** row now always renders ("—" when empty) and, when the
  contact has no wallet token, shows a small inline **Add wallet** action. It
  opens the form focused on Contact and appends ` @anon: ` (only when no
  `@anon:` token is present), caret at the end. It never submits — the user
  still clicks "Save changes". The advisory warning and helper text are
  unchanged, as are validation, the save payload, the restart, and the shared
  `anyoneContactFieldHTML` template.
- Escape cancels the form; Enter still saves.

## 2026-09-30 — OTA UI: no more stuck "Updating…"; robust restart detection; lock release

**Why:** `runOtaUpdate`'s `finally` only re-enabled the Confirm Update button
when `gateway-ui.service` was **not** among the services. Web UI is ticked by
default, so a **failed** update (non-zero exit, or a stream that ended with no
exit code) left the button disabled on "Updating…" until a hard refresh.

**What changed:**
- **`gateway-ui/static/app.js`:** the button state is now decided by outcome.
  Success with `gateway-ui.service` keeps the countdown/reload; success without
  it (and every failure path) resets the button to "Confirm Update". A non-zero
  exit, a stream that ends without a result, and 409/503/other HTTP errors all
  show a clear failure message and **surface the OTA log** (new inline "View
  OTA log for diagnostics" button). The catch no longer keys on the
  browser-specific `e.message.includes('network')`: if `gateway-ui.service` was
  being updated and the stream dropped with no result, it is treated as a
  probable restart and the UI **polls `/api/identity` until it answers**, then
  reloads; after a 2-minute timeout the button resets and a message explains.
- **`gateway-ui/static/index.html`:** the inline diagnostics-log button.
- **`gateway-ui/main.py`:** the OTA in-progress guard (source of the 409) is now
  a small lock object released in the stream's `finally` (non-zero exit, normal
  completion, client disconnect) **and** on any error while constructing the
  response, with a 30-minute max-age backstop for the never-iterated-generator
  leak case, so a retry cannot wedge on a stale 409.

## 2026-09-30 — OTA wrapper: fast-forward-only pull, deterministic git env, failure diagnostics

**Why:** Perth's first OTA to `v2026.09.29` failed with git exit 128 ("Need to
specify how to reconcile divergent branches") on a clean fast-forward, and a
retry after a browser refresh succeeded — cause unexplained. The wrapper ran a
bare `git pull` with the real uid `gateway-ui`, inheriting the gateway-ui
service environment.

**What changed (`scripts/ota-update-wrapper.c`):**
- The bare `git pull` is now an explicit fast-forward-only update of an explicit
  remote/branch: `git pull --ff-only origin main`. No implicit merge or rebase,
  no dependency on `pull.ff`/`pull.rebase`, no hard reset ever.
- Every git child now runs with a **deterministic, explicit environment** (via
  `execve`, not inherited `execvp`): `PATH`, `HOME` set to the repo owner's home
  from `getpwuid`, `LANG=C`, `GIT_TERMINAL_PROMPT=0`. This removes inherited
  `HOME`/`GIT_CONFIG_*`/`GIT_DIR`/`GIT_WORK_TREE` as an explanation. Verified the
  repo needs none of them (origin is public HTTPS, no credential helper, no
  per-user gitconfig on the device).
- On **any** git failure, a diagnostics block is emitted to stderr (reaching the
  SSE stream and OTA log): real/effective uid+gid, the git-child `HOME`,
  inherited `GIT_*` names+values only, and the output of `git rev-parse HEAD`,
  `rev-parse FETCH_HEAD`, `rev-parse origin/main`, `merge-base HEAD
  origin/main`, `status -sb`, and `config --show-origin --get-regexp
  '^(pull|branch)\.'`. It does **not** dump the service environment. All
  diagnostic commands run as the dropped-privilege user, fixed argv, no shell.
- Security properties unchanged: fixed argv, allowlisted service list, no
  widening. Exit-code contract with main.py/frontend unchanged.

## 2026-09-30 — DePIN logs: exact-command sudoers instead of the setuid `depin-logs-wrapper`

**Why:** `depin-logs-wrapper` was a setuid-root binary that exec'd `docker logs
--tail 50 <project>` over a hardcoded five-name allowlist, purely so gateway-ui
(unprivileged) could read Honeygain's Docker json-file logs. Same finding as the
Anyone fingerprint read: there is no technical blocker to sudoers, so the
setuid-root surface is replaced by exact-command grants.

**What changed:**
- **Deleted `scripts/depin-logs-wrapper.c`.** `install-wrappers.sh` builds from
  a `*-wrapper.c` glob, so it simply stops building it.
- **`scripts/sync-provisioning.sh`:** the existing atomic sudoers write
  (temp file → `visudo -c -f` → `mv`, mode 0440 root:root) gains **five exact
  lines, one per container, no wildcards** — the one-to-one mapping of the old
  wrapper's allowlist:
  - `gateway-ui ALL=(root) NOPASSWD: /usr/bin/docker logs --tail 50 honeygain`
  - `... /usr/bin/docker logs --tail 50 urnetwork`
  - `... /usr/bin/docker logs --tail 50 myst`
  - `... /usr/bin/docker logs --tail 50 anyone`
  - `... /usr/bin/docker logs --tail 50 mastchain`
  The orphan cleanup block now also idempotently removes the orphaned
  `/usr/local/bin/depin-logs-wrapper` on every run (alongside the Anyone
  fingerprint orphan).
- **`gateway-ui/main.py`:** Honeygain's log read now runs the fixed argv
  `sudo -n /usr/bin/docker logs --tail 50 honeygain` (no shell); `--tail 50` is
  the constant `DEPIN_LOG_LINES`, kept in sync with the sudoers lines. Only
  Honeygain routes through Docker — the other four DePIN projects already read
  journald, which works — so the five grants preserve the wrapper's exact
  allowlist parity while the only caller remains Honeygain.
  `_run()` gained a `merge_stderr` flag used for this call: Honeygain writes to
  its own stderr, which `docker logs` replays on **our** stderr, and the old
  setuid wrapper explicitly merged stderr into stdout so `subprocess` (which
  captures stdout only) saw it. Without the merge the Honeygain log view was
  empty (observed on the first device deploy, then fixed). The implementation
  uses explicit `stdout`/`stderr` PIPEs, **not** `capture_output=True`, which on
  this Python raises `ValueError` when `stderr` is also passed — an initial
  version of this change used both and broke every subprocess call in the UI
  until it was caught on the device.
- Graceful degradation on failure is unchanged: `_run()` maps a missing binary /
  failed sudo to a negative rc, `_depin_project_status()` treats that as empty
  logs, and the card renders a neutral state — no 500, no error stack. During an
  OTA the old `main.py` keeps running until `gateway-ui` restarts, so there is a
  brief window where it calls the just-deleted wrapper; it degrades to the same
  neutral log view.

## 2026-09-30 — Anyone: post-enable nickname+contact edit; wallet-format evidence; restart-keyed fingerprint cache

**Item 1 — edit ContactInfo after first enable.** The configured Anyone card's
Change control now opens a **single form covering nickname and contact
together**, so editing both is one `POST /api/depin/anyone/configure` call and
therefore **one** relay restart. The card shows the current contact, and the
form reuses the *same* contact field (helper text + advisory warning) as the
first-enable form via one shared JS template (`anyoneContactFieldHTML`), so the
two can't drift. MyFamily is omitted from the payload and preserved server-side.
Server validation is unchanged (control chars/newlines/tabs/length still 400,
anonrc untouched on failure — temp-file+rename). The form is disabled while a
save is in flight and a `depinState.anyoneSaving` guard drops rapid repeat
submits, so no overlapping restarts. Note in the form: changes can take a few
hours to appear on dashboards; fingerprint/reputation unaffected.

**Item 2 — wallet advisory: kept the looser form, with source evidence.** The
live Anyone registry parser tolerates no-space / extra-space / tab forms, so
tightening to exactly one space would falsely warn on registerable input. The
parser is `anyone-protocol/operator-registry-controller`
(`src/validation/validation.service.ts`, `extractAtorKey`, pinned commit
`0bbc789`): it finds `@anon:` case-insensitively, then the next `0x`, takes 42
chars and validates with `ethers`. Its unit tests explicitly cover
`@anon:0x…` ("when not padded"), `@anon:  0x…` (two spaces), and tabs. The
advisory stays `@anon:\s*0x[0-9a-fA-F]{40}`. The advisory warning and the card's
parsed-wallet display now share one `parseAnyoneWallet()` function.

**Item 3 — cache the fingerprint until restart.** `_anyone_fingerprint()` is now
keyed on the `depin-anyone` systemd `InvocationID` (read via unprivileged
`systemctl show`, no sudo): a successful read is cached until the marker changes
(next relay start), with a 24h ceiling as a safety net. Negative results keep
the short 15s TTL; if the marker can't be read the cache falls back to the 60s
TTL rather than caching indefinitely. Target: ~one sudo log entry per relay
start instead of one per minute. Config/enable/uninstall still invalidate the
cache.

## 2026-09-30 — Anyone fingerprint read: sudoers instead of setuid wrapper; wallet ContactInfo hint

**Why (wrapper → sudoers):** The 2026-09-29 change read the Anyone fingerprint
via a new setuid-root `depin-anyone-fingerprint-wrapper` binary. Gary's
position: a setuid-root binary that execs docker is a larger attack surface
than an exact-match sudoers line, so the burden of proof was on the wrapper.
Investigation found **no technical blocker to sudoers**: `gateway-ui.service`
does **not** set `NoNewPrivileges` (it sets only `PrivateTmp=yes`), and the
service already runs many `sudo` commands successfully (systemctl enable/start/
stop/restart, docker pull, depin-uninstall) with the existing NOPASSWD grants.
The original `depin-logs-wrapper` setuid rationale was likewise convenience
("no provisioning script changes needed"), not a documented sudo limitation.

**What changed:**
- **Replaced the wrapper with an exact-command sudoers grant.**
  `scripts/depin-anyone-fingerprint-wrapper.c` is deleted; `install-wrappers.sh`
  no longer builds it. `sync-provisioning.sh` writes
  `gateway-ui ALL=(root) NOPASSWD: /usr/bin/docker exec anyone cat /var/lib/anon/fingerprint`
  (no wildcards) into `/etc/sudoers.d/10-gateway-ui` using the existing atomic
  temp-file + `visudo -c -f` + rename path, and **removes the orphaned setuid
  binary** `/usr/local/bin/depin-anyone-fingerprint-wrapper` idempotently on
  every run, so devices from the earlier build are cleaned up on OTA.
- `gateway-ui/main.py`: `_anyone_fingerprint()` now runs the fixed argv
  `sudo -n /usr/bin/docker exec anyone cat /var/lib/anon/fingerprint` (no shell).
  Behavior, caching (60s/15s) and the neutral "not available" state are
  unchanged; the fingerprint equals the raw `docker exec` output.
- **Anyone contact field: reward-wallet hint.** Per
  docs.anyone.io/dashboard/register.md, reward-claim eligibility requires the
  EVM wallet in `ContactInfo` as `@anon: 0x` + 40 hex. The form now carries a
  helper note with a fake example, states that ContactInfo is published in the
  relay's public descriptor (per the anon/tor manual: descriptors are archived,
  published and indexed), and shows a **soft, non-blocking** warning when the
  value lacks a well-formed wallet token. The card also displays the wallet
  parsed from the current contact, when present.
- Server-side hardening: contact is now explicitly rejected with **400** if it
  contains control characters (tab/CR/LF/other C0, DEL) — previously a tab
  reached the C writer and surfaced as a 500. `@` and `:` remain allowed.

**Reported, not changed:** the same setuid-vs-sudoers concern applies to
`depin-logs-wrapper` (it execs `docker logs <project>` over a 5-name allowlist;
sudoers would need five exact lines since wildcards are disallowed). Left as-is
for Gary to decide. Also: the Anyone contact field is **not editable after
first enable** (the config form hides once configured), so a user who enabled
without a wallet has no UI path to add one — flagged, not built.

## 2026-09-29 — Anyone relay: show fingerprint + nickname, add safe nickname rename

**Why:** The Anyone card only showed a running/healthy badge and generic log
tail — there was no way to see the relay's actual identity (fingerprint) or its
configured nickname, and no way to change the nickname after first enable
without editing `anonrc` by hand on the box.

**What changed:**
- New setuid wrapper `scripts/depin-anyone-fingerprint-wrapper.c` — runs
  `docker exec anyone cat /var/lib/anon/fingerprint` with fixed argv, no
  arguments accepted, no shell. Same setuid/allowlist pattern as
  `depin-logs-wrapper`; no new sudoers entries (nothing to extend in
  `sync-provisioning.sh`). Recompiled by `install-wrappers.sh`, which the OTA
  wrapper already runs every update.
- `gateway-ui/main.py` — `_anonrc_fields()` reads config (source of truth,
  root:root 0644), `_parse_anyone_fingerprint_file()` parses the
  `<nickname> <40-hex>` file defensively, and `_anyone_fingerprint()` reads via
  the wrapper with a 60s/15s server-side cache folded into the existing
  `/api/depin/status` payload (no new polling loop). Anyone status now carries
  nickname/contact/myfamily/fingerprint/`nickname_pending`.
- `POST /api/depin/anyone/configure` (already idempotent) now **preserves
  contact and MyFamily when omitted**, and **restarts `depin-anyone` when it is
  active** so a rename takes effect. Restart is a no-op on first-time configure
  (unit not yet enabled). `anonrc` is still written via the wrapper's
  temp-file + rename, so validation/write failures leave it untouched.
- `gateway-ui/static/{index.html,app.js,style.css}` — the Anyone card shows the
  nickname and the 40-hex fingerprint (monospace, space-grouped for
  readability, copy button yields the clean 40), a "not available yet" neutral
  state when the relay is stopped/uninitialised, and a Change/Cancel inline
  rename form noting the new name can take a few hours to appear on dashboards
  and that the fingerprint/reputation are unaffected. Full Uninstall is
  unchanged and stays visually distinct.

**Research (Anyone Protocol docs):** registration/rewards are keyed on the
relay's identity fingerprint + wallet address, never the nickname. The Anyone
native manual: *"Relays can always be uniquely identified by their identity
fingerprints."* Wallet association is written into `ContactInfo @anon: 0x…`;
the dashboard claims/renounces relays by fingerprint. (Sources:
docs.anyone.io/sdk/native-sdk/manual.md, /dashboard/register.md,
/dashboard/use.md, /dashboard/status.md.)

**Verified on pi4-urquhart** (OTA): fingerprint file format confirmed
(`UrquhartPi4 AF10EC2B31F7CD8E139A175E020CCDCB98F6F130`); card shows that
fingerprint + nickname; neutral state with the relay stopped; rename preserves
the fingerprint across restart; invalid/oversized inputs rejected with `anonrc`
untouched.

## 2026-09-17 — network-online.target false positive with no cable; gate firstrun on a real link

**Why:** The v2026.09.17.x acceptance boot (post-586cb74) showed
`network-online.target` reached while the ethernet cable was unplugged for the entire
window — `gateway-firstrun` ran through all 6 DNS retries (~60s) and failed before the
cable was ever connected. The DNS retry was correct; the network precondition it trusted
was false.

**Root cause (confirmed):** `NetworkManager-wait-online.service` runs `nm-online -s`
(`--wait-for-startup`). NetworkManager 1.52.1's man page: `-s` returns once NM logs
"startup complete", and "After startup has completed, nm-online -s will just return
immediately, regardless of the current network state"; the Debian trixie unit's own
comment notes devices reach a "conclusive activated or deactivated state" (deactivated,
with no carrier). So the unit succeeds, the target is reached, and firstrun runs with no
link.

**What changed:**
- `boot/build-image.sh` (5a): drop-in
  `/etc/systemd/system/NetworkManager-wait-online.service.d/require-connection.conf`
  clears the vendor `ExecStart` and replaces it with `/usr/bin/nm-online -q`, so the unit
  waits for a genuinely-activated connection and fails (rather than falsely succeeding)
  when offline.
- `boot/firstrun.sh`: new `wait_for_link()` — waits for an interface with carrier
  (`LOWER_UP`) AND a default route, up to 6 min — before the existing `wait_for_dns()` and
  apt steps. This is the effective gate: systemd targets aggregate `Wants=` weakly, so
  `network-online.target`'s "reached" state cannot itself be a hard gate without breaking
  the connect-later workflow.
- `systemd/gateway-firstrun.service`: `TimeoutStartSec` 600 → 1200 to cover the added link
  wait plus clone/bootstrap.
- `wait_for_dns()` is unchanged and still applies; the boot/ssh fix is untouched.

**Verified:** `wait_for_link` (extracted verbatim from the repo) run against real kernel
carrier states: no route → fail; carrier + route → pass; **route present but link
`NO-CARRIER` (the false-positive condition) → fail**. The systemd drop-in was verified to
replace the vendor ExecStart with `/usr/bin/nm-online -q`. A full firstrun re-run with a
carrier + route and DNS blackholed-then-released still completes (exit 0). The `nm-online
-s` false positive itself could not be reproduced in a container (no udev/systemd → NM
marks test devices unmanaged and never logs "startup complete"); the mechanism is
confirmed from NetworkManager 1.52.1's man page and the Debian trixie unit. The physical
unplug test on the spare Pi is the remaining confirmation.

**Follow-up:** Fresh-flash with no cable, connect after ~4 min; expect firstrun to wait for
the link, then provision.

## 2026-09-17 — Fix DNS-readiness race in firstrun.sh; ship SSH-enable marker in the image

**Why:** The v2026.09.17 fresh-flash acceptance boot (post-55f9b32) failed, but the
diagnostics added in 55f9b32 made the cause legible from the HDMI console alone (no
SD-card recovery). `network-online.target` had been reached, yet
`apt-get install git` died with `Temporary failure resolving 'deb.debian.org'` — the
same race class as the clock and Docker-registry races: "network online" does not mean
DNS is usable. Separately, the flashed boot partition had no `ssh` file, so SSH was
unavailable for initial access; `sshswitch.service` behaved correctly (file absent →
nothing to do). The build never created it — a gap, not a regression.

**What changed:**
- `boot/firstrun.sh`: added `wait_for_dns()` (6 attempts × 10s, via
  `getent hosts deb.debian.org`) before the git install, and wrapped
  `apt-get update` + `apt-get install git` in a 3×10s retry. The existing `ERR` trap
  and exit handling are unchanged.
- `boot/build-image.sh`: new step 4b creates an empty `ssh` marker at the boot
  partition root (`${WORKDIR}/mnt/boot/ssh`, i.e. `/boot/firmware/ssh` at runtime),
  unconditionally, with a build-time self-check that aborts the build if absent.

**Verified:** The real `firstrun.sh` was run in a Debian trixie container with DNS
blackholed (not the whole network):
- DNS dead from boot, released at t=25s → 3 "DNS not ready" retries, then apt
  succeeded and firstrun completed (exit 0, sentinel written).
- DNS up for the probe, then blackholed during apt → apt attempt 1/3 failed, the retry
  succeeded once DNS recovered, firstrun completed (exit 0).

Issue 2: the literal 4b block extracted from `build-image.sh` was executed against a
simulated mounted boot partition — it creates the empty `ssh` file and is
unconditional. (A full loop-mounted image build cannot run on macOS; the build step
was confirmed by direct execution.)

**Follow-up:** Re-flash and re-run the offline-boot acceptance test; DNS retry and SSH
availability to be confirmed on real hardware.

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
