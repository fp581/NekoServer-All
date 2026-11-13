"""0.50.0, item 13: the words of every question setup, the recovery menu and the uninstaller
ask are held by a golden file (tests/golden/questions.txt), so a change to a question
shows in review. They are read from the source: every text handed to something that asks
(input, getpass, yes_no, a step's `question=`, the helpers that ask in the terminal and
the window alike). A value put into a question at run time shows as {its name}.

When a change is meant, write the list again on purpose:

    NEKO_UPDATE_GOLDEN=1 python3 -m unittest tests.test_question_texts
"""

import ast
import difflib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / 'tests' / 'golden' / 'questions.txt'
UPDATE = os.environ.get('NEKO_UPDATE_GOLDEN') == '1'

# What asks: the name called, and which argument is the question.
ASKERS = {
    'input': 0,
    'getpass': 0,
    'yes_no': 0,
    'prompt': 0,
    'ask': 0,
    'input_fn': 0,
    'ask_terminal': 0,
    'typed': 1,
    'private_entry': 0,
    'ask_choice': 0,
    'ask_on_page': 1,
    'page_field': 2,
    'new_password': 0,
}
# ... and what takes it by name (a guided step's own question, a part's one question).
NAMED = ('question',)
# guided_setup.part(ctx, title, steps, question): its fourth argument.
PART_QUESTION = 3


def text_of(node):
    """A question as written: its words, with {name} for what is put in at run time. None
    for something that is not text (a variable holding a question made elsewhere)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append('{' + ast.unparse(value.value) + '}')
        return ''.join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = text_of(node.left), text_of(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else '{' + ast.unparse(node.left) + '}') + (
            right if right is not None else '{' + ast.unparse(node.right) + '}'
        )
    if isinstance(node, ast.IfExp):
        either = [text_of(node.body), text_of(node.orelse)]
        return ' | '.join(text for text in either if text) or None
    return None


def questions_in(source):
    """Every question in one file's source, in the order they are written."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, 'attr', getattr(node.func, 'id', ''))
        asked = []
        if name in ASKERS and len(node.args) > ASKERS[name]:
            asked.append(node.args[ASKERS[name]])
        if name == 'part' and len(node.args) > PART_QUESTION:
            asked.append(node.args[PART_QUESTION])
        asked += [keyword.value for keyword in node.keywords if keyword.arg in NAMED]
        for argument in asked:
            text = text_of(argument)
            if text and text.strip() and any(letter.isalpha() for letter in text):
                found.append((node.lineno, ' '.join(text.split())))
    return [text for _, text in sorted(found)]


def files(root=ROOT):
    return sorted([*root.glob('*.py'), *root.glob('modules/*.py')])


def listed(root=ROOT, read=None):
    """The golden text: each file with a question, then its questions."""
    lines = []
    for path in files(root):
        source = read(path) if read else path.read_text(encoding='utf-8')
        asked = questions_in(source)
        if asked:
            lines.append(path.relative_to(root).as_posix())
            lines += ['    ' + text for text in asked]
    return '\n'.join(lines) + '\n'


class QuestionTextTests(unittest.TestCase):
    def test_the_questions_are_the_golden_text(self):
        now = listed()
        if UPDATE:
            GOLDEN.write_bytes(now.encode())  # as written here, whatever this computer's line ends are
        kept = GOLDEN.read_bytes().decode() if GOLDEN.is_file() else ''
        diff = '\n'.join(difflib.unified_diff(kept.splitlines(), now.splitlines(), 'golden', 'now', lineterm=''))
        self.assertEqual(now, kept, 'a question changed (write the list again if it was meant):\n' + diff)

    def test_the_reader_finds_a_question_however_it_is_written(self):
        source = (
            "answer = input('Number of the drive: ')\n"
            "ok = yes_no(f'Install {GROUP_NAMES[group]}?', prompt, output)\n"
            "name = typed(input_fn, 'Type ' + name + ' to confirm: ')\n"
            "done = guided_setup.checkpoint(ctx, 'Title', [], question='Is it on?')\n"
            "part(ctx, 'Phone', steps, 'Is your phone set up?')\n"
            "x = ask(question)\n"
            "y = prompt('one' if first else 'two')\n"
        )
        self.assertEqual(
            questions_in(source),
            [
                'Number of the drive:',
                'Install {GROUP_NAMES[group]}?',
                'Type {name} to confirm:',
                'Is it on?',
                'Is your phone set up?',
                'one | two',
            ],
        )

    def test_no_question_is_the_old_one_that_could_be_any_steps(self):
        self.assertNotIn('Have you completed this step successfully', listed())


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    unittest.main()
