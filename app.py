#!/usr/bin/env python3
import atexit
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
import pty
import queue
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid

from flask import Flask, abort, redirect, render_template, request, url_for
from flask_sock import Sock
from simple_websocket import ConnectionClosed

BASE = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


class Session:
    def __init__(self, manager):
        self.manager = manager
        self.sid = uuid.uuid4().hex
        self.root = manager.directory / self.sid
        # ready also wakes create() when startup fails
        self.stop = threading.Event()
        self.ready = threading.Event()
        # only one websocket can own a terminal at a time
        self.attached = threading.Lock()
        # bounded queues keep a slow peer from growing memory without limit
        self.input = queue.Queue(maxsize=256)
        self.output = queue.Queue(maxsize=256)
        self.error = None
        self.thread = threading.Thread(target=self.run, name=f'chroot-{self.sid}', daemon=True)

    def run(self):
        process = None
        master = slave = None
        try:
            self.root.mkdir(parents=True, mode=0o700)
            # each session gets its own unpacked root filesystem
            if not self.extract():
                return
            master, slave = pty.openpty()
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
            # unshare starts the child with separate user, mount, and PID namespaces
            process = subprocess.Popen(
                ['unshare', '--user', '--map-root-user', '--mount', '--pid',
                 '--fork', '--kill-child=SIGKILL', sys.executable,
                 str(BASE / 'terminal_child.py'), str(self.root)],
                stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                     'TERM': 'xterm-256color', 'HOME': '/root', 'LANG': 'C.UTF-8'},
            )
            os.close(slave)
            slave = None
            os.set_blocking(master, False)
            self.ready.set()
            deadline = time.monotonic() + 60
            pending = bytearray()
            while not self.stop.is_set():
                # expire sessions that never connect or have lost their websocket
                if not self.attached.locked() and time.monotonic() > deadline:
                    break
                try:
                    kind, value = self.input.get_nowait() if not pending else (None, None)
                except queue.Empty:
                    kind, value = None, None
                if kind == 'input':
                    pending.extend(value.encode('utf-8'))
                elif kind == 'resize':
                    cols, rows = value
                    fcntl.ioctl(master, termios.TIOCSWINSZ,
                                struct.pack('HHHH', rows, cols, 0, 0))
                # pause PTY reads while output is backed up; retry partial writes
                readable, writable, _ = select.select(
                    [master] if not self.output.full() else [],
                    [master] if pending else [], [], 0.02)
                if writable:
                    try:
                        count = os.write(master, pending)
                        del pending[:count]
                    except BlockingIOError:
                        pass
                if readable:
                    try:
                        data = os.read(master, 16384)
                    except OSError as exc:
                        if exc.errno == errno.EIO:  # linux pty ended
                            break
                        raise
                    if not data:
                        break
                    self.output.put_nowait(data)
                if process.poll() is not None and not readable and not self.output.full():
                    break
        except Exception as exc:
            self.error = str(exc)
            log.exception('Session %s failed', self.sid)
        finally:
            # Killing unshare kills PID 1 and every descendant in its namespace,
            # including jobs which created their own process group or session.
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)
            try:
                if self.root.exists():
                    # Shell commands may have removed directory permissions.
                    self.root.chmod(0o700)
                    for parent, directories, _ in os.walk(self.root):
                        for name in directories:
                            path = Path(parent) / name
                            if not path.is_symlink():
                                path.chmod(0o700)
                    shutil.rmtree(self.root)
            except Exception as exc:
                self.error = f'Cleanup failed: {exc}'
                log.exception('Could not remove %s', self.root)
            self.ready.set()
            with self.manager.lock:
                self.manager.sessions.pop(self.sid, None)

    def extract(self):
        # use native tar, faster than python lib
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ['tar', '--extract', '--gzip', '--file', str(self.manager.archive),
                 '--directory', str(self.root), '--no-same-owner', '--no-same-permissions'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    # extraction can be cancelled before the shell starts
                    if self.stop.wait(0.02):
                        return False
                if process.returncode:
                    errors.seek(0)
                    raise RuntimeError(errors.read(4096).decode(errors='replace').strip())
                return not self.stop.is_set()
            finally:
                if process.poll() is None:
                    # tar runs in its own process group for cancellation
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()

    def close(self):
        # stop thread cooperatively, don't kill
        self.stop.set()
        self.thread.join()


class Sessions:
    def __init__(self, archive, directory):
        self.archive = Path(archive)
        self.directory = Path(directory)
        self.sessions = {}
        self.lock = threading.Lock()

    def create(self):
        session = Session(self)
        with self.lock:
            self.sessions[session.sid] = session
            session.thread.start()
        # wait until the PTY is usable or startup reports an error
        session.ready.wait()
        if session.error:
            session.close()
            raise RuntimeError(session.error)
        return session

    def get(self, sid):
        with self.lock:
            return self.sessions.get(sid)

    def close(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop.set()
        for session in sessions:
            session.close()


def create_app(archive=None, directory=None):
    app = Flask(__name__)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25, 'max_message_size': 65536}
    sessions = Sessions(archive or BASE / 'chroot-host.tgz', directory or BASE / 'sessions')
    app.extensions['sessions'] = sessions
    sock = Sock(app)

    @app.before_request
    def same_origin():
        # reject browser requests arriving from a different site
        origin = request.headers.get('Origin')
        if (origin and origin != request.host_url.rstrip('/')) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            abort(403)

    @app.get('/')
    def index():
        return redirect(url_for('new'))

    @app.get('/new')
    def new():
        try:
            session = sessions.create()
        except (OSError, RuntimeError):
            app.logger.exception('Could not create chroot')
            abort(500, 'Could not create chroot; check the server log.')
        return redirect(url_for('connect', sid=session.sid))

    @app.get('/connect/<sid>')
    def connect(sid):
        if sessions.get(sid) is None:
            abort(404)
        return render_template('terminal.html', sid=sid)

    @app.route('/disconnect/<sid>', methods=['GET', 'POST'])
    def disconnect(sid):
        session = sessions.get(sid)
        if session is not None:
            session.close()
            if session.error:
                abort(500, session.error)
        return '', 204

    @sock.route('/ws/<sid>')
    def terminal(ws, sid):
        session = sessions.get(sid)
        if session is None:
            ws.close(reason=1008, message='Unknown session')
            return
        if not session.attached.acquire(blocking=False):
            ws.close(reason=1008, message='Session already connected')
            return
        try:
            while True:
                # drain some PTY output before waiting for the next client message
                for _ in range(16):
                    try:
                        ws.send(session.output.get_nowait())
                    except queue.Empty:
                        break
                if not session.thread.is_alive() and session.output.empty():
                    if session.error:
                        ws.close(reason=1011, message='Session failed; check server log')
                    break
                raw = ws.receive(timeout=0.02)
                if raw is None:
                    continue
                try:
                    # the client sends only text input and bounded terminal sizes
                    message = json.loads(raw)
                    kind = message['type']
                    if kind == 'input' and isinstance(message['data'], str):
                        item = ('input', message['data'])
                    elif kind == 'resize' and all(
                        type(message[key]) is int and 1 <= message[key] <= 1000
                        for key in ('cols', 'rows')
                    ):
                        item = ('resize', (message['cols'], message['rows']))
                    else:
                        raise ValueError('Invalid message')
                    session.input.put_nowait(item)
                except queue.Full:
                    ws.close(reason=1009, message='Terminal input queue is full')
                    break
                except (ValueError, KeyError, TypeError):
                    ws.close(reason=1008, message='Invalid terminal message')
                    break
        except ConnectionClosed:
            pass
        finally:
            # disconnecting the websocket also tears down its shell
            session.close()
            session.attached.release()
            try:
                ws.close()
            except ConnectionClosed:
                pass

    return app


app = create_app()
atexit.register(app.extensions['sessions'].close)

if __name__ == '__main__':
    def shutdown(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)
    finally:
        app.extensions['sessions'].close()
