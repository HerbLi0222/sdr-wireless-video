#!/usr/bin/env python3
"""Wireless video link over a USRP B210: BPSK modem, 1 MSps, one-way.

Pipeline
    TS bytes -> frames (preamble + seq + 376 B payload + CRC32) -> BPSK symbols
    -> TX port / antenna ~~~~> RX port / antenna -> sync by preamble correlation
    -> derotate -> decide -> CRC check -> reassemble in seq order -> TS bytes

Why this shape works on a single B210: TX and RX share one 40 MHz reference, so
there is no sample-clock offset and (same channel) no carrier offset. That
removes the two hard parts of a real modem; a per-frame phase estimate from the
preamble is enough. Payload is 2 x 188 B TS packets, so a dropped frame loses
whole TS packets and leaves the stream aligned.

Rates: 3327 samples/frame = 3.327 ms -> 376 B / 3.327 ms ~= 113 kB/s ~= 0.9 Mbps.
"""
import argparse
import os
import queue
import sys
import threading
import time
import zlib

import sys
import numpy as np
import uhd

FS = 1_000_000
SPB = 2                  # samples per symbol: 500 ksym/s -> 0.47 Mbps
PRE_LEN = 255
PAYLOAD = 376            # 2 TS packets
HDR = 4
CRC = 4
FRAME_BITS = (HDR + PAYLOAD + CRC) * 8
# QPSK doubles the payload rate at ~3 dB more SNR; SDR_MOD=bpsk falls back.
MOD = os.environ.get("SDR_MOD", "qpsk").lower()
BPS = 2 if MOD == "qpsk" else 1
SYMBOLS = FRAME_BITS // BPS
FRAME_LEN = (PRE_LEN + SYMBOLS) * SPB
BYTES_PER_FRAME = PAYLOAD
AIR_RATE = BYTES_PER_FRAME / (FRAME_LEN / FS)      # bytes/s


def mseq(n=8, taps=(8, 4, 3, 2)):
    reg = [1] * n
    out = []
    for _ in range(2 ** n - 1):
        out.append(reg[-1])
        fb = 0
        for t in taps:
            fb ^= reg[t - 1]
        reg = [fb] + reg[:-1]
    return np.array(out, dtype=np.int8)


PRE = (1.0 - 2.0 * mseq()).astype(np.float32)
assert PRE.size == PRE_LEN
PRE_UP = np.repeat(PRE, SPB).astype(np.float32)     # rectangular pulse, SPB samples/sym
PRE_UP_LEN = PRE_UP.size


# ---------------------------------------------------------------- modulator
_rng = np.random.default_rng(0x5EED)
SCRAMBLE = _rng.integers(0, 256, PAYLOAD, dtype=np.uint8)


def scr(payload):
    return (np.frombuffer(payload, np.uint8) ^ SCRAMBLE).tobytes()


def frame_symbols(seq, payload):
    body = seq.to_bytes(HDR, "big") + scr(payload)
    raw = body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(CRC, "big")
    bits = np.unpackbits(np.frombuffer(raw, np.uint8))
    if BPS == 1:
        sig = np.repeat((1.0 - 2.0 * bits).astype(np.float32), SPB)
    else:
        b = bits.reshape(-1, 2)
        i = (1.0 - 2.0 * b[:, 0]) / np.sqrt(2.0)
        q = (1.0 - 2.0 * b[:, 1]) / np.sqrt(2.0)
        sig = np.repeat((i + 1j * q).astype(np.complex64), SPB)
    return np.concatenate([PRE_UP, sig])


REPEAT = 2          # send every frame twice; the receiver keeps the first good copy


