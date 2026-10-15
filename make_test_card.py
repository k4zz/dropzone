#!/usr/bin/env python3
"""
Creates a fake DJI memory card for testing dropzone.py.

The videos are not playable (the picture data is empty), but they have real MP4
metadata (resolution, frame rate, codec, color, duration), so the details panel
shows meaningful values. Each recording gets DJI-style .SRT telemetry and most of
them also a .LRF proxy file.

Usage:
    python3 make_test_card.py                 # creates ./test_card next to this script
    python3 make_test_card.py D:\\test_card    # creates it somewhere else
    python3 make_test_card.py --small         # tiny files (fast, no real progress bar)

Tip: the progress bar is only interesting when the card is on a different drive than
the `recordings` folder – moving within one drive is an instant rename.
"""
from __future__ import annotations

import calendar
import os
import shutil
import struct
import sys
import time
from pathlib import Path

MP4_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01
MB = 1024 * 1024
HOME = (51.7592, 19.4560)  # take-off point used for the fake telemetry

# name, recorded at (local time), seconds, width, height, codec, bit depth, transfer, fps, size in MB, extras
RECORDINGS = [
    ("DCIM/DJI_001/DJI_20261012143005_0001_D", "2026-10-12 14:30:05", 72, 3840, 2160, "hevc", 10, "hlg", 29.97, 260, "srt lrf"),
    ("DCIM/DJI_001/DJI_20261012143910_0002_D", "2026-10-12 14:39:10", 95, 3840, 2160, "hevc", 10, "sdr", 25, 300, "srt lrf"),
    ("DCIM/DJI_001/DJI_20261012144530_0003_D", "2026-10-12 14:45:30", 41, 1920, 1080, "avc", 8, "sdr", 59.94, 80, "srt"),
    ("DCIM/DJI_001/DJI_20261014091200_0004_D", "2026-10-14 09:12:00", 63, 2720, 1530, "avc", 8, "sdr", 30, 120, "srt lrf"),
    ("DCIM/DJI_001/DJI_20261014092500_0005_D", "2026-10-14 09:25:00", 118, 3840, 2160, "hevc", 10, "sdr", 50, 280, "srt lrf"),
    ("DCIM/DJI_002/DJI_20261015180000_0006_D", "2026-10-15 18:00:00", 34, 2160, 3840, "hevc", 8, "sdr", 30, 90, "srt lrf"),
    ("DCIM/DJI_002/DJI_20261015181500_0007_D", "2026-10-15 18:15:00", 57, 3840, 2160, "hevc", 10, "hlg", 29.97, 160, "srt lrf"),
    ("DCIM/DJI_002/DJI_0008", "2026-10-13 12:00:00", 25, 1920, 1080, "avc", 8, "sdr", 30, 40, ""),
]
COLOR_MODES = {"hlg": "hlg", "sdr": "dlog_m"}


def box(kind: str, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind.encode()) + body


def full_box(kind: str, body: bytes) -> bytes:
    return box(kind, b"\0\0\0\0" + body)  # version 0, no flags


def moov(created: int, seconds: int, width: int, height: int, codec: str,
         bit_depth: int, transfer: str, fps: float) -> bytes:
    mp4_time = created + MP4_EPOCH_OFFSET
    mvhd = full_box("mvhd", struct.pack(">IIII", mp4_time, mp4_time, 1000, seconds * 1000) + b"\0" * 80)

    if codec == "hevc":
        config = bytearray(23)
        config[0], config[1], config[12] = 1, 2 if bit_depth == 10 else 1, 153
        config[16], config[17], config[18] = 0xFD, 0xF8 | (bit_depth - 8), 0xF8 | (bit_depth - 8)
        decoder = box("hvcC", bytes(config))
        fourcc = "hvc1"
    else:
        decoder = box("avcC", bytes([1, 100, 0, 51, 0xFF, 0xE1, 0]))  # High profile, level 5.1
        fourcc = "avc1"
    primaries, transfer_id = (9, 18) if transfer == "hlg" else (1, 1)
    colr = box("colr", b"nclx" + struct.pack(">HHHB", primaries, transfer_id, primaries, 0))
    visual = (b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 16 + struct.pack(">HH", width, height)
              + struct.pack(">IIIH", 0x480000, 0x480000, 0, 1) + b"\0" * 32 + struct.pack(">Hh", 24, -1))
    stsd = full_box("stsd", struct.pack(">I", 1) + box(fourcc, visual + decoder + colr))

    if fps % 1:  # NTSC rates: 29.97 = 30000/1001, 59.94 = 60000/1001
        timescale, delta = round(fps * 1.001) * 1000, 1001
    else:
        timescale, delta = int(fps) * 1000, 1000
    frames = int(seconds * timescale / delta)
    stts = full_box("stts", struct.pack(">III", 1, frames, delta))
    mdhd = full_box("mdhd", struct.pack(">IIII", mp4_time, mp4_time, timescale, frames * delta) + b"\0" * 4)
    hdlr_v = full_box("hdlr", b"\0" * 4 + b"vide" + b"\0" * 12 + b"VideoHandler\0")
    video = box("trak", box("mdia", mdhd + hdlr_v + box("minf", box("stbl", stsd + stts))))

    audio_entry = (b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 8
                   + struct.pack(">HHHHI", 2, 16, 0, 0, 48000 << 16))
    audio = box("trak", box("mdia",
        full_box("mdhd", struct.pack(">IIII", mp4_time, mp4_time, 48000, seconds * 48000) + b"\0" * 4)
        + full_box("hdlr", b"\0" * 4 + b"soun" + b"\0" * 12 + b"SoundHandler\0")
        + box("minf", box("stbl", full_box("stsd", struct.pack(">I", 1) + box("mp4a", audio_entry))))))
    return box("moov", mvhd + video + audio)


