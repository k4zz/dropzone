#!/usr/bin/env python3
"""
Dropzone – drone footage sorter.

Moves selected recordings (e.g. from an SD card) into a destination folder of your
choice, into subfolders named `[<flight location>][<YYYY-MM-DD>]`.
Companion files with the same name (.SRT, .LRF) are moved along with each recording.

Recently used destination folders are remembered in `dropzone_config.json` next to
this script, separately for each environment (Windows, WSL, Linux, macOS), because the
same folder has a different path in each of them (e.g. D:\\Drony vs /mnt/d/Drony).

Usage:
    python3 dropzone.py              # start from the list of drives
    python3 dropzone.py E:\\DCIM      # start browsing in the given folder

Keys:
    Up/Down, PgUp/PgDn, Home/End    move
    Right                           expand a drive / folder
    Left, Backspace, Esc            collapse a folder / jump to its parent
    Enter                           choose the folder with recordings / continue
    Space                           select / unselect a recording
    Ctrl+Space (or a)               select / unselect all recordings
    p                               play the recording (VLC or the default player)
    q                               quit

Requires only Python 3.8+ (standard library).
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DESTINATION = SCRIPT_DIR / "recordings"  # offered when no destination was used yet
RECENT_DESTINATIONS = 5

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".3gp"}
COMPANION_EXT = {".srt", ".lrf"}
SKIP_DIRS = {"system volume information", "$recycle.bin", "lost+found"}

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_WSL = not IS_WINDOWS and "microsoft" in platform.release().lower()

if IS_WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REV = "\x1b[7m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"

Row = Tuple[str, str]  # (text, ANSI style)


class Quit(Exception):
    """The user quit the program."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")


def date_from_name(name: str) -> Optional[date]:
    """Date from a file name, e.g. DJI_20261015123456_0001_D.MP4 -> 2026-10-15."""
    for m in DATE_RE.finditer(name):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


def sanitize_place(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\[\]\x00-\x1f]', "", text)
    return re.sub(r"\s+", " ", text).strip(" .")


def setup_console() -> None:
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # enable ANSI escape codes
        kernel32.SetErrorMode(0x0001)  # no "insert a disk" dialogs for empty card readers
    for stream in (sys.stdout, sys.stdin):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

CONFIG_FILE = SCRIPT_DIR / "dropzone_config.json"
ENVIRONMENT = "windows" if IS_WINDOWS else "wsl" if IS_WSL else "macos" if IS_MAC else "linux"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def ensure_config() -> None:
    """Creates the settings file on the first run; later runs just use the existing one."""
    if not CONFIG_FILE.exists():
        save_config({"recent_destinations": []})


def load_config() -> dict:
    """Settings of the current environment."""
    config = _read_json(CONFIG_FILE).get(ENVIRONMENT)
    return config if isinstance(config, dict) else {}


def save_config(config: dict) -> None:
    data = _read_json(CONFIG_FILE)
    data[ENVIRONMENT] = config
    try:
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        pass  # not being able to remember the destination must not stop the move


def recent_destinations() -> List[Path]:
    return [Path(p) for p in load_config().get("recent_destinations", []) if isinstance(p, str)]


def remember_destination(dest: Path) -> None:
    config = load_config()
    paths = [str(dest)] + [str(p) for p in recent_destinations() if p != dest]
    config["recent_destinations"] = paths[:RECENT_DESTINATIONS]
    save_config(config)


# --------------------------------------------------------------------------- #
# Terminal: full-screen mode, drawing and key input
# --------------------------------------------------------------------------- #

APP_TITLE = "Dropzone"
INFO_LINES = 4  # height of the section describing the current step
KEY_CELL = 14   # text width of one "key description" cell in the key help line

Key = Tuple[str, str]  # (key, what it does)


