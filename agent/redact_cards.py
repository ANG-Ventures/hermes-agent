"""Payment card numbers in tool results, inbound user text and browser screenshots (t_90c8b36c).

On 2026-11-27 one card's full number sat in 15 stored message rows, written by three routes: terminal output
of checkout form-fill scripts, image reads of screenshots, and an owner chat message. Part of the runtime's
redaction layer (``agent/redact.py`` masks secrets; this module masks card numbers). Three switches, each
read from ``security.*`` in config.yaml:

- ``security.mask_card_numbers_tool_results`` (default true): ``mask_card_numbers`` runs on every tool result
  in ``make_tool_result_message`` (``agent/tool_dispatch_helpers.py``), so the masked text is what enters the
  transcript, the session stores and the next model request. ``read_file``/``search_files`` are exempt: their
  text must match the disk so an edit never writes the X's back.
- ``security.mask_card_numbers_user_messages`` (default FALSE): the same mask on inbound user text. It changes
  what the owner typed, so it is turned on only after the owner's decision is recorded on t_90c8b36c.
- ``security.browser_vision_card_ocr`` (default true): ``browser_vision`` OCRs its own screenshot locally and
  refuses with ``CARD_IMAGE_REFUSAL`` before any model sees a full card number. Same check and wording as the
  home repo's hook gate (``hooks/gate_card_image.py``), which covers local-file image reads.

Masking rule (false positives cost more than misses): a 15 or 16 digit run in card grouping (4-4-4-4, 4-6-5,
or unbroken), not touching a letter or digit, that passes the Luhn check, starts with a card-network prefix,
and is not a published sample number. All but the last four digits become ``X``, separators are kept. A CVC
or expiry is masked only when it carries its label and sits on the same line as a masked number or on one of
the next two lines.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Published test numbers sites print as placeholders (Luhn-valid, never a real account). Kept equal to
# PLACEHOLDERS in the home repo's hooks/gate_card_image.py.
PLACEHOLDERS = frozenset({
    "4444333322221111", "4111111111111111", "4242424242424242", "4012888888881881", "4000056655665556",
    "5555555555554444", "5105105105105100", "2223003122003222", "378282246310005", "371449635398431",
    "6011111111111117", "3056930009020004", "3566002020360505"})

# Same wording as hooks/gate_card_image.py DOOR.
CARD_IMAGE_REFUSAL = (
    "the image shows a full payment card number; black out the card number, expiry and CVC fields (or crop "
    "them away) in a copy of the image and read that copy; never ask a model to read an unmasked card")

GROUPINGS = ((4, 4, 4, 4), (4, 6, 5), (16,), (15,))
# A space or tab, raw or as its JSON escape (``\\t``); a hyphen/dash, raw or as a JSON ``\\u2010``-``\\u2015``
# escape (``json.dumps`` with ``ensure_ascii``). Escape digits are separator text, never card digits.
_SP = r"(?:[ \t]|\\t)"
_SEP = r"%s{0,3}(?:[-\u2010-\u2015]|\\u201[0-5])%s{0,3}|%s{1,3}" % (_SP, _SP, _SP)
_RUN = re.compile(r"\d+(?:(?:%s)\d+)*" % _SEP)
_GROUP = re.compile(r"\\u201[0-5]|\d+")

TESSERACT = ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract")
OCR_TIMEOUT_S = 5.0
MAX_OCR_BYTES = 64 << 20


def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def has_network_prefix(digits: str) -> bool:
    """Issuer prefix for the number's length: 15 digits = American Express (34/37); 16 digits = Visa (4),
    Mastercard (51-55, 2221-2720), Discover (6011, 644-649, 65), UnionPay (62), JCB (3528-3589),
    Diners (300-305, 36, 38, 39)."""
    if len(digits) == 15:
        return digits[:2] in ("34", "37")
    if len(digits) != 16:
        return False
    p2, p3, p4 = int(digits[:2]), int(digits[:3]), int(digits[:4])
    return (
        digits[0] == "4"
        or 51 <= p2 <= 55 or 2221 <= p4 <= 2720
        or p4 == 6011 or 644 <= p3 <= 649 or p2 in (62, 65)
        or 3528 <= p4 <= 3589
        or 300 <= p3 <= 305 or p2 in (36, 38, 39)
    )


def _touches_word(text: str, start: int, end: int) -> bool:
    """True when a letter or digit touches [start, end) (a hex hash, a longer id). A JSON escape
    (``\\n``, ``\\t``, ``\\r``) just before the number is a separator, not a letter."""
    if start > 0 and text[start - 1].isalnum():
        if not (text[start - 1] in "ntr" and start > 1 and text[start - 2] == "\\"):
            return True
    return end < len(text) and text[end].isalnum()


def _card_spans(text: str, *, strict: bool) -> Iterator[List[Tuple[int, int]]]:
    """The digit-group spans (absolute ``(start, end)``) of each card number in *text*. Every group is tried
    as a start, so two numbers that share a group are both found. ``strict`` (the mask) also requires a
    network prefix and no letter/digit touching the number; non-strict is the hook gate's OCR check."""
    for run in _RUN.finditer(text):
        groups = [g for g in _GROUP.finditer(run.group(0)) if g.group(0)[0] != "\\"]
        for i in range(len(groups)):
            for shape in GROUPINGS:
                part = groups[i:i + len(shape)]
                if tuple(len(g.group(0)) for g in part) != shape:
                    continue
                # Unicode decimal digits (full-width etc.) are judged as their ASCII values.
                digits = "".join(str(int(c)) for g in part for c in g.group(0))
                if not luhn(digits) or digits in PLACEHOLDERS:
                    continue
                start, end = run.start() + part[0].start(), run.start() + part[-1].end()
                if strict:
                    if not has_network_prefix(digits):
                        continue
                    if _touches_word(text, start, end):
                        continue
                yield [(run.start() + g.start(), run.start() + g.end()) for g in part]
                break


