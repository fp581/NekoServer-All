"""The setup window's look (0.50.0): nothing on the page moves when something is chosen
or a question comes and goes, measured in a real browser (headless Chrome, Chromium or
Edge; skipped where none is installed), its Night / Lamplight switch, and the contrast
of its text in both themes."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'setup-gui'
BROWSERS = (
    'google-chrome-stable',
    'google-chrome',
    'chromium',
    'chromium-browser',
    'microsoft-edge-stable',
    'msedge',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
)


def browser():
    for name in BROWSERS:
        found = shutil.which(name) or (name if Path(name).is_file() else None)
        if found:
            return found
    return None


def extra(ident, title, on=True, asked=False, installs=0):
    return {
        'id': ident,
        'title': title,
        'explanation': 'What this part does, in a sentence or two long enough to wrap onto a second line.',
        'installs': [{'name': f'package-{n}', 'explanation': 'what it is for'} for n in range(installs)],
        'on': on,
        'asked': asked,
    }


CHOOSE = {
    'version': '0.50.0',
    'phase': 'choose',
    'extras': [
        extra('media_apps', 'The media apps: movies, shows, music and photos', installs=0),
        extra('network_storage', 'My media and backup drives are in a network storage box (NAS)', on=False, installs=2),
        extra('tv', 'A TV connected to this PC', asked=True, installs=6),
        extra('notifications', 'Phone alerts when something fails', asked=True),
        extra('nextcloud', 'Nextcloud, your private cloud drive'),
    ],
    'steps': [],
    'current': None,
    'done': 0,
    'total': 0,
    'question': None,
    'terminal': None,
    'finish': [],
    'title': '',
    'stopped': False,
    'complete': False,
}
STEPS = [
    {
        'id': f's{n}',
        'title': f'Step {n}',
        'explanation': 'x',
        'status': 'success' if n < 7 else 'pending',
        'reason': '',
        'packages': [],
        'notes': [],
    }
    for n in range(12)
]
STEPS[7]['status'] = 'running'
RUNNING = {**CHOOSE, 'phase': 'running', 'steps': STEPS, 'current': 's7', 'done': 7, 'total': 12}
QUESTION = {
    **RUNNING,
    'question': {
        'seq': 1,
        'text': 'Install the media apps too? [yes/no]: ',
        'secret': False,
        'kind': 'yesno',
        'context': ['=== Optional ==='],
        'page': None,
    },
}
TERMINAL = {**RUNNING, 'terminal': 'Connecting the apps together'}

# Before setup.js: answers its polls with window.nextState (set by the measuring script).
STUB = """window.nextState = %s;
window.fetch = async (route, options) => {
  if (options && options.method === 'POST') return { ok: true, status: 200, json: async () => ({ ok: true }) };
  return { ok: true, status: 200, json: async () => window.nextState };
};"""

# After setup.js: measures, changes something, measures again; the result goes in a <pre>.
MEASURE = """(async () => {
  const pause = ms => new Promise(r => setTimeout(r, ms));
  const box = n => { const r = n.getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map(Math.round); };
  const cards = () => [...document.querySelectorAll('.extra')].map(card =>
    [...card.querySelectorAll('h3, p, .caption, .switch, .packages-toggle')].map(box));
  const page = () => ['main', '.track', '#phase-badge', '#steps', '#notices'].map(s => box(document.querySelector(s)));
  const result = { presses: [], states: [] };
  await pause(1500);
  for (const input of document.querySelectorAll('.extra input')) {
    const before = JSON.stringify(cards());
    input.click();
    await pause(300);
    result.presses.push(before === JSON.stringify(cards()));
  }
  for (const state of %s) {
    window.nextState = state;
    await pause(1500);
    result.states.push([JSON.stringify(page()), document.getElementById('phase-badge').textContent]);
  }
  const pre = document.createElement('pre');
  pre.id = 'measured';
  pre.textContent = JSON.stringify(result);
  document.body.append(pre);
})();"""


@unittest.skipUnless(browser(), 'no headless Chrome, Chromium or Edge here')
class NothingMovesTests(unittest.TestCase):
    """The page measured in a real browser at the window's usual size and narrow."""

    def measure(self, first, states, size):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / 'static').mkdir()
            for name in ('setup.css', 'setup.js', 'theme.js'):
                shutil.copy(PAGE / name, folder / 'static' / name)
            shutil.copy(ROOT / 'dashboard/theme.css', folder / 'static/theme.css')
            shutil.copy(ROOT / 'dashboard/logo.svg', folder / 'static/logo.svg')
            (folder / 'static/stub.js').write_text(STUB % json.dumps(first), encoding='utf-8')
            (folder / 'static/measure.js').write_text(MEASURE % json.dumps(states), encoding='utf-8')
            html = (PAGE / 'index.html').read_text(encoding='utf-8')
            html = html.replace(
                '<script src="static/theme.js"></script>',
                '<script src="static/stub.js"></script><script src="static/theme.js"></script>',
            ).replace('</head>', '<script src="static/measure.js" defer></script></head>')
            (folder / 'index.html').write_text(html, encoding='utf-8')
            shown = subprocess.run(
                [
                    browser(),
                    '--headless',
                    '--disable-gpu',
                    '--no-sandbox',
                    '--no-first-run',
                    f'--user-data-dir={folder / "profile"}',
                    f'--window-size={size}',
                    '--virtual-time-budget=30000',
                    '--dump-dom',
                    (folder / 'index.html').as_uri(),
                ],
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=120,
                check=False,
            )
        found = re.search(r'<pre id="measured">(.*?)</pre>', shown.stdout, re.S)
        self.assertIsNotNone(found, shown.stderr[-2000:])
        return json.loads(found.group(1).replace('&quot;', '"').replace('&amp;', '&'))

    def test_choosing_moves_nothing_on_the_choose_screen(self):
        # 0.49.0: a card's title shared its row with the switch's caption, which went from
        # "asked later" to "yes" (or from nothing to "no"), so the title rewrapped.
        for size in ('1180,480', '700,620'):
            with self.subTest(size=size):
                measured = self.measure(CHOOSE, [], size)
                self.assertEqual(len(measured['presses']), len(CHOOSE['extras']))
                self.assertTrue(all(measured['presses']), measured['presses'])

    def test_a_question_coming_and_going_moves_nothing(self):
        # 0.49.0: the "needs an answer" strip pushed the page down when a question came, and
        # the badge's words ("Working…", "Waiting for you") slid the progress bar sideways.
        for size in ('1180,480', '700,620'):
            with self.subTest(size=size):
                states = self.measure(RUNNING, [RUNNING, QUESTION, TERMINAL, RUNNING], size)['states']
                badges = [badge for _, badge in states]
                self.assertEqual(badges, ['Working…', 'Waiting for you', 'Waiting for you', 'Working…'])
                boxes = [json.loads(measured) for measured, _ in states]
                if size == '700,620':
                    # One column: the step list comes under the question and follows its
                    # height; the page, the bar, the badge and the strip stay put.
                    boxes = [[b for i, b in enumerate(page) if i != 3] for page in boxes]
                self.assertEqual(len({json.dumps(page) for page in boxes}), 1, states)


