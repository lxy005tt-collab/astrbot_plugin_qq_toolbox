"""Recognize expression-only text without discarding ordinary words."""

import re
import unicodedata

import emoji


_TEXT_FACES = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[Qq][wWＡA_aAvV][Qq]|[UuOo0][wWvV._-][UuOo0]|[Tt][AＡ._-][Tt]|"
    r"[>＞<＜^＾=＝;；TtQqOo0xXಥಠ눈-][_＿.﹏‿^＾oOω-]{1,3}[>＞<＜^＾=＝;；TtQqOo0xXಥಠ눈-]|"
    r"[Oo][Rr][Zz]|OTZ|[xX]D|[:;=8][-^']?[()DPp3/\\]"
    r")(?![A-Za-z0-9])"
)
_BRACKET_FACE = re.compile(r"[（(]([^()（）\n]{1,64})[)）]")
_FACE_LETTERS = frozenset("ωΩДдзЗ益ㅂ눈ಠಥงツᐛᵕᴗᗜつっづノヾﾉoOcCTtQqxXvVuUwW๑03")
_FACE_MARKERS = frozenset("ωΩДдзЗ∀▽△□◇﹏‿︿ᴗᗜᵕᐛツㅂ益◡_")
_ARMS = frozenset("oOｏＯcCｃＣงوヾノﾉつっづ٩۶ᕕᕗᕙᕤ乁ﾞ")


def is_expression_only(text: str) -> bool:
    """Require a known emoji or face, and preserve all remaining ordinary text.

    Emoji sequences use the Unicode emoji dictionary. Kaomoji recognition is
    deliberately limited to common facial glyphs; bracketed words stay text.
    """
    if not text.strip():
        return False
    remaining = emoji.replace_emoji(text, replace="")
    found_emoji = remaining != text
    remaining, face_count = _TEXT_FACES.subn("", remaining)

    def remove_bracket_face(match):
        nonlocal face_count
        body = match.group(1)
        if not any(char in _FACE_MARKERS for char in body):
            return match.group(0)
        if not all(
            char.isspace()
            or unicodedata.category(char)[0] in "PSM"
            or char in _FACE_LETTERS
            for char in body
        ):
            return match.group(0)
        face_count += 1
        return ""

    remaining = _BRACKET_FACE.sub(remove_bracket_face, remaining)
    if not found_emoji and not face_count:
        return False
    return all(
        char.isspace()
        or unicodedata.category(char)[0] in "PSM"
        or char in "\u200d\ufe0e\ufe0f"
        or (face_count and char in _ARMS)
        for char in remaining
    )