def count_card_numbers(text: str) -> int:
    """How many card numbers *text* shows, by the hook gate's rule (per line, no prefix requirement)."""
    return sum(1 for line in (text or "").splitlines() for _ in _card_spans(line, strict=False))


# A line break in raw text, or the two-character ``\n`` escape inside a JSON-encoded tool result.
_LINE_BREAK = re.compile(r"\r\n|\n|\r|\\n")
# Label left edge: not inside a word (a JSON ``\n``/``\t``/``\r`` escape counts as a break), except a
# camelCase join (``cardCvc``, ``cardExpiry``).
_LEFT = r"(?:(?<![A-Za-z])|(?<=\\[ntr])|(?<=[a-z])(?=[A-Z]))"
_RIGHT = r"(?![A-Za-z])"
_LABEL_SEP = r"[ \t\"'\\:=]{0,8}"
_CVC_RE = re.compile(
    _LEFT + r"(?i:cvc2?|cvv2?|csc|security[ _-]?code)" + _RIGHT + _LABEL_SEP + r"(?P<v>\d{3,4})(?!\d)")
_EXP_RE = re.compile(
    _LEFT + r"(?i:exp(?:iry|iration|ires)?(?:[ _-]?(?:date|month|year|mm/?yy|mm|yy))?|valid[ _-]?thru)"
    + _RIGHT + _LABEL_SEP
    + r"(?P<v>\d{1,2}[ \t]?[/-][ \t]?(?:\d{4}|\d{2})|\d{4}|\d{2})(?![\d/-])")


def _window(text: str, start: int, end: int) -> Tuple[int, int]:
    """From the start of the line holding [start, end) to the end of the second line after it."""
    line_start = 0
    for m in _LINE_BREAK.finditer(text, 0, start):
        line_start = m.end()
    stop, breaks = len(text), 0
    for m in _LINE_BREAK.finditer(text, end):
        breaks += 1
        if breaks == 3:
            stop = m.start()
            break
    return line_start, stop


# A bare JSON value: right after ``"key":`` and right before ``,`` ``}`` ``]`` or the end of the text.
_JSON_KEY_BEFORE = re.compile(r'"[ \t]*:[ \t]*$')
_JSON_VALUE_AFTER = re.compile(r"[ \t]*(?:[,}\]]|$)")


