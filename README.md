# audio-tagger

Walkie-talkie audio listener for automatic ski race athlete tagging.

Listens to coach radio channel audio, transcribes speech using Whisper, and detects callout patterns like "{Name} on course" to automatically associate athlete names with detected runs.

## How It Works

1. **Audio Capture** - Records audio from a connected device (e.g., walkie-talkie receiver via USB sound card) in 5-second chunks
2. **Speech Transcription** - Uses [faster-whisper](https://github.com/guillaumekln/faster-whisper) for efficient on-device transcription
3. **Callout Detection** - Parses transcribed text for racing callouts:
   - `"{name} on course"` - athlete started their run
   - `"{name} at the gate"` - athlete queued at start
   - `"{name} ready"`, `"{name} go"` - variants
4. **Name Matching** - Fuzzy matches spoken names against a configured list of known athletes
5. **Run Assignment** - Matches callouts to video detections by timestamp (within 60 seconds)
6. **Upload** - Syncs annotations to S3 for display on skiframes.com

## Requirements

- Python 3.8+
- Linux with ALSA (uses `arecord`)
- USB audio input device
- faster-whisper
- boto3 (for S3 upload)

## Installation

```bash
pip install faster-whisper boto3
```

## Configuration

Edit `config.json`:

```json
{
  "audio_device": "hw:2,0",       // Find with: arecord -l
  "sample_rate": 44100,
  "whisper_model": "small",       // tiny, base, small, medium, large
  "whisper_device": "cpu",        // cpu or cuda
  "whisper_compute_type": "int8", // int8, float16, float32
  "chunk_duration_sec": 5,
  "output_dir": "/path/to/sessions",
  "known_names": ["Margot", "Emma", ...]
}
```

## Usage

```bash
# Auto-detect active session
python listener.py

# Specific session
python listener.py --session 2026-03-09_0900_u14_race

# Add names at runtime
python listener.py --names "NewAthlete" "AnotherName"

# List available sessions
python listener.py --list-sessions

# Skip S3 upload
python listener.py --no-upload
```

## Output

Creates `annotations.json` in the session directory:

```json
{
  "session_id": "2026-03-09_0900_u14_race",
  "callouts": [
    {
      "timestamp": "2026-03-09T09:15:23",
      "name": "Margot",
      "event": "on_course",
      "confidence": 0.85,
      "raw_text": "Margot on course"
    }
  ],
  "run_assignments": {
    "1": {
      "name": "Margot",
      "source": "on_course",
      "confidence": 0.85
    }
  }
}
```

## Hardware Setup

Connect a walkie-talkie receiver's audio output to a USB sound card. Identify the device:

```bash
arecord -l
```

Update `audio_device` in config (e.g., `hw:2,0` for card 2, device 0).

## Status

Written but not yet field-tested. Requires validation with actual coach radio audio.

## Related

- [skiframes/photo-montages](https://github.com/skiframes/photo-montages) - Main video processing pipeline
- [skiframes.com](https://skiframes.com) - Web gallery
