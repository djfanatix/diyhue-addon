import io
import json
import logManager
import requests
import threading

from functions.colors import convert_xy

try:
    from xled import HighControlInterface
except Exception:  # pragma: no cover
    HighControlInterface = None

logging = logManager.logger.get_logger(__name__)
_buffers = {}
_buffers_lock = threading.Lock()


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
    bri = int(data.get("bri", light.state.get("bri", 255)))
    if "xy" in data:
        rgb = convert_xy(data["xy"][0], data["xy"][1], bri)
    elif "ct" in data:
        ct = max(153, min(500, int(data["ct"])))
        ratio = (ct - 153) / 347.0
        rgb = [255, int(175 + ratio * 80), int(72 + ratio * 183)]
    elif "hue" in data and "sat" in data:
        rgb = _hsv_to_rgb(data["hue"], data["sat"], bri)
    else:
        rgb = [255, 255, 255]
    if bri < 255:
        rgb = [int(value * bri / 255.0) for value in rgb]
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
            _buffers[key] = {"device": device, "pixels": bytearray(led_total * 3), "lock": threading.Lock()}
        return _buffers[key]


def _paint_segment(host, led_total, segment_index, rgb):
    bridge = _ensure_device(host, led_total)
    with bridge["lock"]:
        offset = segment_index * 3
        bridge["pixels"][offset:offset + 3] = bytes(rgb)
        frame = bytes(bridge["pixels"])
    try:
        bridge["device"].set_rt_frame_socket(io.BytesIO(frame), version=3, leds_number=led_total)
    except Exception:
        logging.exception(
            "Failed to send realtime frame to Twinkly device %s (segment=%s, rgb=%s)",
            host, segment_index, rgb,
        )
        raise
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
