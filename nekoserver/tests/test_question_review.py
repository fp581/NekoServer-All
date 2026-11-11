"""The page the questions are reviewed on (plans/0.50.0-questions-review.html): one block
for each question, in the order setup shows them, with what the setup window shows (its
title, its hint, its buttons), what the terminal asks under it, and the old text where
the question existed before. It is made here from the real texts (questions.py, the
group switches of explanations.py, the drive page of drive_table.py), with example names
filled in, and held as tests/golden/questions-review.html so it cannot drift from them.

Write it again on purpose when a text is meant to change:

    NEKO_UPDATE_GOLDEN=1 python3 -m unittest tests.test_question_review
"""

import difflib
import html
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table  # noqa: E402
import explanations  # noqa: E402
import questions  # noqa: E402

GOLDEN = ROOT / 'tests' / 'golden' / 'questions-review.html'
UPDATE = os.environ.get('NEKO_UPDATE_GOLDEN') == '1'

STYLE = """
body { font: 16px/1.5 system-ui, sans-serif; max-width: 860px; margin: 2em auto; padding: 0 1em; color: #1d1b26; }
h1 { font-size: 26px; } h2 { margin-top: 2.2em; border-bottom: 2px solid #d9d4ea; padding-bottom: 4px; }
.block { margin: 1.2em 0 2em; }
.window { border: 1px solid #c9c2e3; border-radius: 12px; padding: 14px 16px; background: #faf8ff; }
.window .label { font-size: 11px; letter-spacing: .08em; color: #6a5acd; }
.window h3 { margin: 4px 0 6px; font-size: 19px; }
.window p { margin: 0 0 10px; color: #4a4660; }
.button { display: inline-block; border: 1px solid #6a5acd; border-radius: 8px; padding: 5px 12px; margin: 2px 6px 2px 0; }
.button.main { background: #6a5acd; color: white; }
.box { display: inline-block; border: 1px solid #999; border-radius: 6px; padding: 4px 90px 4px 8px; color: #888; margin-right: 8px; }
.terminal { background: #17151f; color: #e8e6f0; border-radius: 8px; padding: 10px 14px; margin: 8px 0 0; white-space: pre-wrap; font: 13.5px/1.45 ui-monospace, Consolas, monospace; }
.old { color: #7a7690; font-size: 14px; margin: 8px 0 0; } .old b { color: #5a5670; }
.note { color: #4a4660; font-size: 14px; }
"""


def esc(text):
    return html.escape(str(text), quote=False)


def window_block(title, hint, buttons, box=None):
    """What the setup window shows: (the box to fill in, when there is one,) then the buttons."""
    parts = ['<div class="window"><div class="label">THE SETUP WINDOW</div>', f'<h3>{esc(title)}</h3>']
    if hint:
        parts.append(f'<p>{esc(hint)}</p>')
    if box:
        parts.append(f'<span class="box">{esc(box)}</span>')
    for index, label in enumerate(buttons):
        parts.append(f'<span class="button{" main" if index == 0 else ""}">{esc(label)}</span>')
    parts.append('</div>')
    return ''.join(parts)


def block(window, terminal, old=None, note=None):
    parts = ['<div class="block">', window, f'<div class="terminal">{esc(terminal.strip())}</div>']
    if old:
        parts.append(f'<p class="old"><b>Before:</b> {esc(old)}</p>')
    if note:
        parts.append(f'<p class="note">{esc(note)}</p>')
    parts.append('</div>')
    return ''.join(parts)


# Example lists for the questions whose choices are made at run time.
EXAMPLE_CHOICES = {
    'assistant_model': [('0', 'No assistant'), ('1', 'qwen3.5:4b'), ('2', 'qwen3.5:8b')],
}


def question_block(key):
    entry = questions.Q[key]
    values = entry.get('example', {})
    choices = EXAMPLE_CHOICES.get(key)
    page = questions.window(key, choices, **values)
    buttons = [choice['label'] for choice in page['choices']]
    box = None
    if page['kind'] in ('text', 'secret'):
        box = 'a password box' if page['kind'] == 'secret' else 'a box to fill in'
        buttons = [page['send']]
    if page['skip'] is not None:
        buttons.append(page['skip_label'])
    return block(
        window_block(page['title'], page['hint'], buttons, box),
        questions.terminal(key, choices, **values),
        entry.get('old'),
    )


def switches_block():
    """The first screen: a switch for each part that can be left out."""
    rows = ''.join(f'<h3>{esc(extra["title"])}</h3><p>{esc(extra["explanation"])}</p>' for extra in explanations.EXTRAS)
    window = (
        '<div class="window"><div class="label">THE SETUP WINDOW · a switch for each, then Continue and the review</div>'
        + rows
        + '<span class="button main">Continue</span></div>'
    )
    return (
        '<div class="block">'
        + window
        + '<p class="note">In the terminal these are asked one by one when setup gets to them '
        '(the three media groups are in the block below).</p></div>'
    )


