"""Frame grabber for the HDMI capture card (UVC)."""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import threading
import time

import cv2
import numpy as np


def list_avfoundation_devices() -> list[str]:
    """Video device names in AVFoundation order (macOS only, via ffmpeg)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        ).stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in out.splitlines():
        if "audio devices" in line:
            break
        m = re.search(r"\]\s\[(\d+)\]\s(.+)$", line)
        if m:
            names.append(m.group(2).strip())
    return names


def list_v4l2_devices() -> list[tuple[int, str]]:
    """(index, name) of /dev/videoN devices on Linux, from sysfs."""
    out = []
    for d in sorted(glob.glob("/sys/class/video4linux/video*")):
        m = re.search(r"video(\d+)$", d)
        try:
            with open(os.path.join(d, "name")) as fh:
                out.append((int(m.group(1)), fh.read().strip()))
        except (OSError, AttributeError):
            pass
    return out


def list_devices() -> list[tuple[int, str]]:
    """(index, name) of capture devices usable with `HWH_CAMERA`."""
    if sys.platform == "darwin":
        return list(enumerate(list_avfoundation_devices()))
    if sys.platform.startswith("linux"):
        return list_v4l2_devices()
    return []


def resolve_device(device: str | int | None) -> int:
    if device is None:
        device = os.environ.get("HWH_CAMERA", "USB Video")
    if isinstance(device, int) or str(device).isdigit():
        return int(device)
    devices = list_devices()
    if not devices:
        # No name lookup on this platform (e.g. Windows): use index 0 unless told otherwise.
        return 0
    for i, n in devices:
        if n == device:
            return i
    for i, n in devices:
        if device.lower() in n.lower():
            return i
    raise RuntimeError(f"capture device {device!r} not found; available: {devices} "
                       "(set HWH_CAMERA to a name or index)")


class Capture:
    """Continuously reads frames on a background thread so `frame()` is always fresh.

    A supervisor thread reopens the device whenever frames stop arriving (unplugged, card
    reset, read errors, or a hung read), retrying until it comes back. The device name is
    re-resolved on every attempt because its index can change after a replug.
    """

    def __init__(self, device: str | int | None = None, width: int = 1920, height: int = 1080,
                 fps: int = 30, stall_timeout: float = 1.5, retry_interval: float = 0.3):
        self._device = device
        self._size = (width, height, fps)
        self._stall_timeout = stall_timeout
        self._retry_interval = retry_interval
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self._cond = threading.Condition()
        self._running = True
        self._gen = 0  # bumped to abandon the current reader
        self._reader: threading.Thread | None = None
        self._opened_at = 0.0
        self._last_error: str | None = None
        self.reconnects = 0
        self._thread = threading.Thread(target=self._supervise, daemon=True)
        self._thread.start()
        try:
            self.frame(timeout=5.0)
        except TimeoutError:
            self.close()
            raise

    @property
    def connected(self) -> bool:
        return self._frame is not None

    def _open(self) -> cv2.VideoCapture:
        idx = resolve_device(self._device)
        backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
        cap = cv2.VideoCapture(idx, backend)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"failed to open capture device #{idx}")
        width, height, fps = self._size
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        return cap

    def _supervise(self) -> None:
        while self._running:
            now = time.monotonic()
            reader_ok = self._reader is not None and self._reader.is_alive()
            last = max(self._ts, self._opened_at)
            # the first frame after opening can take a while; afterwards expect a steady stream
            limit = self._stall_timeout if self._ts > self._opened_at else max(self._stall_timeout, 3.0)
            if reader_ok and now - last < limit:
                time.sleep(0.1)
                continue
            if self._reader is not None:
                print(f"capture: no frames for {now - last:.1f}s, reconnecting", file=sys.stderr)
            with self._cond:
                self._gen += 1  # a hung reader is left behind; it exits once read() returns
                gen = self._gen
                self._frame = None
            self._reader = None
            try:
                cap = self._open()
            except Exception as e:  # noqa: BLE001  unplugged / not enumerated yet
                self._last_error = f"{type(e).__name__}: {e}"
                time.sleep(self._retry_interval)
                continue
            if self._opened_at:
                self.reconnects += 1
                print(f"capture: reconnected (#{self.reconnects})", file=sys.stderr)
            self._last_error = None
            self._opened_at = time.monotonic()
            self._reader = threading.Thread(target=self._read_loop, args=(cap, gen), daemon=True)
            self._reader.start()

    def _read_loop(self, cap: cv2.VideoCapture, gen: int) -> None:
        try:
            while self._running and gen == self._gen:
                ok, f = cap.read()
                if not ok:
                    time.sleep(0.01)
                    continue
                with self._cond:
                    if gen != self._gen:
                        break
                    self._frame = f
                    self._ts = time.monotonic()
                    self._cond.notify_all()
        finally:
            cap.release()

    def frame(self, timeout: float = 5.0, newer_than: float | None = None) -> np.ndarray:
        """Latest BGR frame. With `newer_than`, wait for a frame captured after that time."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._frame is None or (newer_than is not None and self._ts <= newer_than):
                left = deadline - time.monotonic()
                if left <= 0:
                    if self._frame is None:
                        why = f" ({self._last_error})" if self._last_error else ""
                        raise TimeoutError(f"capture device disconnected, reconnecting{why}")
                    raise TimeoutError("no frame from capture device")
                self._cond.wait(left)
            return self._frame.copy()

    def fresh_frame(self, timeout: float = 5.0) -> np.ndarray:
        """A frame captured after this call (skips anything already buffered)."""
        return self.frame(timeout=timeout, newer_than=time.monotonic())

    def jpeg(self, width: int | None = 1280, quality: int = 80, fresh: bool = True) -> bytes:
        f = self.fresh_frame() if fresh else self.frame()
        if width and f.shape[1] != width:
            h = round(f.shape[0] * width / f.shape[1])
            f = cv2.resize(f, (width, h), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return buf.tobytes()

    def save(self, path: str, width: int | None = None) -> str:
        with open(path, "wb") as fh:
            fh.write(self.jpeg(width=width, quality=92))
        return path

    def close(self) -> None:
        self._running = False
        with self._cond:
            self._gen += 1
        self._thread.join(timeout=1)
        if self._reader is not None:
            self._reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
