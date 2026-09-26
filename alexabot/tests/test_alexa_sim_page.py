"""The simulated Alexa+ page's static files (``alexabot/web``): offline,
honest, and gentle with motion."""

import re
from pathlib import Path

from alexabot import sim

WEB = Path(sim.WEB_DIR)
LIB = Path(sim.LIB_DIR)


def _text(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_no_external_urls_in_html_css_js():
    for name in ("index.html", "sim.css", "sim.js"):
        text = _text(name)
        urls = re.findall(r"(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", text, re.IGNORECASE)
        # The only one allowed is the SVG namespace, which is never fetched.
        assert set(urls) <= {"http://www.w3.org"}, (name, urls)
        assert "@import" not in text


def test_reduced_motion_guards_every_animation():
    css = _text("sim.css")
    guarded = css.split("@media (prefers-reduced-motion: no-preference)", 1)
    assert len(guarded) == 2
    before, after = guarded
    assert "animation:" not in before.replace("animation: none", "")
    assert "transition:" not in before
    assert "@media (prefers-reduced-motion: reduce)" in after
    for name in re.findall(r"@keyframes ([\w-]+)", css):
        uses = re.findall(rf"animation:[^;]*\b{name}\b", css)
        assert uses, name


def test_voice_js_format_js_severity_js_import_only_each_other():
    for name in sim.LIB_FILES:
        text = (LIB / name).read_text(encoding="utf-8")
        imports = re.findall(r"""^import .* from ['"]([^'"]+)['"]""", text, re.M)
        assert set(imports) <= {f"./{n}" for n in sim.LIB_FILES}, (name, imports)
    js = _text("sim.js")
    assert set(re.findall(r"""from ['"]([^'"]+)['"]""", js)) == {"/lib/voice.js"}


def test_font_and_ofl_licence_are_present():
    font = WEB / "fonts" / "libre-baskerville-latin-wght-normal.woff2"
    assert font.read_bytes()[:4] == b"wOF2"
    licence = (WEB / "fonts" / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in licence
    for name in sim.STATIC_FILES:
        assert (WEB / name).is_file(), name


def test_the_footer_says_not_alexa_and_never_orders():
    page = _text("index.html")
    assert "Simulated Alexa+ experience" in page
    assert ("This page simulates the Alexa+ experience in a browser. It is not Alexa, "
            "not an Alexa skill, and not made by Amazon.") in page
    assert "Ada drafts and checks boards and never orders anything." in page
    assert "Chrome sends audio to its speech service" in page


def test_the_page_never_parses_server_text_as_html():
    js = _text("sim.js")
    assert "innerHTML" not in js and "outerHTML" not in js
    assert "insertAdjacentHTML" not in js and "document.write" not in js
    # The one markup it imports is the board SVG, checked first.
    assert "querySelector('script, foreignObject')" in js