def drive_block():
    """The drive question, with example drives (the whole conversation is in
    tests/golden/drive-questions.txt)."""
    config = {}
    what = drive_table.asked_for(config, 'data_ssd')
    window = window_block(
        f'Choose {what}.',
        drive_table.needs('data_ssd') + ' Each drive is a card that says what it is; one that cannot be used says why.',
        ['Use this drive'],
    )
    terminal = (
        f'Now choose {what}. {drive_table.needs("data_ssd")}\n'
        '  1) nvme0n1: 477 GB NVMe SSD, WD SN770  - not this one: it is the disk Linux runs from\n'
        '  2) sda1: 466 GB SSD, Samsung SSD 870, named "apps", ext4, about 1 % full\n'
        f'Number of {what} (Enter alone stops setup here): '
    )
    confirm_window = window_block(
        f"Type sda1 to confirm that this drive is {drive_table.role('data_ssd')}.",
        'Nothing on the drive is changed by confirming it. Setup never formats a drive.',
        ['Confirm this drive'],
        'a box to fill in',
    )
    confirm_terminal = (
        f"Type sda1 to confirm it is {drive_table.role('data_ssd')} "
        '(drive 2: 466 GB SSD, Samsung SSD 870, named "apps"): '
    )
    return block(window, terminal, 'Number of the SSD (Enter alone stops setup here):') + block(
        confirm_window, confirm_terminal, 'Type sda1 to confirm it is the SSD (466 GB):'
    )


def page():
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<title>NekoServer 0.50.0: the questions setup asks</title>',
        f'<style>{STYLE}</style></head><body>',
        '<h1>NekoServer 0.50.0: the questions setup asks</h1>',
        '<p>One block for each question, in the order setup shows them. The light box is what the setup window '
        'shows: the question, one line under it, and its buttons. The dark box is the same question in the '
        'terminal, with one line on how to answer there. "Before" is the old text, where the question existed. '
        'The names (Jellyfin, drive 2, an address) are examples.</p>',
        '<h2>What to install</h2>',
        switches_block(),
    ]
    for screen, keys in questions.screens():
        if screen != 'What to install':
            parts.append(f'<h2>{esc(screen)}</h2>')
        if screen == 'Your drives':
            parts.append(drive_block())
        parts += [question_block(key) for key in keys]
    parts.append('</body></html>\n')
    return '\n'.join(parts)


class ReviewPageTests(unittest.TestCase):
    def test_the_review_page_is_made_from_the_real_texts(self):
        now = page()
        if UPDATE:
            GOLDEN.write_bytes(now.encode())
        kept = GOLDEN.read_bytes().decode() if GOLDEN.is_file() else ''
        diff = '\n'.join(difflib.unified_diff(kept.splitlines(), now.splitlines(), 'golden', 'now', lineterm=''))
        self.assertEqual(now, kept, 'a question changed (write the page again if it was meant):\n' + diff[:4000])

    def test_no_block_shows_a_placeholder_or_a_file_name(self):
        shown = page()
        self.assertNotIn('{', shown.split('</style>')[1])
        self.assertNotIn('.py', shown)

    def test_the_window_never_tells_anyone_to_type(self):
        """What the window shows for a question of questions.py: no "type", "Enter",
        "number", "yes/no", "[1/2]" or "blank". (The two confirmations that are typed on
        purpose keep the word in the code that asks them, not here.)"""
        import re

        banned = re.compile(r'\b(type|typed|enter|number|blank)\b|yes/no|\[\d', re.IGNORECASE)
        for key, entry in questions.Q.items():
            values = entry.get('example', {})
            shown = questions.window(key, EXAMPLE_CHOICES.get(key), **values)
            texts = [shown['title'], shown['hint'], shown['send'], shown['skip_label']]
            texts += [choice['label'] for choice in shown['choices']]
            for text in texts:
                with self.subTest(question=key, text=text):
                    self.assertIsNone(banned.search(text), text)

    def test_every_question_says_what_each_answer_does(self):
        for key, entry in questions.Q.items():
            with self.subTest(question=key):
                self.assertTrue(entry['title'].endswith('?') or entry['kind'] in ('text', 'secret'), entry['title'])
                self.assertLessEqual(len(entry['title']), 80, 'a short question')
                if entry['kind'] == 'yesno':
                    self.assertNotIn(entry['yes'].lower(), ('yes', 'ok'), 'a button says what it does')
                    self.assertNotEqual(entry['no'].lower(), 'no')
                self.assertIn('screen', entry)


if __name__ == '__main__':
    unittest.main()
