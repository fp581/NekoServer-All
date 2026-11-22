"""The Professional theme (DASHBOARD.md, "Themes"): its colours are measured, not claimed.

The colours are read from dashboard/theme.css itself, so a change there is measured again.
WCAG 2.2 asks 4.5:1 for text (1.4.3) and 3:1 for the shapes that show a status (1.4.11)."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME = (ROOT / 'dashboard/theme.css').read_text()
LOOK = (ROOT / 'dashboard/professional.css').read_text()

TEXT = 4.5  # WCAG 1.4.3, normal text
SHAPE = 3.0  # WCAG 1.4.11, the parts of a graphic needed to understand it


def professional_tokens():
    """{'light': {name: hex}, 'dark': {name: hex}} from the Professional block of theme.css."""
    block = THEME[THEME.index(":root[data-neko-look='professional']") :]
    block = block[: block.index('}')]
    found = {'light': {}, 'dark': {}}
    for name, light, dark in re.findall(r'--([a-z-]+): light-dark\((#[0-9a-f]{6}), (#[0-9a-f]{6})\)', block):
        found['light'][name] = light
        found['dark'][name] = dark
    return found


def selectors(rule_head):
    """The selectors of one rule, split at its own commas only (not those inside :is())."""
    found, depth, current = [], 0, ''
    for character in rule_head:
        depth += {'(': 1, ')': -1}.get(character, 0)
        if character == ',' and depth == 0:
            found.append(current.strip())
            current = ''
        else:
            current += character
    return found + [current.strip()]


def channels(hex_colour):
    return [int(hex_colour[i : i + 2], 16) for i in (1, 3, 5)]


def luminance(hex_colour):
    """Relative luminance, as WCAG defines it."""
    linear = []
    for value in channels(hex_colour):
        c = value / 255
        linear.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(one, two):
    lighter, darker = sorted((luminance(one), luminance(two)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def mixed(colour, share, base):
    """CSS color-mix(in srgb, colour share, base): each channel mixed as written."""
    parts = [round(a * share + b * (1 - share)) for a, b in zip(channels(colour), channels(base))]
    return '#' + ''.join(f'{p:02x}' for p in parts)


class ProfessionalColourTests(unittest.TestCase):
    def setUp(self):
        self.modes = professional_tokens()

    def test_it_gives_every_colour_moonlit_neko_has_in_both_schemes(self):
        moonlit = THEME[: THEME.index(":root[data-neko-look='professional']")]
        names = set(re.findall(r'--([a-z-]+): light-dark\(', moonlit))
        for scheme, tokens in self.modes.items():
            self.assertEqual(set(tokens), names, scheme)

    def test_every_text_colour_reads_on_every_surface(self):
        for scheme, t in self.modes.items():
            for text in ('text', 'muted', 'purple', 'mint', 'amber', 'red', 'peach', 'pink'):
                for surface in ('bg', 'side', 'surface', 'raised', 'purple-soft'):
                    ratio = contrast(t[text], t[surface])
                    self.assertGreaterEqual(ratio, TEXT, f'{scheme}: {text} on {surface} is {ratio:.2f}:1')

    def test_a_status_badge_reads_on_its_own_tint(self):
        """style.css .badge: the status colour as text on 13% of it over the panel."""
        for scheme, t in self.modes.items():
            for status in ('mint', 'amber', 'red'):
                tint = mixed(t[status], 0.13, t['surface'])
                ratio = contrast(t[status], tint)
                self.assertGreaterEqual(ratio, TEXT, f'{scheme}: {status} badge is {ratio:.2f}:1')

    def test_a_button_reads_and_a_status_shape_stands_out(self):
        for scheme, t in self.modes.items():
            # style.css .button: the page colour on the accent.
            ratio = contrast(t['bg'], t['purple'])
            self.assertGreaterEqual(ratio, TEXT, f'{scheme}: button text is {ratio:.2f}:1')
            for status in ('mint', 'amber', 'red', 'muted'):
                for surface in ('bg', 'surface', 'raised'):
                    ratio = contrast(t[status], t[surface])
                    self.assertGreaterEqual(ratio, SHAPE, f'{scheme}: {status} shape on {surface}')

    def test_the_status_colours_differ_in_lightness_not_only_hue(self):
        """For people who can't tell red from green: they differ in lightness too
        (and each has its own shape: see the next class)."""
        for scheme, t in self.modes.items():
            values = sorted(luminance(t[s]) for s in ('mint', 'amber', 'red'))
            for darker, lighter in zip(values, values[1:]):
                self.assertGreater(lighter / darker, 1.2, scheme)


class ProfessionalLookTests(unittest.TestCase):
    def test_each_status_has_its_own_shape(self):
        """Never colour alone (WCAG 1.4.1): fine is a circle, needs you a triangle, broken a
        square, not known a ring, on the dots and on the badges."""
        for marker in ('.dot.warning', '.dot.critical', '.dot.unknown'):
            self.assertIn(marker, LOOK)
        self.assertIn('polygon(50% 0, 100% 100%, 0 100%)', LOOK)  # the triangle
        for status in ('.badge.warning', '.badge.fail', '.badge.unknown'):
            name = re.escape(status)
            in_a_list = r':is\([^{]*' + name + r'[^{]*\)::before'  # :is(.badge.warning, ...)::before
            on_its_own = name + '::before'
            self.assertRegex(LOOK, in_a_list + '|' + on_its_own)

    def test_it_is_only_for_the_professional_look(self):
        """Every rule of professional.css starts with the look's own name, so Moonlit Neko
        is exactly as it was."""
        rules = re.sub(r'/\*.*?\*/', '', LOOK, flags=re.S)
        for head in re.findall(r'(?:^|})\s*([^{}@]+?)\s*{', rules):
            for selector in selectors(head):
                self.assertTrue(selector.startswith(":root[data-neko-look='professional']"), selector)

    def test_its_font_is_the_servers_own(self):
        self.assertIn("url('atkinson-latin.woff2') format('woff2')", LOOK)
        self.assertIn("url('atkinson-latin-ext.woff2') format('woff2')", LOOK)
        self.assertNotIn('http', LOOK)


if __name__ == '__main__':
    unittest.main()
