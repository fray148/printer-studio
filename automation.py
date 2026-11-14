"""Local timelapse archive and opt-in Telegram integration; no printer controls."""
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

import requests

LOG = logging.getLogger(__name__)
DEFAULTS = dict(auto_record=False, interval=10, fps=25, retention_days=30,
                storage_gb=10, min_free_gb=2, telegram_enabled=False,
                bot_token='', chat_id='', send_video=True, notify_progress=25)
TERMINAL = {'completed', 'finished', 'cancelled', 'canceled', 'error', 'failed',
            'idle', 'ready', 'standby'}
PAUSED = {'paused', 'pause', 'pausing'}


class AutomationError(Exception):
    pass


class Automation:
    def __init__(self, directory, status, camera, start=False):
        self.root = Path(directory) / 'automation'
        self.root.mkdir(parents=True, exist_ok=True)
        self.media = self.root / 'recordings'
        self.media.mkdir(exist_ok=True)
        self.db = self.root / 'archive.sqlite3'
        self.settings_path = self.root / 'settings.json'
        self.lock = threading.RLock()
        self.camera_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.status_provider, self.camera = status, camera
        self.last_error = ''
        self.bot_error = ''
        self.bot_name = ''
        self.config = DEFAULTS.copy()
        if self.settings_path.exists():
            self.config.update(json.loads(self.settings_path.read_text()))
        with self.connect() as con:
            con.executescript('''
              CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY, label TEXT, status TEXT, created REAL,
                finished REAL, frames INTEGER DEFAULT 0, error TEXT DEFAULT '',
                source TEXT, fps INTEGER, interval INTEGER, size INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
              CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created REAL NOT NULL,
                kind TEXT NOT NULL, message TEXT NOT NULL, file TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, kind TEXT, body TEXT, media TEXT,
                chat TEXT, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0);
            ''')
            if 'details' not in {row[1] for row in con.execute('PRAGMA table_info(events)')}:
                con.execute("ALTER TABLE events ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")
        os.chmod(self.db, 0o600)
        self.active = None
        self.next_frame = 0
        self.render_lock = threading.Lock()
        self.leased = False
        self.quota_bytes = 0
        self.quota_checked = 0
        # Keep interrupted captures; retry interrupted encodes on startup.
        self.pending = []
        with self.connect() as con:
            for row in con.execute("SELECT * FROM recordings WHERE status IN ('recording','encoding','queued') ORDER BY created"):
                r = dict(row)
                if r['status'] == 'recording' and self.active is None:
                    self.active = r
                    self.active['frames'] = len(list((self.media/r['id']).glob('*.jpg')))
                    con.execute('UPDATE recordings SET frames=? WHERE id=?',(self.active['frames'],r['id']))
                else:
                    con.execute("UPDATE recordings SET status='queued' WHERE id=?", (r['id'],))
                    self.pending.append(r['id'])
        self.last_state = self.get_state('printer', {})
        self.last_cleanup = 0
        if start:
            for target in (self.loop, self.telegram_loop):
                threading.Thread(target=target, daemon=True).start()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def get_state(self, key, default=None):
        with self.connect() as con:
            row = con.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.connect() as con:
            con.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))

    def settings(self):
        with self.lock:
            public = {k:v for k,v in self.config.items() if k != 'bot_token'}
            public.update(token_configured=bool(self.config['bot_token']), bot_name=self.bot_name,
                          bot_error=self.bot_error, last_error=self.last_error)
            return public

    def save_settings(self, values):
        if not isinstance(values, dict):
            raise AutomationError('Ожидается объект настроек.')
        with self.lock:
            new = self.config.copy()
            for key in ('auto_record','telegram_enabled','send_video'):
                if key in values:
                    if type(values[key]) is not bool:
                        raise AutomationError('Некорректное значение: '+key)
                    new[key] = values[key]
            limits = dict(interval=(5,3600), fps=(10,60), retention_days=(1,365),
                          storage_gb=(1,500), min_free_gb=(1,100), notify_progress=(0,100))
            for key,(low,high) in limits.items():
                if key in values:
                    value = values[key]
                    if type(value) is not int or not low <= value <= high:
                        raise AutomationError(f'{key}: допустимо {low}–{high}.')
                    new[key] = value
            if 'chat_id' in values:
                chat = str(values['chat_id']).strip()
                if chat and not re.fullmatch(r'-?\d{1,20}',chat):
                    raise AutomationError('chat_id должен быть числом.')
                new['chat_id'] = chat
            token = str(values.get('bot_token','')).strip()
            if token:
                if not re.fullmatch(r'\d{5,20}:[A-Za-z0-9_-]{20,}',token):
                    raise AutomationError('Проверь токен BotFather.')
                new['bot_token'] = token
            if values.get('clear_token') is True:
                new['bot_token'] = ''
            if new['telegram_enabled'] and not (new['bot_token'] and new['chat_id']):
                raise AutomationError('Для включения бота нужны токен и chat_id.')
            auto_enabled = new['auto_record'] and not self.config['auto_record']
            credentials_changed = any(new[k] != self.config[k] for k in ('bot_token','chat_id','telegram_enabled'))
            tmp = self.settings_path.with_suffix('.tmp')
            fd = os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(new,f)
                f.flush(); os.fsync(f.fileno())
            tmp.replace(self.settings_path)
            self.config = new
            if auto_enabled:self.set_state('auto_suppressed',False)
            if credentials_changed:
                self.bot_error = ''; self.bot_name = ''
                self.set_state('telegram_offset',0)
                self.set_state('telegram_ready',False)
                with self.connect() as con:
                    con.execute('DELETE FROM outbox')
            if not new['auto_record'] and self.active and self.active['source'] == 'auto':
                self.finish('Автозапись отключена.')
        return self.settings()

    @contextmanager
    def frame_source(self):
        self.camera.join()
        try:
            yield
        finally:
            self.camera.leave()

    def snapshot(self):
        with self.frame_source():
            return self.camera.snapshot(timeout=20)

    def check_space(self):
        if shutil.disk_usage(self.root).free < self.config['min_free_gb']*1024**3:
            raise AutomationError('Недостаточно свободного места. Запись остановлена.')
        if time.monotonic()-self.quota_checked > 30:
            self.quota_bytes = sum(p.stat().st_size for p in self.media.rglob('*') if p.is_file())
            self.quota_checked = time.monotonic()
        if self.quota_bytes >= self.config['storage_gb']*1024**3:
            raise AutomationError('Достигнут лимит архива. Удали старые записи или увеличь лимит.')

    def start_recording(self, label='', source='manual'):
        with self.lock:
            if self.active:
                raise AutomationError('Запись уже идёт.')
            self.check_space()
            ident = uuid.uuid4().hex
            directory = self.media / ident
            directory.mkdir()
            row = dict(id=ident,label=str(label).strip()[:200] or 'Ручной таймлапс',status='recording',
                       created=time.time(),source=source,fps=self.config['fps'],interval=self.config['interval'],frames=0)
            with self.connect() as con:
                con.execute('INSERT INTO recordings (id,label,status,created,source,fps,interval) VALUES (:id,:label,:status,:created,:source,:fps,:interval)',row)
            self.active = row; self.next_frame = 0; self.last_error = ''
            return ident

    def finish(self, reason=''):
        with self.lock:
            if not self.active:
                raise AutomationError('Активной записи нет.')
            ident = self.active['id']
            with self.connect() as con:
                con.execute("UPDATE recordings SET status='queued',finished=?,error=? WHERE id=?",(time.time(),reason,ident))
            self.pending.append(ident)
            self.active = None
            if self.leased:
                self.camera.leave(); self.leased = False
            return ident

    def stop_recording(self):
        with self.lock:
            if not self.active:
                raise AutomationError('Активной записи нет.')
            self.set_state('auto_suppressed',True)
            return self.finish('Остановлено вручную.')

    def recordings(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM recordings ORDER BY created DESC')]

    def recording(self, ident):
        if not re.fullmatch(r'[a-f0-9]{32}',ident):
            raise AutomationError('Запись не найдена.')
        with self.connect() as con:
            r = con.execute('SELECT * FROM recordings WHERE id=?',(ident,)).fetchone()
        if not r:
            raise AutomationError('Запись не найдена.')
        return dict(r)

    def delete(self, ident):
        with self.lock:
            row = self.recording(ident)
            if row['status'] in ('recording','encoding','queued'):
                raise AutomationError('Сначала останови запись и дождись сборки.')
            shutil.rmtree(self.media/ident,ignore_errors=False)
            with self.connect() as con:
                con.execute('DELETE FROM recordings WHERE id=?',(ident,))
                con.execute('DELETE FROM outbox WHERE media=?',(ident,))
            self.quota_checked = 0

    def capture_tick(self):
        with self.lock:
            if not self.active or time.monotonic() < self.next_frame:
                return
            self.check_space()
            if not self.leased:
                self.camera.join(); self.leased = True
            ident = self.active['id']
            # Camera runs in its own thread; keep one upstream leased until stop.
            frame = self.camera.snapshot(timeout=20)
            if not frame.startswith(b'\xff\xd8') or not frame.endswith(b'\xff\xd9') or len(frame)>8*1024**2:
                raise AutomationError('Камера вернула некорректный JPEG.')
            directory = self.media/ident
            index = self.active['frames']
            temp = directory/'frame.tmp'
            temp.write_bytes(frame)
            temp.replace(directory/f'{index:08d}.jpg')
            self.active['frames'] += 1
            self.quota_bytes += len(frame)
            with self.connect() as con:
                con.execute('UPDATE recordings SET frames=? WHERE id=?',(self.active['frames'],ident))
            self.next_frame = time.monotonic()+self.active['interval']

    def render(self, ident):
        row = self.recording(ident); directory = self.media/ident
        frames = sorted(directory.glob('*.jpg'))
        if len(frames)<2:
            with self.connect() as con:
                con.execute("UPDATE recordings SET status='failed',error=? WHERE id=?",('Для видео нужно хотя бы два кадра.',ident))
            return
        # Reconcile a frame written just before a process/container restart.
        with self.connect() as con:
            con.execute("UPDATE recordings SET status='encoding',frames=? WHERE id=?",(len(frames),ident))
        temporary = directory/'render.mp4'
        command = ['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',
                   '-threads','1','-framerate',str(row['fps']),'-i',str(directory/'%08d.jpg'),
                   '-vf',"scale=w='min(1280,iw)':h=-2:out_range=tv,format=yuv420p",'-c:v','libx264',
                   '-threads','1','-filter_threads','1','-pix_fmt','yuv420p','-color_range','tv','-preset','veryfast','-crf','24',
                   '-movflags','+faststart',str(temporary)]
        try:
            self.check_space()
            subprocess.run(command,check=True,capture_output=True,timeout=3600)
            temporary.replace(directory/'video.mp4')
            size=(directory/'video.mp4').stat().st_size
            with self.connect() as con:
                con.execute("UPDATE recordings SET status='ready',size=? WHERE id=?",(size,ident))
            self.enqueue('message','Таймлапс готов: '+row['label'])
            if self.config['send_video']:
                self.enqueue('video',row['label'],ident)
        except (subprocess.SubprocessError,OSError,AutomationError):
            with self.connect() as con:
                con.execute("UPDATE recordings SET status='failed',error=? WHERE id=?",('Сборка видео не удалась. Исходные кадры сохранены.',ident))
        finally:
            temporary.unlink(missing_ok=True)
            self.quota_checked=0

    def retry_render(self, ident):
        with self.lock:
            row=self.recording(ident)
            if row['status']!='failed':
                raise AutomationError('Повторная сборка доступна для неудачных записей.')
            with self.connect() as con:
                con.execute("UPDATE recordings SET status='queued',error='' WHERE id=?",(ident,))
            self.pending.append(ident)

    def render_next(self):
        if not self.pending or not self.render_lock.acquire(blocking=False):
            return
        with self.lock:
            ident=self.pending.pop(0)
        def work():
            try:
                self.render(ident)
            finally:
                self.render_lock.release()
        threading.Thread(target=work,daemon=True).start()

    def enqueue(self, kind, text, media=''):
        with self.lock:
            cfg=self.config.copy()
        if not cfg['telegram_enabled']:
            return
        with self.connect() as con:
            if con.execute('SELECT count(*) FROM outbox').fetchone()[0] >= 100:
                return
            con.execute('INSERT INTO outbox (kind,body,media,chat) VALUES (?,?,?,?)',
                        (kind,text[:4000],media,cfg['chat_id']))

    @staticmethod
    def status_text(status):
        job=status.get('job') or {};temps=status.get('temps') or {}
        def number(value):
            try:return f'{float(value):g}' if value is not None else '—'
            except (TypeError,ValueError):return '—'
        def duration(value):
            try:
                minutes=max(0,int(float(value)))//60
                return f'{minutes//60} ч {minutes%60} мин'
            except (TypeError,ValueError):return '—'
        names={'printing':'Печать','busy':'Занят','heating':'Нагрев',
               'ready':'Готов','idle':'Ожидание','completed':'Завершено',
               'paused':'Пауза','pausing':'Постановка на паузу','pause':'Пауза',
               'error':'Ошибка','failed':'Сбой','cancelled':'Отменено'}
        state=str(status.get('status','unknown'))
        lines=[f"{status.get('name') or 'Creator 5 Pro'} · {names.get(state,state)}",
               str(job.get('file') or 'Нет задания')[:500],
               f"Прогресс: {number(job.get('progress'))}% · слой {number(job.get('layer'))}/{number(job.get('layer_total'))}",
               f"Прошло: {duration(job.get('elapsed_s'))} · осталось: {duration(job.get('remaining_s'))}",
               f"Скорость: {number(job.get('speed_mm_s'))} мм/с · {number(job.get('speed_pct'))}%",
               '', 'Температуры (текущая → заданная):']
        nozzles=status.get('nozzles') or []
        for nozzle in nozzles[:4]:
            lines.append(f"Сопло {number(nozzle.get('id'))}: {number(nozzle.get('temp'))} → {number(nozzle.get('target'))} °C")
        if not nozzles:
            lines.append(f"Сопло: {number(temps.get('nozzle'))} → {number(temps.get('nozzle_target'))} °C")
        lines.extend([f"Стол: {number(temps.get('bed'))} → {number(temps.get('bed_target'))} °C",
                      f"Камера: {number(temps.get('chamber'))} °C"])
        slots=(status.get('material_station') or {}).get('slots') or []
        if slots:
            lines.extend(['','Филаменты:'])
            for slot in slots[:4]:
                active=' · активен' if slot.get('active') else ''
                material=str(slot.get('material') or 'не указан')[:60]
                loaded='загружен' if slot.get('loaded') else 'не загружен'
                lines.append(f"Слот {number(slot.get('id'))}: {material}, {loaded}{active}")
        if job.get('filament_g') is not None:
            lines.append(f"Расход: ≈{number(job['filament_g'])} г · {number(job.get('filament_m'))} м")
        machine=status.get('machine') or {}
        if machine.get('disk_free_gb') is not None:
            lines.append(f"Память принтера: свободно {number(machine['disk_free_gb'])} ГБ")
        for key,title in (('door','Дверца'),('lid','Крышка')):
            if machine.get(key) is not None:
                value=str(machine[key]);lines.append(title+': '+{'open':'открыта','close':'закрыта','closed':'закрыта'}.get(value,value))
        error=str(status.get('error_code') or '')
        if error not in ('','0','none'):lines.append('Ошибка: '+error[:200])
        if status.get('_stale'):lines.insert(0,'⚠ Данные устарели: связь с принтером потеряна.')
        return '\n'.join(lines)[:4000]

    def log_event(self, kind, message, label='', details=None):
        with self.connect() as con:
            con.execute('INSERT INTO events(created,kind,message,file,details) VALUES(?,?,?,?,?)',
                        (time.time(),kind,message[:1000],label[:500],json.dumps(details or {})))
            con.execute('DELETE FROM events WHERE id <= (SELECT MAX(id)-10000 FROM events)')

    def events(self, before=None, kind=''):
        clauses=[];params=[]
        if before is not None:
            clauses.append('id < ?');params.append(before)
        if kind:
            clauses.append('kind = ?');params.append(kind)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.connect() as con:
            rows=[dict(row) for row in con.execute(
                'SELECT * FROM events'+where+' ORDER BY id DESC LIMIT 100',params)]
        for row in rows:
            row['details']=json.loads(row['details'])
        return rows

    @staticmethod
    def tool_details(slots, old, new, source='observed'):
        def entry(ident):
            slot=next((s for s in slots if str(s.get('id'))==str(ident)),{})
            color=str(slot.get('color') or '')
            return dict(id=ident,color=color if re.fullmatch(r'#[0-9a-fA-F]{6}',color) else None)
        return dict(from_tool=entry(old),to_tool=entry(new),color_source=source)

    def backfill_tool_colors(self, slots):
        count=0
        with self.connect() as con:
            for row in con.execute("SELECT id,message FROM events WHERE kind='tool' AND details='{}'").fetchall():
                match=re.fullmatch(r'Смена инструмента: (\d+) → (\d+)',row['message'])
                if not match:continue
                details=self.tool_details(slots,int(match[1]),int(match[2]),'current_slots')
                if not all(details[key]['color'] for key in ('from_tool','to_tool')):continue
                con.execute('UPDATE events SET details=? WHERE id=?',(json.dumps(details),row['id']))
                count+=1
        return count

    def observe(self, status):
        if not status or status.get('_stale'):
            since=self.get_state('connection_missing_since',0)
            if not since:
                self.set_state('connection_missing_since',time.time())
            elif time.time()-since>=30 and not self.get_state('connection_lost',False):
                self.log_event('connection','Потеряна связь с принтером')
                self.set_state('connection_lost',True)
            return
        if self.get_state('connection_missing_since',0):
            if self.get_state('connection_lost',False):
                self.log_event('connection','Связь с принтером восстановлена')
            self.set_state('connection_missing_since',0)
            self.set_state('connection_lost',False)
        state=status.get('status','unknown'); job=status.get('job',{}); label=str(job.get('file') or '').strip()
        printing=bool(status.get('printing')); previous=self.last_state
        # A maintenance cycle can report heating/busy/printing without a file.
        # Also discard legacy persisted false jobs from before this guard.
        was_printing=bool(previous.get('file')) and (previous.get('printing',False) or previous.get('status') in PAUSED)
        if not was_printing:
            printing=printing and bool(label) and state=='printing'
        if was_printing and state not in TERMINAL and not label:
            label=previous.get('file','')
        # Tool changes can temporarily report a non-printing, non-terminal
        # state. Keep the job alive until an explicit terminal status arrives.
        if was_printing and state not in TERMINAL and state not in PAUSED and label == previous.get('file'):
            printing=True
        if printing and (not was_printing or previous.get('file') != label):
            if self.active and self.active['source']=='auto' and self.active['label']!=label:
                self.finish('Началось другое задание.')
            self.enqueue('message','Началась печать: '+(label or 'Без названия'))
            self.log_event('print','Началась печать',label)
            self.set_state('progress',0)
            self.set_state('auto_suppressed',False)
        if printing and self.config['auto_record'] and not self.active and not self.get_state('auto_suppressed',False):
            try:
                self.start_recording(label or 'Печать',source='auto')
            except AutomationError as e:
                # Persist the observed job even when there is no disk space:
                # otherwise every poll repeats its start notification.
                self.last_error=str(e)
                self.set_state('auto_suppressed',True)
        if was_printing and previous.get('status') not in PAUSED and state in PAUSED:
            self.enqueue('message','Печать на паузе: '+label)
            self.log_event('pause','Печать на паузе',label)
        if printing and was_printing and previous.get('status') in PAUSED:
            self.log_event('pause','Печать возобновлена',label)
        if was_printing and state != previous.get('status') and state not in TERMINAL and state not in PAUSED and not status.get('printing'):
            self.log_event('state','Промежуточное состояние: '+str(state),label)
        slots=(status.get('material_station') or {}).get('slots') or []
        tools=[s.get('id') for s in slots if s.get('active') and s.get('id') is not None]
        tool=tools[0] if len(tools)==1 else None
        prior_tool=previous.get('tool') if previous.get('file')==label and was_printing else None
        if printing and tool is not None and prior_tool is not None and tool!=prior_tool:
            self.log_event('tool',f'Смена инструмента: {prior_tool} → {tool}',label,
                           self.tool_details(slots,prior_tool,tool))
        if was_printing and not printing and state in TERMINAL:
            event='Печать завершена' if state in ('completed','finished') else 'Печать остановлена: '+state
            self.enqueue('message',event+'\n'+label)
            self.log_event('print',event,label or previous.get('file',''))
            if self.active and self.active['source']=='auto':
                self.finish(event)
        error=str(status.get('error_code') or '')
        if error and error not in ('0','none') and error!=previous.get('error'):
            self.enqueue('message','Ошибка принтера: '+error)
            self.log_event('error','Ошибка принтера: '+error,label)
        if previous.get('error') and previous['error'] not in ('0','none') and error in ('','0','none'):
            self.log_event('error','Ошибка сброшена: '+previous['error'],label)
        step=self.config['notify_progress'];progress=float(job.get('progress',0) or 0)
        if printing and step and progress>=self.get_state('progress',0)+step:
            self.enqueue('message',f'Печать: {progress:.0f}%\n{label}')
            self.set_state('progress',int(progress//step)*step)
        self.last_state=dict(status=state,printing=printing,file=label,error=error,
                             tool=(tool if tool is not None else prior_tool) if printing or state in PAUSED else None)
        if self.last_state!=previous:
            self.set_state('printer',self.last_state)

    def cleanup(self):
        cutoff=time.time()-self.config['retention_days']*86400
        with self.connect() as con:
            expired=[r[0] for r in con.execute(
                "SELECT id FROM recordings WHERE status IN ('ready','failed') AND COALESCE(finished,created)<?",(cutoff,))]
        for ident in expired:
            self.delete(ident)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.observe(self.status_provider())
                self.capture_tick()
                self.render_next()
                if time.monotonic()-self.last_cleanup>3600:
                    self.cleanup();self.last_cleanup=time.monotonic()
            except AutomationError as e:
                self.last_error=str(e)
                # Keep frames, stop acquisition and permit archive rendering.
                if self.active:
                    self.set_state('auto_suppressed',True)
                    self.finish(self.last_error)
                self.render_next()
            except Exception as e:
                self.last_error='Временная ошибка автоматизации: '+type(e).__name__
                LOG.warning('Automation iteration failed: %s',type(e).__name__)
            self.stop_event.wait(2)

    def telegram_api(self, cfg, method, data=None, files=None, timeout=20):
        try:
            response=requests.post('https://api.telegram.org/bot'+cfg['bot_token']+'/'+method,
                                   data=data or {},files=files,timeout=(5,timeout))
            result=response.json()
        except (requests.RequestException,ValueError):
            raise AutomationError('Telegram недоступен. Проверь сеть и настройки.') from None
        if not result.get('ok'):
            # Do not expose exception URLs (contain bot token) or upstream text.
            code=result.get('error_code',response.status_code)
            raise AutomationError('Telegram отклонил запрос (код '+str(code)+').')
        return result['result']

    def command(self, cfg, message):
        if str(message.get('chat',{}).get('id','')) != cfg['chat_id']:
            return
        command=str(message.get('text','')).split(' ',1)[0].split('@',1)[0]
        if command in ('/start','/help'):
            self.enqueue('message','/status — подробный статус и фото\n/photo — снимок камеры\n/timelapse — последнее видео\nБез команд движения и нагрева.')
        elif command=='/status':
            status=self.status_provider()
            self.enqueue('message',self.status_text(status) if status and status.get('status') else 'Свежие данные принтера пока недоступны.')
            try:
                frame=self.snapshot()
                self.telegram_api(cfg,'sendPhoto',{'chat_id':cfg['chat_id'],
                    'caption':'Снимок камеры по запросу /status'},
                    {'photo':('camera.jpg',io.BytesIO(frame),'image/jpeg')})
            except AutomationError:
                self.enqueue('message','Фото недоступно: не удалось получить снимок или отправить его в Telegram.')
        elif command=='/photo':
            # Served by the bot thread: one snapshot, using the existing hub.
            frame=self.snapshot()
            self.telegram_api(cfg,'sendPhoto',{'chat_id':cfg['chat_id']}, {'photo':('camera.jpg',io.BytesIO(frame),'image/jpeg')})
        elif command=='/timelapse':
            latest=next((r for r in self.recordings() if r['status']=='ready'),None)
            if latest:self.enqueue('video',latest['label'],latest['id'])
            else:self.enqueue('message','Готовых таймлапсов пока нет.')

    def deliver(self,cfg):
        with self.connect() as con:
            row=con.execute('SELECT * FROM outbox WHERE next_try<=? ORDER BY id LIMIT 1',(time.time(),)).fetchone()
        if not row:return
        try:
            if row['chat']!=cfg['chat_id']:
                return
            if row['kind']=='video':
                path=self.media/row['media']/'video.mp4'
                if path.stat().st_size>49*1024**2:
                    self.telegram_api(cfg,'sendMessage',{'chat_id':row['chat'],'text':'Видео больше лимита отправки. Скачай его в архиве панели.'})
                else:
                    with path.open('rb') as f:
                        self.telegram_api(cfg,'sendVideo',{'chat_id':row['chat'],'caption':row['body'][:900],'supports_streaming':'true'}, {'video':('timelapse.mp4',f,'video/mp4')},timeout=120)
            else:self.telegram_api(cfg,'sendMessage',{'chat_id':row['chat'],'text':row['body']})
        except (AutomationError,OSError):
            attempts=row['attempts']+1
            with self.connect() as con:
                if attempts>=5:con.execute('DELETE FROM outbox WHERE id=?',(row['id'],))
                else:con.execute('UPDATE outbox SET attempts=?,next_try=? WHERE id=?',(attempts,time.time()+min(300,10*2**attempts),row['id']))
            self.bot_error='Сообщение не отправлено; будет повторная попытка (до 5).'
            return
        finally:
            if row['chat']!=cfg['chat_id']:
                with self.connect() as con:con.execute('DELETE FROM outbox WHERE id=?',(row['id'],))
        with self.connect() as con:con.execute('DELETE FROM outbox WHERE id=?',(row['id'],))

    def telegram_loop(self):
        while not self.stop_event.is_set():
            with self.lock:cfg=self.config.copy()
            if not cfg['telegram_enabled']:
                self.stop_event.wait(3);continue
            try:
                if not self.bot_name:
                    me=self.telegram_api(cfg,'getMe')
                    with self.lock:
                        if cfg!=self.config:continue
                        self.bot_name=me.get('username','')
                if not self.get_state('telegram_ready',False):
                    # Expire pre-activation commands rather than replay old requests.
                    updates=self.telegram_api(cfg,'getUpdates',{'offset':-1,'timeout':0})
                    self.set_state('telegram_offset',updates[-1]['update_id']+1 if updates else 0)
                    self.set_state('telegram_ready',True)
                self.deliver(cfg)
                updates=self.telegram_api(cfg,'getUpdates',{'offset':self.get_state('telegram_offset',0),'timeout':10,'allowed_updates':'["message"]'})
                # Ignore replies from an in-flight request after credentials change.
                with self.lock:
                    if cfg!=self.config:continue
                for update in updates:
                    # Persist before executing: reconnect does not replay commands.
                    self.set_state('telegram_offset',update['update_id']+1)
                    self.command(cfg,update.get('message',{}))
                self.bot_error=''
            except AutomationError as e:
                self.bot_error=str(e);self.stop_event.wait(15)
            except Exception as e:
                self.bot_error='Ошибка Telegram: '+type(e).__name__;self.stop_event.wait(15)
