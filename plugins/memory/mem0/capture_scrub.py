"""Deterministic (non-LLM) secret scrubber for mem0 auto-capture (spec INV-4 / NB1).

The salience GATE (an LLM prompt) is NOT a reliable secret boundary — the gated experiment
leaked a bot token verbatim THROUGH the rubric. So a candidate extracted fact must pass a
DETERMINISTIC pattern scrubber before it is accepted for the store/recall surface. A fact that
trips a high-confidence secret pattern is DROPPED (returned as rejected), not stored.

This is intentionally conservative + fail-closed: a false positive drops one durable fact (the
deliberate `mem0_conclude` path is the belt-and-suspenders for anything auto missed); a false
negative writes a secret into a recalled store, which is the exfiltration risk we must not take.

Patterns mirror the fleet redaction set (doc-share privacy-scan + qmd_secrets lineage). Kept as
a standalone module so it is unit-testable and reusable by the A-full server-side seam too.
"""

from __future__ import annotations

import math
import re
from typing import List, Tuple

# High-confidence secret shapes — a hit here DROPS the fact (INV-4).
_SECRET_PATTERNS = [
    # OpenAI / Anthropic / OpenRouter style keys
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "openai_or_anthropic_key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), "anthropic_key"),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{16,}\b"), "openrouter_key"),
    # GitHub tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "github_token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat"),
    # AWS
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "aws_sts_key"),
    # Google API key
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "google_api_key"),
    # Slack
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    # Telegram bot token (digits:base64ish) — this is exactly what the experiment leaked
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "telegram_bot_token"),
    # JWT (three base64url segments)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "jwt"),
    # PEM private key blocks
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "pem_private_key"),
    # Bearer header carrying a real-looking token
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b"), "bearer_token"),
    # Common connection strings with an inline password
    (re.compile(r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/]+:[^\s:@/]+@"), "conn_string_with_password"),
    # AWS secret-access-key-shaped assignment (40 char base64) next to a secret-y label
    (re.compile(r"(?i)\b(?:secret|token|passwd|password|api[_-]?key)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+=_-]{20,}"), "labeled_secret_assignment"),
]

# Natural-language credential disclosure is handled by a code helper (below) rather than a single
# mega-regex, because the real discriminator between a leaked secret and an innocent English
# sentence ("password does not work", "password is required") is the *shape of the value token*,
# which needs character-class counting that regex does poorly. A cred word near a secret-shaped
# value = drop.
_CRED_WORD = re.compile(r"(?i)\b(?:pass(?:word|code|phrase)|passwd|pwd)\b")
# The cred word is a noun modifier here, not a disclosure ('password manager', 'password field').
_CRED_ROLE_NOUN = re.compile(
    r"(?i)(?:manager|managers|app|apps|application|field|fields|box|prompt|step|screen|wall|"
    r"policy|policies|reset|resets|recovery|rotation|entry|entries|store|vault|hint|hints|"
    r"strength|length|requirement|requirements|protection|protected|authentication|form)\b"
)
_QUOTES = "'\"`\u2018\u2019\u201c\u201d"
_QUOTED_VAL = re.compile(r"[" + _QUOTES + r"]([^" + _QUOTES + r"]{5,64})[" + _QUOTES + r"]")
_BARE_TOKEN = re.compile(r"[^\s'\"`,;]{8,64}")
# A connector that signals an actual value follows ('password is X', 'password: X', 'password = X').
_CRED_CONNECTOR = re.compile(r"(?i)^\s*\w{0,20}\s*(?:\bis\b|\bwas\b|\bset to\b|[:=])\s*$")

# A 1Password reference (op://...) is SAFE-BY-DESIGN — it's a pointer, not a secret. Never drop it.
_OP_REF = re.compile(r"\bop://[^\s'\"]+")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    from collections import Counter
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _looks_high_entropy_secret(token: str) -> bool:
    """A bare high-entropy blob (>=24 chars, mixed classes, entropy>~3.5) that isn't a word/path.
    Used only as a LOW-confidence supplementary signal, gated on multiple class requirements to
    avoid nuking normal identifiers/hashes-in-prose."""
    if len(token) < 24:
        return False
    if not re.search(r"[a-z]", token) or not re.search(r"[A-Z0-9]", token):
        return False
    if "/" in token or " " in token:  # paths / phrases are not bare secrets
        return False
    # must have digits AND letters AND (a symbol OR be long)
    has_digit = bool(re.search(r"\d", token))
    has_alpha = bool(re.search(r"[A-Za-z]", token))
    has_sym = bool(re.search(r"[_+=/-]", token))
    if not (has_digit and has_alpha and (has_sym or len(token) >= 32)):
        return False
    return _shannon_entropy(token) >= 3.5


# Common English words that can follow "password" without being a leaked value — guards the bare
# path against sentences like "password is required/unknown/correct/working/rotated/different".
_CRED_STOPWORDS = frozenset({
    "is", "was", "are", "the", "a", "an", "to", "for", "of", "on", "in", "and", "or", "not",
    "required", "correct", "incorrect", "unknown", "working", "works", "rotated", "changed",
    "different", "same", "valid", "invalid", "empty", "missing", "set", "reset", "stored",
    "saved", "does", "did", "must", "should", "authentication", "auth", "field", "prompt",
    "step", "wall", "manager", "protected", "account", "login", "user", "reuse", "reused",
    "hash", "hashed", "policy", "recovery", "confirmation", "known", "provided", "above",
})