def write_mp4(path: Path, size: int, moov_box: bytes) -> None:
    """Writes ftyp + mdat (empty picture data of the given size) + moov at the end, like DJI does."""
    ftyp = box("ftyp", b"isom\0\0\x02\0isomiso2mp41")
    data_size = max(size - len(ftyp) - len(moov_box) - 8, 0)
    chunk = b"\0" * (8 * MB)
    with open(path, "wb") as f:
        f.write(ftyp)
        f.write(struct.pack(">I4s", 8 + data_size, b"mdat"))
        left = data_size
        while left:
            n = min(left, len(chunk))
            f.write(chunk[:n])
            left -= n
        f.write(moov_box)


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def write_srt(path: Path, started: str, seconds: int, color_mode: str, seed: int) -> None:
    """DJI-style telemetry, one entry per second (real files have one per frame)."""
    base = time.strptime(started, "%Y-%m-%d %H:%M:%S")
    lines = []
    for i in range(seconds):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.mktime(base) + i))
        progress = i / max(seconds - 1, 1)
        height = 120 * min(progress * 3, 1) * (1 - max(progress - 0.8, 0) * 4) + seed
        lat = HOME[0] + 0.0012 * progress * (1 + seed / 10)
        lon = HOME[1] + 0.0018 * progress
        iso = 100 if i < seconds * 0.7 else 200 + 100 * (seed % 3)
        lines.append(
            f"{i + 1}\n{srt_time(i)} --> {srt_time(i + 1)}\n"
            f'<font size="28">FrameCnt: {i + 1}, DiffTime: 1000ms\n{stamp}.000\n'
            f"[iso: {iso}] [shutter: 1/{[500, 640, 1000][seed % 3]}.0] [fnum: 1.7] [ev: 0] "
            f"[color_md: {color_mode}] [focal_len: 24.00] [latitude: {lat:.6f}] [longitude: {lon:.6f}] "
            f"[rel_alt: {height:.3f} abs_alt: {height + 210:.3f}] [ct: 5600] </font>\n"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_time(path: Path, started: str, seconds: int) -> None:
    ts = time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S")) + seconds
    os.utime(path, (ts, ts))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    small = "--small" in sys.argv
    card = Path(args[0]) if args else Path(__file__).resolve().parent / "test_card"
    if card.exists():
        answer = input(f"{card} already exists. Delete it and create a new one? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing changed.")
            return 1
        shutil.rmtree(card)

    total = 0
    for seed, (name, started, seconds, w, h, codec, depth, transfer, fps, size_mb, extras) in enumerate(RECORDINGS):
        base = card / name
        base.parent.mkdir(parents=True, exist_ok=True)
        created = calendar.timegm(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
        size = (size_mb // 20 if small else size_mb) * MB
        write_mp4(base.with_suffix(".MP4"), size, moov(created, seconds, w, h, codec, depth, transfer, fps))
        set_time(base.with_suffix(".MP4"), started, seconds)
        total += size
        if "srt" in extras:
            write_srt(base.with_suffix(".SRT"), started, seconds, COLOR_MODES[transfer], seed)
            set_time(base.with_suffix(".SRT"), started, seconds)
        if "lrf" in extras:
            lrf = base.with_suffix(".LRF")
            write_mp4(lrf, size // 25, moov(created, seconds, 1280, 720, "avc", 8, "sdr", fps))
            set_time(lrf, started, seconds)
            total += size // 25
        print(f"  {base.with_suffix('.MP4').relative_to(card)}  ({size // MB} MB)")

    # Things a real card has besides videos.
    (card / "DCIM" / "DJI_001" / "DJI_20261012143300_0010_D.JPG").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 4096)
    (card / "DCIM" / "PANORAMA" / "100_0001").mkdir(parents=True, exist_ok=True)
    (card / "DCIM" / "PANORAMA" / "100_0001" / "PANO_0001.JPG").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 4096)
    (card / "MISC" / "THM").mkdir(parents=True, exist_ok=True)
    (card / "MISC" / "dji_finfo.db").write_bytes(b"\0" * 40960)

    print(f"\nTest card created: {card}  ({total / MB:.0f} MB of recordings)")
    print(f"Run:  python3 dropzone.py \"{card}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
