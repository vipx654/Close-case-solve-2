"""Safe serialization / parsing of inline-keyboard button layouts.

Previously the bot stored button layouts as the ``str()`` of a list of
``InlineKeyboardButton`` objects and later revived them with ``eval()``.
``eval()`` on data that group admins can store is a remote-code-execution
risk (and crashes on malformed data).

New data is stored as JSON (only plain data: ``text``/``url``/``callback_data``).
Reading falls back to a *regex* based parser for old rows so legacy filters
keep working — never ``eval``.
"""
import json
import re
import logging

logger = logging.getLogger(__name__)

__all__ = ["serialize_buttons", "deserialize_buttons", "buttons_from_text"]


def _btn_to_dict(b):
    if isinstance(b, dict):
        return b
    d = {"text": getattr(b, "text", "")}
    url = getattr(b, "url", None)
    cb = getattr(b, "callback_data", None)
    if url:
        d["url"] = url
    elif cb:
        d["callback_data"] = cb
    return d


def serialize_buttons(btn) -> str:
    """Convert a list of buttons (or string/None) into a JSON string for storage."""
    if btn is None:
        return "[]"
    if isinstance(btn, str):
        # Already stored text (e.g. "[]" from the plugin code). Normalise "[]".
        if btn.strip() == "[]" or not btn.strip():
            return "[]"
        # Legacy python-repr string: parse it safely first, then re-store as JSON.
        parsed = _legacy_parse(btn)
        return json.dumps(parsed)
    try:
        rows = [[_btn_to_dict(b) for b in row] for row in btn]
        return json.dumps(rows)
    except Exception:
        logger.exception("Failed to serialize buttons")
        return "[]"


def deserialize_buttons(btn):
    """Return a list-of-lists of InlineKeyboardButton, or "[]" when there are none.

    Mirrors the old behaviour so callers can compare against ``"[]"`` and pass the
    result to ``InlineKeyboardMarkup``.
    """
    from pyrogram.types import InlineKeyboardButton

    if btn is None:
        return "[]"
    if isinstance(btn, list):
        rows = btn
    elif isinstance(btn, str):
        if btn.strip() == "[]" or not btn.strip():
            return "[]"
        rows = _parse_stored(btn)
    else:
        return "[]"

    try:
        out = []
        for row in rows:
            line = []
            for b in row:
                if not isinstance(b, dict):
                    continue
                kwargs = {"text": b.get("text", "⁣")}
                if b.get("url"):
                    kwargs["url"] = b["url"]
                elif b.get("callback_data"):
                    kwargs["callback_data"] = b["callback_data"]
                else:
                    # A button needs an action; skip unusable entries.
                    continue
                line.append(InlineKeyboardButton(**kwargs))
            if line:
                out.append(line)
        return out or "[]"
    except Exception:
        logger.exception("Failed to deserialize buttons")
        return "[]"


def _parse_stored(text: str):
    # Try JSON first (new format).
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # Legacy python-repr format -> regex parse (never eval).
    return _legacy_parse(text)


# Matches InlineKeyboardButton(text='...', url='...') / callback_data='...'
_BTN_RE = re.compile(
    r"InlineKeyboardButton\s*\(\s*(?P<args>.*?)\)\s*(?:,|\]|$)",
    re.DOTALL,
)
_KW_RE = re.compile(r"(text|url|callback_data)\s*=\s*(['\"])(.*?)\2", re.DOTALL)


def _legacy_parse(text: str):
    rows = []
    current = []
    # Split into rows on the top-level "], [" boundaries.
    for chunk in re.split(r"\]\s*,\s*\[", text):
        line = []
        for m in _BTN_RE.finditer(chunk):
            kw = {}
            for km in _KW_RE.finditer(m.group("args")):
                kw[km.group(1)] = km.group(3)
            if "text" in kw and ("url" in kw or "callback_data" in kw):
                line.append(kw)
        if line:
            rows.append(line)
    return rows


def buttons_from_text(note_text: str):
    """Placeholder for completeness; button markup in replies is handled by parser()."""
    return deserialize_buttons(note_text)
