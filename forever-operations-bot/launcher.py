"""Loopback-only token handoff. No token files, command-line arguments or request logs."""
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import urllib.parse

ROOT=Path(__file__).resolve().parent
NONCE=secrets.token_urlsafe(32)
PROC=None
RUNTIME_ENV=None
STATE='ожидает токен'
DIAGNOSTICS=[]
LOCK=threading.Lock()

def monitor(proc):
    global STATE
    for line in proc.stdout:
        if proc is not PROC: continue
        # Only whitelisted status markers leave the subprocess boundary.
        if line.strip()=='FOREVER_READY':
            with LOCK: STATE='подключён'
        elif line.startswith('FOREVER_NOT_READY:'):
            marker=line.strip().split(':',1)[1]
            if marker.replace(':','').replace('_','').isalnum():
                with LOCK: STATE='ошибка: '+marker
        elif line.startswith('FOREVER_DIAGNOSTIC:'):
            marker=line.strip().split(':',1)[1]
            if len(marker)<150 and marker.replace(':','').replace('_','').isalnum():
                with LOCK:
                    DIAGNOSTICS.append(marker)
                    del DIAGNOSTICS[:-20]
    proc.wait()
    with LOCK:
        if proc is not PROC: return
        if STATE=='подключён': STATE='остановлен'
        elif STATE=='подключается': STATE='ошибка подключения; проверь токен и доступ'

PAGE='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Forever Operations — запуск</title><style>body{background:#151821;color:#e8edf8;font:17px system-ui;max-width:740px;margin:70px auto;padding:24px}h1{color:#89baff}input,button{font:inherit;padding:13px;border-radius:8px;border:1px solid #647696}input{width:92%;background:#232938;color:white}button{background:#79aefa;color:#101725;margin-top:16px;cursor:pointer}small{color:#b7c3d9}a{color:#a1c3ff}</style><h1>Forever Operations</h1><p>Локальный запуск бота для WOW forever team.</p><p>В <a href="https://discord.com/developers/applications/1563340261587820624/bot" target="_blank" rel="noopener noreferrer">Developer Portal → Бот</a> самостоятельно выпусти токен кнопкой «Сбросить токен», скопируй и вставь ниже.</p><form method="post" action="/start" autocomplete="off"><input type="hidden" name="nonce" value="NONCE"><label for="token">Токен бота — скрытый ввод</label><p><input id="token" name="token" type="password" required autocomplete="off" spellcheck="false" maxlength="300"></p><button>Запустить бота</button></form><p><small>Токен передаётся только локальному процессу на этом компьютере. В файл, чат и журнал он не записывается. Закрытие вкладки не останавливает процесс; сон и выключение компьютера остановят работу.</small></p><p><a href="/status">Проверить состояние</a></p></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,body,code=200,kind='text/html; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type',kind)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers();self.wfile.write(body.encode('utf-8'))
    def valid_host(self):
        return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
    def do_GET(self):
        if not self.valid_host(): self.send('Недопустимый адрес.',403);return
        if self.path=='/':
            self.send(PAGE.replace('NONCE',NONCE));return
        if self.path=='/health':
            with LOCK: data={'state':STATE,'running':PROC is not None and PROC.poll() is None,'diagnostics':list(DIAGNOSTICS)}
            self.send(json.dumps(data,ensure_ascii=False),kind='application/json; charset=utf-8');return
        if self.path=='/status':
            import html
            with LOCK: status=html.escape(STATE)
            self.send('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Forever Operations — статус</title><h1>Forever Operations</h1><p>Состояние: '+status+'</p><p>Обнови страницу для проверки. Если указана ошибка, сообщи только её код — без токена.</p><a href="/">Вернуться к запуску</a></html>');return
        self.send('Страница не найдена.',404)
    def do_POST(self):
        global PROC,STATE,RUNTIME_ENV
        # Embedded browsers may rewrite/omit Origin. CSRF protection is the
        # unpredictable per-process nonce below, not that optional header.
        # Exact loopback Host + no CORS + CSP prevent cross-origin nonce access.
        if not self.valid_host():
            self.send('Недопустимый адрес запроса.',403);return
        if self.path not in ('/start','/restart'):self.send('Не найдено.',404);return
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: length=0
        if not 1<=length<=2000:self.send('Некорректный запрос.',400);return
        data=urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'),max_num_fields=4)
        if not hmac.compare_digest(data.get('nonce',[''])[0],NONCE):
            self.send('Обнови страницу запуска.',403);return
        if self.path=='/restart':
            with LOCK:
                if RUNTIME_ENV is None:
                    self.send('Для первого запуска нужен токен.',409);return
                if PROC is not None and PROC.poll() is None:
                    PROC.terminate()
                    try: PROC.wait(timeout=5)
                    except subprocess.TimeoutExpired: PROC.kill();PROC.wait()
                DIAGNOSTICS.clear()
                PROC=subprocess.Popen([sys.executable,'-u',str(ROOT/'bot.py')],env=RUNTIME_ENV,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                STATE='подключается'
                threading.Thread(target=monitor,args=(PROC,),daemon=True).start()
            self.send_response(303);self.send_header('Location','/status');self.send_header('Cache-Control','no-store');self.end_headers()
            return
        token=data.get('token',[''])[0].strip()
        if not 20<=len(token)<=300 or any(ch.isspace() for ch in token):
            self.send('Нужен токен бота. Его значение не показывается.',400);return
        with LOCK:
            if PROC is not None and PROC.poll() is None:
                self.send('Бот уже запущен. <a href="/status">Состояние</a>',409);return
            env=os.environ.copy();env['DISCORD_BOT_TOKEN']=token
            # Memory only: permits supervised code reloads without writing secrets.
            RUNTIME_ENV=env.copy()
            PROC=subprocess.Popen([sys.executable,'-u',str(ROOT/'bot.py')],env=env,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            STATE='подключается'
            threading.Thread(target=monitor,args=(PROC,),daemon=True).start()
        del token,env,data
        self.send_response(303);self.send_header('Location','/status');self.send_header('Cache-Control','no-store');self.end_headers()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'LOCAL_LAUNCHER http://127.0.0.1:{server.server_port}',flush=True)
    try: server.serve_forever()
    finally:
        server.server_close()
        if PROC is not None and PROC.poll() is None: PROC.terminate()
