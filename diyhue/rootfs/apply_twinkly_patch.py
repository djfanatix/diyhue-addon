from pathlib import Path
import shutil

base = Path('/opt/hue-emulator')
shutil.copy(base / 'twinkly.py', base / 'lights' / 'protocols' / 'twinkly.py')

protocols = base / 'lights' / 'protocols' / '__init__.py'
text = protocols.read_text()
if 'twinkly' not in text:
    text = text.replace('from lights.protocols import ', 'from lights.protocols import twinkly, ', 1)
    text = text.replace('protocols = [', 'protocols = [twinkly, ', 1)
    protocols.write_text(text)

lights = base / 'lights' / 'discover.py'
text = lights.read_text()
if 'twinkly' not in text.split('\n', 8)[0]:
    text = text.replace('from lights.protocols import ', 'from lights.protocols import twinkly, ', 1)
text = text.replace(
    'for discover_func in [native_multi.discover, tasmota.discover, shelly.discover, esphome.discover]:',
    'for discover_func in [native_multi.discover, tasmota.discover, shelly.discover, esphome.discover, twinkly.discover]:'
)
text = text.replace(
    'if bridgeConfig["config"]["tasmota"]["enabled"]:',
    'if bridgeConfig["config"].get("twinkly", {}).get("enabled", True):\n        twinkly.discover(detectedLights, device_ips)\n    if bridgeConfig["config"]["tasmota"]["enabled"]:'
)
lights.write_text(text)

restful = base / 'flaskUI' / 'restful.py'
text = restful.read_text()
old = 'if last_button_press + 30 >= datetime.now().timestamp(): # 30 sec offset'
new = 'if configManager.runtimeConfig.arg.get("noLinkButton", False) or last_button_press + 30 >= datetime.now().timestamp(): # 30 sec offset'
restful.write_text(text.replace(old, new))