def split_widths(width: int) -> Tuple[int, int]:
    """(list width, details panel width) for a given terminal width; panel is 0 if too narrow."""
    if width < 90:
        return width, 0
    panel = min(52, max(36, width * 2 // 5))
    return width - panel - 3, panel


def screen_width() -> int:
    return max(20, shutil.get_terminal_size((80, 24)).columns) - 1


class Terminal:
    def __init__(self) -> None:
        self.active = False
        self._saved = None

    def __enter__(self) -> "Terminal":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if not IS_WINDOWS:
            fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        self.active = True

    def stop(self) -> None:
        if not self.active:
            return
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if not IS_WINDOWS and self._saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
        self.active = False

    @contextlib.contextmanager
    def paused(self):
        """Temporarily switch back to the normal terminal (e.g. for typing text or sudo)."""
        self.stop()
        try:
            yield
        finally:
            self.start()

    def read_key(self, raw: bool = False) -> str:
        """Named keys ("UP", "ENTER", ...) or a typed character; lowercased unless raw."""
        sys.stdout.flush()
        key = _read_key_windows() if IS_WINDOWS else _read_key_posix()
        return key if raw or len(key) != 1 else key.lower()

    def render(self, step: str, info: List[Row], rows: List[Row], cursor: int, keys: List[Key],
               status: str = "", panel: Optional[List[Row]] = None, highlight: bool = True) -> None:
        """Draws a screen. Every screen has the same sections, always of the same height:

            title │ step
            ──────────────
            info about the step          (INFO_LINES)
            ──────────────
            list         │ details panel (fills the rest)
            ──────────────
            status line
            key help: one line of equally wide cells
        """
        width = screen_width()
        height = shutil.get_terminal_size((80, 24)).lines
        left_w, panel_w = split_widths(width) if panel is not None else (width, 0)
        body = max(3, height - INFO_LINES - 7)
        n = len(rows)
        top = 0 if n <= body else min(max(0, cursor - body // 2), n - body)

        out = ["\x1b[H"]

        def line(text: str, style: str = "") -> None:
            out.append(f"{style}{fit(text, width)}{RESET}\x1b[K\r\n")

        def rule(junction: str) -> None:
            if panel_w:
                line("─" * (left_w + 1) + junction + "─" * (width - left_w - 2), DIM)
            else:
                line("─" * width, DIM)

        out.append(f"{BOLD}{APP_TITLE}{RESET}{DIM}  │  {RESET}"
                   f"{CYAN}{BOLD}{fit(step, width - len(APP_TITLE) - 5)}{RESET}\x1b[K\r\n")
        rule("─")
        for j in range(INFO_LINES):
            line(*(info[j] if j < len(info) else ("", "")))
        rule("┬")
        for j in range(body):
            i = top + j
            text, style = rows[i] if i < n else ("", "")
            cell = fit(text, left_w).ljust(left_w)
            out.append(f"{REV if highlight and i == cursor else ''}{style}{cell}{RESET}")
            if panel_w:
                p_text, p_style = panel[j] if panel and j < len(panel) else ("", "")
                out.append(f"{DIM} │ {RESET}{p_style}{fit(p_text, panel_w)}{RESET}")
            out.append("\x1b[K\r\n")
        rule("┴")
        position = f"[{cursor + 1}/{n}]  " if highlight and n > body else ""
        line(position + status, YELLOW)
        cells = []
        for key, action in keys[: max(1, (width + 1) // (KEY_CELL + 3))]:
            pad = " " * max(KEY_CELL - len(key) - len(action) - 1, 0)
            cells.append(f" {BOLD}{key}{RESET} {action}{pad} ")
        out.append(f"{DIM}│{RESET}".join(cells) + "\x1b[K\r\n")
        out.append("\x1b[J")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def _read_key_posix() -> str:
    fd = sys.stdin.fileno()

    def read(timeout: Optional[float] = None) -> bytes:
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return b""
        return os.read(fd, 1)

    b = read()
    if b == b"\x1b":
        b2 = read(0.05)
        if not b2:
            return "ESC"
        if b2 not in (b"[", b"O"):
            return ""
        seq = b""
        while True:
            c = read(0.05)
            if not c:
                break
            seq += c
            if 0x40 <= c[0] <= 0x7E:
                break
        return {
            "A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT",
            "H": "HOME", "F": "END", "1~": "HOME", "4~": "END",
            "5~": "PGUP", "6~": "PGDN",
        }.get(seq.decode(errors="ignore"), "")
    if b in (b"\r", b"\n"):
        return "ENTER"
    if b == b" ":
        return "SPACE"
    if b == b"\x00":
        return "CTRL_SPACE"
    if b in (b"\x7f", b"\x08"):
        return "BACKSPACE"
    if b == b"\t":
        return "TAB"
    if b == b"\x03":
        raise KeyboardInterrupt
    if b[0] >= 0xC0:  # first byte of a multi-byte UTF-8 character (e.g. ł, ó)
        b += os.read(fd, 1 if b[0] < 0xE0 else 2 if b[0] < 0xF0 else 3)
    return b.decode(errors="ignore")


def _read_key_windows() -> str:
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        if ch == "\x00" and not msvcrt.kbhit():
            return "CTRL_SPACE"
        return {
            "H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT",
            "G": "HOME", "O": "END", "I": "PGUP", "Q": "PGDN",
        }.get(msvcrt.getwch(), "")
    if ch == "\r":
        return "ENTER"
    if ch == " ":
        ctrl_down = ctypes.windll.user32.GetKeyState(0x11) & 0x8000
        return "CTRL_SPACE" if ctrl_down else "SPACE"
    if ch == "\x08":
        return "BACKSPACE"
    if ch == "\x1b":
        return "ESC"
    if ch == "\t":
        return "TAB"
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch


def move_cursor(key: str, cursor: int, n: int) -> int:
    if n == 0:
        return 0
    page = max(1, shutil.get_terminal_size((80, 24)).lines - 8)
    if key == "UP":
        return max(0, cursor - 1)
    if key == "DOWN":
        return min(n - 1, cursor + 1)
    if key == "PGUP":
        return max(0, cursor - page)
    if key == "PGDN":
        return min(n - 1, cursor + page)
    if key == "HOME":
        return 0
    if key == "END":
        return n - 1
    return cursor


# --------------------------------------------------------------------------- #
# Drives
# --------------------------------------------------------------------------- #

@dataclass
class Medium:
    label: str
    path: Optional[Path]
    removable: bool = False
    wsl_drive: str = ""   # letter of a Windows drive that still has to be mounted in WSL
    manual: bool = False  # the "enter a path" entry


DRIVE_TYPES = {2: "removable", 3: "disk", 4: "network", 5: "CD/DVD", 6: "RAM disk"}


def _space(path: Path) -> str:
    try:
        du = shutil.disk_usage(str(path))
    except OSError:
        return ""
    return f"{human_size(du.used)} used of {human_size(du.total)}"


def _windows_media() -> List[Medium]:
    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    media = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        root = f"{chr(65 + i)}:\\"
        dtype = kernel32.GetDriveTypeW(root)
        if dtype in (0, 1):
            continue
        name = ctypes.create_unicode_buffer(261)
        if not kernel32.GetVolumeInformationW(root, name, 261, None, None, None, None, 0):
            continue  # empty card reader
        label = f"{root:<4} {name.value or '(no label)':<20} [{DRIVE_TYPES.get(dtype, '?')}]  {_space(Path(root))}"
        media.append(Medium(label, Path(root), removable=dtype == 2))
    return media


def _wsl_windows_drives() -> Optional[List[Tuple[str, int, str]]]:
    ps = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_LogicalDisk | ForEach-Object "
        "{ if ($_.Size) { '{0}|{1}|{2}' -f $_.DeviceID,$_.DriveType,$_.VolumeName } }"
    )
    try:
        result = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=20,
            cwd="/mnt/c" if os.path.isdir("/mnt/c") else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    drives = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and re.fullmatch(r"[A-Za-z]:", parts[0]):
            drives.append((parts[0][0].upper(), int(parts[1] or 0), parts[2]))
    return drives or None


def _wsl_media() -> List[Medium]:
    media = []
    drives = _wsl_windows_drives()
    if drives is None:
        # No PowerShell: show what is already mounted under /mnt.
        for p in sorted(Path("/mnt").glob("?")):
            if os.path.ismount(p):
                media.append(Medium(f"{p}   {_space(p)}", p))
        return media
    for letter, dtype, name in drives:
        mount = Path(f"/mnt/{letter.lower()}")
        base = f"{letter}:   {name or '(no label)':<20} [{DRIVE_TYPES.get(dtype, '?')}]"
        if os.path.ismount(mount):
            media.append(Medium(f"{base}  {mount}  {_space(mount)}", mount, removable=dtype == 2))
        else:
            media.append(Medium(f"{base}  (not mounted – open to mount)", None,
                                removable=dtype == 2, wsl_drive=letter))
    return media


def _posix_mounts(prefixes: Tuple[str, ...]) -> List[Path]:
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        mp = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
        if mp.startswith(prefixes) and Path(mp) not in found:
            found.append(Path(mp))
    return found


def list_media() -> List[Medium]:
    if IS_WINDOWS:
        media = _windows_media()
    elif IS_MAC:
        media = []
        volumes = Path("/Volumes")
        if volumes.is_dir():
            for p in sorted(volumes.iterdir()):
                if p.resolve() != Path("/"):
                    media.append(Medium(f"{p.name:<24} {_space(p)}", p, removable=True))
    else:
        media = _wsl_media() if IS_WSL else []
        prefixes = ("/media/", "/run/media/") if IS_WSL else ("/media/", "/run/media/", "/mnt/")
        for p in _posix_mounts(prefixes):
            removable = str(p).startswith(("/media/", "/run/media/"))
            media.append(Medium(f"{str(p):<24} {_space(p)}", p, removable=removable))
    media.sort(key=lambda m: not m.removable)
    media.append(Medium("Enter a path…", None, manual=True))
    return media


def mount_wsl_drive(letter: str) -> Optional[Path]:
    mount = Path(f"/mnt/{letter.lower()}")
    print(f"\nMounting drive {letter}: at {mount} (sudo may ask for your password)…\n")
    subprocess.run(["sudo", "mkdir", "-p", str(mount)])
    subprocess.run(["sudo", "mount", "-t", "drvfs", f"{letter}:", str(mount),
                    "-o", f"uid={os.getuid()},gid={os.getgid()}"])
    if os.path.ismount(mount):
        return mount
    input("\nCould not mount the drive. Press Enter to go back…")
    return None


# --------------------------------------------------------------------------- #
# Playback
# --------------------------------------------------------------------------- #

def _popen(args: List[str], **kwargs) -> None:
    if not IS_WINDOWS:
        kwargs.setdefault("start_new_session", True)
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **kwargs)


def play(path: Path) -> str:
    """Opens a recording in VLC if installed, otherwise in the default player. Returns the player name."""
    try:
        if IS_WINDOWS:
            candidates = [shutil.which("vlc")] + [
                os.path.join(os.environ[var], "VideoLAN", "VLC", "vlc.exe")
                for var in ("ProgramFiles", "ProgramFiles(x86)") if var in os.environ
            ]
            vlc = next((c for c in candidates if c and os.path.isfile(c)), None)
            if vlc:
                _popen([vlc, str(path)])
                return "VLC"
            os.startfile(str(path))  # type: ignore[attr-defined]
            return "the default player"
        if IS_WSL:
            win_path = subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
            for vlc in ("/mnt/c/Program Files/VideoLAN/VLC/vlc.exe",
                        "/mnt/c/Program Files (x86)/VideoLAN/VLC/vlc.exe"):
                if os.path.isfile(vlc):
                    _popen([vlc, win_path], cwd="/mnt/c")
                    return "VLC"
            _popen(["cmd.exe", "/c", "start", "", win_path], cwd="/mnt/c")
            return "the default player"
        if IS_MAC:
            if Path("/Applications/VLC.app").exists():
                _popen(["open", "-a", "VLC", str(path)])
                return "VLC"
            _popen(["open", str(path)])
            return "the default player"
        vlc = shutil.which("vlc")
        if vlc:
            _popen([vlc, str(path)])
            return "VLC"
        _popen(["xdg-open", str(path)])
        return "the default player"
    except (OSError, subprocess.CalledProcessError) as e:
        raise RuntimeError(f"Could not start a video player: {e}") from e


def play_status(path: Path) -> str:
    try:
        return f"► Opening {path.name} in {play(path)}"
    except RuntimeError as e:
        return str(e)


# --------------------------------------------------------------------------- #
# Video metadata (MP4 / MOV parser) and DJI telemetry (.SRT)
# --------------------------------------------------------------------------- #

MP4_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01

VIDEO_CODECS = {
    "avc1": "H.264", "avc3": "H.264", "hvc1": "H.265", "hev1": "H.265", "av01": "AV1",
    "mp4v": "MPEG-4", "apch": "ProRes 422 HQ", "apcn": "ProRes 422", "apcs": "ProRes 422 LT",
    "apco": "ProRes 422 Proxy", "ap4h": "ProRes 4444", "ap4x": "ProRes 4444 XQ",
}
AUDIO_CODECS = {"mp4a": "AAC", "lpcm": "PCM", "sowt": "PCM", "twos": "PCM",
                "ac-3": "AC-3", "ec-3": "E-AC-3", "Opus": "Opus"}
AVC_PROFILES = {66: "Baseline", 77: "Main", 88: "Extended", 100: "High",
                110: "High 10", 122: "High 4:2:2", 244: "High 4:4:4"}
HEVC_PROFILES = {1: "Main", 2: "Main 10", 3: "Main Still", 4: "RExt"}
TRANSFERS = {1: "SDR", 6: "SDR", 13: "SDR", 14: "SDR", 15: "SDR", 16: "HDR10 (PQ)", 18: "HLG"}
PRIMARIES = {1: "BT.709", 5: "BT.601", 6: "BT.601", 9: "BT.2020", 12: "Display P3"}


@dataclass
class MediaInfo:
    duration: Optional[float] = None
    created: Optional[datetime] = None
    width: int = 0
    height: int = 0
    fps: Optional[float] = None
    codec: str = ""
    bit_depth: Optional[int] = None
    color: str = ""
    audio: str = ""


def _boxes(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[Tuple[str, int, int]]:
    """Yields (type, body_start, body_end) for consecutive ISO-BMFF boxes."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind.decode("latin-1"), pos + header, pos + size
        pos += size


def _child(data: bytes, span: Tuple[int, int], *path: str) -> Optional[Tuple[int, int]]:
    """Body span of the first box found along a path of box types, e.g. ("minf", "stbl")."""
    for kind in path:
        span = next(((s, e) for k, s, e in _boxes(data, *span) if k == kind), None)
        if span is None:
            return None
    return span


def _read_moov(path: Path) -> Optional[bytes]:
    """Reads the 'moov' box (the index with all metadata) without reading the video data."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_end = f.tell()
        pos = 0
        while pos + 8 <= file_end:
            f.seek(pos)
            head = f.read(16)
            size, kind = struct.unpack_from(">I4s", head)
            header = 8
            if size == 1 and len(head) == 16:
                size = struct.unpack_from(">Q", head, 8)[0]
                header = 16
            elif size == 0:
                size = file_end - pos
            if size < header:
                return None
            if kind == b"moov":
                if size > 64 * 1024 * 1024:
                    return None
                f.seek(pos + header)
                return f.read(size - header)
            pos += size
    return None


def probe_video(path: Path) -> Optional[MediaInfo]:
    try:
        moov = _read_moov(path)
    except (OSError, struct.error):
        return None
    if not moov:
        return None
    info = MediaInfo()
    try:
        whole = (0, len(moov))
        mvhd = _child(moov, whole, "mvhd")
        if mvhd:
            _parse_mvhd(moov, mvhd[0], info)
        for kind, s, e in _boxes(moov):
            if kind == "trak":
                _parse_trak(moov, (s, e), info)
    except (struct.error, IndexError, ValueError, OverflowError):
        pass  # keep whatever was read before the damaged part
    return info


def _parse_mvhd(d: bytes, s: int, info: MediaInfo) -> None:
    if d[s] == 1:
        created = struct.unpack_from(">Q", d, s + 4)[0]
        timescale, duration = struct.unpack_from(">IQ", d, s + 20)
    else:
        created = struct.unpack_from(">I", d, s + 4)[0]
        timescale, duration = struct.unpack_from(">II", d, s + 12)
    if timescale:
        info.duration = duration / timescale
    ts = created - MP4_EPOCH_OFFSET
    if ts > 946684800:  # ignore empty / bogus values before 2000-01-01
        info.created = datetime.fromtimestamp(ts, timezone.utc)


def _parse_trak(d: bytes, trak: Tuple[int, int], info: MediaInfo) -> None:
    mdia = _child(d, trak, "mdia")
    if not mdia:
        return
    hdlr, mdhd, stbl = _child(d, mdia, "hdlr"), _child(d, mdia, "mdhd"), _child(d, mdia, "minf", "stbl")
    stsd = _child(d, stbl, "stsd") if stbl else None
    if not (hdlr and mdhd and stsd):
        return
    handler = d[hdlr[0] + 8:hdlr[0] + 12]
    timescale = struct.unpack_from(">I", d, mdhd[0] + (20 if d[mdhd[0]] == 1 else 12))[0]

    entry = stsd[0] + 8  # skip version/flags and entry count
    entry_size, fmt_raw = struct.unpack_from(">I4s", d, entry)
    fmt = fmt_raw.decode("latin-1")
    body, body_end = entry + 8, min(entry + entry_size, stsd[1])

    if handler == b"vide" and not info.codec:
        info.codec = VIDEO_CODECS.get(fmt, fmt.strip())
        info.width, info.height = struct.unpack_from(">HH", d, body + 24)
        for kind, cs, _ in _boxes(d, body + 78, body_end):
            if kind == "avcC":
                profile = d[cs + 1]
                info.codec += " " + AVC_PROFILES.get(profile, f"profile {profile}")
                info.bit_depth = 10 if profile == 110 else (8 if profile < 110 else None)
            elif kind == "hvcC":
                profile = d[cs + 1] & 0x1F
                info.codec += " " + HEVC_PROFILES.get(profile, f"profile {profile}")
                info.bit_depth = (d[cs + 17] & 0x07) + 8
            elif kind == "colr" and d[cs:cs + 4] == b"nclx":
                primaries, transfer = struct.unpack_from(">HH", d, cs + 4)
                parts = [TRANSFERS.get(transfer, f"transfer {transfer}"), PRIMARIES.get(primaries, "")]
                info.color = " / ".join(p for p in parts if p)
        stts = _child(d, stbl, "stts")
        if stts and timescale:
            count = struct.unpack_from(">I", d, stts[0] + 4)[0]
            deltas: Counter = Counter()
            for i in range(min(count, 10000)):
                samples, delta = struct.unpack_from(">II", d, stts[0] + 8 + 8 * i)
                deltas[delta] += samples
            if deltas:
                delta = deltas.most_common(1)[0][0]
                if delta:
                    info.fps = timescale / delta
    elif handler == b"soun" and not info.audio:
        channels = struct.unpack_from(">H", d, body + 16)[0]
        rate = struct.unpack_from(">I", d, body + 24)[0] >> 16
        info.audio = f"{AUDIO_CODECS.get(fmt, fmt.strip())}, {channels} ch, {rate / 1000:g} kHz"


SRT_RE = re.compile(
    r"\b(iso|shutter|fnum|ev|color_md|latitude|longitude|longtitude|rel_alt|altitude)\s*:\s*([^\s\]]+)",
    re.IGNORECASE,
)
SRT_GPS_RE = re.compile(r"GPS\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")  # older DJI: GPS(lon,lat,alt)


def probe_telemetry(srt: Path) -> List[Tuple[str, str]]:
    """Summary of DJI telemetry subtitles: camera settings, max height, start position."""
    try:
        if srt.stat().st_size > 50 * 1024 * 1024:
            return []
        text = srt.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    values: Dict[str, List[str]] = defaultdict(list)
    for key, value in SRT_RE.findall(text):
        values[key.lower()].append(value)

    def numbers(*keys: str) -> List[float]:
        for key in keys:
            found = []
            for v in values.get(key, []):
                with contextlib.suppress(ValueError):
                    found.append(float(v))
            if found:
                return found
        return []

    def span(nums: List[float]) -> str:
        lo, hi = min(nums), max(nums)
        return f"{lo:g}" if lo == hi else f"{lo:g} – {hi:g}"

    out = []
    if numbers("iso"):
        out.append(("ISO", span(numbers("iso"))))
    if values.get("shutter"):
        common = Counter(values["shutter"]).most_common(1)[0][0]
        out.append(("Shutter", re.sub(r"\.0+$", "", common)))
    if numbers("fnum"):
        f = numbers("fnum")[0]
        out.append(("Aperture", f"f/{f / 100 if f >= 10 else f:g}"))  # some models write 170 for f/1.7
    if numbers("ev"):
        out.append(("EV", span(numbers("ev"))))
    if values.get("color_md"):
        out.append(("Color mode", ", ".join(dict.fromkeys(values["color_md"]))))
    heights = numbers("rel_alt", "altitude")
    if heights:
        out.append(("Max height", f"{max(heights):.1f} m"))
    positions = [(a, b) for a, b in zip(numbers("latitude"), numbers("longitude", "longtitude")) if a or b]
    if not positions:
        m = SRT_GPS_RE.search(text)
        if m and (float(m.group(1)) or float(m.group(2))):
            positions = [(float(m.group(2)), float(m.group(1)))]
    if positions:
        out.append(("Start GPS", f"{positions[0][0]:.5f}, {positions[0][1]:.5f}"))
    return out


def resolution_label(width: int, height: int) -> str:
    long_side = max(width, height)
    for limit, name in ((5400, "5.4K"), (5000, "5K"), (3800, "4K"), (2700, "2.7K"),
                        (1900, "1080p"), (1280, "720p")):
        if long_side >= limit:
            return name + (" vertical" if height > width else "")
    return ""


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

@dataclass
class Video:
    path: Path
    size: int
    mtime: float
    day: date
    day_from_name: bool
    companions: List[Path] = field(default_factory=list)

    @property
    def files(self) -> List[Path]:
        return [self.path] + self.companions

    @property
    def total_size(self) -> int:
        total = self.size
        for c in self.companions:
            with contextlib.suppress(OSError):
                total += c.stat().st_size
        return total


def scan_dir(path: Path) -> Tuple[List[Path], List[Path]]:
    """Returns (subfolders, recordings) of a folder."""
    dirs, videos = [], []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    if entry.name.lower() not in SKIP_DIRS:
                        dirs.append(Path(entry.path))
                elif entry.is_file() and Path(entry.name).suffix.lower() in VIDEO_EXT:
                    videos.append(Path(entry.path))
            except OSError:
                continue
    key = lambda p: p.name.lower()  # noqa: E731
    return sorted(dirs, key=key), sorted(videos, key=key)


def load_videos(folder: Path) -> List[Video]:
    _, paths = scan_dir(folder)
    companions = defaultdict(list)
    with os.scandir(folder) as it:
        for entry in it:
            p = Path(entry.path)
            if p.suffix.lower() in COMPANION_EXT and entry.is_file():
                companions[p.stem.lower()].append(p)
    videos = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        day = date_from_name(p.name)
        videos.append(Video(
            path=p,
            size=st.st_size,
            mtime=st.st_mtime,
            day=day or date.fromtimestamp(st.st_mtime),
            day_from_name=day is not None,
            companions=sorted(companions.get(p.stem.lower(), []), key=lambda c: c.suffix.lower()),
        ))
    return videos


_details_cache: Dict[Path, List[Row]] = {}


def cached_details(v: Video) -> List[Row]:
    if v.path not in _details_cache:
        _details_cache[v.path] = details_panel(v)
    return _details_cache[v.path]


def details_panel(v: Video) -> List[Row]:
    """Right-hand panel with file info, video metadata and telemetry."""
    def kv(label: str, value: str) -> Row:
        return (f"  {label:<12}{value}", "")

    rows: List[Row] = [(v.path.name, BOLD), ("", ""), ("File", CYAN + BOLD)]
    rows.append(kv("Size", human_size(v.size)))
    rows.append(kv("Modified", datetime.fromtimestamp(v.mtime).strftime("%Y-%m-%d %H:%M")))
    rows.append(kv("Date", f"{v.day.isoformat()} ({'file name' if v.day_from_name else 'modified'})"))
    rows.append(kv("Extras", ", ".join(c.suffix[1:].upper() for c in v.companions) or "–"))

    rows += [("", ""), ("Video", CYAN + BOLD)]
    info = probe_video(v.path)
    if info is None or not (info.codec or info.duration):
        rows.append(("  Metadata not available for this file", DIM))
    else:
        if info.width:
            label = resolution_label(info.width, info.height)
            rows.append(kv("Resolution", f"{info.width}×{info.height}" + (f" ({label})" if label else "")))
        if info.fps:
            rows.append(kv("Frame rate", f"{info.fps:.3f}".rstrip("0").rstrip(".") + " fps"))
        if info.duration:
            rows.append(kv("Duration", human_duration(info.duration)))
        if info.codec:
            rows.append(kv("Codec", info.codec))
        if info.bit_depth:
            rows.append(kv("Bit depth", f"{info.bit_depth}-bit"))
        if info.color:
            rows.append(kv("Color", info.color))
        if info.duration:
            rows.append(kv("Bitrate", f"{v.size * 8 / info.duration / 1e6:.1f} Mb/s (avg)"))
        if info.created:
            rows.append(kv("Recorded", info.created.strftime("%Y-%m-%d %H:%M:%S") + " UTC"))
        rows.append(kv("Audio", info.audio or "none"))

    for srt in (c for c in v.companions if c.suffix.lower() == ".srt"):
        telemetry = probe_telemetry(srt)
        if telemetry:
            rows += [("", ""), ("Telemetry (SRT)", CYAN + BOLD)]
            rows += [kv(label, value) for label, value in telemetry]
        break
    return rows


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #



@dataclass(eq=False)
class Node:
    kind: str  # "medium", "dir" or "video"
    path: Optional[Path]
    depth: int = 0
    parent: Optional["Node"] = None
    medium: Optional[Medium] = None
    size: int = 0
    expanded: bool = False
    children: List["Node"] = field(default_factory=list)


class Browser:
    """Tree view: drives at the top level; drives and folders expand in place.

    By default it looks for a folder with recordings; with pick_folder=True it only shows
    folders and lets the user choose any of them (used for the destination).
    """

    def __init__(self, term: Terminal, start: Optional[Path], pick_folder: bool = False) -> None:
        self.term = term
        self.start = start
        self.pick_folder = pick_folder
        self.roots: List[Node] = []
        self.current: Optional[Node] = None
        self.counts: Dict[Path, int] = {}

    def count(self, folder: Path) -> int:
        if folder not in self.counts:
            try:
                self.counts[folder] = len(scan_dir(folder)[1])
            except OSError:
                self.counts[folder] = 0
        return self.counts[folder]

    def _load_roots(self, expand_start: bool) -> None:
        self.term.render(self._step(), [("Looking for drives…", DIM)], [], -1, [])
        self.roots = [Node("medium", m.path, medium=m) for m in list_media()]
        self.current = self.roots[0]
        if self.start:
            node = Node("medium", self.start, medium=Medium(str(self.start), self.start))
            self.roots.insert(0, node)
            self.current = node
            if expand_start:
                self._toggle(node)

    def _visible(self) -> List[Node]:
        out: List[Node] = []

        def walk(nodes: List[Node]) -> None:
            for node in nodes:
                out.append(node)
                if node.expanded:
                    walk(node.children)

        walk(self.roots)
        return out

    def _expand(self, node: Node) -> None:
        dirs, videos = scan_dir(node.path)
        if self.pick_folder:
            videos = []
        self.counts[node.path] = len(videos)
        children = [Node("dir", d, node.depth + 1, node) for d in dirs]
        for v in videos:
            try:
                size = v.stat().st_size
            except OSError:
                size = 0
            children.append(Node("video", v, node.depth + 1, node, size=size))
        node.children = children
        node.expanded = True

    def _add_path(self, node: Node) -> str:
        """Handles the "Enter a path…" entry: adds the typed folder as a new top-level entry."""
        raw = prompt_text(self.term, self._step(), "Type the path of a folder to browse:")
        if not raw:
            return ""
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            return f"No such folder: {path}"
        new = Node("medium", path, medium=Medium(str(path), path))
        self.roots.insert(self.roots.index(node), new)
        self.current = new
        return self._toggle(new)

    def _toggle(self, node: Node) -> str:
        """Expands or collapses a drive / folder. Returns a status message."""
        if node.expanded:
            node.expanded = False
            return ""
        m = node.medium
        if m and m.manual:
            return self._add_path(node)
        if m and m.wsl_drive:
            with self.term.paused():
                path = mount_wsl_drive(m.wsl_drive)
            if not path:
                return f"Could not mount {m.wsl_drive}:"
            m.label = m.label.replace("(not mounted – open to mount)", f"{path}  {_space(path)}")
            m.path = node.path = path
            m.wsl_drive = ""
        if not node.path or not node.path.is_dir():
            return "This drive is not available."
        try:
            self._expand(node)
        except OSError as e:
            return f"Cannot open {node.path}: {e.strerror or e}"
        return "" if node.children else "This folder is empty."

    def refresh(self) -> None:
        """Re-reads all expanded folders (e.g. after files were moved), keeping what was expanded."""
        expanded = {n.path for n in self._visible() if n.expanded and n.path}
        current = self.current.path if self.current else None
        self.counts.clear()

        def rebuild(node: Node) -> None:
            try:
                self._expand(node)
            except OSError:
                node.expanded, node.children = False, []
                return
            for child in node.children:
                if child.path in expanded:
                    rebuild(child)

        for root in self.roots:
            if root.expanded:
                rebuild(root)
        # Put the cursor back on the same entry, or on the closest folder above it that still exists.
        by_path = {n.path: n for n in self._visible() if n.path}
        candidates = [current, *current.parents] if current else []
        self.current = next((by_path[p] for p in candidates if p in by_path), self.roots[0] if self.roots else None)

    def _step(self) -> str:
        if self.pick_folder:
            return "Step 3/5: choose the destination folder"
        return "Step 1/5: choose the folder with recordings"

    def _render(self, nodes: List[Node], cursor: int, status: str) -> None:
        name_w = min(max((2 * n.depth + len(n.path.name) for n in nodes if n.kind == "video"), default=0), 70)
        rows: List[Row] = []
        for n in nodes:
            indent = "  " * n.depth
            arrow = "▼" if n.expanded else "►"
            if n.kind == "medium" and n.medium:
                style = GREEN if n.medium.removable else (DIM if n.medium.manual else "")
                rows.append((f"{indent}{arrow} {n.medium.label}", style))
            elif n.kind == "dir":
                c = 0 if self.pick_folder else self.count(n.path)
                rows.append((f"{indent}{arrow} {n.path.name}/" + (f"   {plural(c, 'recording')}" if c else ""),
                             CYAN + BOLD if c else ""))
            else:
                label = f"{indent}  {n.path.name}"
                rows.append((f"{label:<{name_w + 2}}  {human_size(n.size):>9}", ""))

        node = nodes[cursor]
        info: List[Row] = [(f"Location: {node.path or ''}", "")]
        if self.pick_folder:
            if node.path:
                info.append((f"Enter moves the recordings into: {node.path}", GREEN))
            keys = [("↑↓", "move"), ("→", "expand"), ("←", "collapse"), ("Enter", "choose"),
                    ("Esc", "cancel"), ("r", "refresh"), ("q", "quit")]
        else:
            if node.kind == "video" and node.parent:
                info.append((f"Enter chooses the folder: {node.parent.path}", GREEN))
            elif node.path and self.counts.get(node.path):
                info.append((f"{plural(self.counts[node.path], 'recording')} here – Enter chooses this folder",
                             GREEN))
            keys = [("↑↓", "move"), ("→", "expand"), ("←", "collapse"), ("Enter", "choose"),
                    ("p", "play"), ("r", "refresh"), ("q", "quit")]
        self.term.render(self._step(), info, rows, cursor, keys, status)

    def run(self) -> Optional[Tuple[Path, Optional[Path]]]:
        """Browses until a folder is chosen. Returns (folder, highlighted recording).

        Only in the pick_folder mode it can also return None (cancelled with Esc).
        """
        if not self.roots:
            self._load_roots(expand_start=True)
        status = ""
        while True:
            nodes = self._visible()
            cursor = next((i for i, n in enumerate(nodes) if n is self.current), 0)
            node = self.current = nodes[cursor]
            self._render(nodes, cursor, status)
            key = self.term.read_key()
            status = ""

            if key == "q":
                raise Quit
            if self.pick_folder and key == "ESC":
                return None
            if self.pick_folder and key == "ENTER":
                if node.medium and node.medium.manual:
                    typed = ask_path(self.term)
                    if typed:
                        return typed, None
                    continue
                if node.medium and node.medium.wsl_drive:
                    status = self._toggle(node)  # mounts the drive
                if node.path and node.path.is_dir():
                    return node.path, None
                status = status or "This drive is not available."
                continue
            if key in ("LEFT", "BACKSPACE", "ESC"):
                if node.expanded:
                    node.expanded = False
                elif node.parent:
                    self.current = node.parent
            elif key == "RIGHT":
                if node.kind != "video":
                    if not node.expanded:
                        status = self._toggle(node)
                    elif node.children:
                        self.current = node.children[0]
            elif key == "ENTER":
                if node.kind == "video" and node.parent and node.parent.path:
                    return node.parent.path, node.path
                if node.kind != "video" and node.path and node.expanded and self.count(node.path):
                    return node.path, None
                if node.kind == "dir" and self.count(node.path):
                    return node.path, None
                status = self._toggle(node)
            elif key == "p" and node.kind == "video":
                status = play_status(node.path)
            elif key == "r":
                self._load_roots(expand_start=False)
            else:
                self.current = nodes[move_cursor(key, cursor, len(nodes))]


def prompt_text(term: Terminal, step: str, question: str, hint: str = "") -> Optional[str]:
    """A one-line text field in the regular screen layout. Returns None when cancelled."""
    text = ""
    while True:
        info = [(question, BOLD), ("", ""), (f"> {text}{REV} {RESET}", YELLOW + BOLD), (hint, DIM)]
        keys = [("Enter", "confirm"), ("Bksp", "delete"), ("Esc", "cancel")]
        term.render(step, info, [], 0, keys, highlight=False)
        key = term.read_key(raw=True)
        if key == "ENTER":
            return text.strip().strip("\"'") or None
        if key == "ESC":
            return None
        if key == "BACKSPACE":
            text = text[:-1]
        elif key == "SPACE":
            text += " "
        elif len(key) == 1 and key.isprintable():
            text += key


def ask_path(term: Terminal) -> Optional[Path]:
    """Lets the user type a destination path; it does not have to exist yet."""
    raw = prompt_text(term, "Step 3/5: choose where to move the recordings",
                      "Type the destination folder path:",
                      "The folder will be created if it does not exist yet.")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def choose_destination(term: Terminal, picker: Browser) -> Optional[Path]:
    """Destination step: recently used folders first. Returns None to go back to the selection."""
    recent = recent_destinations()
    paths = recent or [DEFAULT_DESTINATION]
    entries: List[Tuple[str, Optional[Path]]] = [("path", p) for p in paths]
    entries += [("browse", None), ("type", None)]
    cursor, status = 0, ""
    while True:
        rows: List[Row] = []
        for i, (kind, p) in enumerate(entries):
            if kind == "path" and p is not None:
                note = "   (last used)" if recent and i == 0 else ("   (default)" if not recent else "")
                if not p.exists():
                    note += "   – does not exist yet, will be created"
                rows.append((f"  {p}{note}", GREEN + BOLD if i == 0 else ""))
            elif kind == "browse":
                rows.append(("  Browse for a folder…", CYAN))
            else:
                rows.append(("  Type a path…", CYAN))
        info = [
            ("Recordings go into subfolders [<flight location>][<date>] inside the chosen folder.", ""),
        ]
        term.render("Step 3/5: choose where to move the recordings", info, rows, cursor,
                    [("↑↓", "move"), ("Enter", "choose"), ("←", "back"), ("q", "quit")], status)
        key = term.read_key()
        status = ""
        if key == "q":
            raise Quit
        if key in ("LEFT", "ESC", "BACKSPACE"):
            return None
        if key in ("ENTER", "RIGHT"):
            kind, chosen = entries[cursor]
            if kind == "browse":
                picked = picker.run()
                chosen = picked[0] if picked else None
            elif kind == "type":
                chosen = ask_path(term)
            if chosen is None:
                continue
            if chosen.exists() and not chosen.is_dir():
                status = f"Not a folder: {chosen}"
                continue
            remember_destination(chosen)
            return chosen
        cursor = move_cursor(key, cursor, len(entries))


def select_videos(term: Terminal, folder: Path, focus: Optional[Path] = None,
                  preselected: Optional[List[Video]] = None) -> Optional[List[Video]]:
    """Selecting recordings. Returns the selection, or None to go back to the browser."""
    videos = load_videos(folder)
    if not videos:
        return None
    keep = {v.path for v in preselected or []}
    selected = {i for i, v in enumerate(videos) if v.path in keep}
    cursor = next((i for i, v in enumerate(videos) if v.path == focus), 0)
    status = ""
    any_mtime = any(not v.day_from_name for v in videos)
    while True:
        left_w, _ = split_widths(screen_width())
        name_w = max(12, min(max(len(v.path.name) for v in videos), left_w - 28))
        rows: List[Row] = []
        for i, v in enumerate(videos):
            mark = "[x]" if i in selected else "[ ]"
            flag = " " if v.day_from_name else "*"
            text = f"{mark} {fit(v.path.name, name_w):<{name_w}}  {human_size(v.size):>9}  {v.day.isoformat()}{flag}"
            rows.append((text, GREEN if i in selected else ""))
        chosen = [videos[i] for i in sorted(selected)]
        info = [
            (f"Folder: {folder}", ""),
            (f"Selected: {len(chosen)}/{len(videos)}   ({human_size(sum(v.total_size for v in chosen))})", BOLD),
        ]
        if any_mtime:
            info.append(("* no date in the file name – the file modification date is used", DIM))
        keys = [("↑↓", "move"), ("Space", "select"), ("Ctrl+Spc/a", "all"), ("p", "play"),
                ("Enter", "next"), ("←", "back"), ("q", "quit")]
        current = videos[cursor]
        term.render("Step 2/5: select the recordings to move", info, rows, cursor, keys, status,
                    panel=cached_details(current))

        key = term.read_key()
        status = ""
        if key == "q":
            raise Quit
        if key in ("LEFT", "BACKSPACE", "ESC"):
            return None
        if key == "SPACE":
            selected ^= {cursor}
            cursor = min(cursor + 1, len(videos) - 1)
        elif key in ("CTRL_SPACE", "a"):
            selected = set() if len(selected) == len(videos) else set(range(len(videos)))
        elif key == "p":
            status = play_status(current.path)
        elif key == "ENTER":
            if chosen:
                return chosen
            status = "Nothing selected – Space selects a recording, Ctrl+Space selects all."
        else:
            cursor = move_cursor(key, cursor, len(videos))


def interactive(term: Terminal, start: Optional[Path]) -> None:
    browser = Browser(term, start)
    picker = Browser(term, None, pick_folder=True)
    while True:
        chosen = browser.run()
        assert chosen is not None  # only the folder picker can be cancelled
        folder, focus = chosen
        videos: Optional[List[Video]] = None
        dest: Optional[Path] = None
        places: Optional[Dict[date, str]] = None
        step = "select"
        while True:
            if step == "select":
                videos = select_videos(term, folder, focus, preselected=videos)
                if not videos:
                    break  # back to the browser
                focus = videos[0].path
                step = "destination"
            elif step == "destination":
                new_dest = choose_destination(term, picker)
                if new_dest != dest:
                    places = None  # "previously used" names depend on the destination
                dest = new_dest
                step = "name" if dest else "select"
            else:
                places = name_locations(term, videos, dest, initial=places)
                if places is None:
                    step = "destination"
                    continue
                plan = make_plan(videos, places, dest)
                if not confirm_plan(term, plan, dest):
                    continue  # back to naming, the typed names are kept
                report = move_files(term, plan, dest, videos[0].path.parent)
                show_report(term, report)
                browser.refresh()
                break


# --------------------------------------------------------------------------- #
# Locations and moving
# --------------------------------------------------------------------------- #

FOLDER_RE = re.compile(r"^\[(.+)\]\[(\d{4}-\d{2}-\d{2})\]$")


def known_places(dest: Path, limit: int = 9) -> List[str]:
    """Most recently used flight locations, read from the folder names in the destination."""
    found = []
    try:
        folders = list(dest.iterdir()) if dest.is_dir() else []
    except OSError:
        folders = []
    for p in folders:
        m = FOLDER_RE.match(p.name)
        if m and p.is_dir():
            found.append((m.group(2), m.group(1)))
    places: List[str] = []
    for _, place in sorted(found, reverse=True):
        if place not in places:
            places.append(place)
    return places[:limit]


def name_locations(term: Terminal, videos: List[Video], dest: Path,
                   initial: Optional[Dict[date, str]] = None) -> Optional[Dict[date, str]]:
    """Asks for the flight location of every day. Returns None to go back to the destination step.

    With `initial` (coming back from the plan) the names are kept and it starts on the last day.
    """
    by_day: Dict[date, List[Video]] = defaultdict(list)
    for v in videos:
        by_day[v.day].append(v)
    days = sorted(by_day)
    known = known_places(dest)
    places: Dict[date, str] = dict(initial or {})
    i = len(days) - 1 if initial else 0
    while 0 <= i < len(days):
        place = _name_day(term, days, i, by_day[days[i]], known, places)
        if place is None:
            i -= 1
        else:
            places[days[i]] = place
            i += 1
    return places if i == len(days) else None


def _name_day(term: Terminal, days: List[date], index: int, vids: List[Video],
              known: List[str], places: Dict[date, str]) -> Optional[str]:
    """One day of the naming step. Returns the location, or None to go back."""
    day = days[index]
    default = places.get(days[index - 1]) if index else None
    text = places.get(day, "")
    typing = bool(text)
    cursor, status = 0, ""
    name_w = min(max(len(v.path.name) for v in vids), 44)

    def resolve(raw: str) -> Optional[str]:
        raw = raw.strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(known):
            return known[int(raw) - 1]
        return sanitize_place(raw) or None

    while True:
        field_style = (YELLOW if typing else "") + BOLD
        # The text cursor is a highlighted space: it needs no special character in the console font.
        field = f"Flight location: {text}" + (f"{REV} {RESET}{field_style}" if typing else "")
        if not text and default:
            field += f"   (Enter = {default})"
        info = [
            (f"Day {index + 1} of {len(days)}:  {day.isoformat()}  –  {plural(len(vids), 'recording')}, "
             f"{human_size(sum(v.total_size for v in vids))}", BOLD),
            ("Type to enter the name.   p plays the highlighted recording.", DIM),
            (field, field_style),
            ("Previously used (type the number): " + "   ".join(f"{n}. {p}" for n, p in enumerate(known, 1)), DIM)
            if known else ("", ""),
        ]
        if typing:
            keys = [("Enter", "confirm"), ("Bksp", "delete"), ("Esc", "to list")]
        else:
            keys = [("↑↓", "move"), ("p", "play"), ("Tab", "type name"), ("Enter", "confirm"), ("←", "back")]
        rows = [(f"  {fit(v.path.name, name_w):<{name_w}}  {human_size(v.size):>9}", "") for v in vids]
        term.render("Step 4/5: name the flight location for each day", info, rows, cursor, keys, status,
                    panel=cached_details(vids[cursor]))

        key = term.read_key(raw=True)
        status = ""
        printable = len(key) == 1 and key.isprintable()
        if typing:
            if key == "ENTER":
                place = resolve(text)
                if place:
                    return place
                status = "Please type a location name."
            elif key == "BACKSPACE":
                text = text[:-1]
            elif key == "SPACE":
                text += " "
            elif key in ("TAB", "ESC"):
                typing = False
            elif key in ("UP", "DOWN", "PGUP", "PGDN", "HOME", "END"):
                typing = False
                cursor = move_cursor(key, cursor, len(vids))
            elif printable:
                text += key
        else:
            if key.lower() == "p" and printable:
                status = play_status(vids[cursor].path)
            elif key == "ENTER" and resolve(text):
                return resolve(text)
            elif key in ("ENTER", "TAB", "RIGHT"):
                typing = True
            elif key in ("LEFT", "ESC", "BACKSPACE"):
                return None
            elif printable:
                typing = True
                text += key
            else:
                cursor = move_cursor(key, cursor, len(vids))


COPY_CHUNK = 8 * 1024 * 1024
# Force written data to the disk every this many bytes. Doing it for every chunk is slow;
# never doing it lets the system cache run far ahead and makes the progress bar lie.
SYNC_EVERY = 64 * 1024 * 1024


def progress_bar(fraction: float, width: int = 24) -> str:
    filled = int(min(max(fraction, 0.0), 1.0) * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"


class Progress:
    """The "Moving" screen: progress of the current file and of the whole transfer, plus a log."""

    def __init__(self, term: Terminal, total: int, dest: Path) -> None:
        self.term = term
        self.dest = dest
        self.total = max(total, 1)
        self.done = 0           # bytes processed so far, including the current file
        self.copied = 0         # bytes actually copied (instant renames don't count towards speed)
        self.copy_time = 0.0
        self.started = time.monotonic()
        self.name = ""
        self.file_done = 0
        self.file_total = 0
        self.last_draw = 0.0
        self.log: List[Row] = []

    def start_file(self, name: str, size: int) -> None:
        self.name, self.file_done, self.file_total = name, 0, size
        self.draw(force=True)

    def advance(self, n: int, seconds: float) -> None:
        self.file_done += n
        self.done += n
        self.copied += n
        self.copy_time += seconds
        self.draw()

    def finish_file(self) -> None:
        # Renamed, skipped or failed files still count as processed for the total bar.
        self.done += max(self.file_total - self.file_done, 0)
        self.file_done = self.file_total

    def add(self, text: str, style: str = "") -> None:
        """Adds a line to the log."""
        self.log.append((text, style))
        self.draw(force=True)

    def draw(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_draw < 0.1:
            return
        self.last_draw = now
        speed = self.copied / self.copy_time if self.copy_time > 0 else 0.0
        eta = human_duration((self.total - self.done) / speed) if speed else "–"
        file_part = self.file_done / self.file_total if self.file_total else 1.0
        total_part = min(self.done / self.total, 1.0)
        bar_w = max(10, min(40, screen_width() - 70))
        info = [
            (f"File:   {progress_bar(file_part, bar_w)} {file_part:4.0%}   "
             f"{human_size(self.file_done)} / {human_size(self.file_total)}   {self.name}", ""),
            (f"Total:  {progress_bar(total_part, bar_w)} {total_part:4.0%}   "
             f"{human_size(self.done)} / {human_size(self.total)}", BOLD),
            (f"Speed:  {human_size(speed)}/s   ETA {eta}   elapsed {human_duration(now - self.started)}", ""),
            (f"Into:   {self.dest}", DIM),
        ]
        self.term.render("Moving the files…", info, self.log, max(len(self.log) - 1, 0), [("Ctrl+C", "stop")],
                         "Please wait and do not remove the card.", highlight=False)


def _read_ahead(f, chunks: "queue.Queue", stop: threading.Event) -> None:
    """Reads the source in a separate thread, so reading the card and writing the disk overlap."""
    def put(item) -> bool:
        while not stop.is_set():
            try:
                chunks.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    try:
        while True:
            buf = f.read(COPY_CHUNK)
            if not put(buf) or not buf:
                return
    except OSError as e:
        put(e)


def copy_with_progress(src: Path, dst: Path, progress: Progress) -> None:
    total = src.stat().st_size
    tmp = dst.with_name(dst.name + ".part")
    stop = threading.Event()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            chunks: queue.Queue = queue.Queue(maxsize=8)
            reader = threading.Thread(target=_read_ahead, args=(fi, chunks, stop), daemon=True)
            reader.start()
            try:
                unsynced = 0
                last = time.monotonic()
                while True:
                    buf = chunks.get()
                    if isinstance(buf, OSError):
                        raise buf
                    if not buf:
                        break
                    fo.write(buf)
                    unsynced += len(buf)
                    if unsynced >= SYNC_EVERY:
                        fo.flush()
                        os.fsync(fo.fileno())
                        unsynced = 0
                    now = time.monotonic()
                    progress.advance(len(buf), now - last)
                    last = now
                fo.flush()
                os.fsync(fo.fileno())
            finally:
                stop.set()
                reader.join()
        if tmp.stat().st_size != total:
            raise OSError("the copy has a different size than the original")
        with contextlib.suppress(OSError):
            shutil.copystat(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.remove(src)


def move_file(src: Path, dst_dir: Path, progress: Progress) -> str:
    """Moves a file. Returns 'ok', 'exists' or an error message."""
    dst = dst_dir / src.name
    if dst.exists():
        return "exists"
    try:
        os.rename(src, dst)  # same drive: instant
        return "ok"
    except OSError:
        pass
    try:
        copy_with_progress(src, dst, progress)
        return "ok"
    except OSError as e:
        return str(e)


@dataclass
class FileResult:
    src: Path
    size: int
    status: str  # "ok", "exists" or an error message


@dataclass
class Report:
    source: Path
    destination: Path
    folders: List[Tuple[Path, List[FileResult]]]
    elapsed: float

    def results(self) -> List[FileResult]:
        return [r for _, results in self.folders for r in results]


@dataclass
class PlannedFolder:
    target: Path
    videos: List[Video]
    sizes: Dict[Path, int]

    @property
    def files(self) -> List[Path]:
        return [f for v in self.videos for f in v.files]

    @property
    def size(self) -> int:
        return sum(self.sizes[f] for f in self.files)


def make_plan(videos: List[Video], places: Dict[date, str], dest: Path) -> List[PlannedFolder]:
    by_day: Dict[date, List[Video]] = defaultdict(list)
    for v in videos:
        by_day[v.day].append(v)
    plan = []
    for day in sorted(by_day):
        sizes: Dict[Path, int] = {}
        for v in by_day[day]:
            for f in v.files:
                try:
                    sizes[f] = f.stat().st_size
                except OSError:
                    sizes[f] = 0
        plan.append(PlannedFolder(dest / f"[{places[day]}][{day.isoformat()}]", by_day[day], sizes))
    return plan


def confirm_plan(term: Terminal, plan: List[PlannedFolder], dest: Path) -> bool:
    """Shows exactly which file goes where. Returns True to move, False to go back."""
    total_size = sum(f.size for f in plan)
    total_files = sum(len(f.files) for f in plan)
    recordings = sum(len(f.videos) for f in plan)
    name_w = min(max(len(p.name) for f in plan for p in f.files), 48)
    rows: List[Row] = []
    skipped = 0
    for folder in plan:
        note = "exists – files will be added" if folder.target.exists() else "new folder"
        rows.append((f"{folder.target.name}   ({note})   {plural(len(folder.videos), 'recording')}, "
                     f"{plural(len(folder.files), 'file')}, {human_size(folder.size)}", CYAN + BOLD))
        for f in folder.files:
            line = f"    {fit(f.name, name_w):<{name_w}}  {human_size(folder.sizes[f]):>9}"
            if (folder.target / f.name).exists():
                skipped += 1
                rows.append((line + "   already there – will be skipped", YELLOW))
            else:
                rows.append((line, ""))
        rows.append(("", ""))

    existing = dest
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    try:
        free = shutil.disk_usage(str(existing)).free
    except OSError:
        free = None
    if free is not None and total_size > free:
        space: Row = (f"Warning: about {human_size(total_size)} is needed but only {human_size(free)} is free "
                      f"(does not apply when moving within the same drive).", RED + BOLD)
    else:
        space = (f"Free space at the destination: {human_size(free)}" if free is not None else "", DIM)
    info = [
        (f"Into: {dest}", ""),
        (f"{plural(recordings, 'recording')} ({plural(total_files, 'file')}, {human_size(total_size)}) "
         f"into {plural(len(plan), 'folder')}", BOLD),
        (f"{plural(skipped, 'file')} already in the target folder – will be skipped and left on the source", YELLOW)
        if skipped else ("", ""),
        space,
    ]
    keys = [("↑↓", "scroll"), ("Enter", "move"), ("←", "back"), ("q", "quit")]
    cursor, status = 0, ""
    while True:
        term.render("Step 5/5: check where the files will go", info, rows, cursor, keys, status)
        key = term.read_key()
        status = ""
        if key == "q":
            raise Quit
        if key in ("LEFT", "ESC", "BACKSPACE"):
            return False
        if key in ("ENTER", "y"):
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                status = f"Cannot create the destination folder: {e}"
                continue
            return True
        cursor = move_cursor(key, cursor, len(rows))


def move_files(term: Terminal, plan: List[PlannedFolder], dest: Path, source: Path) -> Report:
    progress = Progress(term, sum(f.size for f in plan), dest)
    report = Report(source=source, destination=dest, folders=[], elapsed=0.0)
    try:
        for folder in plan:
            results: List[FileResult] = []
            report.folders.append((folder.target, results))
            progress.add(f"→ {folder.target.name}", BOLD)
            try:
                folder.target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                for src in folder.files:
                    results.append(FileResult(src, folder.sizes[src], f"cannot create the folder: {e}"))
                progress.add(f"    × cannot create the folder: {e}", RED)
                continue
            for src in folder.files:
                size = folder.sizes[src]
                progress.start_file(src.name, size)
                status = move_file(src, folder.target, progress)
                progress.finish_file()
                results.append(FileResult(src, size, status))
                if status == "ok":
                    progress.add(f"    √ {src.name}   {human_size(size)}", GREEN)
                elif status == "exists":
                    progress.add(f"    = {src.name} – already in the target folder, left on the source", YELLOW)
                else:
                    progress.add(f"    × {src.name} – {status}", RED)
    finally:
        report.elapsed = time.monotonic() - progress.started
    return report


def show_report(term: Terminal, report: Report) -> None:
    """Summary screen after moving. Returns when the user wants to go back to the browser."""
    results = report.results()
    moved = [r for r in results if r.status == "ok"]
    skipped = [r for r in results if r.status == "exists"]
    failed = [r for r in results if r.status not in ("ok", "exists")]
    moved_bytes = sum(r.size for r in moved)
    speed = moved_bytes / report.elapsed if report.elapsed > 0 else 0.0

    if failed:
        outcome: Row = (f"× {plural(len(failed), 'file')} failed – they are still on the source.", RED + BOLD)
    elif skipped:
        outcome = (f"= {plural(len(skipped), 'file')} skipped – already in the target folder, left on the source.",
                   YELLOW + BOLD)
    else:
        outcome = ("√ Everything was moved successfully.", GREEN + BOLD)
    info = [
        (f"Moved {plural(len(moved), 'file')} ({human_size(moved_bytes)}) in {human_duration(report.elapsed)}"
         f"  –  average {human_size(speed)}/s", BOLD),
        outcome,
        (f"From: {report.source}", ""),
        (f"To:   {report.destination}", ""),
    ]

    name_w = min(max((len(r.src.name) for r in results), default=10), 48)
    rows: List[Row] = []
    for target, folder_results in report.folders:
        ok = [r for r in folder_results if r.status == "ok"]
        recordings = sum(1 for r in ok if r.src.suffix.lower() in VIDEO_EXT)
        clean = len(ok) == len(folder_results)
        rows.append((f"{'√' if clean else '!'} {target.name}   "
                     f"{plural(recordings, 'recording')}, {plural(len(ok), 'file')}, "
                     f"{human_size(sum(r.size for r in ok))}", (GREEN if clean else YELLOW) + BOLD))
        for r in folder_results:
            line = f"    {fit(r.src.name, name_w):<{name_w}}  {human_size(r.size):>9}"
            if r.status == "ok":
                rows.append((line, ""))
            elif r.status == "exists":
                rows.append((line + "  skipped: already in the target folder", YELLOW))
            else:
                rows.append((line + f"  error: {r.status}", RED))
        rows.append(("", ""))

    cursor = 0
    while True:
        term.render("Report", info, rows, cursor, [("↑↓", "scroll"), ("Enter", "browser"), ("q", "quit")])
        key = term.read_key()
        if key == "q":
            raise Quit
        if key in ("ENTER", "ESC", "LEFT", "BACKSPACE"):
            return
        cursor = move_cursor(key, cursor, len(rows))


# --------------------------------------------------------------------------- #

def main() -> int:
    setup_console()
    ensure_config()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("This script is interactive – run it in a terminal.")
        return 1
    start = None
    if len(sys.argv) > 1:
        start = Path(sys.argv[1]).expanduser().resolve()
        if not start.is_dir():
            print(f"No such folder: {start}")
            return 1
    try:
        with Terminal() as term:
            interactive(term, start)
    except Quit:
        print("Bye.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
