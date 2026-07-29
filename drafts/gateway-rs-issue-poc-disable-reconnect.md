# [poc] disable = true does not gate the PoC ingest reconnect loop

## Summary

Setting `[poc] disable = true` correctly suppresses RF activity (beacon
construction, transmission, and witness reporting), but the `PocIotService`
reconnect loop in the beaconer continues to run unconditionally. This
produces recurring `gateway_rs::beaconer: failed to reconnect` WARNs on a
backoff cadence — noise that now affects every hotspot on the network since
the PoC ingest oracles were decommissioned post-[HIP-149].

## Environment

- gateway-rs v1.3.0 (release binary, aarch64-unknown-linux-musl)
- Debian Trixie ARM64, Raspberry Pi 4B
- ECC608A keypair (`ecc://i2c-1:96?slot=0&network=mainnet`)
- The behaviour is config-driven and hardware-independent.

## Reproduction

1. Run gateway-rs v1.3.0 with `[poc] disable = true` in settings.toml.
2. Run `server`. Observe journal output over ≥30 minutes.

Representative journal excerpt (log level `info`):
```
INFO run: gateway_rs::beaconer: starting beacon_interval=21600 disabled=true uri=http://mainnet-pociot.helium.io:9080
WARN run: gateway_rs::beaconer: failed to reconnect err=service error: rpc status: Unavailable, message: "error trying to connect: deadline has elapsed"
... (repeats, backoffs from 5 s to a steady 30 min cadence) ...
```

The "starting" line confirms `disabled=true` is recognised. The reconnect
WARNs nevertheless appear because the reconnect path is not gated by the
flag.

## Analysis (as of v1.3.0)

### The flag is parsed and stored correctly

```rust
// src/settings.rs:80-84
pub struct PocSettings {
    // Enable/disable poc related activities (baecon/witness)
    #[serde(default)]
    pub disable: bool,
```

The upstream config file documents it as intended to avoid unnecessary RF
traffic (`config/settings.toml:36-39`). The `Beaconer` struct stores it in
`self.disabled` (`src/beaconer.rs:76`).

### Gated: beacon tick (RF transmission)

```rust
// src/beaconer.rs:109-112
_ = tokio::time::sleep_until(next_beacon_instant.into_inner().into()) => {
    // Check if beaconing is enabled and we have valid region params
    if !self.disabled && self.region_params.check_valid().is_ok() {
        self.handle_beacon_tick().await;
    }
```

### Gated: witness reporting from received beacons

```rust
// src/beaconer.rs:256-260
async fn handle_received_beacon(&mut self, packet: PacketUp) {
    // Check if poc reporting is disabled
    if self.disabled {
        return;
    }
```

### NOT gated: reconnect to PoC ingest

```rust
// src/beaconer.rs:183-186 (inside tokio::select!)
_ = self.reconnect.wait() => {
    let reconnect_result = self.handle_reconnect().await;
    self.reconnect.update_next_time(reconnect_result.is_err());
},
```

```rust
// src/beaconer.rs:231-239
async fn handle_reconnect(&mut self) -> Result {
    self.service
        .reconnect()
        .inspect_err(|err| warn!(%err, "failed to reconnect"))
        .await
}
```

Neither the `reconnect.wait()` branch nor `handle_reconnect()` checks
`self.disabled`. The `PocIotService` is constructed unconditionally at
`src/beaconer.rs:69-74`, and the reconnect backoff runs forever
(`src/service/mod.rs:6-8`: min 5 s, max 1800 s / 30 min).

The `service_message` branch (`src/beaconer.rs:165-182`) is similarly
unguarded.

### No workaround via log filtering

The tracing subscriber uses `Targets::with_target` with a bare `Level`
(`src/main.rs:35-37`), and `LogSettings.level` is parsed as a single
`tracing::Level` via `from_str` (`src/settings.rs:243, 279-282`). Operators
cannot set a directive like `"info,gateway_rs::beaconer=error"` — the only
choices are to accept the noise at `info` or drop to `error` globally (losing
useful informational logs from other subsystems).

## Suggested fix

When `[poc] disable = true`, skip constructing the `PocIotService` connection
entirely — or at minimum gate the `reconnect.wait()` and
`service_message.recv()` branches in the `select!` loop on `self.disabled`.
This would:

1. Eliminate recurring `failed to reconnect` WARNs to a decommissioned
   endpoint.
2. Remove unnecessary network activity (TCP connect attempts every ~30 min
   to a dead service).
3. Preserve current behaviour when `disable = false` (the default).
