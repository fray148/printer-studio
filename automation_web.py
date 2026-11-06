"""Dashboard routes for local automation. Secret values never leave the server."""
from functools import wraps
import io
import os
import secrets
import json
import queue
import threading
import zipfile
from flask import Response, jsonify, render_template, request, send_file, session
from automation import AutomationError


def register_automation(app, manager):
    key = manager.root / 'session.key'
    try:
        fd = os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd,'w') as f:
            f.write(secrets.token_hex(32))
    app.secret_key = key.read_text()
    app.config.update(SESSION_COOKIE_NAME='printer_studio_session',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      MAX_CONTENT_LENGTH=1024*1024)

    def csrf():
        if 'csrf' not in session:
            session['csrf']=secrets.token_hex(32)
        return session['csrf']

    def protected(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            expected=session.get('csrf','')
            if not expected or not secrets.compare_digest(expected,request.headers.get('X-CSRF-Token','')):
                return jsonify(error='Обнови страницу и повтори действие.'),403
            try:
                return fn(*args,**kwargs)
            except AutomationError as e:
                return jsonify(error=str(e)),400
        return wrapped

    def json_body():
        body=request.get_json(silent=True)
        if not isinstance(body,dict):
            raise AutomationError('Ожидается JSON-объект.')
        return body

    @app.after_request
    def automation_headers(response):
        if request.path.startswith(('/api/automation','/automation','/bot')):
            response.headers['Cache-Control']='no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            response.headers['Content-Security-Policy']="frame-ancestors 'self'"
        return response

    @app.get('/automation')
    @app.get('/bot')
    def automation_page():
        return render_template('automation.html',csrf=csrf(),tab='bot' if request.path=='/bot' else 'timelapse')

    @app.get('/api/automation')
    def automation_state():
        return jsonify(settings=manager.settings(), recordings=manager.recordings(),
                       active=manager.active['id'] if manager.active else None)

    @app.post('/api/automation/settings')
    @protected
    def automation_settings():
        return jsonify(settings=manager.save_settings(request.get_json(silent=True)))

    @app.post('/api/automation/start')
    @protected
    def automation_start():
        body=json_body()
        return jsonify(id=manager.start_recording(body.get('label','')))

    @app.post('/api/automation/stop')
    @protected
    def automation_stop():
        return jsonify(id=manager.stop_recording())

    @app.post('/api/automation/telegram-test')
    @protected
    def telegram_test():
        if not manager.config['telegram_enabled']:
            raise AutomationError('Сначала настрой и включи Telegram.')
        manager.enqueue('message','Creator 5 Pro: бот подключён к панели.')
        return jsonify(ok=True)

    @app.delete('/api/automation/recordings/<ident>')
    @protected
    def automation_delete(ident):
        manager.delete(ident)
        return jsonify(ok=True)

    @app.post('/api/automation/recordings/<ident>/retry')
    @protected
    def automation_retry(ident):
        manager.retry_render(ident)
        return jsonify(ok=True)

    @app.get('/api/automation/recordings/<ident>/video')
    def automation_video(ident):
        try:
            row=manager.recording(ident)
        except AutomationError:
            return jsonify(error='Запись не найдена.'),404
        if row['status']!='ready':
            return jsonify(error='Видео пока не готово.'),409
        return send_file(manager.media/ident/'video.mp4',mimetype='video/mp4',conditional=True,
                         as_attachment=request.args.get('download')=='1',
                         download_name='timelapse-'+ident[:8]+'.mp4')

    @app.get('/api/automation/snapshot')
    def automation_snapshot():
        try:
            frame=manager.snapshot()
        except AutomationError as e:
            return jsonify(error=str(e)),503
        return send_file(io.BytesIO(frame),mimetype='image/jpeg',max_age=0)

    @app.post('/api/automation/recordings/<ident>/rename')
    @protected
    def automation_rename(ident):
        label=str(json_body().get('label','')).strip()
        if not label or len(label)>200:
            raise AutomationError('Название должно содержать от 1 до 200 символов.')
        with manager.lock:
            row=manager.recording(ident)
            if row['status'] in ('recording','encoding','queued'):
                raise AutomationError('Дождись окончания записи и сборки.')
            with manager.connect() as con:
                con.execute('UPDATE recordings SET label=? WHERE id=?',(label,ident))
        return jsonify(ok=True)

    @app.get('/api/automation/recordings/<ident>/info')
    def automation_info(ident):
        try:
            row=manager.recording(ident)
        except AutomationError:
            return jsonify(error='Запись не найдена.'),404
        return Response(json.dumps(row,ensure_ascii=False,indent=2),mimetype='application/json',
                        headers={'Content-Disposition':f'attachment; filename="timelapse-{ident[:8]}.json"'})

    @app.get('/api/automation/recordings/<ident>/poster')
    def automation_poster(ident):
        try:
            manager.recording(ident)
        except AutomationError:
            return '',404
        path=manager.media/ident/'00000000.jpg'
        if not path.is_file():return '',404
        return send_file(path,mimetype='image/jpeg',conditional=True)

    @app.get('/api/automation/recordings/<ident>/frames.zip')
    def automation_frames(ident):
        try:
            row=manager.recording(ident)
        except AutomationError:
            return jsonify(error='Запись не найдена.'),404
        if row['status'] in ('recording','encoding','queued'):
            return jsonify(error='Дождись окончания записи и сборки.'),409
        # JPEGs are already compressed: store without recompression, stream in RAM.
        # Bounded queue and cancellation prevent abandoned downloads filling memory.
        chunks=queue.Queue(maxsize=8)
        cancelled=threading.Event()
        class Sink:
            position=0
            def write(self,data):
                if cancelled.is_set():raise BrokenPipeError()
                if data:
                    while not cancelled.is_set():
                        try:chunks.put(bytes(data),timeout=.5);break
                        except queue.Full:pass
                    else:raise BrokenPipeError()
                self.position+=len(data)
                return len(data)
            def tell(self):return self.position
            def flush(self):pass
        def produce():
            try:
                with zipfile.ZipFile(Sink(),'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
                    for path in sorted((manager.media/ident).glob('*.jpg')):
                        archive.write(path,arcname=path.name)
                    archive.writestr('recording.json',json.dumps(row,ensure_ascii=False,indent=2))
            except (OSError,ValueError):
                pass
            finally:
                while not cancelled.is_set():
                    try:chunks.put(None,timeout=.5);break
                    except queue.Full:pass
        def stream():
            threading.Thread(target=produce,daemon=True).start()
            try:
                while True:
                    part=chunks.get()
                    if part is None:break
                    yield part
            finally:cancelled.set()
        return Response(stream(),mimetype='application/zip',headers={
            'Content-Disposition':f'attachment; filename="frames-{ident[:8]}.zip"',
            'X-Accel-Buffering':'no'})
