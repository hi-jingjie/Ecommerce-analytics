"""Local SQLBot services, owned entirely by the current OS user."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
BIN = ROOT / 'runtime/env/bin'
PGDATA = ROOT / 'data/postgresql-local'
LOGS = ROOT / 'logs'
STATE = ROOT / 'runtime/processes'


def environment():
    env = os.environ.copy()
    for line in (ROOT / '.env').read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            env[key] = value
    env.update({
        'PATH': str(BIN) + os.pathsep + env.get('PATH', ''),
        'PYTHONNOUSERSITE': '1',
        'PYTHONPATH': str(ROOT / 'sqlbot/backend'),
        'POSTGRES_SERVER': '127.0.0.1', 'POSTGRES_PORT': '15432',
        'POSTGRES_USER': 'sqlbot', 'POSTGRES_DB': 'sqlbot',
        'BASE_DIR': str(ROOT / 'sqlbot'),
        'SCRIPT_DIR': str(ROOT / 'runtime/scripts'),
        'UPLOAD_DIR': str(ROOT / 'data/sqlbot/file'),
        'EXCEL_PATH': str(ROOT / 'data/sqlbot/excel'),
        'MCP_IMAGE_PATH': str(ROOT / 'data/sqlbot/images'),
        'LOCAL_MODEL_PATH': str(ROOT / 'runtime/models'),
        'LOG_DIR': str(LOGS / 'app'),
        'FRONTEND_HOST': 'http://localhost:18000',
        'BACKEND_CORS_ORIGINS': 'http://localhost:18000,http://127.0.0.1:18000',
        'SERVER_IMAGE_HOST': 'http://localhost:18001/images/',
        'MCP_IMAGE_HOST': 'http://127.0.0.1:13000',
        'SQLBOT_CHART_HOST': '127.0.0.1', 'SQLBOT_CHART_PORT': '13000',
        'HF_HUB_OFFLINE': '1', 'OMP_NUM_THREADS': '4', 'MKL_NUM_THREADS': '4',
    })
    return env


def process_identity(pid):
    try:
        proc = Path('/proc') / str(pid)
        if proc.stat().st_uid != os.getuid():
            return None
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def running_pid(name):
    path = STATE / (name + '.json')
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data['started'] and data['started'] == process_identity(data['pid']):
        return data['pid']
    return None


def spawn(name, command, cwd):
    if running_pid(name):
        return
    with (LOGS / (name + '.log')).open('ab') as log:
        proc = subprocess.Popen(command, cwd=cwd, env=environment(),
                                stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    (STATE / (name + '.json')).write_text(json.dumps({
        'pid': proc.pid, 'started': process_identity(proc.pid),
    }))


def wait_http(name, url, timeout=180):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not running_pid(name):
            raise RuntimeError(f'{name} exited; see {LOGS / (name + ".log")}')
        try:
            with opener.open(url, timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError(f'{name} did not become ready; see {LOGS / (name + ".log")}')


def database(action):
    command = [str(BIN / 'pg_ctl'), '-D', str(PGDATA)]
    if action == 'start':
        check = subprocess.run(command + ['status'], capture_output=True)
        if check.returncode == 0:
            return
        command += ['-l', str(LOGS / 'postgresql.log'), '-w', 'start']
    elif action == 'stop':
        check = subprocess.run(command + ['status'], capture_output=True)
        if check.returncode != 0:
            return
        command += ['-m', 'fast', '-w', 'stop']
    else:
        command += ['status']
    subprocess.run(command, check=True)


def prepare_frontend():
    """Restore the installed upstream extension assets removed by Vite builds."""
    destination = ROOT / 'sqlbot/frontend/dist'
    if not (destination / 'index.html').is_file():
        raise RuntimeError('Frontend build missing; run local_runtime.py build_frontend first')
    lookup = subprocess.run([str(BIN / 'python'), '-c',
        "from importlib.util import find_spec; from pathlib import Path; "
        "print(Path(find_spec('sqlbot_xpack').origin).parent / 'static')"],
        env=environment(), capture_output=True, text=True, check=True)
    source = Path(lookup.stdout.strip())
    if not (source / 'license-generator.umd.js').is_file():
        raise RuntimeError('Official SQLBot extension assets missing; check the installed sqlbot-xpack package')
    shutil.copytree(source, destination / 'xpack_static', dirs_exist_ok=True)


def build_frontend():
    subprocess.run(['npm', 'run', 'build'], cwd=ROOT / 'sqlbot/frontend', env=environment(), check=True)
    prepare_frontend()
    print('Frontend built; official extension assets restored.')


def start():
    for path in [LOGS, STATE, LOGS / 'app', ROOT / 'runtime/scripts',
                 ROOT / 'data/sqlbot/file', ROOT / 'data/sqlbot/excel',
                 ROOT / 'data/sqlbot/images']:
        path.mkdir(parents=True, exist_ok=True)
    prepare_frontend()
    database('start')
    backend = ROOT / 'sqlbot/backend'
    spawn('api', [str(BIN / 'python'), '-m', 'uvicorn', 'main:app',
                  '--host', '127.0.0.1', '--port', '18000'], backend)
    wait_http('api', 'http://127.0.0.1:18000/')
    spawn('mcp', [str(BIN / 'python'), '-m', 'uvicorn', 'main:mcp_app',
                  '--host', '127.0.0.1', '--port', '18001'], backend)
    wait_http('mcp', 'http://127.0.0.1:18001/openapi.json')
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js is not available on PATH')
    spawn('charts', [node, str(ROOT / 'sqlbot/g2-ssr/app.js')], ROOT / 'sqlbot/g2-ssr')
    wait_http('charts', 'http://127.0.0.1:13000/')
    print('SQLBot ready: http://localhost:18000/')


def stop():
    for name in ['charts', 'mcp', 'api']:
        pid = running_pid(name)
        if pid:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 30
            while running_pid(name) and time.monotonic() < deadline:
                time.sleep(0.2)
            if running_pid(name):
                raise RuntimeError(f'{name} has not stopped; database left running')
        (STATE / (name + '.json')).unlink(missing_ok=True)
    database('stop')


def status():
    for name in ['api', 'mcp', 'charts']:
        pid = running_pid(name)
        print(f'{name}: ' + (f'running (pid {pid})' if pid else 'stopped'))
    database('status')


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action not in {'start', 'stop', 'status', 'build_frontend'}:
        raise SystemExit('Usage: local_runtime.py [start|stop|status|build_frontend]')
    try:
        globals()[action]()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
