"""1Password items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --format json`` (every category, metadata only). Two tiers:
Login items (with a website) and Credit Card items are fillable; every other
category is listed as kind ``reference`` (title, category, vault) and can never
be filled. ``additional_information`` is shown only for logins (the username);
for a card it yields ``last4`` only when it is a masked four-digit suffix.
Resolve: ``op item get <id> --vault <vault id> ...`` (service accounts require
the vault): ``--fields label=password`` for a login, ``--format json`` for a
card, parsed in-process by built-in field id (``ccnum``, ``cvv``, ``cardholder``,
``expiry``; ``op item template get "Credit Card"``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, VaultAccessDenied, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_OP_ID = re.compile(r"^[A-Za-z0-9]+$")  # op item/vault ids; nothing starting "-" reaches argv

_LOGIN, _CARD = "LOGIN", "CREDIT_CARD"
# A card's list summary is shown as last4 only when it is nothing but mask characters and exactly
# four digits; anything with more digits (a full or partial PAN) is dropped, never trimmed.
_MASKED_LAST4 = re.compile(r"^[\s*•·.xX-]*(\d{4})$")
_ACCESS_DENIED = ("isn't an item in", "is not an item in", "forbidden", "403", "permission", "not authorized",
                  "doesn't have access", "does not have access")

# Listing cache for discovery (`list_items`: browser_vault_list, vault.list): an all-category
# `op item list` draws on the account's shared rate budget. Keyed by profile home + a hash of the
# credential, so a lock, a token change or another profile never reads another's listing. Never used to
# authorize: `get_meta` (fill-time origins) and every secret read list fresh.
_LIST_TTL_S = 60.0
_LIST_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict]]] = {}
_LIST_LOCK = threading.Lock()


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> Dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        for k in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        return bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _run(self, *args: str) -> str:
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            if "session" in err.lower() or "sign in" in err.lower() or "not signed in" in err.lower():
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            if any(marker in err.lower() for marker in _ACCESS_DENIED):
                raise VaultAccessDenied(f"1Password refused access: {err[:200]}")
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self) -> List[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        out: List[VaultItemMeta] = []
        for item in self._list_cached():
            meta = _meta_for(self.prefix, item)
            if meta is not None:
                out.append(meta)
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        """Fresh metadata for ONE item. The fill path authorizes against these origins, so this never reads
        the listing cache: a website changed in 1Password moments ago must not still authorize the old one."""
        if not self.is_unlocked():
            return None
        item_id = handle[len(self.prefix):] if handle.startswith(self.prefix) else ""
        matches = [i for i in self._list_raw() if item_id and str(i.get("id") or "") == item_id]
        if len(matches) != 1:
            return None
        return _meta_for(self.prefix, matches[0])

    def _list_raw(self) -> List[Dict]:
        raw = json.loads(self._run("item", "list", "--format", "json") or "[]")
        return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []

    def _list_cached(self) -> List[Dict]:
        from hermes_constants import hermes_home_key
        credential = self._service_token or _unlock.get_session_token(self.name) or ""
        key = (hermes_home_key(), sha256(credential.encode()).hexdigest())
        now = time.monotonic()
        with _LIST_LOCK:
            hit = _LIST_CACHE.get(key)
            if hit and now - hit[0] < _LIST_TTL_S:
                return hit[1]
        items = self._list_raw()
        with _LIST_LOCK:
            for stale in [k for k, (ts, _) in _LIST_CACHE.items() if now - ts >= _LIST_TTL_S]:
                del _LIST_CACHE[stale]
            _LIST_CACHE[key] = (now, items)
        return items

    def _scoped_item(self, handle: str, category: str) -> List[str]:
        """``<id> --vault <vault id>`` for a listed item of ``category``.

        Service-account auth refuses an unscoped item read ("a vault query must be provided"),
        so every read names the vault the listing says owns the item. The listing is read fresh,
        never from the metadata cache. Only an id listed exactly once, in the category the caller
        is about to read, is resolved: a missing, ambiguous or other-category id fails closed,
        which also keeps a caller-supplied handle from reaching op's argv unvetted.
        """
        item_id = handle[len(self.prefix):] if handle.startswith(self.prefix) else ""
        matches = [i for i in self._list_raw() if item_id and str(i.get("id") or "") == item_id]
        if len(matches) != 1 or str(matches[0].get("category") or "").upper() != category:
            reason = "ambiguous" if len(matches) > 1 else "not listed" if not matches else "not fillable"
            raise RuntimeError(f"1Password item {handle!r} is {reason}")
        vault = matches[0].get("vault")
        vault_id = str(vault.get("id") or "") if isinstance(vault, dict) else ""
        if not _OP_ID.match(item_id) or not _OP_ID.match(vault_id):
            raise RuntimeError(f"1Password item {handle!r} has no resolvable vault")
        return [item_id, "--vault", vault_id]

    def resolve_password(self, handle: str) -> str:
        return self._run("item", "get", *self._scoped_item(handle, _LOGIN), "--fields", "label=password",
                         "--reveal").rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        try:
            code = self._run("item", "get", *self._scoped_item(handle, _LOGIN), "--otp").strip()
        except Exception:
            return None
        return code if code.isdigit() else None

    def resolve_secret(self, handle: str) -> Dict[str, str]:
        """Card fields of a Credit Card item, keyed by ``PAYMENT_FIELDS``. Any other category raises.

        The JSON is parsed in-process (secrets never reach argv or a log); only the built-in
        ``ccnum``/``cvv``/``cardholder``/``expiry`` fields are kept and each is validated. A
        malformed value raises :class:`CardDataError` naming the field, never its value.
        """
        scoped = self._scoped_item(handle, _CARD)
        try:
            item = json.loads(self._run("item", "get", *scoped, "--format", "json", "--reveal") or "{}")
        except ValueError:
            raise CardDataError("1Password returned an unreadable item") from None
        if not isinstance(item, dict) or str(item.get("category") or "").upper() != _CARD \
                or str(item.get("id") or "") != scoped[0]:
            raise CardDataError("1Password returned a different item than the one listed")
        return parse_card_fields(item.get("fields"))


class CardDataError(RuntimeError):
    """A card item is missing a required field or holds a malformed one. Never carries a value."""


_CARD_FIELD_IDS = ("ccnum", "cvv", "cardholder", "expiry")


def parse_card_fields(fields) -> Dict[str, str]:
    """Map a 1Password Credit Card item's ``fields`` onto ``PAYMENT_FIELDS`` names, by built-in field id
    (labels are localised and user-editable). Every other field is dropped. A built-in id that appears
    twice is refused rather than guessed. 1Password's Credit Card category has no postal-code field, so
    ``billing_postal_code`` is never produced."""
    by_id: Dict[str, str] = {}
    for f in fields if isinstance(fields, list) else []:
        fid = f.get("id") if isinstance(f, dict) else None
        if fid not in _CARD_FIELD_IDS:
            continue
        if fid in by_id:
            raise CardDataError(f"card field {fid!r} appears more than once")
        value = f.get("value")
        by_id[fid] = "" if value is None else str(value)
    number = re.sub(r"[\s-]", "", by_id.get("ccnum", ""))
    if not (number.isascii() and number.isdigit() and 12 <= len(number) <= 19 and _luhn_ok(number)):
        raise CardDataError("card number is missing or not a valid card number")
    cvc = by_id.get("cvv", "").strip()
    if not (cvc.isascii() and cvc.isdigit() and 3 <= len(cvc) <= 4):
        raise CardDataError("verification number is missing or not 3-4 digits")
    month, year = _parse_expiry(by_id.get("expiry", ""))
    out = {"card_number": number, "cvc": cvc, "exp_month": month, "exp_year": year}
    # Real items carry editor residue (a trailing newline, a tab between first and last name, a soft
    # wrap). The name is a non-secret display field, so normalise whitespace instead of refusing the
    # whole card; only a name that is still non-printable after that, or absurdly long, is refused.
    name = " ".join(by_id.get("cardholder", "").split())
    if name:
        if len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise CardDataError("cardholder name is malformed")
        out["cardholder_name"] = name
    return out


def _parse_expiry(value: str) -> Tuple[str, str]:
    """1Password MONTH_YEAR: ``YYYYMM`` or ``YYYY/MM`` (developer docs, item fields); ``MM/YYYY`` is the
    display form. Returns zero-padded ``MM`` and four-digit ``YYYY``."""
    v = value.strip()
    m = (re.fullmatch(r"(\d{4})(\d{2})", v) or re.fullmatch(r"(\d{4})/(\d{1,2})", v))
    if m:
        year, month = m.group(1), m.group(2)
    else:
        m = re.fullmatch(r"(\d{1,2})/(\d{4})", v)
        if not m:
            raise CardDataError("expiry date is missing or not in a 1Password month/year format")
        month, year = m.group(1), m.group(2)
    if not 1 <= int(month) <= 12 or not 2000 <= int(year) <= 2099:
        raise CardDataError("expiry date is out of range")
    return month.zfill(2), year


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _meta_for(prefix: str, item: Dict) -> Optional[VaultItemMeta]:
    category = str(item.get("category") or "").upper()
    return (_login_meta if category == _LOGIN else _card_meta if category == _CARD else _reference_meta)(prefix, item)


def _vault_name(item: Dict) -> Optional[str]:
    vault = item.get("vault")
    name = str(vault.get("name") or "").strip() if isinstance(vault, dict) else ""
    return name or None


def _item_urls(item: Dict) -> List[str]:
    return [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]


def _login_meta(prefix: str, item: Dict) -> Optional[VaultItemMeta]:
    origins = _all_origins(_item_urls(item))
    if not origins:
        return None  # a website-less login has nowhere it may be filled
    username = str(item.get("additional_information") or "").strip() or None
    return VaultItemMeta(
        id=f"{prefix}{item.get('id')}", kind="login", label=str(item.get("title") or origins[0]),
        origin=origins[0], created_at=str(item.get("created_at") or ""),
        identifier_type="username" if username else None, identifier=username,
        allowed_origins=_web_origins(origins), category=_LOGIN, vault=_vault_name(item))


def _card_meta(prefix: str, item: Dict) -> VaultItemMeta:
    """A card binds to its saved websites when it has any (exact match, as for logins, whatever their
    scheme); only a card with NO saved website is filled on the page the user approves for that one fill."""
    origins = _all_origins(_item_urls(item))
    bound = _web_origins(origins) if origins else ()
    m = _MASKED_LAST4.match(str(item.get("additional_information") or "").strip())
    return VaultItemMeta(
        id=f"{prefix}{item.get('id')}", kind="payment", label=str(item.get("title") or "Credit card"),
        origin=bound[0] if bound else None, created_at=str(item.get("created_at") or ""),
        allowed_origins=bound, category=_CARD, vault=_vault_name(item), last4=m.group(1) if m else None)


def _reference_meta(prefix: str, item: Dict) -> VaultItemMeta:
    """Every other category: discoverable, never fillable. ``additional_information`` is dropped: for
    these categories it can hold account numbers, file names or key fingerprints."""
    category = str(item.get("category") or "").upper()
    return VaultItemMeta(
        id=f"{prefix}{item.get('id')}", kind="reference", label=str(item.get("title") or category.title() or "Item"),
        origin=None, created_at=str(item.get("created_at") or ""), category=category or None,
        vault=_vault_name(item), fill_supported=False)


def _web_origins(origins: List[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: List[str]) -> List[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: List[str] = []
    for u in urls:
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out
