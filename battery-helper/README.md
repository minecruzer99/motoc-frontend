# fbt-battery

Tiny helper that prints per-device battery status from Monado/WiVRn, for
the FBT Calibrator GUI's device panel.

## Output

One line per device, keyed by the same device index `motoc show` prints:

```
<index> <percent|-> <charging|->
```

- `<percent>`: 0–100, or `-` when the device reports no battery
- `<charging>`: `1` = charging, `0` = discharging, `-` = unknown

Exit 0 on success (even with zero devices), 1 when the Monado/WiVRn
service is unreachable.

## How it works

Uses the [`libmonado`](https://crates.io/crates/libmonado) crate — the
same Rust client library `motoc` itself uses — to call
`device.battery_status()` over Monado's client IPC (added in Monado
v25.0.0; implemented by the `steamvr_lh` and `survive` drivers, i.e. the
ones that drive Vive trackers). The binary only links libc — no Monado
libraries needed at runtime.

## Rebuild

```bash
cargo build --release
cp target/release/fbt-battery ..
```

Then make sure the `fbt-battery` binary sits next to
`fbt-calibrator.py`. The GUI finds it automatically; if it's missing or
the service is down, the battery badges are simply hidden.
