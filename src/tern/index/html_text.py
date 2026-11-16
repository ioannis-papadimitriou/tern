"""HTML → plain text for indexing, with hidden-text stripping (plan 04 step 3, threat T9).

Built on stdlib :mod:`html.parser` (no new dependency). Text that a mail client would not show is
dropped, because hidden HTML text is a prompt-injection vector (T9):

- elements whose own or an ancestor's inline ``style`` says ``display:none``, ``opacity:0`` or a
  zero ``width``/``height`` (also ``max-width``/``max-height``; the ``width``/``height``
  attributes only together with ``overflow:hidden``, since a table cell grows to fit its text);
  no descendant can undo these;
- text whose effective ``font-size`` is 0: inherited, so a descendant's absolute size (``px``,
  ``pt``, a keyword, ``rem``, ``<font size>``) shows its text again, while ``em``, ``%``, ``ex``
  or ``ch`` under a zero size stay 0 (newsletter column wrappers use ``font-size:0`` with sized
  children);
- text whose effective ``visibility`` is ``hidden``/``collapse``: inherited, a descendant's
  ``visibility:visible`` shows its text again;
- text whose colour (inline ``color`` or ``<font color>``, inherited) is ``transparent``, has
  alpha 0 (inline ``color``: ``rgba()`` or the alpha of any CSS colour function; both:
  ``#rgba``/``#rrggbbaa``), or
  equals the background colour (inline ``background``/``background-color`` or ``bgcolor``,
  inherited). ``inherit``/``unset``/``currentcolor`` and an invalid colour value keep the
  parent's colour, as a browser ignores an invalid declaration; a background that is
  transparent, invalid or has no colour (``background:url(x.png)``) keeps the parent's
  background. Named colours are matched as whole words outside ``url()`` and gradients;
- elements with the ``hidden`` attribute;
- the content of ``<head>`` (closed implicitly, as in HTML5, by the first element or text that
  does not belong in it), ``<title>``, ``<style>``, ``<script>``, ``<template>``, ``<iframe>``,
  ``<noembed>`` and ``<noframes>``; comments (also ``/* … */`` inside ``style`` values);
- zero-width (U+200B–U+200F, U+2060–U+2064, U+FEFF), bidi control (U+202A–U+202E,
  U+2066–U+2069), soft hyphen (U+00AD), combining grapheme joiner (U+034F), Mongolian vowel
  separator (U+180E), tag (U+E0000–U+E007F) and supplementary variation selector
  (U+E0100–U+E01EF) characters, everywhere (:func:`strip_invisible`). The variation selectors
  U+FE00–U+FE0F are kept: they choose emoji/text presentation.

Parsing is a simplified tree builder (known differences from HTML5 listed below): the ``/`` of a
self-closing tag is ignored on a non-void HTML element (``<span style="display:none"/>`` stays
open), honoured inside ``<svg>``/``<math>``; a duplicated attribute keeps its first value. In a
colour attribute, a ``#``-less value of six hex digits is hex and one of three hex digits is
read the legacy way, ``XYZ`` → #0X0Y0Z (``bgcolor="fff"`` is #0f0f0f, not white); any other
``#``-less value that is not a named colour of the basic table (or ``transparent``) is an
unknown colour, ignored like an invalid one: the element keeps its parent's colour.

Documented limitations (no CSS engine): hiding through ``<style>`` classes or ids, off-screen
positioning, ``text-indent``, ``clip``, ``clip-path``, transforms, the ``font`` shorthand,
``line-height:0``, CSS escapes in property names or values, colours given as ``hsl()``/``hwb()``/
``lab()``/``color()`` (only their alpha 0 is detected), percentage ``rgb()`` channels, named
colours outside the basic table, ``background-color`` winning over ``background`` whatever their
order, colour contrast short of equality, and similar tricks. A CSS ``width``/``height`` of 0
hides even without ``overflow:hidden`` (over-drops the rare visible overflow).

Known limitations of the tree builder and attribute colours (SHOW = hidden text can come out,
DROP = visible text can be lost; T9 here is defence in depth on top of ``untrusted.wrap()``):

- misnested end tag: ``</span>`` pops through an open special element (div, p, table, li) to
  the span, where HTML5 ignores it and the span keeps hiding: SHOW;
- quirks mode (no doctype): ``<table>`` closes an open ``<p>`` here, a browser nests the table
  inside the (hidden) p: SHOW;
- optional end tags close implicitly only when the element is on top of the stack
  (``<p hidden><span>x<div>`` keeps the p open): DROP;
- text inside an unclosed ``<head><noscript>`` does not end the head: DROP;
- foster parenting (text or elements misplaced directly in ``<table>``, moved before the table
  by a browser) is not modelled: SHOW or DROP;
- active formatting elements are not reconstructed (``<p><font color=…>x</p><p>y``: the font
  does not reopen in the second p) and the adoption agency is not run: SHOW or DROP;
- ``<select>`` insertion mode is not modelled (tags and end tags inside it are not ignored):
  SHOW or DROP;
- other legacy colour strings (``"chucknorris"`` → #c00000, ``"ffffffff"`` → #ffffff, ``"0"``
  → #000000, named colours outside the basic table, ``rgb()``) keep the parent's colour:
  ``<font color="chucknorris">`` inside a black-on-black parent is dropped although a browser
  shows it in #c00000 (DROP), and ``<font color="ffffffff">`` inside black-on-white is kept
  although a browser draws it white on white (SHOW): SHOW or DROP;
- ``#``-prefixed attribute colours are read as CSS hex (4 and 8 digits carry alpha, other
  lengths are ignored), where a browser runs the legacy parser on them: SHOW or DROP;
- foreign content: HTML breakout tags (``div``, ``p``, ``span``, ``font``, …) and integration
  points (``foreignObject``, ``desc``, ``title``, ``mi``/``mo``/``mn``/``ms``/``mtext``,
  ``annotation-xml``) are not modelled; the ``/`` closes them and HTML-only void tags are not
  void there: SHOW or DROP;
- frameset insertion mode is not modelled (text inside ``<frameset>`` is shown, a browser
  renders none): SHOW;
- ``color="transparent"`` on ``<font>`` counts as transparent, a browser ignores it: DROP.

Work bound (T13): :func:`html_to_text_limited` stops, mid-feed, after :data:`MAX_HTML_EVENTS`
parser events (start and end tags, text runs, comments, declarations: hidden markup produces no
text but costs parse time) and as soon as its text limit is passed. While html.parser holds back
an unfinished construct (an unclosed attribute value, comment, ``<script>``), each feed doubles,
so its rescans of the pending buffer stay linear in the input. Measured worst cases on 20 MB
inputs (unclosed attribute, comment, ``<script>`` or tag; floods of hidden ``<``, comments or
tags; a run of ``&`` that html.parser holds until ``close()``) take 0.04–1.4 s, against up to
21 s with fixed 64 KiB feeds and tag-only counting.

Layout: block elements break lines (``<p>``/headings/``<blockquote>`` with a blank line), ``<br>``
breaks a line, table cells are separated by a space, whitespace runs collapse to one space outside
``<pre>``, NBSP becomes a space. Text inside ``<blockquote>`` is prefixed with one ``>`` per nesting
level, so the quote split in :mod:`tern.index.mime` treats it as quoted. Link targets and image
``alt`` texts are not emitted.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Invisible format characters (T9): zero-width, bidi controls, soft hyphen, combining grapheme
# joiner, Mongolian vowel separator, tag characters ("ASCII smuggling") and the supplementary
# variation selectors. U+FE00-FE0F stay (emoji/text presentation).
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u180e\u200b-\u200f\u2060-\u2064\ufeff\u202a-\u202e\u2066-\u2069"
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)

_SKIP_CONTENT = frozenset(
    {"head", "title", "style", "script", "template", "iframe", "noembed", "noframes"}
)
# Elements allowed in <head>; any other start tag (or non-blank text) ends it implicitly.
_HEAD_CONTENT = frozenset(
    {
        "html",
        "head",
        "base",
        "basefont",
        "bgsound",
        "link",
        "meta",
        "noframes",
        "noscript",
        "script",
        "style",
        "template",
        "title",
    }
)
# Roots of foreign content, where a self-closing tag really closes the element.
_FOREIGN = frozenset({"svg", "math"})
# Void in HTML content only: inside <svg>/<math> these are ordinary foreign elements (as at the
# plan 04 step 3 review: they keep their content and their hiding style).
_VOID_HTML_ONLY = frozenset({"basefont", "bgsound", "frame", "keygen"})
_VOID = frozenset(
    {
        "area",
        "base",
        "basefont",
        "bgsound",
        "br",
        "col",
        "embed",
        "frame",
        "hr",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_PARAGRAPH = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table"})
_BLOCK = frozenset(
    {
        "address",
        "article",
        "aside",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "section",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
)
_CELL = frozenset({"td", "th"})
# A start tag of one of these implicitly closes an open element of the key (HTML's optional
# end tags, the common cases only).
_CLOSES: dict[str, frozenset[str]] = {
    "p": _PARAGRAPH | _BLOCK,
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "td": frozenset({"td", "th", "tr"}),
    "th": frozenset({"td", "th", "tr"}),
    "tr": frozenset({"tr"}),
    "option": frozenset({"option"}),
}

_NAMED_COLOURS = {
    "black": "#000000",
    "silver": "#c0c0c0",
    "gray": "#808080",
    "grey": "#808080",
    "white": "#ffffff",
    "maroon": "#800000",
    "red": "#ff0000",
    "purple": "#800080",
    "fuchsia": "#ff00ff",
    "magenta": "#ff00ff",
    "green": "#008000",
    "lime": "#00ff00",
    "olive": "#808000",
    "yellow": "#ffff00",
    "navy": "#000080",
    "blue": "#0000ff",
    "teal": "#008080",
    "aqua": "#00ffff",
    "cyan": "#00ffff",
    "orange": "#ffa500",
}
# CSS colour functions; only rgb()/rgba() are evaluated, the others only for alpha 0.
_COLOUR_FUNCTIONS = frozenset(
    {"rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color"}
)
_NUM = r"(\d+(?:\.\d*)?|\.\d+)"
_RGB_ARGS = re.compile(
    r"(\d{1,3})(?:\s*,\s*|\s+)(\d{1,3})(?:\s*,\s*|\s+)(\d{1,3})(?:\s*[,/]\s*" + _NUM + r"(%?))?"
)
_ALPHA_ARG = re.compile(_NUM + r"(%?)")
# Tokens of a colour value: a hex colour, a function call (no nesting), an identifier.
_COLOUR_TOKEN = re.compile(r"#[0-9a-z]*|[a-z][a-z0-9-]*\([^()]*\)|[a-z][a-z0-9-]*")
# Background images, removed before looking for the colour of a ``background`` shorthand.
_IMAGES = re.compile(r"url\([^)]*\)|[a-z-]*gradient\((?:[^()]|\([^()]*\))*\)")
_LEGACY_HEX = re.compile(r"[0-9a-f]{6}")
_LEGACY_HEX3 = re.compile(r"[0-9a-f]{3}")
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_IMPORTANT = re.compile(r"!\s*important")
_NUMBER = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))")
_WS = re.compile(r"[ \t\n\r\f\xa0]+")

_FEED_CHUNK = 65536
#: Parser events (tags, text runs, comments, declarations) handled per part before
#: :func:`html_to_text_limited` stops (T13): about 1 s of hostile all-hidden markup; a large
#: real newsletter has a few tens of thousands.
MAX_HTML_EVENTS = 500_000

# Colour results besides "#rrggbb" and None (a valid colour this module does not evaluate,
# equal to nothing).
_TRANSPARENT = "transparent"
_INHERIT = "inherit"  # inherit / unset
_CURRENT = "currentcolor"
_INVALID = "invalid"  # not a colour: a browser ignores the declaration

# font-size units and keywords that do not depend on the parent's size (0 × em is still 0).
_ABSOLUTE_UNITS = frozenset(
    {"px", "pt", "pc", "in", "cm", "mm", "q", "rem", "vw", "vh", "vmin", "vmax", ""}
)
_SIZE_KEYWORDS = frozenset(
    {"xx-small", "x-small", "small", "medium", "large", "x-large", "xx-large", "xxx-large"}
)
_LENGTH = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))([a-z%]*)$")


def strip_invisible(text: str) -> str:
    """Remove invisible format characters (T9; the set is listed in the module docstring)."""
    return _INVISIBLE.sub("", text)


def _alpha(number: str, percent: str) -> float:
    return float(number) / (100 if percent else 1)


def _hex_colour(digits: str) -> str | None:
    if len(digits) not in (3, 4, 6, 8) or not re.fullmatch(r"[0-9a-f]+", digits):
        return _INVALID
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    if len(digits) == 8:
        digits, alpha = digits[:6], digits[6:]
        if alpha == "00":
            return _TRANSPARENT
        if alpha != "ff":
            return None
    return "#" + digits


def _function_colour(name: str, args: str) -> str | None:
    if name not in _COLOUR_FUNCTIONS:
        return _INVALID
    args = args.strip()
    if name in ("rgb", "rgba"):
        m = _RGB_ARGS.fullmatch(args)
        if m is None:
            return _INVALID
        if m.group(4) is not None:
            alpha = _alpha(m.group(4), m.group(5))
            if alpha == 0:
                return _TRANSPARENT
            if alpha < 1:
                return None
        r, g, b = (min(int(x), 255) for x in m.group(1, 2, 3))
        return f"#{r:02x}{g:02x}{b:02x}"
    # Other functions: only an explicit alpha 0 ("/ a", or a 4th comma argument) is read.
    if "/" in args:
        last = args.rsplit("/", 1)[1].strip()
    elif args.count(",") == 3:
        last = args.rsplit(",", 1)[1].strip()
    else:
        return None
    m = _ALPHA_ARG.fullmatch(last)
    return _TRANSPARENT if m is not None and _alpha(m.group(1), m.group(2)) == 0 else None


def _token_colour(token: str) -> str | None:
    if token.startswith("#"):
        return _hex_colour(token[1:])
    if token.endswith(")"):
        name, _, args = token[:-1].partition("(")
        return _function_colour(name, args)
    if token == "transparent":
        return _TRANSPARENT
    if token in ("inherit", "unset"):
        return _INHERIT
    if token == "currentcolor":
        return _CURRENT
    return _NAMED_COLOURS.get(token, _INVALID)


def _colour(value: str, *, shorthand: bool = False, legacy: bool = False) -> str | None:
    """Parse a colour value (lower-cased): ``#rrggbb``, :data:`_TRANSPARENT`, :data:`_INHERIT`,
    :data:`_CURRENT`, :data:`_INVALID`, or None for a valid colour that is not evaluated.

    ``shorthand``: a ``background`` value, whose first colour token counts (images removed;
    no colour at all → transparent). ``legacy``: an HTML attribute, where six hex digits
    without ``#`` are a hex colour and three are ``XYZ`` → #0X0Y0Z (the legacy parser's
    result); any other value without ``#`` that is not a named colour is :data:`_INVALID` (the
    element keeps its parent's colour; see the limitations in the module docstring)."""
    v = value.strip()
    if legacy and v and not v.startswith("#") and v not in _NAMED_COLOURS and v != "transparent":
        if _LEGACY_HEX3.fullmatch(v):
            return "#" + "".join("0" + c for c in v)
        return _hex_colour(v) if _LEGACY_HEX.fullmatch(v) else _INVALID
    if not shorthand:
        tokens = _COLOUR_TOKEN.findall(v)
        if len(tokens) != 1 or tokens[0] != v:
            return _INVALID
        return _token_colour(v)
    for token in _COLOUR_TOKEN.findall(_IMAGES.sub(" ", v)):
        colour = _token_colour(token)
        if colour != _INVALID:
            return colour
    return _TRANSPARENT


