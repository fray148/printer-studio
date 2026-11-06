#!/usr/bin/env python3
"""Exercise a new installation using a disposable recording. No bot messages."""
import argparse
import http.cookiejar
import io
import json
from pathlib import Path
import re
import time
import urllib.request
import zipfile

parser=argparse.ArgumentParser()
parser.add_argument('phase',choices=['create','after-recreate'])
parser.add_argument('--base',default='http://127.0.0.1:5012')
args=parser.parse_args()
marker=Path(__file__).resolve().parents[1]/'data'/'.install-verification.json'
client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
csrf=''
def call(path,method='GET',body=None,headers=None):
    request_headers={'X-CSRF-Token':csrf,**(headers or {})}
    if body is not None:request_headers['Content-Type']='application/json'
    request=urllib.request.Request(args.base+path,data=json.dumps(body).encode() if body is not None else None,
                                   headers=request_headers,method=method)
    with client.open(request,timeout=30) as response:
        raw=response.read()
        return json.loads(raw) if response.headers.get_content_type()=='application/json' else raw

assert call('/healthz')['ok']
for path in ['/','/automation','/bot','/materials']:
    page=call(path)
    assert b'<html' in page
    if path=='/bot':csrf=re.search(rb'name="csrf-token" content="([^"]+)"',page)[1].decode()
state=call('/api/automation')
assert not state['settings']['telegram_enabled'],'Skip test: Telegram already enabled.'
assert 'bot_token' not in state['settings']
if args.phase=='create':
    assert not marker.exists(),'Previous verification marker exists.'
    assert not state['active'],'A user recording is active; do not interrupt it.'
    status=call('/api/status')
    assert not status.get('_stale') and not status.get('_error'),'Printer status unavailable.'
    print('Printer status:',status.get('status'),flush=True)
    ident=call('/api/automation/start','POST',dict(label='Проверка установки · временная запись'))['id']
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(json.dumps(dict(id=ident,interval=state['settings']['interval'])))
    deadline=time.monotonic()+55
    while time.monotonic()<deadline:
        records=call('/api/automation')['recordings']
        record=next(r for r in records if r['id']==ident)
        if record['frames']>=2:break
        assert record['status']=='recording',record
        time.sleep(2)
    else:raise AssertionError('Camera did not deliver two frames.')
    call('/api/automation/stop','POST',{})
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        record=next(r for r in call('/api/automation')['recordings'] if r['id']==ident)
        if record['status']=='ready':break
        assert record['status']!='failed',record
        time.sleep(1)
    else:raise AssertionError('Encoding did not finish.')
    base='/api/automation/recordings/'+ident
    assert b'ftyp' in call(base+'/video',headers={'Range':'bytes=0-31'})
    video=call(base+'/video?download=1')
    with zipfile.ZipFile(io.BytesIO(call(base+'/frames.zip'))) as archive:
        assert len([n for n in archive.namelist() if n.endswith('.jpg')])>=2
        assert archive.testzip() is None
    call(base+'/rename','POST',dict(label='Проверка установки · сохранение после пересоздания'))
    call('/api/automation/settings','POST',dict(interval=state['settings']['interval']+1))
    print('Live camera, MP4, Range, ZIP, metadata and rename: OK; frames:',record['frames'],'MP4 bytes:',len(video),flush=True)
else:
    saved=json.loads(marker.read_text());ident=saved['id'];base='/api/automation/recordings/'+ident
    assert state['settings']['interval']==saved['interval']+1,'Settings did not survive recreation.'
    record=call(base+'/info')
    assert record['status']=='ready' and record['frames']>=2,'Archive did not survive recreation.'
    assert b'ftyp' in call(base+'/video',headers={'Range':'bytes=0-31'})
    call('/api/automation/settings','POST',dict(interval=saved['interval']))
    call(base,'DELETE')
    assert all(r['id']!=ident for r in call('/api/automation')['recordings'])
    marker.unlink()
    print('Settings/archive survived recreation; original interval restored; test recording deleted.',flush=True)
