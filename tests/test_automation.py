import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from flask import Flask
from automation import Automation,AutomationError
from automation_web import register_automation

ROOT=Path(__file__).resolve().parents[1]
TOKEN='123456789:'+('x'*35)

class Camera:
    def __init__(self,jpeg=b'\xff\xd8test\xff\xd9'):
        self.leases=0;self.jpeg=jpeg
    def join(self):self.leases+=1
    def leave(self):self.leases-=1
    def snapshot(self,timeout=20):return self.jpeg

class AutomationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.camera=Camera();self.status={}
        self.a=Automation(self.tmp.name,lambda:self.status,self.camera)
    def tearDown(self):self.tmp.cleanup()
    def frame(self):
        self.a.next_frame=0;self.a.capture_tick()
    def print_status(self,state='printing',name='part.gcode',progress=1):
        return dict(status=state,printing=state=='printing',job=dict(file=name,progress=progress),error_code=0)

class AutomationTests(AutomationFixture):
    def test_filament_maintenance_does_not_notify_or_record(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True,auto_record=True))
        for state in ('ready','heating','busy','printing','loading','paused','ready'):
            status=self.print_status(state,name='')
            status['printing']=state in ('heating','busy','printing')
            self.a.observe(status)
        self.assertEqual(self.a.events(),[])
        self.assertIsNone(self.a.active)
        with self.a.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_heating_with_old_filename_waits_for_actual_printing(self):
        with patch.object(self.a,'enqueue') as notify:
            for state in ('heating','busy','ready'):
                status=self.print_status(state)
                status['printing']=state in ('heating','busy')
                self.a.observe(status)
            notify.assert_not_called()
            self.a.observe(self.print_status())
            self.assertEqual(notify.call_count,1)

    def test_legacy_nameless_job_does_not_resume_or_finish(self):
        self.a.last_state=dict(status='heating',printing=True,file='')
        with patch.object(self.a,'enqueue') as notify:
            self.a.observe(self.print_status('loading',name=''))
            self.a.observe(self.print_status('ready',name=''))
            notify.assert_not_called()
        self.assertEqual(self.a.events(),[])

    def test_events_persist_without_telegram_and_tool_gaps_do_not_duplicate(self):
        status=self.print_status()
        status['material_station']={'slots':[{'id':1,'active':True}]}
        self.a.observe(status);self.a.observe(status)
        self.a.observe(self.print_status('changing_filament'))
        status['material_station']['slots'][0]['id']=2
        self.a.observe(status);self.a.observe(status)
        self.a.observe(self.print_status('pausing'))
        self.a.observe(self.print_status('paused'))
        self.a.observe(status)
        self.a.observe(self.print_status('completed'))
        restarted=Automation(self.tmp.name,lambda:{},Camera())
        events=restarted.events()
        self.assertEqual(sum(e['kind']=='tool' for e in events),1)
        self.assertEqual(sum(e['kind']=='pause' for e in events),2)
        self.assertEqual(sum(e['kind']=='print' for e in events),2)
        self.assertTrue(any('1 → 2' in e['message'] for e in events))
        self.assertTrue(all(e['file']=='part.gcode' for e in events))
    def test_connection_events_debounce_and_survive_restart(self):
        with patch('automation.time.time',return_value=100):self.a.observe({'_stale':True})
        with patch('automation.time.time',return_value=120):self.a.observe({'_stale':True})
        self.assertEqual(self.a.events(),[])
        restarted=Automation(self.tmp.name,lambda:{},Camera())
        with patch('automation.time.time',return_value=131):
            restarted.observe({'_stale':True});restarted.observe({'_stale':True})
        restarted.observe(self.print_status('ready'))
        restarted.observe(self.print_status('ready'))
        self.assertEqual(len(restarted.events(kind='connection')),2)
    def test_event_retention_and_pagination(self):
        with self.a.connect() as con:
            con.executemany('INSERT INTO events(created,kind,message,file) VALUES(?,?,?,?)',
                            [(1,'print','test','')]*10001)
        self.a.log_event('error','test')
        with self.a.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0],10000)
        first=self.a.events();second=self.a.events(before=first[-1]['id'])
        self.assertEqual(len(first),100);self.assertEqual(len(second),100)
        self.assertLess(second[0]['id'],first[-1]['id'])
        self.assertEqual(len(self.a.events(kind='error')),1)
    def test_capture_shares_single_lease_and_stop_releases(self):
        self.a.start_recording('Part');self.frame();self.frame()
        self.assertEqual(self.camera.leases,1);self.assertEqual(self.a.active['frames'],2)
        self.a.stop_recording();self.assertEqual(self.camera.leases,0)
        self.assertEqual(self.a.recordings()[0]['status'],'queued')
    def test_resume_uses_frames_on_disk_after_container_restart(self):
        ident=self.a.start_recording('Part');self.frame()
        (self.a.media/ident/'00000001.jpg').write_bytes(self.camera.jpeg)
        restarted=Automation(self.tmp.name,lambda:{},Camera())
        self.assertEqual(restarted.active['id'],ident);self.assertEqual(restarted.active['frames'],2)
        restarted.capture_tick();self.assertTrue((restarted.media/ident/'00000002.jpg').exists())
    def test_settings_persist_and_never_return_token(self):
        result=self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True,interval=15))
        self.assertNotIn('bot_token',result);self.assertTrue(result['token_configured'])
        self.assertEqual(self.a.settings_path.stat().st_mode&0o777,0o600)
        restarted=Automation(self.tmp.name,lambda:{},Camera())
        self.assertEqual(restarted.config['bot_token'],TOKEN)
        restarted.save_settings(dict(bot_token='',interval=20))
        self.assertEqual(restarted.config['bot_token'],TOKEN)
    def test_config_validation_does_not_write_invalid_values(self):
        for value in [0,4,3601,'10',True]:
            with self.assertRaises(AutomationError):self.a.save_settings(dict(interval=value))
        with self.assertRaises(AutomationError):self.a.save_settings(dict(telegram_enabled=True))
    def test_pause_and_stale_status_do_not_finish_job(self):
        self.a.save_settings(dict(auto_record=True))
        self.a.observe(self.print_status());ident=self.a.active['id']
        self.a.observe(self.print_status('paused'));self.assertEqual(self.a.active['id'],ident)
        self.a.observe(dict(self.print_status('idle'),_stale=True));self.assertEqual(self.a.active['id'],ident)
        self.a.observe(self.print_status('completed'));self.assertIsNone(self.a.active)
    def test_manual_stop_suppresses_auto_restart_until_next_job(self):
        self.a.save_settings(dict(auto_record=True));self.a.observe(self.print_status())
        self.a.stop_recording();self.a.observe(self.print_status());self.assertIsNone(self.a.active)
        self.a.observe(self.print_status('completed'));self.a.observe(self.print_status())
        self.assertIsNotNone(self.a.active)
    def test_delete_protects_active_and_rejects_path_traversal(self):
        ident=self.a.start_recording()
        with self.assertRaises(AutomationError):self.a.delete(ident)
        with self.assertRaises(AutomationError):self.a.recording('../settings.json')
    def test_low_disk_space_prevents_recording(self):
        with patch('automation.shutil.disk_usage',return_value=type('Disk',(),{'free':100})()):
            with self.assertRaises(AutomationError):self.a.start_recording()
        self.assertIsNone(self.a.active)
    def test_quota_prevents_recording(self):
        self.a.quota_checked=time.monotonic();self.a.quota_bytes=11*1024**3
        with self.assertRaises(AutomationError):self.a.start_recording()
    def test_unknown_chat_commands_are_ignored(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True))
        with patch.object(self.a,'telegram_api') as api:
            self.a.command(self.a.config,dict(chat=dict(id=99),text='/photo'))
            api.assert_not_called()
        with self.a.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
    def test_terminal_status_is_not_falsely_announced_as_success(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True))
        self.a.observe(self.print_status());self.a.observe(self.print_status('idle'))
        with self.a.connect() as con:messages=[r[0] for r in con.execute('SELECT body FROM outbox')]
        self.assertTrue(any('остановлена' in m for m in messages));self.assertFalse(any('завершена' in m for m in messages))
    def test_repeated_status_does_not_duplicate_notifications(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True))
        self.a.observe(self.print_status());self.a.observe(self.print_status())
        with self.a.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
    def test_tool_change_keeps_job_and_progress_across_restart(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True))
        self.a.observe(self.print_status(progress=30))
        self.a.observe(self.print_status('changing_filament',progress=30))
        restarted=Automation(self.tmp.name,lambda:{},Camera())
        restarted.observe(self.print_status('changing_filament',progress=30))
        restarted.observe(self.print_status(progress=31))
        with restarted.connect() as con:messages=[r[0] for r in con.execute('SELECT body FROM outbox')]
        self.assertEqual(sum('Началась печать' in m for m in messages),1)
        self.assertEqual(sum('Печать: 30%' in m for m in messages),1)
        restarted.observe(self.print_status('completed',progress=100))
        restarted.observe(self.print_status(progress=1))
        with restarted.connect() as con:messages=[r[0] for r in con.execute('SELECT body FROM outbox')]
        self.assertEqual(sum('Началась печать' in m for m in messages),2)
    def test_transient_states_keep_recording_and_progress(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True,auto_record=True))
        self.a.observe(self.print_status(progress=30));ident=self.a.active['id']
        for state in ('changing_filament','unknown','heating','printing'):
            self.a.observe(self.print_status(state,name='',progress=30))
            self.assertEqual(self.a.active['id'],ident)
            self.assertEqual(self.a.last_state['file'],'part.gcode')
            self.assertEqual(self.a.get_state('progress'),25)
        self.a.observe(self.print_status(progress=31))
        with self.a.connect() as con:messages=[r[0] for r in con.execute('SELECT body FROM outbox')]
        self.assertEqual(sum('Началась печать' in m for m in messages),1)
    def test_pause_transitions_notify_once_and_resume_same_job(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True))
        for state in ('printing','pausing','paused','pause','printing'):
            self.a.observe(self.print_status(state))
        with self.a.connect() as con:messages=[r[0] for r in con.execute('SELECT body FROM outbox')]
        self.assertEqual(sum('на паузе' in m for m in messages),1)
        self.assertEqual(sum('Началась печать' in m for m in messages),1)
    def test_terminal_after_tool_change_notifies_once(self):
        for terminal in ('completed','finished','cancelled','error','idle','ready'):
            with self.subTest(terminal=terminal), patch.object(self.a,'enqueue') as notify:
                self.a.last_state={}
                self.a.observe(self.print_status())
                self.a.observe(self.print_status('changing_filament'))
                self.a.observe(self.print_status(terminal,name=''))
                self.a.observe(self.print_status(terminal,name=''))
                self.assertEqual(notify.call_count,2)
    def test_different_job_after_tool_change_starts_new_recording(self):
        self.a.save_settings(dict(auto_record=True))
        self.a.observe(self.print_status());ident=self.a.active['id']
        self.a.observe(self.print_status('changing_filament'))
        with patch.object(self.a,'enqueue') as notify:
            self.a.observe(self.print_status(name='other.gcode'))
        self.assertNotEqual(self.a.active['id'],ident)
        self.assertEqual(self.a.active['label'],'other.gcode')
        self.assertEqual(notify.call_count,1)
    def test_failed_auto_start_does_not_repeat_start_notifications(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True,auto_record=True))
        with patch.object(self.a,'check_space',side_effect=AutomationError('No space')):
            self.a.observe(self.print_status());self.a.observe(self.print_status())
        self.assertIsNone(self.a.active)
        self.assertTrue(self.a.get_state('auto_suppressed'))
        self.assertEqual(self.a.last_error,'No space')
        with self.a.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
    def test_token_is_redacted_when_network_throws_exception(self):
        import requests
        with patch('automation.requests.post',side_effect=requests.RequestException('https://bot'+TOKEN)):
            with self.assertRaises(AutomationError) as ctx:self.a.telegram_api(dict(bot_token=TOKEN),'getMe')
        self.assertNotIn(TOKEN,str(ctx.exception))
    def test_failed_delivery_retries_durably(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True));self.a.enqueue('message','test')
        with patch.object(self.a,'telegram_api',side_effect=AutomationError('offline')):self.a.deliver(self.a.config)
        with self.a.connect() as con:
            row=con.execute('SELECT * FROM outbox').fetchone();self.assertEqual(row['attempts'],1);self.assertGreater(row['next_try'],time.time())
    def test_bot_identity_recovers_after_restart_without_resetting_offset(self):
        self.a.save_settings(dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True))
        self.a.set_state('telegram_ready',True);self.a.set_state('telegram_offset',123)
        restarted=Automation(self.tmp.name,lambda:{},Camera())
        def telegram(cfg,method,data=None):
            if method=='getMe':return dict(username='test_printer_bot')
            restarted.stop_event.set();return []
        with patch.object(restarted,'telegram_api',side_effect=telegram):restarted.telegram_loop()
        self.assertEqual(restarted.bot_name,'test_printer_bot')
        self.assertEqual(restarted.get_state('telegram_offset'),123)
    def test_retention_deletes_only_finished_old_recordings(self):
        ident=self.a.start_recording();self.frame();self.a.stop_recording();self.a.render(ident)
        with self.a.connect() as con:con.execute('UPDATE recordings SET finished=? WHERE id=?',(time.time()-86400*40,ident))
        self.a.cleanup();self.assertEqual(self.a.recordings(),[])
    def test_long_running_capture_gets_full_retention_after_finishing(self):
        ident=self.a.start_recording();self.frame();self.a.stop_recording();self.a.render(ident)
        with self.a.connect() as con:con.execute('UPDATE recordings SET created=? WHERE id=?',(time.time()-86400*40,ident))
        self.a.cleanup();self.assertEqual(self.a.recording(ident)['status'],'failed')
    def test_archive_does_not_hide_records_beyond_one_hundred(self):
        with self.a.connect() as con:
            con.executemany('INSERT INTO recordings (id,label,status,created,finished) VALUES (?,?,?,?,?)',
                [(f'{i:032x}',str(i),'ready',time.time()+i,time.time()) for i in range(105)])
        self.assertEqual(len(self.a.recordings()),105)

