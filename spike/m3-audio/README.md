# M3 — Audio Transport Viability spike

**Exploratory spike code (see [ADR-014](../../docs/adr/014-audio-transport-viability.md)).**
Not part of the `partybox` SDK, not production code, not expected to survive
into later milestones. Its job is to produce **evidence** for one question:

> Can a Raspberry Pi reliably act as both the **BLE control** endpoint and the
> Bluetooth **A2DP audio source** for a JBL PartyBox at the same time?

The philosophy is *reduce uncertainty, don't build features*. Where a problem
shows up, these tools **instrument** it (timeline events, xrun counters,
reconnect timings) rather than trying to solve it — solving belongs to M9.

## Where this runs

On the **Pi**, never in the devcontainer: BlueZ and PipeWire are Linux-only and
the container has no Bluetooth radio (see the memory note
*bluetooth-runs-on-pi-not-devcontainer*). You edit here, sync, and run there.

Pi baseline (verified 2026-06-27): Debian, BlueZ 5.82, PipeWire with the full
`bluez5` codec set, adapter advertising A2DP **Audio Source**. `ffmpeg`,
`pw-dump`, `pw-play`, `pw-top`, `wpctl` present. `librespot` is **not** installed
(local-audio validation comes first; librespot is a later step).

## Setup

From the devcontainer:

```bash
./setup-pi.sh          # rsyncs SDK + spike to the Pi, builds a venv
```

This installs the `partybox` SDK editable into `~/m3-audio/venv` (pulling in
`bleak`); the audio side uses the system PipeWire CLI tools.

### One-time bonding

The PartyBox uses rotating BLE private addresses and refuses new bonds in
standby. Bond **once**, with the speaker awake and in pairing mode:

```bash
# On the Pi, with the speaker in pairing mode:
bluetoothctl
  pairable on
  scan on            # note the PartyBox BR/EDR address (Audio Sink UUID 110b)
  pair    <MAC>
  trust   <MAC>
  connect <MAC>
```

After bonding, BlueZ resolves the rotating address to a stable identity and the
scripts can reconnect by address. `reconnect_stress.py` reports whether bonding
was actually required.

## The scripts

Run each as `../venv/bin/python <script>.py` from `~/m3-audio/run`.

| Script | Question it answers |
|---|---|
| [`audio_connect.py`](audio_connect.py) | Do A2DP + BLE control come up together? What codec is negotiated? *(smallest slice)* |
| [`audio_stream.py`](audio_stream.py) | Does a 30+ min stream stay clean while BLE control is probed throughout? Optional `--power-cycle-test`. |
| [`reconnect_stress.py`](reconnect_stress.py) | Does it reconnect after disconnect/standby? How fast? Is bonding required? |

Common flags: `--audio-mac <MAC>` (else discover by name), `--no-ble` to isolate
the audio path. `audio_stream.py --duration` is in seconds (default 1800).

```bash
../venv/bin/python audio_connect.py
../venv/bin/python audio_stream.py --duration 1800
../venv/bin/python audio_stream.py --duration 600 --power-cycle-test
../venv/bin/python reconnect_stress.py --cycles 10
../venv/bin/python reconnect_stress.py --cycles 5 --mode standby
```

## Evidence

Every run writes `evidence/<timestamp>-<run>/`:

- `events.jsonl` — full timeline (one JSON object per line)
- `summary.json` / `summary.md` — headline verdict + metrics
- `environment.json` — Pi software baseline

Pull results back to the repo for the writeup:

```bash
rsync -a jonathan@partybox:m3-audio/run/evidence/ ./spike/m3-audio/evidence/
```

The synthesis of all runs lives in
[`docs/validation/m3-findings.md`](../../docs/validation/m3-findings.md).

> `evidence/` run outputs are gitignored; the findings doc is the durable record.
