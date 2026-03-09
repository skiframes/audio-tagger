#!/usr/bin/env python3
"""
Upload annotations.json to S3 so they're visible on skiframes.com.

Usage:
    python upload_annotations.py SESSION_ID
    python upload_annotations.py --all-today
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

OUTPUT_DIR = Path(CONFIG["output_dir"])
S3_BUCKET = "avillachlab-netm"  # media bucket
S3_REGION = "us-east-1"


def upload_annotations(session_id):
    """Upload annotations.json for a session to S3."""
    annotations_path = OUTPUT_DIR / session_id / "annotations.json"
    if not annotations_path.exists():
        print(f"No annotations found for {session_id}")
        return False

    s3 = boto3.client("s3", region_name=S3_REGION)
    s3_key = f"events/{session_id}/annotations.json"

    try:
        s3.upload_file(
            str(annotations_path),
            S3_BUCKET,
            s3_key,
            ExtraArgs={
                "ContentType": "application/json",
                "CacheControl": "no-cache, no-store, must-revalidate",
            },
        )
        print(f"Uploaded: s3://{S3_BUCKET}/{s3_key}")
        return True
    except ClientError as e:
        print(f"Upload failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Upload annotations to S3")
    parser.add_argument("session_id", nargs="?", help="Session ID")
    parser.add_argument("--all-today", action="store_true", help="Upload all sessions from today")
    args = parser.parse_args()

    if args.all_today:
        today = datetime.now().strftime("%Y-%m-%d")
        count = 0
        for d in sorted(OUTPUT_DIR.iterdir()):
            if d.is_dir() and d.name.startswith(today):
                if (d / "annotations.json").exists():
                    upload_annotations(d.name)
                    count += 1
        print(f"Uploaded {count} annotation files")
    elif args.session_id:
        upload_annotations(args.session_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
