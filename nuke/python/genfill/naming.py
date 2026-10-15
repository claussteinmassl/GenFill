"""Where results are written and what they are called, from patterns a pipeline sets.

A pipeline tool usually knows the show, shot and task and puts them into environment
variables when it launches Nuke (or opens a script). GenFill reads three more variables on
every Generate, so each script writes where the pipeline wants it:

* ``GENFILL_CACHE``: root folder, e.g. ``/jobs/$SHOW/$SHOT/comp/genfill``.
* ``GENFILL_NAME``: name of a generation's folder, e.g. ``${SHOT}_${TASK}_genfill_v{gen:03d}``.
* ``GENFILL_VARIATION_NAME``: file name of each variation (without extension), e.g.
  ``{name}_var{var:02d}``.

Patterns mix two kinds of placeholders:

* ``$VAR``, ``${VAR}`` or ``%VAR%``: any environment variable, on every platform. An unset
  variable expands to nothing (and is logged).
* ``{token}`` or ``{token:format}``: values GenFill knows (see :data:`ROOT_TOKENS`,
  :data:`NAME_TOKENS`, :data:`VARIATION_TOKENS`). ``format`` follows ``str.format``, e.g.
  ``{gen:04d}``.

GenFill always numbers generations and variations itself: a name pattern without ``{gen}``
(or a variation pattern without ``{var}``) gets the number appended, so names never collide.
"""
from __future__ import annotations

import os
import re

ENV_ROOT = "GENFILL_CACHE"
ENV_NAME = "GENFILL_NAME"
ENV_VARIATION = "GENFILL_VARIATION_NAME"

DEFAULT_NAME = "g{gen:03d}_{date}-{time}"
DEFAULT_VARIATION = "var_{var:02d}"

ROOT_TOKENS = ("script_dir", "script", "node", "uid", "user")
NAME_TOKENS = ("script", "node", "uid", "user", "gen", "date", "time", "frame", "backend",
               "model")
VARIATION_TOKENS = NAME_TOKENS + ("name", "var")

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}|\$(\w+)|%(\w+)%|\{(\w+)(?::([^{}]*))?\}")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class PatternError(ValueError):
    """A pattern names an unknown token, has a bad format or expands to nothing."""


def uses(pattern, token):
    """True if ``pattern`` contains the GenFill token ``{token}`` (with or without format)."""
    return any(m.group(4) == token for m in _PLACEHOLDER.finditer(pattern or ""))


def expand(pattern, tokens, environ=None):
    """Replace environment variables and tokens in ``pattern``.

    Args:
        pattern: Text with ``$VAR`` / ``${VAR}`` / ``%VAR%`` and ``{token[:format]}``.
        tokens: Values for the GenFill tokens allowed in this pattern.
        environ: Environment to read (default ``os.environ``).

    Returns:
        ``(text, missing)``: the expanded text and the names of unset environment variables.

    Raises:
        PatternError: An unknown token or a format that does not fit its value.
    """
    env = os.environ if environ is None else environ
    missing = []

    def sub(m):
        var = m.group(1) or m.group(2) or m.group(3)
        if var:
            if var in env:
                return env[var]
            missing.append(var)
            return ""
        name, spec = m.group(4), m.group(5) or ""
        if name not in tokens:
            raise PatternError("Unknown token {%s} in %r. Known tokens: %s" % (
                name, pattern, ", ".join("{%s}" % t for t in sorted(tokens))))
        try:
            return format(tokens[name], spec)
        except (ValueError, TypeError) as exc:
            raise PatternError("Bad format {%s:%s} in %r: %s" % (name, spec, pattern, exc))

    return _PLACEHOLDER.sub(sub, pattern), missing


def safe_name(text):
    """``text`` as one file or folder name: only letters, digits, ``.``, ``_`` and ``-``."""
    text = _UNSAFE.sub("_", text)
    text = re.sub(r"_{2,}", "_", text)
    return text.strip("._- ")


def generation_name(pattern, tokens, environ=None):
    """Folder name of one generation.

    Returns:
        ``(name, missing)``; ``missing`` lists unset environment variables.

    Raises:
        PatternError: Bad pattern, or it expands to an empty name.
    """
    pattern = pattern or DEFAULT_NAME
    if not uses(pattern, "gen"):
        pattern += "_{gen:03d}"
    text, missing = expand(pattern, tokens, environ)
    name = safe_name(text)
    if not name:
        raise PatternError("%s %r expands to an empty name." % (ENV_NAME, pattern))
    return name, missing


def variation_name(pattern, tokens, environ=None):
    """File name (without extension) of one variation; same contract as :func:`generation_name`."""
    pattern = pattern or DEFAULT_VARIATION
    if not uses(pattern, "var"):
        pattern += "_{var:02d}"
    text, missing = expand(pattern, tokens, environ)
    name = safe_name(text)
    if not name:
        raise PatternError("%s %r expands to an empty name." % (ENV_VARIATION, pattern))
    return name, missing


def root_path(pattern, tokens, environ=None):
    """Root folder from a path pattern. Relative results are taken from ``{script_dir}``.

    Returns:
        ``(path, missing)``; ``missing`` lists unset environment variables.

    Raises:
        PatternError: Bad pattern, or it expands to an empty path.
    """
    text, missing = expand(pattern, tokens, environ)
    text = os.path.expanduser(text.strip())
    if not text:
        raise PatternError("Cache folder %r expands to an empty path." % pattern)
    if not os.path.isabs(text) and tokens.get("script_dir"):
        text = os.path.join(tokens["script_dir"], text)
    return os.path.normpath(text), missing
