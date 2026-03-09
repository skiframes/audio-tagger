#!/usr/bin/env python3
"""
Walkie-talkie audio listener for ski race annotations.

Listens to coach channel audio, transcribes speech using faster-whisper,
and detects "{Name} on course" / "{Name} at the gate" patterns.
Writes annotations to JSON files alongside photo-montage session outputs.

Usage:
    python listener.py                          # auto-detect active session
    python listener.py --session SESSION_ID     # specific session
    python listener.py --list-sessions          # show available sessions
"""

import os
import sys
import json
import time
import wave
import struct
import signal
import argparse
import subprocess
import tempfile
import re
from datetime import datetime, timedelta
from pathlib import Path
from difflib import get_close_matches
from threading import Thread, Event
from collections import deque

# Load config
CONFIG_PATH = Path(__file__).parent / "config.json"
if not CONFIG_PATH.exists():
    example_path = Path(__file__).parent / "config.example.json"
    if example_path.exists():
        print(f"ERROR: config.json not found. Copy the example:")
        print(f"  cp {example_path} {CONFIG_PATH}")
        sys.exit(1)
    else:
        print("ERROR: config.json not found")
        sys.exit(1)

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

AUDIO_DEVICE = CONFIG["audio_device"]
SAMPLE_RATE = CONFIG["sample_rate"]
CHUNK_SEC = CONFIG["chunk_duration_sec"]
OVERLAP_SEC = CONFIG["overlap_sec"]
OUTPUT_DIR = Path(CONFIG["output_dir"])
KNOWN_NAMES = [n.lower() for n in CONFIG["known_names"]]
KNOWN_NAMES_ORIGINAL = CONFIG["known_names"]


def load_whisper_model():
    """Load faster-whisper model."""
    from faster_whisper import WhisperModel
    print(f"Loading whisper model '{CONFIG['whisper_model']}'...")
    model = WhisperModel(
        CONFIG["whisper_model"],
        device=CONFIG["whisper_device"],
        compute_type=CONFIG["whisper_compute_type"],
    )
    print("Model loaded.")
    return model


def find_active_sessions():
    """Find sessions from today that might still be active."""
    today = datetime.now().strftime("%Y-%m-%d")
    sessions = []
    if not OUTPUT_DIR.exists():
        return sessions
    for d in sorted(OUTPUT_DIR.iterdir()):
        if d.is_dir() and d.name.startswith(today):
            manifest = d / "manifest.json"
            if manifest.exists():
                sessions.append(d.name)
    return sessions


