// fbt-battery: query per-device battery status from Monado/WiVRn.
//
// Prints one line per device, keyed by the same device index that
// `motoc show` prints, so a GUI can join the two outputs:
//
//     <index> <percent|-> <charging|->
//
//   <percent>  0-100, or "-" when the device reports no battery
//   <charging> "1" = charging, "0" = discharging, "-" = unknown
//
// Exit code 0 on success (even with zero devices), 1 when the
// Monado/WiVRn service is unreachable.

use libmonado::{DeviceLogic, Monado};

fn main() {
    let monado = match Monado::auto_connect() {
        Ok(m) => m,
        Err(_) => std::process::exit(1),
    };
    let devices = match monado.devices() {
        Ok(d) => d,
        Err(_) => std::process::exit(1),
    };
    for d in devices {
        let (pct, charging) = match d.battery_status() {
            Ok(s) if s.present => (
                format!("{}", (s.charge * 100.0).round().clamp(0.0, 100.0) as i32),
                if s.charging { "1" } else { "0" },
            ),
            _ => ("-".to_string(), "-"),
        };
        println!("{} {} {}", d.index, pct, charging);
    }
}
