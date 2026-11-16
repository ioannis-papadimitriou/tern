"""Plan 04 step 3: `html_to_text` and the T9 hidden-text rules.

Each hiding technique has its own inline case with a visible control word, so removing that one
rule makes exactly that case fail. The fixture's inj-hidden-html HTML part is checked as is.
"""

from __future__ import annotations

import email
import time
from email import policy
from email.message import EmailMessage

import pytest
import test_fixture_manifest as tfm

from tern.index import html_text
from tern.index.html_text import (
    MAX_HTML_EVENTS,
    html_to_text,
    html_to_text_limited,
    strip_invisible,
)

HIDDEN = "SECRET-TOKEN"


def _fixture_html(key: str) -> str:
    man = tfm.load_manifest(tfm.FIXTURE)
    (copy,) = tfm.messages(man)[key]["copies"]
    msg = email.message_from_bytes(tfm.read_copy(tfm.FIXTURE, man, copy), policy=policy.default)
    assert isinstance(msg, EmailMessage)
    (html,) = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    content = html.get_content()
    assert isinstance(content, str)
    return content


def test_inj_hidden_html_part() -> None:
    html = _fixture_html("inj-hidden-html")
    assert "CANARY-INJ-2" in html  # the canary is really in the input
    text = html_to_text(html)
    assert "CANARY-INJ-2" not in text
    assert "ASSISTANT INSTRUCTION" not in text
    assert "Spring sale" in text


