# Changelog

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
- Existing live field devices (Perth, Birnie's gateway) are not migrated;
  this is a source-repo and fresh-provisioning change only.