class WebTests(AutomationFixture):
    def test_events_routes_and_invalid_filters(self):
        self.a.log_event('error','Ошибка','part.gcode')
        self.assertEqual(self.client.get('/events').status_code,200)
        response=self.client.get('/api/automation/events?kind=error')
        self.assertEqual(response.json['events'][0]['file'],'part.gcode')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        for query in ('before=bad','before=-1','kind=bad'):
            self.assertEqual(self.client.get('/api/automation/events?'+query).status_code,400)
    def setUp(self):
        super().setUp();self.app=Flask(__name__,template_folder=str(ROOT/'templates'),static_folder=str(ROOT/'static'))
        self.app.testing=True;register_automation(self.app,self.a);self.client=self.app.test_client()
        self.page=self.client.get('/automation').text
        with self.client.session_transaction() as session:self.csrf=session['csrf']
    def post(self,url,data=None):return self.client.post(url,json=data or {},headers={'X-CSRF-Token':self.csrf})
    def test_web_settings_hide_credentials_and_require_csrf(self):
        self.assertEqual(self.client.post('/api/automation/settings',json=dict(interval=15)).status_code,403)
        self.assertEqual(self.post('/api/automation/settings',dict(bot_token=TOKEN,chat_id='42',telegram_enabled=True)).status_code,200)
        self.assertNotIn(TOKEN,self.client.get('/api/automation').text)
        for path in ['/automation','/bot']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertNotIn(TOKEN,response.text)
    def test_file_zip_stream_and_metadata(self):
        ident=self.a.start_recording('Part');self.frame();self.frame();self.a.stop_recording()
        with self.a.connect() as con:con.execute("UPDATE recordings SET status='failed' WHERE id=?",(ident,))
        response=self.client.get(f'/api/automation/recordings/{ident}/frames.zip')
        self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as z:
            self.assertEqual(z.namelist(),['00000000.jpg','00000001.jpg','recording.json'])
            self.assertEqual(z.read('00000000.jpg'),self.camera.jpeg)
        self.assertIn('attachment',response.headers['Content-Disposition'])
    def test_rename_and_delete(self):
        ident=self.a.start_recording();self.frame();self.a.stop_recording();self.a.render(ident)
        base='/api/automation/recordings/'+ident
        self.assertEqual(self.post(base+'/rename',dict(label='<b>Part</b>')).status_code,200)
        self.assertEqual(self.a.recording(ident)['label'],'<b>Part</b>')
        self.assertEqual(self.client.delete(base,headers={'X-CSRF-Token':self.csrf}).status_code,200)
        self.assertFalse((self.a.media/ident).exists())
    def test_invalid_json_bodies_are_rejected(self):
        for url in ['/api/automation/start','/api/automation/recordings/'+'a'*32+'/rename']:
            response=self.client.post(url,json=['invalid'],headers={'X-CSRF-Token':self.csrf})
            self.assertEqual(response.status_code,400)