@pytest.mark.parametrize(
    "hidden_html",
    [
        pytest.param(f'<div style="display:none">{HIDDEN}</div>', id="display-none"),
        pytest.param(f'<div style="DISPLAY: None !important">{HIDDEN}</div>', id="display-case"),
        pytest.param(f'<span style="visibility:hidden">{HIDDEN}</span>', id="visibility-hidden"),
        pytest.param(f'<span style="font-size:0px">{HIDDEN}</span>', id="font-size-0"),
        pytest.param(f'<span style="font-size:0">{HIDDEN}</span>', id="font-size-0-unitless"),
        pytest.param(f'<div style="width:0;overflow:hidden">{HIDDEN}</div>', id="width-0"),
        pytest.param(f'<div style="height:0px;overflow:hidden">{HIDDEN}</div>', id="height-0"),
        pytest.param(f'<div style="max-height:0;overflow:hidden">{HIDDEN}</div>', id="max-h-0"),
        pytest.param(f'<td width="0" style="overflow:hidden">{HIDDEN}</td>', id="width-attr-0"),
        pytest.param(
            f'<div style="font-size:0"><span style="font-size:1.5em">{HIDDEN}</span></div>',
            id="font-size-0-relative-child",
        ),
        pytest.param(
            f'<div style="visibility:hidden"><p style="color:#000">{HIDDEN}</p></div>',
            id="visibility-inherited",
        ),
        pytest.param(f'<span style="color:transparent">{HIDDEN}</span>', id="colour-transparent"),
        pytest.param(f'<span style="color:rgba(0, 0, 0, 0)">{HIDDEN}</span>', id="colour-alpha-0"),
        pytest.param(
            f'<div style="background:#fff"><p style="background:transparent;color:#fff">{HIDDEN}'
            "</p></div>",
            id="colour-eq-background-through-transparent",
        ),
        pytest.param(f'<span style="opacity:0">{HIDDEN}</span>', id="opacity-0"),
        pytest.param(f"<div hidden>{HIDDEN}</div>", id="hidden-attr"),
        pytest.param(f"<style>.x{{}} {HIDDEN}</style>", id="style"),
        pytest.param(f"<script>var a = '{HIDDEN}';</script>", id="script"),
        pytest.param(f"<head><title>{HIDDEN}</title></head>", id="head"),
        pytest.param(f"<template><p>{HIDDEN}</p></template>", id="template"),
        pytest.param(f"<!-- {HIDDEN} -->", id="comment"),
        pytest.param(
            f'<div style="background-color:#ffffff"><span style="color:#FFF">{HIDDEN}</span></div>',
            id="colour-eq-background",
        ),
        pytest.param(
            f'<p style="background:white url(x.png);color:rgb(255, 255, 255)">{HIDDEN}</p>',
            id="colour-eq-background-forms",
        ),
        pytest.param(
            f'<td bgcolor="#000000"><font color="black">{HIDDEN}</font></td>', id="font-bgcolor"
        ),
        pytest.param(
            f'<div style="color:#123456;background:#123456"><b>{HIDDEN}</b></div>',
            id="colour-inherited",
        ),
        pytest.param(
            f'<div style="display:none"><p style="display:block">{HIDDEN}</p></div>',
            id="ancestor-hidden",
        ),
        # HTML5 ignores "/" on a non-void element: the span stays open and hides what follows.
        pytest.param(f'<span style="display:none"/>{HIDDEN}</span>', id="selfclose-nonvoid"),
        # A duplicated attribute keeps its first value.
        pytest.param(f'<div style="display:none" style="">{HIDDEN}</div>', id="duplicate-style"),
        pytest.param(f'<span style="display:/**/none">{HIDDEN}</span>', id="css-comment"),
        pytest.param(f"<title>{HIDDEN}</title>", id="title-in-body"),
        pytest.param(f"<iframe>{HIDDEN}</iframe>", id="iframe-content"),
        pytest.param(f'<span style="color:#ffffff00">{HIDDEN}</span>', id="colour-hex8-alpha-0"),
        pytest.param(f'<span style="color:#fff0">{HIDDEN}</span>', id="colour-hex4-alpha-0"),
        pytest.param(
            f'<span style="color:rgba(0,0,0,0%)">{HIDDEN}</span>', id="colour-pct-alpha-0"
        ),
        pytest.param(
            f'<span style="color:rgb(0 0 0 / 0)">{HIDDEN}</span>', id="colour-slash-alpha"
        ),
        pytest.param(
            f'<span style="color:hsla(0,0%,0%,0)">{HIDDEN}</span>', id="colour-hsla-alpha"
        ),
        pytest.param(
            f'<div style="background:#fff;color:#fff"><span style="color:inherit">{HIDDEN}</span>'
            "</div>",
            id="colour-inherit",
        ),
        pytest.param(
            f'<div style="background:#fff;color:#fff"><span style="color:currentColor">{HIDDEN}'
            "</span></div>",
            id="colour-currentcolor",
        ),
        pytest.param(
            f'<div style="background:#fff;color:#fff"><span style="color:rgba(0,0,0,.)">{HIDDEN}'
            "</span></div>",
            id="colour-invalid-keeps-parent",
        ),
        pytest.param(
            f'<div style="background:#fff;color:#fff"><span style="background:url(x.png)">{HIDDEN}'
            "</span></div>",
            id="background-image-keeps-parent-background",
        ),
        pytest.param(
            f'<div style="background:#000"><span style="background:currentcolor;color:#fff">'
            f"{HIDDEN}</span></div>",
            id="background-currentcolor",
        ),
        pytest.param(
            f'<td bgcolor="ffffff"><font color="FFFFFF">{HIDDEN}</font></td>',
            id="legacy-hex-without-hash",
        ),
        # Three hex digits without "#" are 0X0Y0Z: "000" is black, like "#000".
        pytest.param(
            f'<div style="background:#000"><font color="000">{HIDDEN}</font></div>',
            id="legacy-3-digit-font-color",
        ),
        pytest.param(
            f'<div style="color:#000"><table bgcolor="000"><tr><td>{HIDDEN}</td></tr></table>'
            "</div>",
            id="legacy-3-digit-bgcolor",
        ),
        pytest.param(
            f'<table bgcolor="#0f0f0f"><tr><td><font color="fff">{HIDDEN}</font></td></tr></table>',
            id="legacy-3-digit-is-0x0y0z",
        ),
        # Any other "#"-less value that is not a basic named colour is an unknown colour: it is
        # ignored and the element keeps its parent's (here hiding) colour or background.
        pytest.param(
            f'<div style="color:#fff;background:#fff"><font color="ffffffff">{HIDDEN}</font></div>',
            id="legacy-unknown-8-digit-font-color",
        ),
        pytest.param(
            f'<div style="color:#000;background:#000"><font color="0">{HIDDEN}</font></div>',
            id="legacy-unknown-1-digit-font-color",
        ),
        pytest.param(
            f'<div style="color:#ffc0cb;background:#ffc0cb"><font color="pink">{HIDDEN}</font>'
            "</div>",
            id="legacy-unknown-name-font-color",
        ),
        pytest.param(
            f'<div style="color:#fff;background:#fff"><table bgcolor="ffffffff"><tr><td>{HIDDEN}'
            "</td></tr></table></div>",
            id="legacy-unknown-8-digit-bgcolor",
        ),
        pytest.param(
            f'<div style="color:#ffc0cb;background:#ffc0cb"><table bgcolor="pink"><tr><td>{HIDDEN}'
            "</td></tr></table></div>",
            id="legacy-unknown-name-bgcolor",
        ),
        # Inside <svg>/<math> these tags are foreign elements, not void: their hiding style
        # covers their content. <image> is a real SVG element.
        pytest.param(
            f'<svg><image style="display:none"><text>{HIDDEN}</text></image></svg>',
            id="svg-image-not-void",
        ),
        pytest.param(
            f'<svg><basefont style="display:none"><text>{HIDDEN}</text></basefont></svg>',
            id="svg-basefont-not-void",
        ),
        pytest.param(
            f'<math><keygen style="display:none">{HIDDEN}</keygen></math>',
            id="math-keygen-not-void",
        ),
    ],
)
def test_hidden_text_is_dropped(hidden_html: str) -> None:
    text = html_to_text(f"<html><body><p>Visible start</p>{hidden_html}<p>visible end</p>")
    assert HIDDEN not in text
    assert "Visible start" in text and "visible end" in text


