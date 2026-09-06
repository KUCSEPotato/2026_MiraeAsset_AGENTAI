"""Exercise the real frontend image, proxy and API-container replacement in CI."""
import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

prefix = f"finory-smoke-{os.getpid()}"
network = prefix
api, frontend, blocker = (f"{prefix}-{name}" for name in ("api", "frontend", "blocker"))
backend = '''
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        question = query.get('question', [''])[0]
        status = 503 if question == 'unavailable' else 200
        body = json.dumps({'answer': question, 'host': self.headers['Host']}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
'''


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def start_api():
    docker('run', '-d', '--name', api, '--network', network, '--network-alias',
           'agent-api', 'python:3.12-alpine', 'python', '-c', backend)


def get(path, status=200):
    try:
        with urlopen(origin + path, timeout=5) as response:
            actual, body = response.status, response.read()
    except HTTPError as response:
        actual, body = response.code, response.read()
    assert actual == status, (path, actual, status)
    return body


def wait_answer():
    for _ in range(30):
        try:
            result = json.loads(get('/answer?' + urlencode({'question_id': 'smoke', 'question': '원화채권 AA- & ETF?'})))
            assert result['answer'] == '원화채권 AA- & ETF?'
            assert result['host'] == origin.removeprefix('http://')
            return
        except (AssertionError, URLError):
            time.sleep(0.5)
    raise AssertionError('frontend did not reconnect to API')


try:
    docker('network', 'create', network)
    start_api()
    docker('run', '-d', '--name', frontend, '--network', network,
           '--read-only', '--tmpfs', '/tmp:mode=1777', '--cap-drop', 'ALL',
           '-p', '127.0.0.1::8080', sys.argv[1])
    port = docker('port', frontend, '8080/tcp').rsplit(':', 1)[1]
    origin = f'http://127.0.0.1:{port}'
    wait_answer()
    for path in ('/', '/chat', '/chat/'):
        assert b'/assets/app.js' in get(path)
    for name in ('app.js', 'styles.css', 'logo.png', 'ory.png'):
        assert get('/assets/' + name)
    get('/assets/missing.js', 404)
    get('/missing', 404)
    get('/answer?question=unavailable', 503)
    docker('rm', '-f', api)
    assert b'/assets/app.js' in get('/chat')
    # Occupy the old API address to force service DNS to change.
    docker('run', '-d', '--name', blocker, '--network', network,
           'python:3.12-alpine', 'python', '-c', 'import time; time.sleep(120)')
    start_api()
    wait_answer()
    print('Frontend routes, Korean query proxy, error propagation and API replacement passed')
finally:
    for name in (frontend, api, blocker):
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