# Product names / proper-noun compounds that carry a digit or mixed-case (so they pass the
# secret-shape test) but are common non-secrets appearing near the word "password".
_CRED_NONSECRET_WORDS = frozenset({
    "1password", "lastpass", "bitwarden", "keepassxc", "keepass", "dashlane",
    "nordpass", "protonpass", "onepassword", "macos", "iphone", "ipados",
})


def _looks_like_password_value(token: str) -> bool:
    """True if `token` looks like an actual literal password/secret value rather than an English
    word or identifier. The bare (unquoted) path is deliberately STRICT to avoid flagging
    hyphenated compounds ('nexus-command', 'Plex-owned'), URLs, and file paths that legitimately
    appear near the word 'password'."""
    t = token.strip(".,;:!?)('\"`\u2018\u2019\u201c\u201d")
    if len(t) < 8 or len(t) > 64:
        return False
    if t.lower() in _CRED_STOPWORDS:
        return False
    # Known product / proper-noun compounds that carry a digit+case but are NOT secrets.
    if t.lower() in _CRED_NONSECRET_WORDS:
        return False
    # URLs / paths / emails are not bare password values
    if "://" in t or "/" in t or "@" in t or "\\" in t:
        return False
    # A hyphenated lowercase word-compound (nexus-command, plex-owned, cross-seed) is an
    # identifier, not a secret — reject unless it also carries a digit or a symbol beyond '-'.
    core = t
    has_digit = bool(re.search(r"\d", core))
    has_special = bool(re.search(r"[!@#$%^&*_+=~<>?]", core))  # NOT counting '-' or '.'
    has_upper = bool(re.search(r"[A-Z]", core))
    has_lower = bool(re.search(r"[a-z]", core))
    if "-" in core and not (has_digit or has_special):
        return False
    # secret-shaped: a digit or a strong symbol alongside letters, OR long mixed-case with digit
    if (has_digit or has_special) and (has_upper or has_lower):
        return True
    return False


def _scan_nl_credentials(text: str) -> List[str]:
    """Detect natural-language credential disclosure: a credential word ('password', 'passcode',
    'passphrase', 'passwd', 'pwd') sitting near a value token that looks like a real secret. Two
    value shapes are caught: a QUOTED value or a BARE secret-shaped token within a short window
    after the cred word.

    Guards against false positives: a cred word used as a NOUN MODIFIER ('password manager',
    'password app/field/policy/step/prompt/reset') is not a disclosure, and a quoted value that is
    a known product name ('1Password', 'Bitwarden') is not a secret.
    """
    hits: List[str] = []
    for m in _CRED_WORD.finditer(text):
        # Skip 'password manager/app/field/...' — the cred word is modifying a noun, not disclosing.
        after = text[m.end(): m.end() + 12].lstrip()
        if _CRED_ROLE_NOUN.match(after):
            continue
        window = text[m.end(): m.end() + 60]
        matched = False
        # 1) quoted value in the window
        for qm in _QUOTED_VAL.finditer(window):
            val = qm.group(1).strip()
            low = val.lower()
            if low in _CRED_STOPWORDS or low in _CRED_NONSECRET_WORDS:
                continue  # quoted product/label name, not a secret
            # a multi-word quoted value is a passphrase iff it has >=3 tokens (correct horse battery
            # staple) OR any token is secret-shaped; a 1-2 word quoted proper noun is not.
            words = val.split()
            if len(words) >= 3 or any(_looks_like_password_value(w) for w in words) or (
                len(words) == 1 and len(val) >= 6
            ):
                hits.append("nl_credential_quoted")
                matched = True
                break
        if matched:
            continue
        # 2) bare secret-shaped token in the window
        for bm in _BARE_TOKEN.finditer(window):
            tok = bm.group(0)
            if _looks_like_password_value(tok):
                hits.append("nl_credential_bare")
                break
            # pure-digit PIN (>=6 digits) ONLY when it directly follows a value connector, so we
            # don't flag ports/IDs/counts that merely sit near the word 'password'.
            core = tok.strip(".,;:!?)('\"`\u2018\u2019\u201c\u201d")
            if core.isdigit() and len(core) >= 6:
                pre = window[: bm.start()]
                if _CRED_CONNECTOR.search(pre[-12:]) or pre.strip() in ("is", "was", ":", "="):
                    hits.append("nl_credential_bare")
                    break
    return hits


def scan(text: str, *, entropy_check: bool = False) -> List[str]:
    """Return a list of matched secret-pattern names in `text`. Empty = clean.
    op:// references are stripped before scanning so they never count as a hit."""
    if not text:
        return []
    scrubbed = _OP_REF.sub("", text)
    hits: List[str] = []
    for pat, name in _SECRET_PATTERNS:
        if pat.search(scrubbed):
            hits.append(name)
    hits.extend(_scan_nl_credentials(scrubbed))
    if entropy_check:
        for tok in re.findall(r"[A-Za-z0-9._+=/-]{24,}", scrubbed):
            if _looks_high_entropy_secret(tok):
                hits.append("high_entropy_blob")
                break
    return hits


def is_secret(text: str, *, entropy_check: bool = False) -> bool:
    return bool(scan(text, entropy_check=entropy_check))


def filter_facts(facts: List[str], *, entropy_check: bool = False) -> Tuple[List[str], List[dict]]:
    """Split extracted facts into (kept, dropped). A dropped fact carries the matched pattern
    names so the drop is auditable (logged as a count + reason, never the secret itself)."""
    kept: List[str] = []
    dropped: List[dict] = []
    for f in facts:
        hits = scan(f, entropy_check=entropy_check)
        if hits:
            dropped.append({"reason": ",".join(sorted(set(hits))), "len": len(f)})
        else:
            kept.append(f)
    return kept, dropped