@pytest.mark.parametrize(
    "visible_html",
    [
        pytest.param(f'<span style="font-size:0.8em">{HIDDEN}</span>', id="small-font"),
        pytest.param(f'<span style="opacity:0.5">{HIDDEN}</span>', id="half-opacity"),
        pytest.param(f'<span style="color:#ffffff">{HIDDEN}</span>', id="white-no-background"),
        pytest.param(
            f'<div style="background:#fff;color:#fff"><span style="color:#000">{HIDDEN}</span>'
            "</div>",
            id="contrasting-child",
        ),
        pytest.param(f'<p style="display:none">x<p>{HIDDEN}</p>', id="implicit-p-close"),
        pytest.param(f'<div style="width:10px">{HIDDEN}</div>', id="nonzero-width"),
        pytest.param(
            f'<div style="font-size:0px;display:inline-block"><div style="font-size:14px">{HIDDEN}'
            "</div></div>",
            id="font-size-0-wrapper-px-child",
        ),
        pytest.param(
            f'<div style="font-size:0"><font size="2">{HIDDEN}</font></div>',
            id="font-size-0-wrapper-font-size-attr",
        ),
        pytest.param(
            f'<div style="visibility:hidden"><p style="visibility:visible">{HIDDEN}</p></div>',
            id="visibility-visible-child",
        ),
        pytest.param(f'<table><tr><td width="0">{HIDDEN}</td></tr></table>', id="td-width-0"),
        pytest.param(
            f'<span style="color:rgba(0, 0, 0, 0.5)">{HIDDEN}</span>', id="colour-half-alpha"
        ),
        # A malformed alpha is an invalid colour (ignored), never an exception.
        pytest.param(f'<p style="color:rgba(0,0,0,.)">{HIDDEN}</p>', id="colour-malformed-alpha"),
        pytest.param(f'<p style="color:rgba(0,0,0,1.2.3)">{HIDDEN}</p>', id="colour-bad-alpha"),
        # "red" inside url() is not a background colour.
        pytest.param(
            f'<div style="background:url(red.png);color:red">{HIDDEN}</div>', id="url-not-colour"
        ),
        pytest.param(f'<span style="color:hsl(0,0%,0%)">{HIDDEN}</span>', id="hsl-no-alpha"),
        # A gradient is a background image: its colours are not the background colour.
        pytest.param(
            f'<span style="background:linear-gradient(rgb(0,0,0),red);color:#000">{HIDDEN}</span>',
            id="gradient-not-colour",
        ),
        # In foreign content a self-closing tag does close the element.
        pytest.param(f'<svg style="display:none"/>{HIDDEN}', id="svg-selfclose"),
        # Only six hex digits without "#" are hex: "fff" is #0f0f0f to a browser, not white.
        pytest.param(
            f'<table bgcolor="fff"><tr><td><font color="#ffffff">{HIDDEN}</font></td></tr></table>',
            id="legacy-3-digit-not-hex",
        ),
        # An unknown legacy colour keeps the parent's colour; it hides nothing on its own
        # ("chucknorris" is #c00000 to a browser: visible on white, as here).
        pytest.param(
            f'<div style="color:#000;background:#fff"><font color="chucknorris">{HIDDEN}</font>'
            "</div>",
            id="legacy-unknown-text-colour-keeps-visible-parent",
        ),
        # CSS-only values in a colour attribute are legacy strings to a browser (shown), not
        # CSS colours: no longer read as the parent's colour or as transparent.
        pytest.param(
            f'<div style="color:#000"><table bgcolor="currentcolor"><tr><td>{HIDDEN}</td></tr>'
            "</table></div>",
            id="legacy-currentcolor-bgcolor-not-css",
        ),
        pytest.param(
            f'<div style="background:#fff"><font color="rgb(255,255,255)">{HIDDEN}</font></div>',
            id="legacy-rgb-font-color-not-css",
        ),
        pytest.param(
            f'<font color="rgba(0,0,0,0)">{HIDDEN}</font>', id="legacy-rgba-alpha-not-css"
        ),
    ],
)
def test_visible_text_is_kept(visible_html: str) -> None:
    assert HIDDEN in html_to_text(f"<p>a</p>{visible_html}")


