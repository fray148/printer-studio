#!/usr/bin/env python3
"""Local-only UI preview with synthetic printer data. Never connects to a printer."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
TEMP=tempfile.TemporaryDirectory(prefix='pro5-review-')
os.environ['DATA_DIR']=TEMP.name
os.environ['DASHBOARD_BACKGROUND']='0'
os.environ['PRINTER_HOST']='192.168.1.98'
import app as dashboard

image=Path(TEMP.name)/'frame.jpg'
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',
                'color=c=0x1c3353:s=640x480','-frames:v','1','-threads','1',str(image)],check=True)
class DemoCamera:
    def __init__(self):
        self.frame=image.read_bytes();self.seq=1;self.cond=threading.Condition();self.viewers=0
    def join(self):self.viewers+=1
    def leave(self):self.viewers=max(0,self.viewers-1)
    def snapshot(self,timeout=20):return self.frame
camera=DemoCamera()
dashboard._cam=camera;dashboard.automation.camera=camera
# Everything the old dashboard might call is redirected to fixed demo values.
detail=dict(status='printing',name='Creator 5 Pro',firmwareVersion='2.0.0',printFileName='Корпус · PETG.gcode.3mf',
            printProgress=.64,printLayer=128,targetPrintLayer=200,printDuration=4800,estimatedTime=2700,
            nozzleTemps=[238,29,28,30],nozzleTargetTemps=[240,0,0,0],platTemp=79,platTargetTemp=80,
            chamberTemp=35,chamberTargetTemp=0,lightStatus='open',remainingDiskSpace=12.8,cumulativePrintTime=8450,
            matlStationInfo=dict(currentSlot=1,slotCnt=4,slotInfos=[dict(slotId=1,materialName='PETG',materialColor='#4c9aff',hasFilament=True)]))
dashboard.fetch_detail=lambda:(detail,None)
dashboard.ff_post=lambda *a,**k:dict(code=0,detail=detail)
dashboard.ff_control=lambda *a,**k:(True,{})
dashboard.requests.get=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Network disabled in preview'))
dashboard.requests.post=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Network disabled in preview'))
dashboard._cache.update(detail=detail,ts=time.time(),error=None)
a=dashboard.automation
for label in ['Тестовый корпус · PETG','Крепление камеры · PLA']:
    ident=a.start_recording(label)
    for _ in range(3):a.next_frame=0;a.capture_tick()
    a.stop_recording();a.render(ident)
if __name__=='__main__':
    dashboard.app.run(host='127.0.0.1',port=55011,use_reloader=False)
