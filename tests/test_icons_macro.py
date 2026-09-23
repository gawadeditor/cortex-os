"""
tests/test_icons_macro.py — hypogeum/templates/_icons.html::icon()

The macro's rendered <svg ...> opening tag must always stay on a single
line. Several templates (discover.html, at least) inline `{{ icon(...) }}`
directly into a single-quoted JavaScript string literal inside <script>;
a raw newline in that output breaks the string and throws a JS
SyntaxError, silently aborting the rest of that inline <script> block.
See tests/test_discover_route.py::test_inline_script_has_no_unterminated_js_string
for the integration-level regression this guards.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "hypogeum" / "templates"


@pytest.fixture(scope="module")
def icon():
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))
    tmpl = env.from_string("{% from '_icons.html' import icon %}{{ icon(name, cls, label) }}")

    def _render(name, cls="", label=""):
        return tmpl.render(name=name, cls=cls, label=label)
    return _render


def _icon_names():
    src = (_TEMPLATES_DIR / "_icons.html").read_text()
    return re.findall(r"^\s*'([a-z0-9-]+)':\s*'<", src, re.MULTILINE)


@pytest.mark.parametrize("name", _icon_names())
def test_icon_output_is_single_line(icon, name):
    assert "\n" not in icon(name)


@pytest.mark.parametrize("name", _icon_names())
def test_icon_with_label_output_is_single_line(icon, name):
    assert "\n" not in icon(name, label="Alert")


def test_icon_with_label_still_sets_aria_label(icon):
    out = icon("warning", label="Alert")
    assert 'aria-label="Alert"' in out
    assert 'role="img"' in out


def test_unknown_icon_falls_back_to_info(icon):
    assert icon("does-not-exist") == icon("info")