def _is_zero(value: str) -> bool:
    m = _NUMBER.match(value.strip())
    return m is not None and float(m.group(1)) == 0


def _font_zero(style: dict[str, str], tag: str, attrs: dict[str, str | None], parent: bool) -> bool:
    """Whether the effective font-size is 0 (inherited; see the module docstring)."""
    if tag == "font" and (attrs.get("size") or "").strip():
        parent = False  # <font size=N> is an absolute size (1–7), never 0
    value = style.get("font-size")
    if value is None:
        return parent
    if value in _SIZE_KEYWORDS:
        return False
    m = _LENGTH.match(value)
    if m is None or float(m.group(1)) < 0:
        return parent  # invalid: ignored, as a browser would
    if float(m.group(1)) == 0:
        return True
    return parent if m.group(2) not in _ABSOLUTE_UNITS else False


def _invisible(style: dict[str, str], parent: bool) -> bool:
    """Whether the effective visibility is hidden (inherited, ``visible`` resets it)."""
    value = style.get("visibility")
    if value in ("hidden", "collapse"):
        return True
    if value == "visible":
        return False
    return parent


def _style(attr: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in _CSS_COMMENT.sub(" ", attr).split(";"):
        name, colon, value = decl.partition(":")
        if colon:
            out[name.strip().lower()] = _IMPORTANT.sub("", value.lower()).strip()
    return out


@dataclass
class _Node:
    tag: str
    hidden: bool  # display:none, opacity:0, zero size, ``hidden``: no descendant undoes it
    font_zero: bool
    invisible: bool
    skip: bool
    colour: str | None
    background: str | None
    quote: int
    pre: bool


def _hidden_by(style: dict[str, str], attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    if style.get("display") == "none":
        return True
    for prop in ("opacity", "width", "height", "max-width", "max-height"):
        if prop in style and _is_zero(style[prop]):
            return True
    if style.get("overflow") not in ("hidden", "clip"):
        return False
    return any(_is_zero(attrs.get(a) or "x") for a in ("width", "height"))


def _text_colour(
    style: dict[str, str], tag: str, attrs: dict[str, str | None], parent: str | None
) -> str | None:
    if "color" in style:
        colour = _colour(style["color"])
    elif tag == "font" and attrs.get("color"):
        colour = _colour((attrs["color"] or "").lower(), legacy=True)
    else:
        return parent
    # currentcolor on ``color`` itself is the inherited colour; an invalid value is ignored.
    return parent if colour in (_INHERIT, _CURRENT, _INVALID) else colour


def _background(
    style: dict[str, str], attrs: dict[str, str | None], colour: str | None, parent: str | None
) -> str | None:
    if "background-color" in style:
        bg = _colour(style["background-color"])
    elif "background" in style:
        bg = _colour(style["background"], shorthand=True)
    elif attrs.get("bgcolor"):
        bg = _colour((attrs["bgcolor"] or "").lower(), legacy=True)
    else:
        return parent
    if bg == _CURRENT:
        return colour
    # A transparent (or ignored) background shows the parent's through it.
    return parent if bg in (_TRANSPARENT, _INHERIT, _INVALID) else bg


@dataclass
class _Out:
    lines: list[tuple[int, str]] = field(default_factory=list)
    current: list[str] = field(default_factory=list)
    depth: int = 0
    chars: int = 0

    def text(self, data: str, depth: int, *, collapse: bool = True) -> None:
        if not self.current:
            self.depth = depth
        elif collapse and data.startswith(" ") and self.current[-1].endswith(" "):
            data = data[1:]
        self.current.append(data)
        self.chars += len(data)

    def newline(self, blank: bool = False, force: bool = False) -> None:
        """End the current line (a block boundary is a no-op on an empty line, ``<br>`` is
        not); ``blank`` also ends the paragraph with one blank line."""
        line = "".join(self.current)
        if line.strip() or force:
            self.lines.append((self.depth, line))
        self.current = []
        if blank and self.lines and self.lines[-1][1].strip():
            self.lines.append((0, ""))

    def render(self) -> str:
        if self.current:
            self.newline()
        rendered: list[str] = []
        for depth, line in self.lines:
            line = strip_invisible(line).strip()
            if line and depth:
                line = ">" * depth + " " + line
            if line or (rendered and rendered[-1]):
                rendered.append(line)
        return "\n".join(rendered).strip("\n")


class _Stop(Exception):
    """Raised by a handler when a :func:`html_to_text_limited` bound is reached."""


class _Converter(HTMLParser):
    def __init__(self, limit: int | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Node] = []
        # Open elements by tag: an end tag with nothing open to close costs O(1), so hostile
        # markup (many unmatched end tags over a deep stack) stays linear.
        self.open: Counter[str] = Counter()
        self.out = _Out()
        self.limit = limit  # None: unbounded (html_to_text)
        self.events = 0

    def _event(self) -> None:
        self.events += 1
        if self.limit is not None and self.events > MAX_HTML_EVENTS:
            raise _Stop

    def _top(self) -> _Node:
        if self.stack:
            return self.stack[-1]
        return _Node("", False, False, False, False, None, None, 0, False)

    @staticmethod
    def _shows(node: _Node) -> bool:
        return not (node.hidden or node.skip or node.font_zero or node.invisible)

    def _close(self, tag: str) -> None:
        if not self.open[tag]:
            return
        while self.stack:
            node = self.stack.pop()
            self.open[node.tag] -= 1
            self._break_for(node.tag)
            if node.tag == tag:
                return

    def _break_for(self, tag: str) -> None:
        if tag in _PARAGRAPH:
            self.out.newline(blank=True)
        elif tag in _BLOCK:
            self.out.newline()
        elif tag in _CELL:
            self.out.text(" ", self._top().quote)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._event()
        if self.open["head"] and tag not in _HEAD_CONTENT:
            self._close("head")
        while self.stack and self.stack[-1].tag in _CLOSES and tag in _CLOSES[self.stack[-1].tag]:
            self._close(self.stack[-1].tag)
        amap: dict[str, str | None] = {}
        for name, value in attrs:
            amap.setdefault(name.lower(), value)  # a duplicated attribute keeps its first value
        parent = self._top()
        if tag == "br":
            if self._shows(parent):
                self.out.newline(force=True)
            return
        self._break_for(tag)
        if self._void(tag):
            return
        style = _style(amap.get("style") or "")
        colour = _text_colour(style, tag, amap, parent.colour)
        self.open[tag] += 1
        self.stack.append(
            _Node(
                tag=tag,
                hidden=parent.hidden or _hidden_by(style, amap),
                font_zero=_font_zero(style, tag, amap, parent.font_zero),
                invisible=_invisible(style, parent.invisible),
                skip=parent.skip or tag in _SKIP_CONTENT,
                colour=colour,
                background=_background(style, amap, colour, parent.background),
                quote=parent.quote + (tag == "blockquote"),
                pre=parent.pre or tag == "pre",
            )
        )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML5 ignores the "/" on a non-void HTML element: it stays open (and keeps hiding
        # what follows); in foreign content (<svg>, <math>) it does close the element.
        foreign = tag in _FOREIGN or self._in_foreign()
        void = self._void(tag)
        self.handle_starttag(tag, attrs)
        if not void and foreign:
            self._close(tag)

    def _in_foreign(self) -> bool:
        return any(self.open[f] for f in _FOREIGN)

    def _void(self, tag: str) -> bool:
        return tag in _VOID and not (tag in _VOID_HTML_ONLY and self._in_foreign())

    def handle_endtag(self, tag: str) -> None:
        self._event()
        if tag == "br":
            self.handle_starttag(tag, [])
            return
        self._close(tag)

    def handle_comment(self, data: str) -> None:
        self._event()

    def handle_decl(self, decl: str) -> None:
        self._event()

    def handle_pi(self, data: str) -> None:
        self._event()

    def unknown_decl(self, data: str) -> None:
        self._event()

    def handle_data(self, data: str) -> None:
        self._event()
        node = self._top()
        if node.tag == "head" and data.strip(" \t\n\r\f"):
            self._close("head")  # text directly in <head> ends it implicitly (HTML5)
            node = self._top()
        if not self._shows(node):
            return
        if node.colour == _TRANSPARENT:
            return
        if node.colour is not None and node.colour == node.background:
            return
        if self.limit is not None:
            data = data[: self.limit - self.out.chars + 1]
        if node.pre:
            lines = data.replace("\r\n", "\n").split("\n")
            for i, line in enumerate(lines):
                if i:
                    self.out.newline(force=True)
                self.out.text(line, node.quote, collapse=False)
        else:
            self.out.text(_WS.sub(" ", data), node.quote)
        if self.limit is not None and self.out.chars > self.limit:
            raise _Stop


def html_to_text_limited(html: str, limit: int) -> tuple[str, bool]:
    """Like :func:`html_to_text`, but stops as soon as more than ``limit`` characters of text
    were produced or more than :data:`MAX_HTML_EVENTS` parser events were handled, so a huge
    part costs bounded work (module docstring, T13). Returns (text, stopped early)."""
    conv = _Converter(limit)
    try:
        pos = 0
        while pos < len(html):
            # html.parser rescans what it holds back on every feed: double the feed while a
            # construct is unfinished, so those rescans cost O(n) in total, not O(n²/chunk).
            # ``rawdata`` (the held-back input) is undocumented but typed in typeshed.
            size = max(_FEED_CHUNK, len(conv.rawdata))
            conv.feed(html[pos : pos + size])
            pos += size
        conv.close()
    except _Stop:
        return conv.out.render(), True
    return conv.out.render(), False


def html_to_text(html: str) -> str:
    """Visible text of an HTML document or fragment, hidden text removed (module docstring)."""
    conv = _Converter()
    conv.feed(html)
    conv.close()
    return conv.out.render()