def find_latest_session():
    """Find the most recently modified session directory."""
    if not OUTPUT_DIR.exists():
        return None
    sessions = sorted(OUTPUT_DIR.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in sessions:
        if d.is_dir() and (d / "manifest.json").exists():
            return d.name
    return None


def match_name(text_name):
    """Match a spoken name to known names, or accept any valid first name."""
    text_lower = text_name.lower().strip()

    # Skip if empty or too short
    if len(text_lower) < 2:
        return None

    # Exact match with known names
    for i, name in enumerate(KNOWN_NAMES):
        if text_lower == name:
            return KNOWN_NAMES_ORIGINAL[i]

    # Close match with known names (edit distance) - use strict cutoff
    matches = get_close_matches(text_lower, KNOWN_NAMES, n=1, cutoff=0.8)
    if matches:
        idx = KNOWN_NAMES.index(matches[0])
        return KNOWN_NAMES_ORIGINAL[idx]

    # Accept any name that looks valid (alphabetic, reasonable length)
    if text_lower.isalpha() and 2 <= len(text_lower) <= 20:
        return text_name.strip().title()

    return None


def parse_callout(text):
    """
    Parse transcribed text for racing callouts.
    Returns list of (name, event_type) tuples.
    event_type: 'on_course' or 'at_the_gate'
    """
    results = []
    text_lower = text.lower().strip()

    # Patterns: "{name} on course", "{name} at the gate", "{name} in the gate"
    patterns = [
        (r'(\w+)\s+on\s+(?:the\s+)?course', 'on_course'),
        (r'(\w+)\s+(?:at|in)\s+the\s+gate', 'at_the_gate'),
        (r'(\w+)\s+on\s+track', 'on_course'),
        (r'(\w+)\s+ready', 'at_the_gate'),
        (r'(\w+)\s+go(?:ing)?', 'on_course'),
    ]

    for pattern, event_type in patterns:
        for m in re.finditer(pattern, text_lower):
            raw_name = m.group(1)
            matched = match_name(raw_name)
            if matched:
                results.append((matched, event_type))

    return results


class AnnotationStore:
    """Manages annotations for a session."""

    def __init__(self, session_id, upload=True):
        self.session_id = session_id
        self.session_dir = OUTPUT_DIR / session_id
        self.annotations_path = self.session_dir / "annotations.json"
        self.upload = upload
        self.data = self._load()

    def _load(self):
        if self.annotations_path.exists():
            with open(self.annotations_path) as f:
                return json.load(f)
        return {
            "session_id": self.session_id,
            "created_at": datetime.now().isoformat(),
            "callouts": [],
            "run_assignments": {},
        }

    def save(self):
        with open(self.annotations_path, "w") as f:
            json.dump(self.data, f, indent=2)
        if self.upload:
            self._upload_to_s3()

    def _upload_to_s3(self):
        """Upload annotations to S3 in background."""
        try:
            import boto3
            s3 = boto3.client("s3", region_name="us-east-1")
            s3_key = f"events/{self.session_id}/annotations.json"
            s3.upload_file(
                str(self.annotations_path),
                "avillachlab-netm",
                s3_key,
                ExtraArgs={
                    "ContentType": "application/json",
                    "CacheControl": "no-cache, no-store, must-revalidate",
                },
            )
        except Exception as e:
            print(f"  S3 upload failed (non-fatal): {e}")

    def add_callout(self, name, event_type, timestamp, confidence, raw_text):
        """Record a detected callout."""
        entry = {
            "timestamp": timestamp.isoformat(),
            "name": name,
            "event": event_type,
            "confidence": round(confidence, 2),
            "raw_text": raw_text,
        }
        self.data["callouts"].append(entry)
        print(f"  >> {name} {event_type.replace('_', ' ')} @ {timestamp.strftime('%H:%M:%S')} (conf={confidence:.0%})")
        self._update_run_assignments()
        self.save()

    def _update_run_assignments(self):
        """Match callouts to runs based on timestamps."""
        manifest_path = self.session_dir / "manifest.json"
        if not manifest_path.exists():
            return

        with open(manifest_path) as f:
            manifest = json.load(f)

        runs = manifest.get("runs", [])
        if not runs:
            return

        # Sort callouts by timestamp
        callouts = sorted(self.data["callouts"], key=lambda c: c["timestamp"])
        on_course = [c for c in callouts if c["event"] == "on_course"]
        at_gate = [c for c in callouts if c["event"] == "at_the_gate"]

        assignments = {}
        for run in runs:
            run_num = run["run_number"]
            run_ts = datetime.fromisoformat(run["timestamp"])

            # Find most recent "on course" before this run's timestamp
            best_callout = None
            for c in on_course:
                c_ts = datetime.fromisoformat(c["timestamp"])
                # Must be before the run, within 60 seconds
                delta = (run_ts - c_ts).total_seconds()
                if 0 <= delta <= 60:
                    best_callout = c

            # If no "on course", check if the previous run's "at the gate"
            # matches (the person at the gate becomes the next one on course)
            if best_callout is None and run_num > 1:
                prev_run = next((r for r in runs if r["run_number"] == run_num - 1), None)
                if prev_run:
                    prev_ts = datetime.fromisoformat(prev_run["timestamp"])
                    for c in at_gate:
                        c_ts = datetime.fromisoformat(c["timestamp"])
                        delta = (prev_ts - c_ts).total_seconds()
                        if -10 <= delta <= 60:
                            best_callout = c

            if best_callout:
                existing = self.data["run_assignments"].get(str(run_num))
                assignments[str(run_num)] = {
                    "name": best_callout["name"],
                    "source": best_callout["event"],
                    "callout_timestamp": best_callout["timestamp"],
                    "confidence": best_callout["confidence"],
                    "validated": existing["validated"] if existing and "validated" in existing else None,
                }

        self.data["run_assignments"] = assignments


def record_chunk(device, rate, duration_sec, wav_path):
    """Record a chunk of audio using arecord (Linux) or ffmpeg (Mac)."""
    import platform

    if platform.system() == "Darwin":
        # macOS: use ffmpeg with avfoundation
        # device format is ":N" where N is audio device index
        cmd = [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-i", device,
            "-t", str(duration_sec),
            "-ar", str(rate),
            "-ac", "1",
            "-acodec", "pcm_s16le",
            "-loglevel", "error",
            wav_path,
        ]
    else:
        # Linux: use arecord
        cmd = [
            "arecord", "-D", device,
            "-f", "S16_LE",
            "-r", str(rate),
            "-c", "1",
            "-d", str(duration_sec),
            "-q",
            wav_path,
        ]

    result = subprocess.run(cmd, capture_output=True, timeout=duration_sec + 10)
    return result.returncode == 0


def listen_loop(model, store, stop_event):
    """Main loop: record audio chunks and transcribe."""
    chunk_dur = CHUNK_SEC
    tmp_dir = tempfile.mkdtemp(prefix="skiframes_audio_")

    print(f"\nListening on {AUDIO_DEVICE} (rate={SAMPLE_RATE}, chunk={chunk_dur}s)")
    print(f"Session: {store.session_id}")
    print(f"Known names: {len(KNOWN_NAMES_ORIGINAL)}")
    print("Press Ctrl+C to stop.\n")

    chunk_idx = 0
    while not stop_event.is_set():
        wav_path = os.path.join(tmp_dir, f"chunk_{chunk_idx:06d}.wav")

        try:
            ok = record_chunk(AUDIO_DEVICE, SAMPLE_RATE, chunk_dur, wav_path)
            if not ok:
                print("  Warning: audio recording failed, retrying...")
                time.sleep(1)
                continue
        except subprocess.TimeoutExpired:
            print("  Warning: audio recording timed out, retrying...")
            continue

        # Transcribe
        try:
            segments, info = model.transcribe(
                wav_path,
                language=CONFIG.get("language", "en"),
                beam_size=3,
                best_of=3,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=300,
                    speech_pad_ms=200,
                ),
            )

            now = datetime.now()
            chunk_start = now - timedelta(seconds=chunk_dur)

            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue

                seg_time = chunk_start + timedelta(seconds=seg.start)
                callouts = parse_callout(text)

                if callouts:
                    for name, event_type in callouts:
                        store.add_callout(
                            name=name,
                            event_type=event_type,
                            timestamp=seg_time,
                            confidence=seg.avg_logprob if hasattr(seg, 'avg_logprob') else 0.5,
                            raw_text=text,
                        )
                elif text and len(text) > 3:
                    # Log non-matching speech for debugging
                    print(f"  [speech] {text}")

        except Exception as e:
            print(f"  Transcription error: {e}")

        # Cleanup old chunk files (keep last 5)
        try:
            os.unlink(wav_path)
        except OSError:
            pass

        chunk_idx += 1