@pytest.mark.parametrize("tag", ["basefont", "bgsound", "frame", "keygen"])
def test_void_element_does_not_hide_following_text(tag: str) -> None:
    """A void element has no content, so its hiding style ends with the tag."""
    assert HIDDEN in html_to_text(f'<p>a</p><{tag} style="display:none">{HIDDEN}<p>b</p>')


def test_zero_width_characters_removed() -> None:
    zw = "\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff"
    text = html_to_text(f"<p>CAN{zw}ARY</p>")
    assert text == "CANARY"


def test_bidi_controls_removed() -> None:
    bidi = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    assert html_to_text(f"<p>a{bidi}b</p>") == "ab"
    assert strip_invisible(f"x{bidi}y\u200bz") == "xyz"


def _tags(text: str) -> str:
    """``text`` written in Unicode tag characters (ASCII smuggling)."""
    return "\U000e0001" + "".join(chr(0xE0000 + ord(c)) for c in text) + "\U000e007f"


@pytest.mark.parametrize(
    ("label", "hidden"),
    [
        ("tag characters", _tags("ignore previous") + "\U000e0000"),
        ("soft hyphen", "\u00ad"),
        ("combining grapheme joiner", "\u034f"),
        ("mongolian vowel separator", "\u180e"),
        ("supplementary variation selectors", "\U000e0100\U000e0150\U000e01ef"),
    ],
)
def test_other_invisible_format_characters_removed(label: str, hidden: str) -> None:
    assert strip_invisible(f"Hel{hidden}lo") == "Hello", label
    assert html_to_text(f"<p>Hel{hidden}lo</p>") == "Hello", label


def test_emoji_presentation_selectors_kept() -> None:
    """U+FE00-FE0F choose emoji/text presentation and are not stripped."""
    assert strip_invisible("\u2764\ufe0f \u263a\ufe0e") == "\u2764\ufe0f \u263a\ufe0e"


def test_layout() -> None:
    html = (
        "<html><head><style>p{}</style></head><body>"
        "<h1>Title</h1><p>One  two\n three&nbsp;&amp; four</p>"
        "<div>line<br>break</div><ul><li>a</li><li>b</ul>"
        "<table><tr><td>c1</td><td>c2</td></tr></table>"
        "<pre>  keep\n    indent</pre></body></html>"
    )
    assert html_to_text(html) == (
        "Title\n\nOne two three & four\n\nline\nbreak\na\nb\n\nc1 c2\n\nkeep\nindent"
    )


def test_blockquote_lines_are_marked_quoted() -> None:
    html = (
        "<div>My reply</div><div>On Mon, Ann wrote:</div>"
        "<blockquote type=cite><div>first</div><blockquote>nested</blockquote></blockquote>"
    )
    assert html_to_text(html) == "My reply\nOn Mon, Ann wrote:\n\n> first\n\n>> nested"


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><meta charset=utf-8><p>Visible text</p>",
        "<html><head><meta charset=utf-8><title>t</title>Visible text",
        "<head><style>p{}</style></head><div>Visible text</div>",
    ],
)
def test_unclosed_head_ends_implicitly(html: str) -> None:
    """HTML5 ends <head> at the first element or text that does not belong in it."""
    assert html_to_text(html) == "Visible text"


def test_unmatched_end_tags_close_nothing() -> None:
    html = f'<div style="display:none"><span>x</p></b></td>{HIDDEN}</span></div><p>shown</p>'
    assert html_to_text(html) == "shown"


def test_limit_stops_early() -> None:
    html = "<p>" + "word " * 100_000 + "</p>"
    text, stopped = html_to_text_limited(html, 1000)
    assert stopped and len(text) < 200_000
    text, stopped = html_to_text_limited("<p>short</p>", 1000)
    assert (text, stopped) == ("short", False)


