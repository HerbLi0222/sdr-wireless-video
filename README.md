# sdr-wireless-video

A one-way wireless video link built on a **USRP B210** with a hand-written BPSK
modem: a video file is packetized, modulated, transmitted at 915 MHz, received on
the second port, demodulated, and handed to VLC — over the air, not over a cable.

![VLC playing the over-the-air stream](docs/vlc-sdr-playing.png)

## Architecture

```
video.mp4 ─ ffmpeg ─► MPEG-TS ─► frame+CRC32 ─► scramble ─► BPSK
                                                              │
                                             B210 TX 915 MHz, 50 dB
                                                              │  ~~~ air ~~~
                                             B210 RX2 915 MHz, 20 dB
                                                              │
VLC ◄── UDP 127.0.0.1:18096 ◄── demod (sync, derotate, decide) ◄── CRC + descramble
```

Both ends are the same B210 (ch1 `TX/RX` transmits, ch1 `RX2` receives), sharing
one reference clock — so there is no carrier or sample offset to track.

## Requirements

- Ubuntu with a USRP B210 on USB 3
- UHD 4.6+ (`uhd_find_devices` working)
- ffmpeg, python3-numpy, VLC

One-time setup (UHD + udev rule + FPGA images):

```bash
sudo ./setup.sh
uhd_find_devices          # must list a serial
```

## Quick start

```bash
./run.sh                  # synthetic or VIDEO source -> over the air -> VLC
./run.sh file             # transmit and verify the payload byte-exactly
./run.sh selftest         # modem only, no radio
```

With your own source:

```bash
VIDEO=~/Videos/clip.mp4 ./run.sh
LOOP=0 ./run.sh           # keep streaming until Ctrl-C
```

The link sustains ≈**26 kB/s**, so the encoder targets ≈**24 kB/s** (110 kbps
video + 24 kbps audio at 480×270/20 fps). Feed it anything that fits.

## Parameters

| item | value |
|---|---|
| sample rate | 1 MSps |
| modulation | BPSK, 2 samples/symbol (500 ksym/s) |
| preamble | 255-bit m-sequence, rectangular pulse |
| frame | preamble + seq(4 B) + payload(376 B = 2 TS packets) + CRC32 |
| frame length | 6654 samples ≈ 6.65 ms |
| raw rate | 0.5 Mbps |
| repetition | every frame sent twice |
| **payload throughput** | **≈ 26 kB/s (~210 kbps)** |
| measured SNR | ~22 dB |
| frame success | 2642/2642 (full clip), 1904/1904 (30 s live clip) |

CLI: `--freq 915e6 --tx-ch 1 --rx-ch 1 --rx-ant RX2 --tx-gain 50 --rx-gain 20`,
plus `--loop N` (0 = forever) in live mode.

## What the receiver does

1. Correlate the capture with the upsampled preamble to find frame starts.
2. Refine each candidate to the nearest sample (tracks timing drift).
3. Estimate carrier phase from the preamble, derotate the payload.
4. Integrate-and-dump over each symbol, decide BPSK, check CRC32.
5. Reassemble payloads in sequence order; missing frames are dropped whole
   (376 B = exactly 2 TS packets, so the TS stream stays packet-aligned).

## Five bugs worth knowing

1. **`send()` with a 2-D buffer is ~20× slower** — `buf.reshape(-1,1)` made a
   2040-sample send take 12 ms instead of 2 ms, permanently underflowing TX.
   Keep buffers 1-D. (`recv()` has the mirror-image trap: a large 2-D buffer
   degrades to 1 sample per call.)
2. **SPB=1 cannot work.** Zero timing margin; the same data decodes at SPB=2.
3. **The TS payload must be scrambled.** Long `0xFF` stuffing runs map to a DC
   symbol stream and break the link: random payload 398/399 frames, TS payload
   39/399 at identical SNR. A fixed XOR mask fixed it — the single most
   important fix here.
4. **TX startup underflow** ruins the first ~0.3 s, so the payload is preceded
   by a prime burst.
5. **Sending each frame twice** halves the rate and buys almost zero loss.

## Limitations

- **One-way**: no feedback, no retransmission, no rate adaptation.
- ~210 kbps payload — enough for 480×270/20 fps; 720p would need QPSK/QAM.
- **This is a transport demo, not a camera pipeline.** The source is a
  pre-encoded file, so the receiver may run ahead of playback. A real camera
  feed must be encoded at exactly 1× and needs a bounded buffer plus a drop
  policy; the RF layer itself would be unchanged.
- Single radio on both ends means carrier/sample offsets are zero by
  construction; two separate USRPs would need CFO estimation and timing
  recovery.

## Files

| file | purpose |
|---|---|
| `sdr_link.py` | modem + `selftest` / `file` / `live` modes |
| `run.sh` | one-command demo (check → encode → stream → VLC) |
| `setup.sh` | host setup: UHD, udev rules, FPGA images |
| `docs/vlc-sdr-playing.png` | VLC playing the received stream |
