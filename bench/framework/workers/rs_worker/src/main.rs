// Copyright 2026 Parmond AB
// SPDX-License-Identifier: Apache-2.0
// rs_worker — Rust `contour` crate (contour-rs) under the isobench worker protocol.
//   LOAD <field.f64> <nx> <ny> <levels_csv> <mode: lines> -> OK
//   RUN <loops> <markers 0|1> -> TIME <wall_ns> <cpu_ns> <majflt> <min_ns> <max_ns>
//        each call timed on its own (clock around the BEGIN_TIMED block only);
//        the previous result is dropped before the clock starts
//   DUMP <path> -> OK  every line of the last result, binary (outside the timed block)
//   INFO -> JSON {api, field_sum1, field_sum2}  (checksums of the field as loaded, R11) ; QUIT
use contour::{Band, ContourBuilder, Line};
use std::io::{self, BufRead, Write};
use std::time::Instant;

fn cpu_ns() -> i64 {
    let mut t = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut t); }
    t.tv_sec as i64 * 1_000_000_000 + t.tv_nsec as i64
}

fn majflt() -> i64 {
    let mut ru: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut ru); }
    ru.ru_majflt as i64
}

fn main() {
    let stdin = io::stdin();
    let mut out = io::stdout();
    let mut values: Vec<f64> = Vec::new();
    let mut levels: Vec<f64> = Vec::new();
    let mut builder: Option<ContourBuilder> = None;
    let mut last: Vec<Line> = Vec::new();
    let mut last_bands: Vec<Band> = Vec::new();
    let mut bands_mode = false;
    let mut thresholds: Vec<f64> = Vec::new();
    let (mut sum1, mut sum2) = (0u64, 0u64);
    for line in stdin.lock().lines() {
        let line = line.unwrap();
        let p: Vec<&str> = line.split_whitespace().collect();
        if p.is_empty() { continue; }
        match p[0] {
            "LOAD" => {
                let nx: usize = p[2].parse().unwrap();
                let ny: usize = p[3].parse().unwrap();
                let bytes = std::fs::read(p[1]).expect("read field");
                values = bytes.chunks_exact(8).map(|c| f64::from_le_bytes(c.try_into().unwrap())).collect();
                assert_eq!(values.len(), nx * ny);
                // checksums of the loaded field (R11: the auditor compares them with the
                // array the harness handed over): sum and index-weighted sum of the bits, mod 2^64
                sum1 = 0; sum2 = 0;
                for (i, v) in values.iter().enumerate() {
                    let w = v.to_bits();
                    sum1 = sum1.wrapping_add(w);
                    sum2 = sum2.wrapping_add(w.wrapping_mul(i as u64 + 1));
                }
                levels = p[4].split(',').map(|s| s.parse().unwrap()).collect();
                builder = Some(ContourBuilder::new(nx, ny, true));
                // isobands: the benchmark levels plus outer bounds below the minimum and above
                // the maximum, so both outer bands are produced (as every bands tool does)
                bands_mode = p.len() > 5 && p[5] == "isobands";
                if bands_mode {
                    let (mn, mx) = values.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), &v| (a.min(v), b.max(v)));
                    thresholds = std::iter::once(mn - 1.0).chain(levels.iter().copied()).chain(std::iter::once(mx + 1.0)).collect();
                }
                writeln!(out, "OK").unwrap();
            }
            "RUN" => {
                let loops: usize = p[1].parse().unwrap();
                let markers = p[2] == "1";
                let b = builder.as_ref().unwrap();
                let f0 = majflt();
                let (mut wall, mut cpu, mut mn, mut mx) = (0i64, 0i64, -1i64, 0i64);
                if markers { eprint!("ISOBENCH_T0\n"); }
                for _ in 0..loops {
                    drop(std::mem::take(&mut last));      // release untimed
                    drop(std::mem::take(&mut last_bands));
                    let c0 = cpu_ns();
                    let t0 = Instant::now();
                    // BEGIN_TIMED
                    if bands_mode { last_bands = b.isobands(&values, &thresholds).unwrap(); } else { last = b.lines(&values, &levels).unwrap(); }
                    // END_TIMED
                    let d = t0.elapsed().as_nanos() as i64;
                    cpu += cpu_ns() - c0;
                    wall += d;
                    if mn < 0 || d < mn { mn = d; }
                    if d > mx { mx = d; }
                }
                if markers { eprint!("ISOBENCH_T1\n"); }
                writeln!(out, "TIME {} {} {} {} {}", wall, cpu, majflt() - f0, mn, mx).unwrap();
            }
            "DUMP" if bands_mode => {
                // polygons: i64 npolygons; per polygon: f64 lo, f64 hi, i64 nrings;
                // per ring: i64 n, n x (x, y) f64 (ring 0 = exterior)
                let mut buf: Vec<u8> = Vec::new();
                let mut np: i64 = 0;
                let ring = |buf: &mut Vec<u8>, pts: Vec<(f64, f64)>| {
                    buf.extend_from_slice(&(pts.len() as i64).to_le_bytes());
                    for (x, y) in pts { buf.extend_from_slice(&x.to_le_bytes()); buf.extend_from_slice(&y.to_le_bytes()); }
                };
                for bd in &last_bands {
                    for pg in &bd.geometry().0 {
                        buf.extend_from_slice(&bd.min_v().to_le_bytes());
                        buf.extend_from_slice(&bd.max_v().to_le_bytes());
                        buf.extend_from_slice(&(1 + pg.interiors().len() as i64).to_le_bytes());
                        ring(&mut buf, pg.exterior().0.iter().map(|c| (c.x, c.y)).collect());
                        for h in pg.interiors() { ring(&mut buf, h.0.iter().map(|c| (c.x, c.y)).collect()); }
                        np += 1;
                    }
                }
                let mut f = np.to_le_bytes().to_vec();
                f.extend_from_slice(&buf);
                std::fs::write(p[1], f).expect("dump");
                writeln!(out, "OK").unwrap();
            }
            "DUMP" => {
                // every line of the last result, binary, outside the timed block:
                // i64 nlines; per line: f64 level, i64 n, n x (x, y) f64
                let mut buf: Vec<u8> = Vec::new();
                let mut nl: i64 = 0;
                for l in &last {
                    for ls in &l.geometry().0 {
                        buf.extend_from_slice(&l.threshold().to_le_bytes());
                        buf.extend_from_slice(&(ls.0.len() as i64).to_le_bytes());
                        for c in &ls.0 { buf.extend_from_slice(&c.x.to_le_bytes()); buf.extend_from_slice(&c.y.to_le_bytes()); }
                        nl += 1;
                    }
                }
                let mut f = nl.to_le_bytes().to_vec();
                f.extend_from_slice(&buf);
                std::fs::write(p[1], f).expect("dump");
                writeln!(out, "OK").unwrap();
            }
            "INFO" => { writeln!(out, "{{\"version\":null,\"api\":\"ContourBuilder::lines\",\"version_source\":\"Cargo.lock\",\"field_sum1\":\"{}\",\"field_sum2\":\"{}\"}}", sum1, sum2).unwrap(); }
            "QUIT" => break,
            _ => { writeln!(out, "ERR unknown command").unwrap(); }
        }
        out.flush().unwrap();
    }
}