def colours(theme):
    """theme.css's colours in one theme: light-dark(Lamplight, night)."""
    css = (ROOT / 'dashboard/theme.css').read_text(encoding='utf-8')
    pairs = re.findall(r'--([a-z-]+): light-dark\((#[0-9a-f]{6}), (#[0-9a-f]{6})\)', css)
    return {name: lamplight if theme == 'light' else night for name, lamplight, night in pairs}


def rgb(hex_colour):
    return [int(hex_colour[i : i + 2], 16) for i in (1, 3, 5)]


def mix(first, share, second):
    """color-mix(in srgb, first share%, second)."""
    return [a * share / 100 + b * (100 - share) / 100 for a, b in zip(rgb(first), rgb(second))]


def contrast(text, surface):
    def luminance(channels):
        linear = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(text), luminance(surface)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class ThemeTests(unittest.TestCase):
    def test_night_by_default_with_the_switch_and_no_following_the_device(self):
        html = (PAGE / 'index.html').read_text(encoding='utf-8')
        css = (PAGE / 'setup.css').read_text(encoding='utf-8')
        script = (PAGE / 'theme.js').read_text(encoding='utf-8')
        self.assertIn('<html lang="en" data-neko-theme="dark">', html)
        self.assertIn('id="theme-toggle"', html)
        # Set before the page is drawn: not deferred, and before setup.js.
        self.assertIn('<script src="static/theme.js"></script>', html)
        self.assertLess(html.index('static/theme.js'), html.index('static/setup.js'))
        self.assertNotIn('light dark', css.split('}')[0])  # 0.49.0 followed the device only
        self.assertIn("return theme === 'light' ? 'light' : 'dark';", script)  # anything else is Night

    def test_the_choice_is_remembered_only_where_the_browser_allows(self):
        script = (PAGE / 'theme.js').read_text(encoding='utf-8')
        for use in ('localStorage.getItem(KEY)', 'localStorage.setItem(KEY'):
            before = script[: script.index(use)]
            self.assertGreater(before.rfind('try {'), before.rfind('}'), use + ' must be inside try')
        self.assertIn("const KEY = 'neko-theme';", script)  # the dashboard's name and format

    def test_the_window_serves_the_theme_script(self):
        import sys

        sys.path.insert(0, str(ROOT))
        try:
            import setup_gui
        except ImportError:  # (pwd: not on Windows)
            self.skipTest('setup_gui needs a Linux system')
        self.assertEqual(setup_gui.STATIC['theme.js'], ('theme.js', 'text/javascript; charset=utf-8'))

    def test_text_reads_at_five_to_one_on_every_setup_surface_in_both_themes(self):
        # theme.css promises 5:1; the setup window adds surfaces of its own (a card that is
        # on, the notice strips), checked here like the shared ones.
        for theme in ('light', 'dark'):
            c = colours(theme)
            surfaces = {
                'page': rgb(c['bg']),
                'header': rgb(c['side']),
                'card': rgb(c['surface']),
                'raised': rgb(c['raised']),
                'card that is on': mix(c['purple-soft'], 45, c['surface']),
                'running step': rgb(c['purple-soft']),
                'question strip': mix(c['amber'], 16, c['surface']),
            }
            texts = {'text': rgb(c['text']), 'muted': rgb(c['muted'])}
            for surface_name, surface in surfaces.items():
                for text_name, text in texts.items():
                    with self.subTest(theme=theme, text=text_name, surface=surface_name):
                        self.assertGreaterEqual(contrast(text, surface), 5)
            with self.subTest(theme=theme, text='amber', surface='question strip'):
                self.assertGreaterEqual(contrast(rgb(c['amber']), surfaces['question strip']), 5)
            with self.subTest(theme=theme, text='purple', surface='running step'):
                self.assertGreaterEqual(contrast(rgb(c['purple']), surfaces['running step']), 5)


if __name__ == '__main__':
    unittest.main()
