"""Whether a quote appears in a text, the way models quote.

Models re-type punctuation and shorten long passages with "...", so quotes
are compared by their letters and digits alone (or, for a near quote, word by
word), and each shortened piece must still be specific enough to count.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# Letters and digits a quote needs, so a word or two cannot count as evidence.
MIN_QUOTE_LETTERS = 12


def quoted_in(quote: str, sources: list[str]) -> bool:
    """Whether ``quote`` appears in one of ``sources``.

    Quotes are compared by their letters and digits alone, because models
    re-type punctuation: typographic apostrophes become straight ones, and
    diff markers, comment signs, and escaping disappear. A quote shortened
    with "..." counts when each piece appears, in order. Paraphrases and
    quotes stitched together from separate places still do not match.
    """

    parts = _parts(quote)
    if parts is None:
        return False
    pieces = [letters(part) for part in parts]
    for source in sources:
        text = letters(source)
        position = 0
        for piece in pieces:
            found = text.find(piece, position)
            if found < 0:
                break
            position = found + len(piece)
        else:
            return True
    return False


_ELISION = re.compile(r"\.{3,}|…|\[\.\.\.\]")
# A quote shortened with "..." keeps a few real pieces: each one this many letters
# (or two words with a few letters fewer), so it cannot be spelled out of single
# letters or short words found anywhere in a long session.
MIN_PIECE_LETTERS = 8
MIN_PIECE_WORDS = 2
MIN_PIECE_WORDS_LETTERS = 6
MAX_QUOTE_PIECES = 4


def _parts(quote: str) -> list[str] | None:
    """The parts of ``quote`` between "..." marks, or None when they are too little to be evidence."""

    parts = [part for part in _ELISION.split(quote) if letters(part)]
    if sum(len(letters(part)) for part in parts) < MIN_QUOTE_LETTERS:
        return None
    if len(parts) > 1 and (len(parts) > MAX_QUOTE_PIECES or not all(_real_piece(part) for part in parts)):
        return None
    return parts


def _real_piece(part: str) -> bool:
    count = len(letters(part))
    return count >= MIN_PIECE_LETTERS or (len(words(part)) >= MIN_PIECE_WORDS and count >= MIN_PIECE_WORDS_LETTERS)


# A near quote: this share of its words, in order, within one stretch of the source.
NEAR_QUOTE_SHARE = 0.85
_NEAR_ANCHORS = 3
_NEAR_POSITIONS = 60
_NEAR_SLACK = 3


def nearly_quoted_in(quote: str, sources: list[str], *, share: float = NEAR_QUOTE_SHARE) -> bool:
    """Whether ``quote`` appears in one of ``sources`` with a few words re-typed, dropped, or added.

    Models copy long passages imperfectly: a changed word, a dropped article,
    a line break read as a space. Each piece of the quote (split at "...")
    must still match, word by word and in order, within one stretch of the
    source about as long as itself, so paraphrases and quotes stitched from
    separate places do not match.
    """

    parts = _parts(quote)
    if parts is None:
        return False
    pieces = [piece for piece in (words(part) for part in parts) if piece]
    for source in sources:
        tokens, positions = _word_index(source)
        after = 0
        for piece in pieces:
            end = _near_piece(piece, tokens, positions, share, after=after)
            if end is None:
                break
            after = end
        else:
            return True
    return False


def _near_piece(piece: tuple[str, ...], tokens: tuple[str, ...], positions: dict[str, list[int]], share: float, *,
                after: int = 0) -> int | None:
    """Where a near match of ``piece`` ends, at or past word ``after``; None when there is none."""

    anchors = sorted({word for word in piece if word in positions}, key=lambda word: len(positions[word]))
    best = None
    for anchor in anchors[:_NEAR_ANCHORS]:
        offset = piece.index(anchor)
        for position in [item for item in positions[anchor] if item - offset >= after - _NEAR_SLACK][:_NEAR_POSITIONS]:
            start = max(after, position - offset - _NEAR_SLACK)
            end = position - offset + len(piece) + _NEAR_SLACK
            blocks = SequenceMatcher(None, piece, tokens[start:end], autojunk=False).get_matching_blocks()
            if sum(block.size for block in blocks) >= share * len(piece):
                best = end if best is None else min(best, end)
                break
    return best


@lru_cache(maxsize=256)
def words(text: str) -> tuple[str, ...]:
    """The words of ``text``, compatibility-normalized and casefolded."""

    return tuple(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


@lru_cache(maxsize=16)
def _word_index(text: str) -> tuple[tuple[str, ...], dict[str, list[int]]]:
    tokens = words(text)
    positions: dict[str, list[int]] = {}
    for index, word in enumerate(tokens):
        positions.setdefault(word, []).append(index)
    return tokens, positions


@lru_cache(maxsize=256)
def letters(text: str) -> str:
    """Only the letters and digits of ``text``, compatibility-normalized and casefolded."""

    return "".join(character for character in unicodedata.normalize("NFKC", text).casefold() if character.isalnum())
