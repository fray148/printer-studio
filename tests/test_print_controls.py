"""Control API regression tests: printer I/O is always mocked."""
import ast
from pathlib import Path
import secrets
import threading
import unittest
from unittest.mock import Mock
from flask import Flask, jsonify, request, session

class PrintControlTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = 'test-only'
        source = ast.parse(Path('app.py').read_text())
        names = {'pick', 'to_float', 'normalize', 'api_cancel'}
        nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.detail = {'status': 'printing', 'printFileName': 'part.gcode'}
        self.fetch = Mock(return_value=(self.detail, None))
        self.control = Mock(return_value=(True, {}))
        self.ns = dict(app=app, jsonify=jsonify, request=request, session=session,
                       secrets=secrets, _cancel_lock=threading.Lock(),
                       CANCELLABLE_STATES={'printing','paused','pause','pausing'},
                       fetch_detail=self.fetch, ff_control=self.control,
                       time=Mock(), PRINTER_HOST='test')
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),self.ns)
        self.client = app.test_client()
        with self.client.session_transaction() as s: s['csrf']='test-token'
    def post(self, path='/api/cancel', **kw):
        return self.client.post(path,json=kw.get('body',{'confirm':True,'file':'part.gcode'}),
                                headers=kw.get('headers',{'X-CSRF-Token':'test-token'}))
    def test_csrf_blocks_all_actions(self):
        for path in ('/api/cancel','/api/pause','/api/resume'):
            self.assertEqual(self.post(path,headers={}).status_code,403)
        self.control.assert_not_called()
    def test_confirmation_and_file_required(self):
        for body in ({},{'confirm':False,'file':'part.gcode'},{'confirm':True,'file':''},[]):
            self.assertEqual(self.post(body=body).status_code,400)
        self.control.assert_not_called()
    def test_changed_job_rejected(self):
        self.assertEqual(self.post(body={'confirm':True,'file':'other.gcode'}).status_code,409)
        self.control.assert_not_called()
    def test_offline_rejected(self):
        self.fetch.return_value=(None,'offline')
        self.assertEqual(self.post().status_code,502)
        self.control.assert_not_called()
    def test_inactive_and_maintenance_rejected(self):
        for state in ('ready','completed','heating','busy','unknown'):
            self.detail['status']=state
            self.assertEqual(self.post().status_code,409)
        self.control.assert_not_called()
    def test_rejected_control_is_not_success(self):
        self.control.return_value=(False,{})
        self.assertEqual(self.post().status_code,502)
    def test_ack_alone_is_not_confirmation(self):
        r=self.post()
        self.assertEqual(r.status_code,202)
        self.assertFalse(r.json['confirmed'])
        self.control.assert_called_once_with('jobCtl_cmd',{'jobID':'','action':'cancel'})
    def test_cancel_confirmation(self):
        self.fetch.side_effect=[(self.detail,None),({'status':'cancel'},None)]
        r=self.post()
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['confirmed'])
    def test_pause_and_resume_protocol(self):
        for route,state,target,action in (('/api/pause','printing','pause','pause'),('/api/resume','pause','printing','continue')):
            self.fetch.side_effect=[({'status':state,'printFileName':'part.gcode'},None),({'status':target},None)]
            r=self.post(route)
            self.assertEqual(r.status_code,200)
            self.assertTrue(r.json['confirmed'])
            self.control.assert_called_with('jobCtl_cmd',{'jobID':'','action':action})
    def test_invalid_pause_resume_states_rejected(self):
        self.detail['status']='printing'
        self.assertEqual(self.post('/api/resume').status_code,409)
        self.detail['status']='pause'
        self.assertEqual(self.post('/api/pause').status_code,409)
        self.control.assert_not_called()
    def test_concurrent_commands_rejected(self):
        self.ns['_cancel_lock'].acquire()
        self.assertEqual(self.post().status_code,409)
        self.control.assert_not_called()
    def test_get_never_controls_printer(self):
        self.assertEqual(self.client.get('/api/cancel').status_code,405)
        self.control.assert_not_called()

if __name__=='__main__': unittest.main()
