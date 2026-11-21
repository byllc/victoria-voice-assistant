# Stream replies to satellites

- Status: in progress
- Phase: current
- Origin: Stage 14
- Created: 2026-11-06 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

## Done when

- [ ] Replies to satellites are streamed, so playback starts before the whole reply is synthesized.

## Blocking decisions

None.

## Evidence and notes

Built 2026-11-12, switched off until it has been heard.

- `ReplyStream` (`satellite/esphome.py`): the reply as 48 kHz mono FLAC that grows while Kokoro speaks. The audio server (`AudioServer.open_stream`) sends it to the device as it is written.
- With `stream_replies: true` on a satellite (`tools.yaml`, default false), the device is given the reply's address at its first audio instead of after the whole reply. The turn ends for the device then, as it does today when a whole reply is sent; the session counts as busy until the stream closes, so an announcement still waits for it.
- Announcements, notifications and timers are still sent whole. With the setting off nothing changes.
- The wake word during a streamed reply closes the stream.
- Known limit: whether the device listens again (a question is open) is read when the stream starts. A question a tool raises after the model has begun speaking is missed.
- Tests (`tests/test_satellite.py`, 5 new): the stream can be read while it is written and carries the same audio frames as the finished file; the audio server sends the first part while the stream is open; the turn's events and the busy state; off by default and for announcements; the wake word cuts it.
- The bytes as a device would receive them decode in two other decoders: `ffmpeg` (6.0 s in, 288000 samples out) and `flac -t` (ok; it notes the unset checksum, which is expected for a stream of unknown length).
- Docs: `docs/satellites.md`, "Streamed replies"; the example `tools.yaml`.

Why it is off: it has not been played by a satellite. The risks are a gap in the audio if synthesis falls behind playback, and the device's decoder refusing a FLAC of unknown length. To check: set `stream_replies: true` for the office satellite, ask something with a long answer, and listen. The criterion stays unticked until then.