def main():
    parser = argparse.ArgumentParser(description="Walkie-talkie listener for ski race annotations")
    parser.add_argument("--session", help="Session ID to annotate")
    parser.add_argument("--list-sessions", action="store_true", help="List available sessions")
    parser.add_argument("--names", nargs="+", help="Additional names to recognize")
    parser.add_argument("--no-upload", action="store_true", help="Don't upload annotations to S3")
    args = parser.parse_args()

    if args.list_sessions:
        sessions = find_active_sessions()
        if not sessions:
            latest = find_latest_session()
            if latest:
                print(f"No sessions from today. Latest: {latest}")
            else:
                print("No sessions found.")
        else:
            print("Today's sessions:")
            for s in sessions:
                print(f"  {s}")
        return

    # Add extra names if provided
    if args.names:
        for n in args.names:
            if n.lower() not in KNOWN_NAMES:
                KNOWN_NAMES.append(n.lower())
                KNOWN_NAMES_ORIGINAL.append(n)

    # Find session
    session_id = args.session
    if not session_id:
        sessions = find_active_sessions()
        if sessions:
            session_id = sessions[-1]
            print(f"Auto-detected active session: {session_id}")
        else:
            session_id = find_latest_session()
            if session_id:
                print(f"Using latest session: {session_id}")
            else:
                print("No session found. Use --session to specify.")
                sys.exit(1)

    session_dir = OUTPUT_DIR / session_id
    if not session_dir.exists():
        print(f"Session directory not found: {session_dir}")
        sys.exit(1)

    # Initialize
    model = load_whisper_model()
    store = AnnotationStore(session_id, upload=not args.no_upload)

    # Handle Ctrl+C
    stop = Event()
    def handler(sig, frame):
        print("\nStopping...")
        stop.set()
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    listen_loop(model, store, stop)

    print(f"\nAnnotations saved to: {store.annotations_path}")
    print(f"Total callouts: {len(store.data['callouts'])}")
    print(f"Run assignments: {len(store.data['run_assignments'])}")


if __name__ == "__main__":
    main()
