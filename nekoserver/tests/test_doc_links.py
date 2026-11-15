"""Deep check plan 286: every relative link and #anchor in NekoServer's own guides leads
somewhere, and a guide's section named in words (README.md, "Passwords") exists. A broken
anchor opens the guide at its top, so the reader has to hunt for the part meant."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDES = sorted(ROOT.glob('*.md'))
LINK = re.compile(r'(?<!!)\[[^\]]*\]\(([^)\s]+)\)')
NAMED = re.compile(r'([A-Z][A-Z0-9-]+\.md),\s*"([^"]{3,80})"')


def prose(text):
    """The text outside code blocks and inline code, where links are links."""
    return re.sub(r'`[^`\n]*`', '', re.sub(r'```.*?```', '', text, flags=re.S))


def anchors(text):
    """The #anchors GitHub gives the headings: lower case, spaces as dashes, other
    punctuation left out, a repeated heading numbered."""
    found, seen = set(), {}
    for heading in re.findall(r'^#{1,6}\s+(.*?)\s*$', prose(text), re.M):
        heading = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', heading).lower()
        slug = ''.join('-' if c == ' ' else c for c in heading if c.isalnum() or c in ' -_')
        count = seen.get(slug, 0)
        found.add(f'{slug}-{count}' if count else slug)
        seen[slug] = count + 1
    return found


def sections(text):
    """What a guide names its parts: its headings and its bold paragraph labels."""
    heads = re.findall(r'^#{1,6}\s+(.*)$', text, re.M) + re.findall(r'\*\*([^*]+)\*\*', text)
    return [h.replace('`', '').strip().lower() for h in heads]


class DocLinkTests(unittest.TestCase):
    def test_every_relative_link_and_anchor_exists(self):
        broken = []
        for guide in GUIDES:
            for url in LINK.findall(prose(guide.read_text())):
                if re.match(r'^[a-z]+:', url):
                    continue
                path, _, anchor = url.partition('#')
                target = (guide.parent / path) if path else guide
                if not target.exists():
                    broken.append(f'{guide.name}: {url}')
                elif anchor and target.suffix == '.md' and anchor.lower() not in anchors(target.read_text()):
                    broken.append(f'{guide.name}: {url}')
        self.assertEqual(broken, [])

    def test_every_section_named_in_words_exists(self):
        missing = []
        for guide in GUIDES:
            for name, section in NAMED.findall(guide.read_text()):
                target = ROOT / name
                if target.exists() and not any(section.lower() in s for s in sections(target.read_text())):
                    missing.append(f'{guide.name}: {name}, "{section}"')
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
