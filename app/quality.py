"""Video quality gate: resolution + size checks. Import-safe (stdlib + Pillow).

NEVER raises: every public function catches all exceptions internally and
returns a safe fallback (None / reject-or-allow tuple).
"""
import logging
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("instaward-bot")

_MAX_BOXES = 200
_MAX_SCAN = 32 * 1024 * 1024


def _ffprobe_resolution(path: str) -> Optional[tuple[int, int]]:
    """Return (w, h) via ffprobe, or None when unavailable/unparsable."""
    try:
        exe = shutil.which("ffprobe")
        if not exe:
            return None
        out = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        txt = (out.stdout or "").strip().splitlines()
        if not txt:
            return None
        first = txt[0].strip()
        parts = [p.strip() for p in first.split(",")]
        if len(parts) < 2:
            return None
        w, h = int(parts[0]), int(parts[1])
        if 100 <= w <= 8192 and 100 <= h <= 8192:
            return (w, h)
        return None
    except Exception:  # noqa: BLE001 - never raise from probing
        return None


def _read_box_header(buf: bytes, off: int) -> Optional[tuple[str, int, int]]:
    """Parse [u32be size][4-char type] at off. Returns (type, box_size, header_len)."""
    try:
        if off + 8 > len(buf):
            return None
        (size,) = struct.unpack_from(">I", buf, off)
        try:
            btype = buf[off + 4 : off + 8].decode("ascii")
        except Exception:  # noqa: BLE001
            return None
        if size == 1:
            if off + 16 > len(buf):
                return None
            (largesize,) = struct.unpack_from(">Q", buf, off + 8)
            if largesize < 16:
                return None
            return (btype, int(largesize), 16)
        if size == 0:
            # Box extends to end of buffered region.
            return (btype, len(buf) - off, 8)
        if size < 8:
            return None
        return (btype, int(size), 8)
    except Exception:  # noqa: BLE001
        return None


def _iter_child_boxes(buf: bytes, start: int, end: int) -> list[tuple[str, int, int]]:
    """List (type, payload_off, payload_end) children in [start, end). Capped."""
    kids: list[tuple[str, int, int]] = []
    try:
        off = start
        count = 0
        while off + 8 <= end and count < _MAX_BOXES:
            hdr = _read_box_header(buf, off)
            if hdr is None:
                break
            btype, bsize, hlen = hdr
            if bsize <= 0 or off + bsize > end:
                break
            kids.append((btype, off + hlen, off + bsize))
            if bsize == 0:
                break
            off += bsize
            count += 1
        return kids
    except Exception:  # noqa: BLE001
        return kids


def _tkhd_resolution(payload: bytes) -> Optional[tuple[int, int]]:
    """Parse tkhd payload -> (w, h). Returns None on any miss/insane values."""
    try:
        if len(payload) < 1:
            return None
        version = payload[0]
        if version == 0:
            need = 84
            if len(payload) < need:
                return None
            (w_raw,) = struct.unpack_from(">I", payload, 76)
            (h_raw,) = struct.unpack_from(">I", payload, 80)
        elif version == 1:
            need = 96
            if len(payload) < need:
                return None
            (w_raw,) = struct.unpack_from(">I", payload, 88)
            (h_raw,) = struct.unpack_from(">I", payload, 92)
        else:
            return None
        w, h = int(w_raw >> 16), int(h_raw >> 16)
        if 100 <= w <= 8192 and 100 <= h <= 8192:
            return (w, h)
        return None
    except Exception:  # noqa: BLE001
        return None


