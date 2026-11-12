"""Give the running Screenpilot a task from the terminal.

    python3 cli.py "Write today's to-do list in a note and save it"
    python3 cli.py --stop
"""
import json
import sys
from urllib.request import Request, urlopen

URL = 'http://localhost:8080/api/'


def post(path, body):
    req = Request(URL + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Screenpilot': '1'})
    with urlopen(req, timeout=10) as r: return json.load(r)


if __name__ == '__main__':
    if len(sys.argv) < 2: sys.exit(__doc__)
    if sys.argv[1] == '--stop': post('stop', {}); print('Stopped.')
    else:
        result = post('chat', {'text': ' '.join(sys.argv[1:])})
        print('Queued.' if result['queued'] else 'Started. Watch it at http://localhost:8080')
