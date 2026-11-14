"""Moonraker transport and translation to the dashboard's firmware fields."""
import os
import re
from urllib.parse import quote

import requests


class Moonraker:
    def __init__(self, host):
        self.url = os.getenv('MOONRAKER_URL', f'http://{host}:7125').rstrip('/')
        self.headers = {}
        if os.getenv('MOONRAKER_API_KEY'):
            self.headers['X-Api-Key'] = os.environ['MOONRAKER_API_KEY']
        self.light_object = os.getenv('ZMOD_LIGHT_OBJECT', 'led chamber_led')

    def call(self, path, payload=None):
        response = requests.request('GET' if payload is None else 'POST',
                                    self.url + path, json=payload,
                                    headers=self.headers, timeout=8)
        response.raise_for_status()
        data = response.json()
        if 'error' in data:
            raise RuntimeError('Moonraker rejected the request')
        return data['result']

    def detail(self):
        names = self.call('/printer/objects/list')['objects']
        wanted = {'print_stats', 'virtual_sdcard', 'display_status', 'toolhead',
                  'heater_bed', 'gcode_move', 'motion_report', 'fan', 'webhooks',
                  self.light_object, 'zmod_color', 'heater_generic chamber_heater',
                  'fan_generic fanM106', 'fan_generic chamber_fan',
                  'gcode_button frontDoor', 'gcode_button topDoor'}
        wanted.update(n for n in names if re.fullmatch(r'extruder\d*', n)
                      or n in ('temperature_sensor chamber', 'heater_generic chamber'))
        status = self.call('/printer/objects/query',
                           {'objects': {n: None for n in names if n in wanted}})['status']
        if 'print_stats' not in status:
            raise RuntimeError('Moonraker: print_stats unavailable; Klipper is not ready')
        return translate(status, self.light_object)

    def light(self, on):
        self.call('/printer/gcode/script', {'script': 'LED_ON' if on else 'LED_OFF'})

    def clear_finish_dialog(self):
        self.call('/printer/gcode/script', {'script': 'FAST_CLOSE_DIALOGS'})

    def thumbnail(self, filename):
        meta = self.call('/server/files/metadata?filename=' + quote(filename, safe=''))
        thumbs = meta.get('thumbnails') or []
        if not thumbs:
            raise RuntimeError('No thumbnail in G-code metadata')
        thumb = max(thumbs, key=lambda t: t.get('size', 0))['relative_path']
        directory = filename.rsplit('/', 1)[0] + '/' if '/' in filename else ''
        response = requests.get(self.url + '/server/files/gcodes/'
                                + quote(directory + thumb, safe='/'),
                                headers=self.headers, timeout=8)
        response.raise_for_status()
        return response


def translate(s, light_object):
    stats = s['print_stats']
    state = stats.get('state', 'unknown')
    state = {'complete': 'completed', 'standby': 'idle'}.get(state, state)
    webhooks = s.get('webhooks', {})
    if webhooks.get('state') in ('shutdown', 'error'):
        state = 'error'
    progress = max(0, min(1, s.get('virtual_sdcard', {}).get('progress',
                                  s.get('display_status', {}).get('progress', 0)) or 0))
    elapsed = stats.get('print_duration', 0) or 0
    tools = sorted((n for n in s if re.fullmatch(r'extruder\d*', n)),
                   key=lambda n: int(n[8:] or 0))
    active = s.get('toolhead', {}).get('extruder', 'extruder')
    chamber = s.get('heater_generic chamber_heater',
                    s.get('heater_generic chamber', s.get('temperature_sensor chamber', {})))
    info = stats.get('info') or {}
    light = s.get(light_object, {})
    light_value = light.get('value')
    if light_value is None and light.get('color_data'):
        light_value = max((max(color) for color in light['color_data']), default=0)
    colors = s.get('zmod_color') or {}
    slots = []
    for slot in colors.get('slots') or []:
        try:
            ident = int(slot['ID'])
        except (KeyError, TypeError, ValueError):
            continue
        color = str(slot.get('HEX') or '').lstrip('#')
        slots.append({'slotId': ident, 'materialName': slot.get('Material', ''),
                      'materialColor': '#' + color if re.fullmatch('[0-9a-fA-F]{6}', color) else '',
                      'hasFilament': bool(slot.get('hasFilament'))})
    try:
        channel = int(colors.get('channel', 0))
    except (ValueError, TypeError):
        channel = 0
    return {
        '_backend': 'zmod', '_active_nozzle': tools.index(active) + 1 if active in tools else 0,
        '_light_available': light_value is not None,
        '_job_filament_m': (stats.get('filament_used', 0) or 0) / 1000,
        'name': 'Creator 5 Pro · ZMOD', 'model': 'Creator 5 Pro',
        'status': state, 'errorCode': stats.get('message', '') if state == 'error' else '',
        'camera': True, 'printFileName': stats.get('filename', ''),
        'printProgress': progress, 'printDuration': elapsed,
        'estimatedTime': elapsed * (1 - progress) / progress if 0 < progress < 1 else 0,
        'printLayer': info.get('current_layer'), 'targetPrintLayer': info.get('total_layer'),
        'nozzleTemps': [s[n].get('temperature', 0) for n in tools],
        'nozzleTargetTemps': [s[n].get('target', 0) for n in tools],
        'platTemp': s.get('heater_bed', {}).get('temperature'),
        'platTargetTemp': s.get('heater_bed', {}).get('target'),
        'chamberTemp': chamber.get('temperature'), 'chamberTargetTemp': chamber.get('target'),
        'currentPrintSpeed': (s.get('motion_report', {}).get('live_velocity', 0) or 0),
        'printSpeedAdjust': (s.get('gcode_move', {}).get('speed_factor', 1) or 0) * 100,
        'coolingFanSpeed': (s.get('fan_generic fanM106', s.get('fan', {})).get('speed', 0) or 0) * 100,
        'chamberFanSpeed': (s.get('fan_generic chamber_fan', {}).get('speed', 0) or 0) * 100,
        'nozzleCnt': len(tools),
        'lightStatus': 'on' if light_value else 'off',
        **({'matlStationInfo': {'slotInfos': slots, 'slotCnt': len(slots),
                                'currentSlot': channel}} if slots else {}),
    }