def _mp4_resolution(path: str) -> Optional[tuple[int, int]]:
    """Minimal MP4 box parser: moov > trak > tkhd. Returns (w, h) or None."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        # Read at most _MAX_SCAN bytes (header boxes live near the front,
        # but moov may be at the tail; cap keeps this bounded).
        size = p.stat().st_size
        n = min(size, _MAX_SCAN)
        if n < 8:
            return None
        with open(p, "rb") as fh:
            buf = fh.read(n)
        # If the file is larger than our window and moov is at the tail
        # (faststart not applied), also peek at the tail window.
        tail_buf: bytes = b""
        tail_base = 0
        if size > _MAX_SCAN:
            tail_n = min(size, _MAX_SCAN)
            tail_base = size - tail_n
            try:
                with open(p, "rb") as fh:
                    fh.seek(tail_base)
                    tail_buf = fh.read(tail_n)
            except Exception:  # noqa: BLE001
                tail_buf = b""
        for window, base in ((buf, 0), (tail_buf, tail_base)):
            if not window or len(window) < 8:
                continue
            try:
                off = 0
                boxes = 0
                scanned = 0
                while off + 8 <= len(window) and boxes < _MAX_BOXES and scanned < _MAX_SCAN:
                    hdr = _read_box_header(window, off)
                    if hdr is None:
                        break
                    btype, bsize, hlen = hdr
                    if bsize <= 0 or off + bsize > len(window):
                        # size==0 (to EOF) is only meaningful at real EOF;
                        # in a truncated head-window it means "past window".
                        break
                    if btype == "moov":
                        moov_start = off + hlen
                        moov_end = off + bsize
                        for ctype, cstart, cend in _iter_child_boxes(window, moov_start, moov_end):
                            if ctype != "trak":
                                continue
                            for ttype, tstart, tend in _iter_child_boxes(window, cstart, cend):
                                if ttype != "tkhd":
                                    continue
                                res = _tkhd_resolution(window[tstart:tend])
                                if res is not None:
                                    return res
                            # No sane tkhd in this trak: keep scanning others.
                    off += bsize
                    scanned += bsize
                    boxes += 1
                    if bsize == 0:
                        break
            except Exception:  # noqa: BLE001
                continue
        return None
    except Exception:  # noqa: BLE001
        return None


def video_resolution(path: str | Path) -> Optional[tuple[int, int]]:
    """Return actual video (w, h). ffprobe first, MP4 box parser fallback.

    Never raises; returns None when resolution cannot be determined.
    """
    try:
        s = str(path or "")
        if not s:
            return None
        try:
            if not Path(s).is_file():
                return None
        except Exception:  # noqa: BLE001
            return None
        res = _ffprobe_resolution(s)
        if res is not None:
            return res
        return _mp4_resolution(s)
    except Exception:  # noqa: BLE001
        return None


def check_video(
    path: str | Path,
    min_bytes: int,
    min_w: int,
    min_h: int,
    strict: int | bool,
) -> tuple[bool, str, Optional[tuple[int, int]]]:
    """Gate a video file. Never raises.

    Fallback note: when resolution is unknown AND strict=0 this returns
    allow; the caller (curation) should additionally consult
    check_thumbnail as a proxy signal only for logging (never reject on it).
    """
    try:
        s = str(path or "")
        if not s:
            return (False, "missing file", None)
        try:
            pp = Path(s)
            if not pp.is_file():
                return (False, "missing file", None)
            size = pp.stat().st_size
        except Exception:  # noqa: BLE001
            return (False, "missing file", None)
        try:
            min_b = int(min_bytes)
        except (TypeError, ValueError):
            min_b = 0
        if size < min_b:
            return (False, f"too small: {size} < {min_b}", None)
        res = video_resolution(s)
        if res is None:
            if int(bool(strict)):
                return (False, "resolution unknown, rejected", None)
            return (True, "resolution unknown, allowed", None)
        w, h = res
        try:
            mw, mh = int(min_w), int(min_h)
        except (TypeError, ValueError):
            mw, mh = 0, 0
        if w < mw or h < mh:
            return (False, f"low-res {w}x{h} < {mw}x{mh}", res)
        return (True, "", res)
    except Exception:  # noqa: BLE001
        try:
            if int(bool(strict)):
                return (False, "resolution unknown, rejected", None)
            return (True, "resolution unknown, allowed", None)
        except Exception:  # noqa: BLE001
            return (True, "resolution unknown, allowed", None)


def check_thumbnail(
    path: str | Path | None,
    min_w: int,
    min_h: int,
) -> tuple[bool, str]:
    """Check thumbnail dims. Never blocks: None/unreadable -> (True, 'skipped')."""
    try:
        if path is None:
            return (True, "skipped")
        s = str(path or "")
        if not s:
            return (True, "skipped")
        try:
            pp = Path(s)
            if not pp.is_file():
                return (True, "skipped")
        except Exception:  # noqa: BLE001
            return (True, "skipped")
        try:
            from PIL import Image  # noqa: BLE001 - Pillow is a declared dep
        except Exception:  # noqa: BLE001
            return (True, "skipped")
        try:
            with Image.open(s) as im:
                w, h = int(im.width), int(im.height)
        except Exception:  # noqa: BLE001
            return (True, "skipped")
        try:
            mw, mh = int(min_w), int(min_h)
        except (TypeError, ValueError):
            return (True, "")
        if w < mw or h < mh:
            return (False, f"low-res thumb {w}x{h} < {mw}x{mh}")
        return (True, "")
    except Exception:  # noqa: BLE001
        return (True, "skipped")