@pytest.mark.heavy  # ~1 s; fast twin: test_limit_stops_on_hidden_markup_at_a_small_cap (L1)
def test_limit_stops_on_hidden_markup() -> None:
    """T13: hidden markup yields no text, so the tag count bounds the work."""
    tags = '<div style="display:none">' + "<i>x</i>" * (MAX_HTML_EVENTS // 2 + 50_000)
    _, stopped = html_to_text_limited(tags + "</div><p>end</p>", 1000)
    assert stopped
    text, stopped = html_to_text_limited("<i>x</i>" * 1000 + "<p>end</p>", 10_000)
    assert not stopped and text.endswith("end")


@pytest.mark.heavy  # ~0.5-0.7 s each; fast twin: ..._at_a_small_cap (L1)
@pytest.mark.parametrize(
    "flood",
    [
        pytest.param("<" * (MAX_HTML_EVENTS + 10_000), id="text-runs"),
        pytest.param("<!---->" * (MAX_HTML_EVENTS + 10_000), id="comments"),
        pytest.param("<!x>" * (MAX_HTML_EVENTS + 10_000), id="bogus-comments"),
        pytest.param("<!DOCTYPE x>" * (MAX_HTML_EVENTS + 10_000), id="declarations"),
        pytest.param("<![if x]>" * (MAX_HTML_EVENTS + 10_000), id="marked-sections"),
        pytest.param("<?x>" * (MAX_HTML_EVENTS + 10_000), id="processing-instructions"),
    ],
)
def test_limit_counts_every_parser_event(flood: str) -> None:
    """T13: hidden events that are not tags (each lone "<" is a text run) are bounded too."""
    _, stopped = html_to_text_limited(f'<div style="display:none">{flood}</div>', 1000)
    assert stopped


# Fast twins of the two `slow` tests above (plan 04 merge review L1): the same assertions with
# the event cap lowered to SMALL_CAP, so the same counter is exercised for every event kind.
SMALL_CAP = 2_000

# One parser event per unit (a lone "<" is a text run).
FLOOD_UNITS = {
    "text-runs": "<",
    "comments": "<!---->",
    "bogus-comments": "<!x>",
    "declarations": "<!DOCTYPE x>",
    "marked-sections": "<![if x]>",
    "processing-instructions": "<?x>",
}


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(html_text, "MAX_HTML_EVENTS", SMALL_CAP)
    return SMALL_CAP


def test_limit_stops_on_hidden_markup_at_a_small_cap(small_cap: int) -> None:
    tags = '<div style="display:none">' + "<i>x</i>" * (small_cap // 2 + 500)
    _, stopped = html_to_text_limited(tags + "</div><p>end</p>", 1000)
    assert stopped
    # Control under the cap (two events per tag pair, plus the <p>).
    text, stopped = html_to_text_limited("<i>x</i>" * (small_cap // 4) + "<p>end</p>", 10_000)
    assert not stopped and text.endswith("end")


@pytest.mark.parametrize("unit", list(FLOOD_UNITS.values()), ids=list(FLOOD_UNITS))
def test_limit_counts_every_parser_event_at_a_small_cap(unit: str, small_cap: int) -> None:
    flood = unit * (small_cap + 500)
    _, stopped = html_to_text_limited(f'<div style="display:none">{flood}</div>', 1000)
    assert stopped
    # Control: a hidden flood under the cap does not stop.
    under = unit * (small_cap // 2)
    _, stopped = html_to_text_limited(f'<div style="display:none">{under}</div><p>e</p>', 1000)
    assert not stopped


def test_limit_applies_to_text_held_back_until_close() -> None:
    """html.parser holds a run of "&" until close(): the text limit still applies to it."""
    text, stopped = html_to_text_limited("<p>" + "&" * 3_000_000, 1000)
    assert stopped and 0 < len(text) <= 1001


@pytest.mark.parametrize(
    "unfinished",
    ['<p>a</p><div class="' + "x" * 16_000_000, "<p>a</p><div " + "x" * 16_000_000],
    ids=["attribute-value", "start-tag"],
)
def test_unfinished_construct_is_linear(unfinished: str) -> None:
    """T13: a construct html.parser keeps pending is not rescanned per 64 KiB feed (that took
    6-10 s for 16-20 MB); doubling feeds keep it well under a second."""
    # Wall-clock bound guards complexity (linear run is >10x under it), not latency: not `slow`.
    start = time.perf_counter()
    text, _ = html_to_text_limited(unfinished, 1 << 20)
    assert time.perf_counter() - start < 2.0
    assert text.startswith("a")
