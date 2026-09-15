import io
import json
import logManager
import requests
import threading
import time

from functions.colors import convert_xy

try:
    from xled import HighControlInterface
except Exception:  # pragma: no cover
    HighControlInterface = None

logging = logManager.logger.get_logger(__name__)
_buffers = {}
_buffers_lock = threading.Lock()

# Twinkly's realtime mode reverts to its own effect (e.g. a plain white idle
# pattern) if it stops receiving UDP frames for a while. Resend the last known
# frame on this interval so quiet/unchanged channels don't cause a drop-out.
_HEARTBEAT_INTERVAL = 1.0
# Some Twinkly firmwares appear to expire the "realtime mode" flag separately
# from the frame data itself. Re-assert the mode on this (coarser) interval.
_MODE_REASSERT_INTERVAL = 5.0
# Log a heartbeat-alive line at this interval so it's visible in the log
# without spamming it every single tick.
_HEARTBEAT_LOG_INTERVAL = 30.0

# Last known full-brightness color per light, independent of light.state.
# During entertainment streaming diyHue calls set_light() directly with
# per-frame deltas without necessarily updating light.state, so relying on
# light.state alone caused color to collapse to a stale/neutral value as soon
# as a frame omitted explicit color info.
_last_base_rgb = {}
_last_base_rgb_lock = threading.Lock()


def _hsv_to_rgb(hue, sat, bri):
    h = (max(0, min(65535, int(hue))) / 65535.0) * 360.0
    s = max(0, min(254, int(sat))) / 254.0
    v = max(0, min(254, int(bri))) / 254.0
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:
        rgb = (c, x, 0)
    elif h < 120:
        rgb = (x, c, 0)
    elif h < 180:
        rgb = (0, c, x)
    elif h < 240:
        rgb = (0, x, c)
    elif h < 300:
        rgb = (x, 0, c)
    else:
        rgb = (c, 0, x)
    return [int((value + m) * 255) for value in rgb]


def _coerce_rgb(data, light):
    state = light.state
    bri = int(data.get("bri", state.get("bri", 255)))
    xy = data.get("xy", state.get("xy"))
    ct = data.get("ct", state.get("ct"))
    hue = data.get("hue", state.get("hue"))
    sat = data.get("sat", state.get("sat"))
    key = light.id_v1

    if xy:
        base = convert_xy(xy[0], xy[1], 255)
    elif ct:
        ct_val = max(153, min(500, int(ct)))
        ratio = (ct_val - 153) / 347.0
        base = [255, int(175 + ratio * 80), int(72 + ratio * 183)]
    elif hue is not None and sat is not None:
        base = _hsv_to_rgb(hue, sat, 255)
    else:
        with _last_base_rgb_lock:
            base = _last_base_rgb.get(key)
        if base is None:
            # No color info anywhere (incoming command, light.state, or our
            # own cache) - only then fall back to white, matching a
            # never-configured light.
            base = [255, 255, 255]

    with _last_base_rgb_lock:
        _last_base_rgb[key] = base

    rgb = [int(value * bri / 255.0) for value in base] if bri < 255 else list(base)
    return [max(0, min(255, int(value))) for value in rgb]


def _get_device_state(ip):
    response = requests.get(f"http://{ip}/xled/v1/gestalt", timeout=3)
    if response.status_code != 200:
        raise RuntimeError(f"Twinkly gestalt call failed for {ip}: {response.status_code}")
    return response.json()


def _get_total_leds(info):
    for key in ("number_of_led", "led_count"):
        if isinstance(info.get(key), int):
            return info[key]
    return 150


def _segment_range_for_index(led_total, segment_index):
    return segment_index, segment_index


def _send_frame_locked(bridge, host, led_total):
    """Send the current pixel buffer. Caller must hold bridge['lock']."""
    frame = bytes(bridge["pixels"])
    try:
        bridge["device"].set_rt_frame_socket(io.BytesIO(frame), version=3, leds_number=led_total)
    except Exception:
        logging.exception("Failed to send realtime frame to Twinkly device %s", host)
        raise


