"""No module defines one top-level name twice: a merge that brings in two functions of one
name keeps only the later, and the earlier one's callers then run the wrong one (the Media
folders' Extra-drive check and T1-18's drive-away refusal were both `drive_there`)."""

import ast
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def twice(source):
    """[name, ...]: the top-level functions and classes `source` defines more than once."""
    seen, again = set(), []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen:
                again.append(node.name)
            seen.add(node.name)
    return again


class OneDefinitionTests(unittest.TestCase):
    def test_a_name_defined_twice_is_found(self):
        self.assertEqual(twice('def a(x):\n    pass\n\n\ndef b():\n    pass\n\n\ndef a(y):\n    pass\n'), ['a'])

    def test_no_module_defines_a_top_level_name_twice(self):
        found = [
            f'{path.relative_to(HERE)}: {name}'
            for path in sorted(HERE.rglob('*.py'))
            if 'node_modules' not in path.parts and 'target' not in path.parts
            for name in twice(path.read_text(encoding='utf-8'))
        ]
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
