"""Check a running Screenpilot from the outside, on the real desktop: does it answer, does the screen arrive, do apps
open, does Reset desktop close them and bring the browser back, does a closed browser stay closed.

    python3 check.py                     (Screenpilot must be running: ./run.sh)
    python3 check.py --url http://localhost:8080

No model is used and nothing is spent. It opens the text editor, resets the desktop and closes the browser once."""
import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def call(base, path, body=None, raw=False, timeout=30):
    req = Request(base + '/api/' + path, data=None if body is None else json.dumps(body).encode(),
                  headers={'Content-Type': 'application/json', 'X-Screenpilot': '1'})
    with urlopen(req, timeout=timeout) as r: return r.read() if raw else json.load(r)


def titles(base):
    return [(w['app'] + ' ' + w['title']).lower() for w in call(base, 'windows')['windows']]


def wait_for(test, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if test(): return True
        except (HTTPError, URLError, OSError, ValueError): pass
        time.sleep(1)
    return False


def main():
    p = argparse.ArgumentParser(description='Check a running Screenpilot on its real desktop.')
    p.add_argument('--url', default='http://localhost:8080')
    base = p.parse_args().url.rstrip('/')
    results = []
    def check(name, ok, why=''):
        results.append(ok); print(('PASS  ' if ok else 'FAIL  ') + name + ('' if ok or not why else '  (' + why + ')'), flush=True)
    try: health = call(base, 'health')
    except (URLError, OSError) as exc: sys.exit(f'Screenpilot is not answering at {base}. Start it with ./run.sh  ({exc})')
    check('It answers and reports itself ready', bool(health.get('ok')), json.dumps(health)[:200])
    state = call(base, 'state')
    if state['desktop'].get('lab'): sys.exit('This is the test mode, not the real desktop. Start it with ./run.sh')
    if state['agent']['status'] in ('running', 'waiting'): sys.exit('A task is running. Wait for it or stop it first.')
    picture = call(base, 'screen', raw=True)
    check('The picture of the desktop arrives', len(picture) > 5000, f'{len(picture)} bytes')
    browser = lambda: any('chrom' in t for t in titles(base))
    check('The browser is open', wait_for(browser, 30))
    call(base, 'action', {'kind': 'open_app', 'app': 'notes'})
    notes = lambda: any('mousepad' in t for t in titles(base))
    check('An app opens (the text editor)', wait_for(notes, 20), ', '.join(titles(base)))
    call(base, 'reset', {}, timeout=60)
    check('Reset desktop closes the apps', wait_for(lambda: not notes(), 20), ', '.join(titles(base)))
    check('Reset desktop brings the browser back', wait_for(browser, 30), ', '.join(titles(base)))
    call(base, 'action', {'kind': 'key', 'key': 'ctrl+shift+w'})          # close the browser window, as a person would
    closed = wait_for(lambda: not browser(), 15)
    check('The browser closes', closed, ', '.join(titles(base)))
    if closed:
        time.sleep(6)
        check('A closed browser stays closed', not browser(), ', '.join(titles(base)))
        call(base, 'action', {'kind': 'open_app', 'app': 'browser'})
        check('The browser opens again when asked', wait_for(browser, 30), ', '.join(titles(base)))
    print(f'\n{sum(results)} of {len(results)} passed.' + ('' if all(results) else ' Send the lines above and `docker compose logs --tail 80` to whoever helps you.'))
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
