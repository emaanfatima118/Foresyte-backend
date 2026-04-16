#!/usr/bin/env python3
"""
Extract frames from a video at a fixed time interval (default: 1 frame per second).

Uses OpenCV. For whole-video extraction with exact timing, install ffmpeg and use:
  ffmpeg -i video.mp4 -vf fps=1 out_dir/frame_%04d.jpg

Usage:
  python scripts/extract_video_frames.py path/to/video.mp4
  python scripts/extract_video_frames.py video.mp4 --interval 2 -o ./frames
  python scripts/extract_video_frames.py video.mp4 --start 15:27 --end 31:07
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import cv2


def parse_time(s: str | None) -> float | None:
    """Parse 'SS', 'MM:SS', or 'HH:MM:SS' to seconds."""
    if s is None:
        return None
    parts = [float(x) for x in s.strip().split(":")]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return int(parts[0]) * 60 + parts[1]
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + parts[2]
    raise ValueError(f"Invalid time: {s}")


def extract_frames(
    video_path: str,
    interval_seconds: float,
    output_dir: str | None,
    start_seconds: float | None,
    end_seconds: float | None,
    prefix: str,
    jpeg_quality: int,
) -> int:
    video_path = os.path.abspath(video_path)
    if not os.path.isfile(video_path):
        print(f"Error: file not found: {video_path}", file=sys.stderr)
        return -1

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: could not open: {video_path}", file=sys.stderr)
        return -1

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0.0

    t_start = 0.0 if start_seconds is None else max(0.0, float(start_seconds))
    t_end = duration if end_seconds is None else min(duration, float(end_seconds))

    stem = Path(video_path).stem
    if output_dir is None:
        out = os.path.join(os.path.dirname(video_path), f"{stem}_frames_{interval_seconds:g}s")
    else:
        out = os.path.abspath(output_dir)
    os.makedirs(out, exist_ok=True)

    if interval_seconds <= 0:
        print("Error: --interval must be > 0", file=sys.stderr)
        cap.release()
        return -1

    print(f"Video: {video_path}")
    print(f"  FPS: {fps:.3f}, duration: {duration:.2f}s, frames: {total_frames}")
    print(f"  Extract: {t_start:.2f}s .. {t_end:.2f}s, every {interval_seconds}s -> {out}")

    encode = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    count = 0
    t = t_start
    while t <= t_end + 1e-6:
        idx = int(t * fps)
        if idx >= total_frames:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            break
        m, s = divmod(int(t), 60)
        h, m = divmod(m, 60)
        if h > 0:
            ts = f"{h:02d}h{m:02d}m{s:02d}s"
        else:
            ts = f"{m:02d}m{s:02d}s"
        name = f"{prefix}{count:05d}_{ts}.jpg"
        cv2.imwrite(os.path.join(out, name), frame, encode)
        count += 1
        t += interval_seconds

    cap.release()
    print(f"Saved {count} frames.")
    return count


def main() -> int:
    p = argparse.ArgumentParser(description="Extract video frames at a fixed interval (default 1/s).")
    p.add_argument("video", help="Input video path")
    p.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between frames (default: 1 = one frame per second)",
    )
    p.add_argument("-o", "--output", default=None, help="Output directory (default: beside video, <name>_frames_<interval>s)")
    p.add_argument("--start", default=None, metavar="T", help="Start time: SS, MM:SS, or HH:MM:SS (default: 0)")
    p.add_argument("--end", default=None, metavar="T", help="End time (default: end of video)")
    p.add_argument("--prefix", default="frame_", help="Filename prefix (default: frame_)")
    p.add_argument("--quality", type=int, default=92, help="JPEG quality 0-100 (default: 92)")
    args = p.parse_args()

    try:
        start = parse_time(args.start)
        end = parse_time(args.end)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    n = extract_frames(
        args.video,
        args.interval,
        args.output,
        start,
        end,
        args.prefix,
        args.quality,
    )
    return 1 if n < 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
