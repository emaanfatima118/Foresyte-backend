"""
Clip a video between two timestamps.

Usage:
  python scripts/clip_video.py --input "path/to/video.mp4" --start 00:10:00 --end 00:12:30
  python scripts/clip_video.py --input "video.mp4" --start 600 --duration 150 --output "clip.mp4"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional


def parse_timestamp(value: str) -> float:
    """Parse timestamp formats: SS, MM:SS, HH:MM:SS(.mmm)."""
    raw = str(value).strip()
    if not raw:
        raise ValueError("Timestamp is empty")

    parts = raw.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Invalid timestamp: {value}")


def format_hhmmss(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def default_output_path(input_path: str, start_sec: float, end_sec: float) -> str:
    base, ext = os.path.splitext(input_path)
    ext = ext or ".mp4"
    return f"{base}_clip_{int(start_sec)}s_to_{int(end_sec)}s{ext}"


def clip_with_ffmpeg(input_path: str, output_path: str, start_sec: float, end_sec: float) -> None:
    duration = end_sec - start_sec
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        format_hhmmss(start_sec),
        "-i",
        input_path,
        "-t",
        str(duration),
        "-c",
        "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or "ffmpeg failed")


def clip_with_opencv(input_path: str, output_path: str, start_sec: float, end_sec: float) -> None:
    import cv2  # lazy import fallback

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError("Could not open input video")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    if fourcc == 0:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    start_frame = int(start_sec * fps)
    end_frame = int(end_sec * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
    if not out.isOpened():
        cap.release()
        raise RuntimeError("Could not create output video")

    current = start_frame
    while current < end_frame:
        ok, frame = cap.read()
        if not ok:
            break
        out.write(frame)
        current += 1

    cap.release()
    out.release()


def compute_end_time(start: float, end: Optional[float], duration: Optional[float]) -> float:
    if end is not None and duration is not None:
        raise ValueError("Use either --end or --duration, not both")
    if end is None and duration is None:
        raise ValueError("Provide --end or --duration")
    if duration is not None:
        if duration <= 0:
            raise ValueError("--duration must be > 0")
        end = start + duration
    assert end is not None
    if end <= start:
        raise ValueError("End time must be greater than start time")
    return end


def main() -> int:
    parser = argparse.ArgumentParser(description="Clip a video between timestamps.")
    parser.add_argument("--input", required=True, help="Input video path")
    parser.add_argument("--start", required=True, help="Start time (SS | MM:SS | HH:MM:SS)")
    parser.add_argument("--end", help="End time (SS | MM:SS | HH:MM:SS)")
    parser.add_argument("--duration", type=float, help="Duration in seconds (alternative to --end)")
    parser.add_argument("--output", help="Output path (optional)")
    args = parser.parse_args()

    input_path = args.input
    if not os.path.isfile(input_path):
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1

    try:
        start_sec = parse_timestamp(args.start)
        end_sec = compute_end_time(
            start=start_sec,
            end=parse_timestamp(args.end) if args.end else None,
            duration=args.duration,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_path = args.output or default_output_path(input_path, start_sec, end_sec)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    print(f"Input : {input_path}")
    print(f"Start : {format_hhmmss(start_sec)}")
    print(f"End   : {format_hhmmss(end_sec)}")
    print(f"Output: {output_path}")

    try:
        clip_with_ffmpeg(input_path, output_path, start_sec, end_sec)
        print("Done (ffmpeg stream copy).")
        return 0
    except FileNotFoundError:
        print("ffmpeg not found; falling back to OpenCV re-encode...")
    except Exception as exc:
        print(f"ffmpeg failed ({exc}); falling back to OpenCV re-encode...")

    try:
        clip_with_opencv(input_path, output_path, start_sec, end_sec)
        print("Done (OpenCV fallback).")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
