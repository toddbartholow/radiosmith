# Write safety

radiosmith writes to radio flash, so every path that can change a radio is
narrow, guarded and checked by read-back.

## DM-32UV

- Normal sessions cannot write. Every frame passes an allowlist
  (`radiosmith/dm32/protocol.py: check_read_only`) that refuses the `W` (flash
  write) and `S` frames and anything else unknown, and tests assert that only
  allowed frames reach the simulated radio.
- Writes exist only in `dm32/write.py`. A write is one whole 4 KB page at an
  aligned address in the codeplug area, onto a page whose tag was scanned in the
  same session, keeping that tag, and never onto calibration (0x02, 0x69) or
  unidentified pages. `S` is never sent. Each write is read back. A write with
  no acknowledgement is reported as possibly partial, not retried.
- `dm32 write-test` makes a full backup first and checks that the radio's
  identity, page map and target page still match it. It writes only with
  `--yes`, then makes another full backup and compares the two.
- The protocol has no checksums, and CH340 cables are known to flip bit 7 of
  received bytes. Every block is read three times and merged, and V queries are
  repeated until three replies agree.
- Sessions are kept at least 5 s apart, including across separate runs. The
  radio stays in programming mode until 2 s pass without a request.
- A read that fails partway still saves what it read, marked incomplete.

## RT-950 PRO

- The driver has its own allowlist: the handshake, info queries, key exchange,
  128-byte `R` reads, the APRS `T` read and `E`. Block writes, `UPDATE`
  (bootloader), `D` (boot picture, erases flash) and AA- or A5-framed packets
  are refused.
- Writes exist only in `rt950/write.py`. The radio erases a whole 4 KB sector
  when the first block of that sector is written, so writes cover whole sectors
  only: all 32 blocks, in order from the sector's start, each read back. Only
  sectors 0x0000-0x6FFF (channels 1-896) are writable, never settings, VFOs,
  APRS, 0x7000-0x7FFF or calibration.
- Each write session is preceded and followed by a full backup, and
  `rt950 restore` writes sectors back from any backup.
- A receive-only channel is one with no TX frequency stored. The radio has no
  per-channel TX-inhibit flag.

## The service

- It writes only when started with `--allow-writes`, and only a draft whose
  current `preview_id` the caller passes back.
- Editing a draft never opens a port.
- It listens on 127.0.0.1 and refuses requests whose Host or Origin is not
  local.