class EncodeTests(unittest.TestCase):
    def test_real_ffmpeg_encode_and_archive_download(self):
        with tempfile.TemporaryDirectory() as d:
            image=Path(d)/'test.jpg'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=320x240','-frames:v','1','-threads','1',str(image)],check=True)
            a=Automation(d,lambda:{},Camera(image.read_bytes()));ident=a.start_recording('Smoke')
            for _ in range(3):a.next_frame=0;a.capture_tick()
            a.stop_recording();a.render(ident)
            row=a.recording(ident);self.assertEqual(row['status'],'ready',row)
            info=json.loads(subprocess.check_output(['ffprobe','-v','quiet','-show_streams','-of','json',str(a.media/ident/'video.mp4')]))
            stream=info['streams'][0];self.assertEqual(stream['codec_name'],'h264');self.assertEqual(stream['pix_fmt'],'yuv420p');self.assertEqual(stream['nb_frames'],'3')
            app=Flask(__name__,template_folder=str(ROOT/'templates'))
            register_automation(app,a)
            with app.test_client() as client:
                url=f'/api/automation/recordings/{ident}/video'
                with client.get(url+'?download=1') as response:
                    self.assertEqual(response.status_code,200)
                    self.assertIn('attachment',response.headers['Content-Disposition'])
                    self.assertEqual(response.data,(a.media/ident/'video.mp4').read_bytes())
                with client.get(url,headers={'Range':'bytes=0-31'}) as response:
                    self.assertEqual(response.status_code,206)
                    self.assertEqual(len(response.data),32)

if __name__=='__main__':unittest.main()

class NormalizedPrintStateTests(unittest.TestCase):
    def test_maintenance_without_filename_is_not_printing(self):
        import ast
        tree=ast.parse((ROOT/'app.py').read_text())
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('pick','to_float','normalize')]
        namespace={'PRINTER_HOST':'test'}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'app.py','exec'),namespace)
        for state in ('heating','busy','printing','loading','ready'):
            with self.subTest(state=state):
                self.assertFalse(namespace['normalize']({'status':state})['printing'])
        self.assertFalse(namespace['normalize']({'status':'printing','printFileName':'  '})['printing'])
        self.assertTrue(namespace['normalize']({'status':'printing','printFileName':'part.gcode'})['printing'])
