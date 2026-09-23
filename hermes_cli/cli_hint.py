"""Render copy-pasteable CLI hints that survive the shell and argparse verbatim.

An error message that tells the operator what to type is a remedy only if the
printed string WORKS when pasted. Interpolating a user-controlled token (a
repository key, a filesystem path) into a bare ``--flag <token>`` form breaks
for two token shapes, and breaks in the SHELL and in ARGPARSE respectively:

* a token beginning with ``-`` -- argparse binds the next token as an option
  rather than as the value and refuses with ``expected one argument``;
* a token containing whitespace -- the shell splits it into two words before
  argparse ever sees it, so the trailing half arrives as a stray positional.

Both print a remedy that does not parse, which leaves the very state the
message exists to escape with no accepted input at all. ``hint_arg`` is the
single place that knows the two escaping rules, so a hint site gets them by
calling it instead of re-deriving them.
"""
from __future__ import annotations

import shlex

__all__ = ["hint_arg"]


def hint_arg(flag: str, value: str) -> str:
    """Render ``flag``/``value`` as a shell- and argparse-safe argument pair.

    Returns the plain ``--flag value`` form when `value` needs no escaping --
    that is the spelling an operator expects to read, and it is what the great
    majority of tokens get. Otherwise returns the quoted ``'--flag=value'``
    form: the attached ``=`` is the only spelling argparse accepts for a value
    beginning with ``-``, and the quotes are what stop the shell splitting a
    value containing whitespace.

    The "needs no escaping" test is measured, not assumed: the token must
    survive ``shlex.split`` as exactly one unchanged word. Deciding by a
    denylist of metacharacters is the same guess that produced the bug.
    """
    value = str(value)
    if not value.startswith("-"):
        try:
            if shlex.split(value) == [value]:
                return f"{flag} {value}"
        except ValueError:
            # Unbalanced quotes -- the shell would not accept it bare at all.
            pass
    return shlex.quote(f"{flag}={value}")