def _heartbeat_loop(host, led_total, bridge):
    elapsed = 0.0
    since_log = 0.0
    while True:
        time.sleep(_HEARTBEAT_INTERVAL)
        elapsed += _HEARTBEAT_INTERVAL
        since_log += _HEARTBEAT_INTERVAL

        if elapsed >= _MODE_REASSERT_INTERVAL:
            elapsed = 0.0
            try:
                bridge["device"].set_mode("rt")
            except Exception:
                logging.exception("Failed to re-assert realtime mode on Twinkly device %s", host)

        try:
            with bridge["lock"]:
                _send_frame_locked(bridge, host, led_total)
        except Exception:
            # Already logged in _send_frame_locked; keep the heartbeat alive
            # so a transient failure doesn't permanently stop the refresh.
            pass

        if since_log >= _HEARTBEAT_LOG_INTERVAL:
            since_log = 0.0
            logging.info("Twinkly device %s: heartbeat alive", host)


def _ensure_device(host, led_total):
    if HighControlInterface is None:
        logging.error("Cannot control Twinkly device %s: the 'xled' package is not installed", host)
        raise RuntimeError("xled is not installed")
    device_host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    key = (host, led_total)
    with _buffers_lock:
        if key not in _buffers:
            try:
                device = HighControlInterface(device_host)
                device.set_mode("rt")
            except Exception:
                logging.exception("Failed to connect to Twinkly device %s (led_total=%s)", device_host, led_total)
                raise
            logging.info("Twinkly device %s switched to realtime mode (led_total=%s)", device_host, led_total)
            bridge = {"device": device, "pixels": bytearray(led_total * 3), "lock": threading.Lock()}
            _buffers[key] = bridge
            threading.Thread(
                target=_heartbeat_loop, args=(host, led_total, bridge), daemon=True,
            ).start()
            logging.info("Twinkly device %s: started heartbeat thread (interval=%.1fs)", host, _HEARTBEAT_INTERVAL)
        return _buffers[key]


def _paint_segment(host, led_total, segment_index, rgb):
    bridge = _ensure_device(host, led_total)
    with bridge["lock"]:
        offset = segment_index * 3
        bridge["pixels"][offset:offset + 3] = bytes(rgb)
        _send_frame_locked(bridge, host, led_total)
    logging.debug("Twinkly device %s: segment %s set to rgb=%s", host, segment_index, rgb)


def set_light(light, data):
    try:
        if "lights" in data and isinstance(data["lights"], dict):
            data = data["lights"].get(str(light.id_v1), data["lights"])
        rgb = _coerce_rgb(data, light)
        if data.get("on") is False:
            rgb = [0, 0, 0]
        ip = light.protocol_cfg["ip"]
        led_total = int(light.protocol_cfg["led_total"])
        segment_index = int(light.protocol_cfg["segment_index"])
        logging.info(
            "Twinkly set_light for light id_v1=%s ip=%s segment=%s data=%s -> rgb=%s",
            light.id_v1, ip, segment_index, data, rgb,
        )
        _paint_segment(ip, led_total, segment_index, rgb)
    except Exception:
        logging.exception("Twinkly set_light failed for light id_v1=%s data=%s", getattr(light, "id_v1", "?"), data)
        return {"status": "error"}
    return {"status": "ok"}


def get_light_state(light):
    return {"on": True, "bri": 255, "xy": [0.3, 0.3], "reachable": True}


def generate_light_name(base_name, light_number):
    suffix = f" {light_number}"
    return f"{base_name[:32 - len(suffix)]}{suffix}"


def discover(detected_lights, device_ips):
    for ip in device_ips:
        try:
            info = _get_device_state(ip)
            led_total = _get_total_leds(info)
            base_name = info.get("device_name") or "Twinkly"
            for index in range(led_total):
                detected_lights.append({
                    "protocol": "twinkly",
                    "name": generate_light_name(base_name, index + 1),
                    "modelid": "LST002",
                    "protocol_cfg": {
                        "ip": ip,
                        "mac": info.get("mac", ip),
                        "led_total": led_total,
                        "segment_index": index,
                        "segment_count": led_total,
                        "segment_start": index,
                        "segment_end": index,
                        "protocol": "twinkly",
                    },
                })
        except Exception as exc:
            logging.info("ip %s is not a Twinkly device: %s", ip, exc)
    return detected_lights