def build_frames(data, seq0=0):
    frames = []
    for i in range(0, len(data), PAYLOAD):
        chunk = data[i:i + PAYLOAD]
        if len(chunk) < PAYLOAD:
            chunk = chunk + b"\x00" * (PAYLOAD - len(chunk))
        f = frame_symbols(seq0 + i // PAYLOAD, chunk)
        for _ in range(REPEAT):
            frames.append(f)
    return np.concatenate(frames) if frames else np.zeros(0, np.float32)


# -------------------------------------------------------------- demodulator
def demod_at(rx, pos):
    """Try to decode one frame starting at sample pos. Returns dict or None."""
    if pos < 0 or pos + FRAME_LEN > rx.size:
        return None
    seg = rx[pos:pos + FRAME_LEN]
    c = float(np.dot(seg[:PRE_UP_LEN].real, PRE_UP)) + 1j * float(np.dot(seg[:PRE_UP_LEN].imag, PRE_UP))
    if abs(c) < 1e-9:
        return None
    phi = np.angle(c)
    y = seg[PRE_UP_LEN:] * np.exp(-1j * phi)
    # integrate-and-dump over each symbol period: this is the matched filter
    sym = y.reshape(-1, SPB).mean(axis=1)
    if BPS == 2:
        bits = np.stack([sym.real < 0, sym.imag < 0], axis=1).astype(np.uint8).reshape(-1)
    else:
        bits = (sym.real < 0).astype(np.uint8)
    raw = np.packbits(bits).tobytes()
    body, crc = raw[:HDR + PAYLOAD], raw[HDR + PAYLOAD:]
    if (zlib.crc32(body) & 0xFFFFFFFF) != int.from_bytes(crc, "big"):
        return None
    if BPS == 2:
        dec = (np.sign(sym.real) + 1j * np.sign(sym.imag)) / np.sqrt(2.0)
        scale = float(np.mean(np.abs(sym)))
        evm = float(np.sqrt(np.mean(np.abs(sym - scale * dec) ** 2)) / (scale + 1e-12))
        amp, snr = scale, (20 * np.log10(1.0 / evm) if evm > 0 else 99.0)
    else:
        amp = float(np.mean(np.abs(sym.real)))
        noise = float(np.mean(np.abs(sym.imag)))
        snr = 20 * np.log10(amp / noise) if noise > 0 else 99.0
    return {"seq": int.from_bytes(body[:HDR], "big"),
            "payload": scr(body[HDR:]), "amp": amp, "snr": snr}


def corr_mag(seg, pre_c):
    n = seg.size + pre_c.size - 1
    nfft = 1 << (n - 1).bit_length()
    pre_pad = np.zeros(nfft, np.complex64)
    pre_pad[:pre_c.size] = pre_c
    C = np.fft.ifft(np.fft.fft(seg, nfft) * np.conj(np.fft.fft(pre_pad)))
    return np.abs(C[:seg.size])


def refine(rx, pos, span=64):
    """Locally maximise |preamble correlation| around pos (handles drift)."""
    lo = max(0, pos - span)
    hi = min(rx.size - PRE_UP_LEN, pos + span)
    if hi <= lo:
        return pos
    best_v, best_k = -1.0, pos
    for k in range(lo, hi + 1):
        v = abs(np.dot(rx[k:k + PRE_UP_LEN], PRE_UP))
        if v > best_v:
            best_v, best_k = v, k
    return best_k


def candidates(rx, topk=400, sep=800):
    """Strongest preamble correlations, non-maximum suppressed."""
    mag = corr_mag(rx, PRE_UP.astype(np.complex64))
    idx = np.argsort(mag)[::-1]
    kept = []
    for i in idx:
        if len(kept) >= topk:
            break
        i = int(i)
        if all(abs(i - j) >= sep for j in kept):
            kept.append(i)
    return kept


def _resync(rx, pos, window=3 * FRAME_LEN, tries=40, sep=300):
    """Find a decodable frame near pos. Candidates are non-maximum suppressed:
    without that, the top hits are all the same peak's neighbours."""
    end = min(pos + window, rx.size)
    if end - pos < FRAME_LEN:
        return None
    mag = corr_mag(rx[pos:end], PRE_UP.astype(np.complex64))
    tried = []
    for i in np.argsort(mag)[::-1]:
        if len(tried) >= tries:
            break
        i = int(i)
        if any(abs(i - j) < sep for j in tried):
            continue
        tried.append(i)
        p = refine(rx, pos + i, span=8)
        r = demod_at(rx, p)
        if r is not None:
            return p, r
    return None


def demod_stream(rx, log=print):
    """Chain frames with local refinement; resync if the chain breaks."""
    got = {}
    res = _resync(rx, 0, window=2_000_000, tries=200)
    if res is None:
        return got, 1, 0
    p, r = res
    got[r["seq"]] = (r["payload"], r["snr"], r["amp"])
    # walk backwards first: _resync may have landed on a later frame
    b = p - FRAME_LEN
    while b > 0:
        b = refine(rx, b, span=12)
        rb = demod_at(rx, b)
        if rb is None:
            break
        got[rb["seq"]] = (rb["payload"], rb["snr"], rb["amp"])
        b -= FRAME_LEN
    q = p + FRAME_LEN
    gaps = 0
    while q < rx.size - FRAME_LEN:
        q = refine(rx, q, span=12)
        r = demod_at(rx, q)
        if r is not None:
            got[r["seq"]] = (r["payload"], r["snr"], r["amp"])
            q += FRAME_LEN
            gaps = 0
            continue
        found = _resync(rx, q, window=4 * FRAME_LEN, tries=60)
        if found is None:
            gaps += 1
            q += FRAME_LEN
            if gaps > 6:
                break
            continue
        p, r = found
        got[r["seq"]] = (r["payload"], r["snr"], r["amp"])
        q = p + FRAME_LEN
        gaps = 0
    return got, 0, 0


# ------------------------------------------------------------------- device
class Link:
    def __init__(self, a):
        self.a = a
        self.u = uhd.usrp.MultiUSRP("type=b200")
        self.u.set_tx_rate(FS)
        self.u.set_rx_rate(FS)
        self.u.set_tx_freq(uhd.types.TuneRequest(a.freq), a.tx_ch)
        self.u.set_rx_freq(uhd.types.TuneRequest(a.freq), a.rx_ch)
        self.u.set_tx_gain(a.tx_gain, a.tx_ch)
        self.u.set_rx_gain(a.rx_gain, a.rx_ch)
        self.u.set_tx_antenna("TX/RX", a.tx_ch)
        self.u.set_rx_antenna(a.rx_ant, a.rx_ch)
        self.tst = self.u.get_tx_stream(_args(a.tx_ch))
        self.rst = self.u.get_rx_stream(_args(a.rx_ch))

    def send(self, samples):
        n = 0
        total = samples.size
        while n < total:
            # 1-D buffer: pyuhd's send() with a (N,1) array runs ~20x slower
            # (per-sample fallback) and underflows the TX stream. Keep it 1-D.
            seg = samples[n:n + 2040]
            md = uhd.types.TXMetadata()
            md.start_of_burst = (n == 0)
            md.end_of_burst = (n + 2040 >= total)
            sent = self.tst.send(seg, md, 1.0)
            if sent <= 0:
                break
            n += sent
        return n

    def recv(self, nsamps):
        buf = np.zeros(nsamps, np.complex64)
        md = uhd.types.RXMetadata()
        got = 0
        errs = 0
        while got < nsamps:
            c = self.rst.recv(buf[got:], md, 1.0)
            if md.error_code != uhd.types.RXMetadataErrorCode.none:
                errs += 1
                break
            if c == 0:
                break
            got += c
        return buf[:got], errs


def _args(ch):
    sa = uhd.usrp.StreamArgs("fc32", "sc16")
    sa.channels = [ch]
    return sa


# --------------------------------------------------------------------- modes
def run_file(a):
    data = open(a.input, "rb").read()
    tx = build_frames(data) * a.tx_amp
    # prime the TX stream: the device underflows for the first few hundred ms,
    # which would otherwise shred the first frames
    tx = np.concatenate([np.zeros(int(0.3 * FS), np.float32), tx]).astype(np.complex64)
    nframes = (len(data) + PAYLOAD - 1) // PAYLOAD
    print(f"payload {len(data)} B -> {nframes} frames, "
          f"{tx.size/FS:.2f} s air time, {AIR_RATE/1e3:.1f} kB/s")

    link = Link(a)
    print(f"tx ch{a.tx_ch} {a.tx_gain:.0f} dB -> rx ch{a.rx_ch} ({a.rx_ant}) "
          f"{a.rx_gain:.0f} dB @ {a.freq/1e6:.3f} MHz")

    nsamps = tx.size + FS // 2
    # Preallocate: no big allocations while streaming (that stalls the RX loop).
    rx = np.zeros(nsamps, np.complex64)
    rx_md = uhd.types.RXMetadata()
    state = {"got": 0, "err": 0, "done": False}

    cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
    cmd.num_samps = nsamps
    cmd.stream_now = True
    link.rst.issue_stream_cmd(cmd)

    def rx_worker():
        while state["got"] < nsamps:
            c = link.rst.recv(rx[state["got"]:], rx_md, 1.0)
            if rx_md.error_code != uhd.types.RXMetadataErrorCode.none:
                state["err"] += 1
                break
            if c == 0:
                break
            state["got"] += c
        state["done"] = True

    th = threading.Thread(target=rx_worker, daemon=True)
    th.start()
    time.sleep(0.05)
    t0 = time.time()
    sent = link.send(tx)
    tx_dt = time.time() - t0
    th.join()
    rx = rx[:state["got"]]
    print(f"rx {rx.size} samples ({rx.size/FS:.2f} s), overflows={state['err']}, "
          f"tx sent {sent} samples in {tx_dt:.2f} s (air {tx.size/FS:.2f} s), "
          f"max|rx|={np.max(np.abs(rx)):.4f}")

    got, bad, npeaks = demod_stream(rx)
    snrs = [v[1] for v in got.values()] or [0]
    print(f"frames decoded {len(got)}/{nframes} (crc fail at {bad} slots), "
          f"peak SNR {np.median(snrs):.1f} dB")

    out = bytearray()
    missing = []
    for seq in range(nframes):
        if seq in got:
            out += got[seq][0]
        else:
            missing.append(seq)
    open(a.output, "wb").write(out)
    print(f"wrote {len(out)} B ({(len(out)/len(data) if data else 0)*100:.1f}% of input), "
          f"missing frames {len(missing)}")
    if a.debug:
        line = []
        for seq in range(min(nframes, 120)):
            line.append(f"{got[seq][1]:.0f}" if seq in got else "--")
        print("  frame SNR timeline (first 120):")
        print("   " + " ".join(line))
    if missing:
        runs, start, prev = [], missing[0], missing[0]
        for m in missing[1:]:
            if m != prev + 1:
                runs.append((start, prev)); start = m
            prev = m
        runs.append((start, prev))
        print(f"  {len(runs)} gaps, first: {runs[:6]}")
    return 0 if len(out) >= len(data) * 0.9 else 1


def run_selftest(a):
    rng = np.random.default_rng(1)
    data = bytes(rng.integers(0, 256, 200 * PAYLOAD, dtype=np.uint8))
    tx = build_frames(data)
    phi = 0.7
    for snr_db in (20, 15, 12):
        amp = 0.5
        sig = tx * np.exp(1j * phi) * amp
        snr_lin = 10 ** (snr_db / 10)
        sigma = np.sqrt(amp ** 2 / (2 * snr_lin))     # per-component noise std
        noise = (rng.normal(size=sig.size) + 1j * rng.normal(size=sig.size)) * sigma
        rx = (sig + noise).astype(np.complex64)
        rx = np.concatenate([np.zeros(137, np.complex64), rx])   # random timing offset
        got, bad, npeaks = demod_stream(rx, log=lambda *x: None)
        ok = sum(1 for s in range(len(data) // PAYLOAD) if s in got
                 and got[s][0] == data[s * PAYLOAD:(s + 1) * PAYLOAD])
        print(f"selftest SNR {snr_db:2d} dB: {ok}/{len(data)//PAYLOAD} frames bit-exact, "
              f"crc-fail slots {bad}, peaks {npeaks}")
        if ok < len(data) // PAYLOAD * 0.99:
            print("  FAIL")
            return 1
    print("selftest OK")
    return 0




def emit_in_order(got, nxt, send, gap=8, force=False):
    """Send payloads in sequence order. One lost frame would block the stream
    forever, so allow a small reorder window and skip larger gaps (or any gap
    after a stall)."""
    if got and min(got) < nxt:
        for k in [k for k in got if k < nxt]:
            del got[k]
    sent = 0
    while nxt in got:
        send(got.pop(nxt)[0])
        sent += PAYLOAD
        nxt += 1
    if got:
        m = min(got)
        if m - nxt > gap or force:
            nxt = m
    return nxt, sent


def run_live(a):
    """Continuous mode: stream a TS file over the air, forward decoded bytes to
    a UDP sink as they arrive (VLC plays udp://@host:port)."""
    import socket
    data = open(a.input, "rb").read()
    nframes = (len(data) + PAYLOAD - 1) // PAYLOAD
    pass_samples = nframes * FRAME_LEN * REPEAT
    passes = a.loop if a.loop > 0 else 10 ** 9          # 0 = keep going until Ctrl-C
    total = pass_samples * min(passes, 10 ** 6) + int(0.3 * FS) + 2 * FS
    link = Link(a)
    print(f"streaming {len(data)} B ({nframes} frames, {pass_samples/FS:.1f} s air per pass, "
          f"{a.loop} pass(es), {total/FS:.1f} s total) -> udp://{a.udp_host}:{a.udp_port}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (a.udp_host, a.udp_port)
    cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
    cmd.num_samps = total
    cmd.stream_now = True
    link.rst.issue_stream_cmd(cmd)

    sent_box = {}

    def tx_loop():
        n = 0
        n += link.send(np.zeros(int(0.3 * FS), np.float32).astype(np.complex64))
        p = 0
        while a.loop == 0 or p < a.loop:
            txp = (build_frames(data, seq0=p * nframes) * a.tx_amp).astype(np.complex64)
            n += link.send(txp)
            p += 1
        sent_box["n"] = n

    t = threading.Thread(target=tx_loop, daemon=True)
    t.start()

    chunk = int(0.15 * FS)
    overlap = int(0.05 * FS)
    buf = np.zeros(chunk, np.complex64)
    carry = np.zeros(0, np.complex64)
    got = {}
    nxt = 0
    started = False
    last_emit = time.time()
    written = 0
    t0 = time.time()
    got_total = 0
    while got_total < total:
        md = uhd.types.RXMetadata()
        c = link.rst.recv(buf, md, 2.0)
        if a.debug:
            print(f"   recv {c} err={md.strerror()}")
        if c <= 0:
            break
        got_total += c
        window = np.concatenate([carry, buf[:c]])
        frames, _, _ = demod_stream(window)
        got.update({k: v for k, v in frames.items() if k not in got})
        if not started and got:
            nxt = min(got)
            started = True
        nxt, sent = emit_in_order(got, nxt, lambda b: sock.sendto(b, dest),
                                  force=(time.time() - last_emit) > 0.4)
        if sent:
            last_emit = time.time()
        written += sent
        carry = window[-overlap:]
    # drain
    t.join(timeout=5)
    print(f"live: {written} B forwarded, next expected seq {nxt}, "
          f"wall {time.time()-t0:.1f} s, tx sent {sent_box.get('n', 0)} samples")
    return 0




def demod_window(rx, thresh=10.0, topk=80, sep=200):
    """Cheap streaming demodulation: one correlation pass, then try the strongest
    few candidates (NMS). Far cheaper than demod_stream's exhaustive resync."""
    mag = corr_mag(rx, PRE_UP.astype(np.complex64))
    out = {}
    tried = []
    for i in np.argsort(mag)[::-1]:
        if len(tried) >= topk:
            break
        i = int(i)
        if any(abs(i - j) < sep for j in tried):
            continue
        tried.append(i)
        p = refine(rx, i, span=8)
        r = demod_at(rx, p)
        if r is not None:
            out[r["seq"]] = (r["payload"], r["snr"], r["amp"])
    return out

def run_pipe(a):
    """True live pipeline: read MPEG-TS from stdin as it is produced, frame it,
    transmit immediately, and forward decoded bytes to a UDP sink.

    Unlike `live`, nothing is pre-built: the encoder runs at 1x, bytes arrive
    continuously, and the link consumes them at the same rate it sends them.
    """
    import socket
    link = Link(a)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (a.udp_host, a.udp_port)
    print(f"live pipeline: stdin -> B210 ch{a.tx_ch}/ch{a.rx_ch} @ {a.freq/1e6:.3f} MHz "
          f"-> udp://{a.udp_host}:{a.udp_port}", flush=True)

    buf = bytearray()
    lock = threading.Lock()
    eof = threading.Event()
    stats = {"in": 0, "out": 0, "sent": 0, "dropped": 0}
    max_backlog = 120 * PAYLOAD         # ~45 kB ~ 1.8 s of stream; drop oldest beyond

    def reader():
        while True:
            chunk = sys.stdin.buffer.read(4096)
            if not chunk:
                eof.set()
                return
            with lock:
                buf.extend(chunk)
                stats["in"] += len(chunk)
                while len(buf) > max_backlog:      # encoder outrunning the link
                    del buf[:PAYLOAD]
                    stats["dropped"] += 1

    def tx():
        seq = 0
        first = True
        pending = np.zeros(0, np.complex64)
        while True:
            data = None
            with lock:
                if len(buf) >= PAYLOAD:
                    data = bytes(buf[:PAYLOAD])
                    del buf[:PAYLOAD]
            if data is not None:
                pending = np.concatenate(
                    [pending, (frame_symbols(seq, data) * a.tx_amp).astype(np.complex64)])
                seq += 1
            if pending.size == 0:
                if eof.is_set():
                    break
                # keep the TX stream fed with silence instead of underflowing;
                # the receiver simply sees a gap between frames
                pending = np.zeros(2040, np.complex64)
            n = int(min(2040, pending.size))
            md = uhd.types.TXMetadata()
            md.start_of_burst = first
            md.end_of_burst = False
            sent = link.tst.send(pending[:n], md, 1.0)
            if sent <= 0:
                break
            pending = pending[sent:]
            first = False
            stats["sent"] = seq
        # final chunk with end-of-burst
        if not first:
            md = uhd.types.TXMetadata()
            md.start_of_burst = False
            md.end_of_burst = True
            link.tst.send(np.zeros(2040, np.complex64), md, 1.0)
        stats["done"] = True

    th_r = threading.Thread(target=reader, daemon=True)
    th_t = threading.Thread(target=tx, daemon=True)
    th_r.start()
    th_t.start()

    cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    cmd.stream_now = True
    link.rst.issue_stream_cmd(cmd)

    chunk = int(0.15 * FS)
    overlap = int(0.05 * FS)
    cbuf = np.zeros(chunk, np.complex64)
    md = uhd.types.RXMetadata()
    carry = np.zeros(0, np.complex64)
    got = {}
    nxt = 0
    started = False
    last_emit = time.time()
    t0 = time.time()
    deadline = t0 + a.duration if a.duration else None
    while True:
        if not th_t.is_alive() and not got:
            break                       # tx finished and nothing buffered
        if deadline and time.time() > deadline:
            break
        c = link.rst.recv(cbuf, md, 0.5)
        if c <= 0:
            if not th_t.is_alive():
                break
            continue
        window = np.concatenate([carry, cbuf[:c]])
        got.update({k: v for k, v in demod_window(window).items() if k not in got})
        if not started and got:
            nxt = min(got)
            started = True
        nxt, sent = emit_in_order(got, nxt, lambda b: sock.sendto(b, dest),
                                  force=(time.time() - last_emit) > 0.4)
        if sent:
            last_emit = time.time()
        stats["out"] += sent
        carry = window[-overlap:]
        if not th_t.is_alive() and eof.is_set():
            # drain the tail once the transmitter has stopped
            time.sleep(0.3)
            break
    link.rst.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
    th_t.join(timeout=2)
    dur = time.time() - t0
    print(f"live: in {stats['in']} B, out {stats['out']} B, frames {stats['sent']}, "
          f"dropped {stats['dropped']}, {dur:.1f} s -> {stats['out']/max(dur,1e-9)/1e3:.1f} kB/s")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["file", "selftest", "live", "pipe"])
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--freq", type=float, default=915e6)
    ap.add_argument("--tx-ch", type=int, default=1)
    ap.add_argument("--rx-ch", type=int, default=1)
    ap.add_argument("--rx-ant", default="RX2")
    ap.add_argument("--tx-gain", type=float, default=50.0)
    ap.add_argument("--rx-gain", type=float, default=20.0)
    ap.add_argument("--tx-amp", type=float, default=0.5)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--udp-host", default="127.0.0.1")
    ap.add_argument("--udp-port", type=int, default=18096)
    ap.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0=until stdin EOF)")
    ap.add_argument("--loop", type=int, default=1,
                    help="re-send the file N times, 0 = forever (seq continues)")
    a = ap.parse_args()
    if a.mode == "selftest":
        return run_selftest(a)
    if a.mode == "live":
        return run_live(a)
    if a.mode == "pipe":
        return run_pipe(a)
    return run_file(a)


if __name__ == "__main__":
    raise SystemExit(main())
