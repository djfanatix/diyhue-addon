#!/usr/bin/env python3
"""Local Twinkly realtime bridge helper for diyHue.

This module avoids the normal REST color endpoint and pushes a full pixel
frame to a Twinkly device over its local realtime UDP protocol. The intent is
that diyHue can expose one physical Twinkly as multiple virtual lights by
keeping a shared frame buffer and flush it in one send.

The bridge is intentionally conservative and works as a helper for a custom
light protocol / config, not as a complete replacement for the entire diyHue
emulator. It expects a known Twinkly IP and total LED count.
"""

import io
import logging
import threading
import time

try:
    from xled import HighControlInterface
except Exception:  # pragma: no cover
    HighControlInterface = None

log = logging.getLogger(__name__)

_buffers = {}
_buffers_lock = threading.Lock()


class TwinklyRealtimeBridge:
    """Shared framebuffer and realtime flush helper."""

    def __init__(self, host, led_count, fps=30, protocol_version=3):
        if HighControlInterface is None:
            raise RuntimeError("xled is not installed. Install it with: pip install xled")

        self.host = host
        self.led_count = int(led_count)
        self.fps = max(1, int(fps))
        self.protocol_version = int(protocol_version)
        self.min_interval = 1.0 / self.fps
        self.last_flush = 0.0
        self.lock = threading.Lock()
        self.pixels = bytearray(self.led_count * 3)
        device_host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        self.device = HighControlInterface(device_host)
        self.device.set_mode("rt")

    def _bytes_for_rgb(self, rgb):
        r, g, b = rgb
        return bytes((int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF))

    def set_segment(self, start_idx, end_idx, rgb):
        if self.led_count <= 0:
            return
        start = max(0, min(int(start_idx), self.led_count - 1))
        end = max(start, min(int(end_idx), self.led_count - 1))
        pixel = self._bytes_for_rgb(rgb)

        with self.lock:
            for idx in range(start, end + 1):
                offset = idx * 3
                self.pixels[offset] = pixel[0]
                self.pixels[offset + 1] = pixel[1]
                self.pixels[offset + 2] = pixel[2]

    def flush(self, force=False):
        now = time.time()
        if not force and (now - self.last_flush) < self.min_interval:
            return

        with self.lock:
            payload = bytes(self.pixels)
        try:
            self.device.set_rt_frame_socket(
                io.BytesIO(payload),
                version=self.protocol_version,
                leds_number=self.led_count,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("Twinkly realtime flush failed for %s: %s", self.host, exc)
            return

        self.last_flush = now

    def clear(self):
        with self.lock:
            self.pixels = bytearray(self.led_count * 3)
        self.flush(force=True)

    def close(self):
        try:
            self.device.close()
        except Exception:  # pragma: no cover
            pass


def get_bridge(host, led_count, fps=30, protocol_version=3):
    key = (host, int(led_count), int(fps), int(protocol_version))
    with _buffers_lock:
        bridge = _buffers.get(key)
        if bridge is None:
            bridge = TwinklyRealtimeBridge(host, led_count, fps=fps, protocol_version=protocol_version)
            _buffers[key] = bridge
        return bridge


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bridge = get_bridge("192.168.1.50", 150, fps=30)
    bridge.set_segment(0, 149, (255, 0, 120))
    bridge.flush(force=True)
    time.sleep(1)
    bridge.clear()
    bridge.close()
