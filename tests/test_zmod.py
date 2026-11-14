import unittest
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch, Mock

from zmod import Moonraker, translate


class ZmodTests(unittest.TestCase):
    def status(self, state='printing'):
        return {'print_stats': {'state': state, 'filename': 'PLA/test.gcode',
                                'print_duration': 60, 'filament_used': 1234,
                                'info': {'current_layer': 3, 'total_layer': 10}},
                'virtual_sdcard': {'progress': .25},
                'extruder': {'temperature': 210, 'target': 220},
                'extruder1': {'temperature': 200, 'target': 220},
                'toolhead': {'extruder': 'extruder1'},
                'output_pin light': {'value': .5}}

    def test_units_active_tool_and_estimate(self):
        d = translate(self.status(), 'output_pin light')
        self.assertEqual(d['_job_filament_m'], 1.234)
        self.assertNotIn('cumulativeFilament', d)
        self.assertEqual(d['estimatedTime'], 180)
        self.assertEqual(d['_active_nozzle'], 2)
        self.assertEqual(d['nozzleTemps'], [210, 200])
        self.assertEqual(d['lightStatus'], 'on')

    def test_terminal_pause_and_shutdown_states(self):
        for raw, expected in [('complete', 'completed'), ('paused', 'paused'),
                              ('cancelled', 'cancelled'), ('standby', 'idle')]:
            self.assertEqual(translate(self.status(raw), '')['status'], expected)
        s = self.status(); s['webhooks'] = {'state': 'shutdown'}
        self.assertEqual(translate(s, '')['status'], 'error')

    def test_missing_optional_objects(self):
        d = translate({'print_stats': {'state': 'standby'}}, '')
        self.assertEqual(d['estimatedTime'], 0)
        self.assertEqual(d['nozzleTemps'], [])
        self.assertFalse(d['_light_available'])

    def test_query_discovers_all_extruders(self):
        m = Moonraker('printer')
        m.call = Mock(side_effect=[{'objects': list(self.status()) + ['extruder2']},
                                   {'status': self.status()}])
        m.detail()
        objects = m.call.call_args.args[1]['objects']
        self.assertIn('extruder2', objects)
        self.assertNotIn('output_pin light', objects)

    def test_klipper_not_ready_does_not_publish_idle(self):
        m = Moonraker('printer')
        m.call = Mock(side_effect=[{'objects': ['webhooks']}, {'status': {}}])
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            m.detail()

    @patch.dict('os.environ', {'MOONRAKER_API_KEY': 'test-secret'})
    @patch('zmod.requests.request')
    def test_auth_and_error_response(self, request):
        m = Moonraker('printer')
        request.return_value.json.return_value = {'result': {'objects': []}}
        self.assertEqual(m.call('/printer/objects/list'), {'objects': []})
        self.assertEqual(request.call_args.kwargs['headers'], {'X-Api-Key': 'test-secret'})
        request.return_value.json.return_value = {'error': {'message': 'denied'}}
        with self.assertRaises(RuntimeError):
            m.call('/printer/info')

    def test_light_uses_only_documented_macros(self):
        m = Moonraker('printer'); m.call = Mock()
        m.light(True)
        m.call.assert_called_with('/printer/gcode/script', {'script': 'LED_ON'})
        m.light(False)
        m.call.assert_called_with('/printer/gcode/script', {'script': 'LED_OFF'})

    def test_creator5_objects_and_material_slots(self):
        s = self.status()
        s.update({'led chamber_led': {'color_data': [[0, 0, 0, .5]]},
                  'heater_generic chamber_heater': {'temperature': 35, 'target': 45},
                  'fan_generic fanM106': {'speed': .25},
                  'fan_generic chamber_fan': {'speed': .5},
                  'zmod_color': {'channel': 2, 'slots': [
                      {'ID': '2', 'Material': 'PETG', 'HEX': 'AA00FF', 'hasFilament': True}]}})
        d = translate(s, 'led chamber_led')
        self.assertEqual(d['chamberTemp'], 35)
        self.assertEqual(d['chamberTargetTemp'], 45)
        self.assertEqual(d['lightStatus'], 'on')
        self.assertTrue(d['_light_available'])
        self.assertEqual(d['coolingFanSpeed'], 25)
        self.assertEqual(d['chamberFanSpeed'], 50)
        self.assertEqual(d['matlStationInfo']['currentSlot'], 2)
        self.assertEqual(d['matlStationInfo']['slotInfos'][0]['materialColor'], '#AA00FF')

    def test_close_dialog_uses_fixed_macro(self):
        m = Moonraker('printer'); m.call = Mock()
        m.clear_finish_dialog()
        m.call.assert_called_once_with('/printer/gcode/script', {'script': 'FAST_CLOSE_DIALOGS'})

    def test_dashboard_integration_and_pause_history(self):
        # Import the application in an isolated process: its databases and
        # background workers must not touch the user's running installation.
        script = '''
from unittest.mock import Mock, patch
import app
from zmod import translate
raw = translate({'print_stats': {'state': 'printing', 'filename': 'PLA.gcode',
    'print_duration': 300, 'filament_used': 2000},
    'extruder': {'temperature': 230, 'target': 240},
    'extruder1': {'temperature': 210, 'target': 220},
    'toolhead': {'extruder': 'extruder1'}}, '')
n = app.normalize(raw)
assert n['temps']['nozzle_id'] == 2
assert n['temps']['nozzle'] == 210
assert n['job']['has_thumb']
hist = {'jobs': [], 'current': None}
cur, delta = app.track_filament(hist, n)
assert app.current_filament_m(cur) == 2
assert delta == 0
states = []
for state in ['printing', 'paused', 'printing', 'completed']:
    states.append((dict(raw, status=state), None))
class Done(Exception): pass
sleeps = [None, None, None, Done()]
with patch.object(app, 'fetch_detail', side_effect=states), \\
     patch.object(app, 'load_history', return_value=hist), \\
     patch.object(app, 'save_history'), \\
     patch.object(app.materials, 'register_slots'), \\
     patch.object(app.materials, 'commit_job') as commit, \\
     patch.object(app.time, 'sleep', side_effect=sleeps):
    try: app.poller()
    except Done: pass
    assert commit.call_count == 1
    assert len(hist['jobs']) == 1
    assert hist['jobs'][0]['status'] == 'completed'
with patch.object(app, 'ff_post', side_effect=AssertionError('stock API called')):
    assert app.ff_control('streamCtrl', {'action': 'open'})[0]
    with patch.object(app, 'fetch_detail', return_value=(dict(raw, status='printing'), None)), \\
         patch.object(app.moonraker, 'clear_finish_dialog') as close:
        assert app.app.test_client().post('/api/clear_job').status_code == 409
        close.assert_not_called()
    with patch.object(app, 'fetch_detail', return_value=(dict(raw, status='completed'), None)), \\
         patch.object(app.moonraker, 'clear_finish_dialog') as close:
        assert app.app.test_client().post('/api/clear_job').json['ok']
        close.assert_called_once()
with patch.object(app.moonraker, 'detail', side_effect=RuntimeError('offline')):
    response = app.app.test_client().get('/api/status')
    assert response.status_code == 200 and response.json['_stale']
    app._cache['detail'] = None
    assert app.app.test_client().get('/api/status').status_code == 502
with patch.object(app, 'SERIAL', 'serial'), patch.object(app, 'CHECK_CODE', 'code'), \\
     patch.object(app.moonraker, 'detail', return_value=raw), \\
     patch.object(app, 'fetch_native_detail', return_value=({'status': 'idle',
         'cumulativeFilament': 123, 'firmwareVersion': '2.0',
         'matlStationInfo': {'slotInfos': [{'slotId': 2, 'materialName': 'PETG',
             'materialColor': '#123456', 'hasFilament': True}], 'currentSlot': 2}}, None)):
    enriched, error = app.fetch_detail()
    n = app.normalize(enriched)
    assert n['status'] == 'printing'
    assert n['filament']['cumulative_m'] == 123
    assert n['filament']['counter_m'] == 2
    assert n['material_station']['slots'][0]['active']
    assert n['firmware'] == '2.0'
with patch.object(app, 'SERIAL', 'serial'), patch.object(app, 'CHECK_CODE', 'code'), \\
     patch.object(app.moonraker, 'detail', return_value=raw), \\
     patch.object(app, 'fetch_native_detail', return_value=(None, 'offline')):
    assert app.fetch_detail() == (raw, None)
'''
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, DATA_DIR=directory, DASHBOARD_BACKGROUND='0',
                       PRINTER_BACKEND='zmod')
            result = subprocess.run([sys.executable, '-c', script], env=env,
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