def mask_card_numbers(text: str) -> str:
    """*text* with every card number masked to its last four, plus labelled CVC/expiry near one.

    Digits become ``X`` and separators are kept. Numbers that share a group are masked together. A masked
    bare JSON number (``"cvv":123``) is quoted so a valid JSON payload stays valid."""
    if not isinstance(text, str) or len(text) < 15:
        return text
    cards = list(_card_spans(text, strict=True))
    if not cards:
        return text
    masked: set = set()
    values: List[Tuple[int, int]] = []
    for groups in cards:
        digit_pos = [i for s, e in groups for i in range(s, e)]
        masked.update(digit_pos[:-4])
        start, end = groups[0][0], groups[-1][1]
        values.append((start, end))
        lo, hi = _window(text, start, end)
        for rx in (_CVC_RE, _EXP_RE):
            for m in rx.finditer(text, lo, hi):
                vs, ve = m.span("v")
                masked.update(i for i in range(vs, ve) if text[i].isdigit())
                values.append((vs, ve))
    quote_at = set()
    for vs, ve in values:
        if (vs > 0 and text[vs - 1] != '"' and _JSON_KEY_BEFORE.search(text[max(0, vs - 16):vs])
                and _JSON_VALUE_AFTER.match(text, ve)):
            quote_at.update((vs, ve))
    out = []
    for i, ch in enumerate(text):
        if i in quote_at:
            out.append('"')
        out.append("X" if i in masked else ch)
    if len(text) in quote_at:
        out.append('"')
    return "".join(out)


def mask_card_numbers_in_content(content: Any) -> Any:
    """``mask_card_numbers`` over a message ``content``: a string, or a list of parts whose ``text`` is masked.
    Image parts and other shapes pass through unchanged. Returns the input object when nothing changed."""
    if isinstance(content, str):
        return mask_card_numbers(content)
    if isinstance(content, list):
        changed, parts = False, []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                masked = mask_card_numbers(part["text"])
                if masked != part["text"]:
                    part, changed = {**part, "text": masked}, True
            parts.append(part)
        return parts if changed else content
    return content


_KNOB_DEFAULTS = {
    "mask_card_numbers_tool_results": True,
    "mask_card_numbers_user_messages": False,
    "browser_vision_card_ocr": True,
}


def card_knob(name: str) -> bool:
    """``security.<name>`` from config.yaml, or its default when unset or unreadable."""
    default = _KNOB_DEFAULTS[name]
    try:
        from hermes_cli.config import load_config_readonly
        value = (load_config_readonly().get("security") or {}).get(name)
    except Exception:
        return default
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def mask_tool_result_content(content: Any) -> Any:
    """Tool-result entry point (``security.mask_card_numbers_tool_results``)."""
    if not card_knob("mask_card_numbers_tool_results"):
        return content
    return mask_card_numbers_in_content(content)


def mask_user_message_content(content: Any) -> Any:
    """Inbound user-message entry point (``security.mask_card_numbers_user_messages``, default off)."""
    if content is None or not card_knob("mask_card_numbers_user_messages"):
        return content
    return mask_card_numbers_in_content(content)


def _ocr(path: str) -> Tuple[Optional[str], Optional[str]]:
    """(text, None) or (None, why OCR did not run). Same tesseract call as hooks/gate_card_image.py."""
    binary = next((b for b in TESSERACT if os.access(b, os.X_OK)), None)
    if binary is None:
        return None, "tesseract not installed"
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_OCR_BYTES + 1)
        if len(data) > MAX_OCR_BYTES:
            return None, "image larger than %d MiB, not OCRed" % (MAX_OCR_BYTES >> 20)
        from tools.environments.local import build_subprocess_env
        out = subprocess.run([binary, "stdin", "stdout", "--psm", "11"], input=data, capture_output=True,
                             timeout=OCR_TIMEOUT_S, env=build_subprocess_env(extra={"OMP_THREAD_LIMIT": "1"}))
    except subprocess.TimeoutExpired:
        return None, "OCR timed out after %.0f s" % OCR_TIMEOUT_S
    except OSError as exc:
        return None, "OCR failed (%s)" % type(exc).__name__
    if out.returncode != 0:
        return None, "OCR exited %d" % out.returncode
    return out.stdout.decode("utf-8", "replace"), None


def screenshot_card_refusal(path: Any) -> Optional[str]:
    """``CARD_IMAGE_REFUSAL`` when the screenshot at *path* shows a full card number, else None.
    Fails open like the hook gate: no tesseract, an OCR error or timeout allows the image with one warning."""
    if not card_knob("browser_vision_card_ocr"):
        return None
    text, why = _ocr(str(path))
    if text is None:
        logger.warning("card-image OCR: %s; screenshot sent UNCHECKED for a card number: %s", why, path)
        return None
    return CARD_IMAGE_REFUSAL if count_card_numbers(text) else None
