"""Per-ecosystem version ordering and constraint satisfaction for the OSS Ecosystems pack.

Each ecosystem is compared by its own rules, never by one loose ordering:

* **PyPI** - PEP 440 through ``packaging`` (the comparator
  :func:`src.domains.technical.impact._compare` already uses), with PEP 440
  specifier sets; pre-releases satisfy a specifier only when it names one.
* **npm** - Semantic Versioning 2.0 (``impact._npm_key``) with node-semver
  ranges: ``||`` alternatives, hyphen ranges, x-ranges, ``~`` and ``^``, and
  node-semver's pre-release rule (a pre-release satisfies a comparator set only
  when a comparator in it names a pre-release of the same ``major.minor.patch``).
* **Cargo** - Semantic Versioning 2.0 with Cargo requirement syntax:
  comma-separated comparators, caret by default, ``~``, ``=``, wildcards and
  the same pre-release rule.
* **Maven** - Maven's ``ComparableVersion`` ordering (qualifiers
  ``alpha < beta < milestone < rc = cr < snapshot < '' = ga = final = release
  < sp``) and Maven version ranges (``[1.0,2.0)``, ``(,1.0],[1.2,)``, ``[1.0]``);
  a plain version is Maven's *soft requirement* and resolves to itself.

:func:`src.domains.technical.model.version_key` stays the loose fallback for
ecosystems without a native comparator; v1 needs none.

Anything the grammar does not cover raises :class:`UnsupportedConstraint`,
which the graph resolver reports as an unresolved edge; nothing is guessed.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from src.domains.technical.impact import _npm_key
from src.domains.technical.model import version_key

ECOSYSTEMS = ("pypi", "npm", "cargo", "maven")


class UnsupportedConstraint(ValueError):
    """A constraint or version the ecosystem's grammar does not cover."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------- normalising


def normalise_version(ecosystem: str, version: str) -> str:
    """The version text used in record keys: PEP 440 normal form on PyPI, else as published."""

    text = str(version).strip()
    if ecosystem == "pypi":
        try:
            return str(Version(text))
        except InvalidVersion:
            return text
    return text


# ----------------------------------------------------------------- semver


_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> SemVer:
        match = _SEMVER.fullmatch(str(text).strip())
        if match is None:
            raise UnsupportedConstraint(
                "invalid_version", f"{text!r} is not a semantic version"
            )
        pre = tuple(match[4].split(".")) if match[4] else ()
        return cls(int(match[1]), int(match[2]), int(match[3]), pre)

    @property
    def key(self) -> tuple[Any, ...]:
        # The same ordering as impact._npm_key (SemVer 2.0 precedence).
        parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.pre)
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, parts)

    @property
    def triple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def _cmp(a: Any, b: Any) -> int:
    return (a > b) - (a < b)


_OPS = {
    "<": lambda c: c < 0,
    "<=": lambda c: c <= 0,
    ">": lambda c: c > 0,
    ">=": lambda c: c >= 0,
    "=": lambda c: c == 0,
}


def _semver_test(comparators: list[tuple[str, SemVer]], version: SemVer) -> bool:
    for op, bound in comparators:
        if not _OPS[op](_cmp(version.key, bound.key)):
            return False
    if version.pre:
        # A pre-release satisfies only a set naming a pre-release of the same major.minor.patch.
        return any(
            bound.pre and bound.triple == version.triple for _, bound in comparators
        )
    return True


_PARTIAL = re.compile(
    r"^v?(?P<major>0|[1-9]\d*|[xX*])(?:\.(?P<minor>0|[1-9]\d*|[xX*])"
    r"(?:\.(?P<patch>0|[1-9]\d*|[xX*])(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)?)?$"
)
_ZERO_PRE = ("0",)


def _partial(text: str) -> tuple[int | None, int | None, int | None, tuple[str, ...]]:
    match = _PARTIAL.fullmatch(text)
    if match is None:
        raise UnsupportedConstraint(
            "unsupported_constraint", f"{text!r} is not a version or x-range"
        )

    def num(value: str | None) -> int | None:
        return None if value is None or value in {"x", "X", "*"} else int(value)

    major, minor, patch = num(match["major"]), num(match["minor"]), num(match["patch"])
    if major is None:
        minor = patch = None
    elif minor is None:
        patch = None
    pre = tuple(match["pre"].split(".")) if match["pre"] else ()
    return major, minor, patch, pre


def _v(major: int, minor: int, patch: int, pre: tuple[str, ...] = ()) -> SemVer:
    return SemVer(major, minor, patch, pre)


def _npm_primitive(op: str, text: str) -> list[tuple[str, SemVer]]:
    major, minor, patch, pre = _partial(text)
    if major is None:  # '*' with an operator
        return [("<", _v(0, 0, 0, _ZERO_PRE))] if op in {"<", ">"} else []
    if minor is None:
        low, high = _v(major, 0, 0), _v(major + 1, 0, 0, _ZERO_PRE)
    elif patch is None:
        low, high = _v(major, minor, 0), _v(major, minor + 1, 0, _ZERO_PRE)
    else:
        exact = _v(major, minor, patch, pre)
        return [(op or "=", exact)]
    if op in {"", "="}:
        return [(">=", low), ("<", high)]
    if op == ">":
        return [
            (">=", high if not high.pre else _v(high.major, high.minor, high.patch))
        ]
    if op == ">=":
        return [(">=", low)]
    if op == "<":
        return [("<", _v(low.major, low.minor, low.patch, _ZERO_PRE))]
    return [("<", high)]  # "<="


def _npm_tilde(text: str) -> list[tuple[str, SemVer]]:
    major, minor, patch, pre = _partial(text)
    if major is None:
        return []
    if minor is None:
        return [(">=", _v(major, 0, 0)), ("<", _v(major + 1, 0, 0, _ZERO_PRE))]
    return [
        (">=", _v(major, minor, patch or 0, pre)),
        ("<", _v(major, minor + 1, 0, _ZERO_PRE)),
    ]


def _npm_caret(text: str) -> list[tuple[str, SemVer]]:
    major, minor, patch, pre = _partial(text)
    if major is None:
        return []
    if minor is None:
        return [(">=", _v(major, 0, 0)), ("<", _v(major + 1, 0, 0, _ZERO_PRE))]
    low = _v(major, minor, patch or 0, pre)
    if major > 0:
        high = _v(major + 1, 0, 0, _ZERO_PRE)
    elif minor > 0 or patch is None:
        high = _v(0, minor + 1, 0, _ZERO_PRE)
    else:
        high = _v(0, 0, patch + 1, _ZERO_PRE)
    return [(">=", low), ("<", high)]


def _npm_hyphen(left: str, right: str) -> list[tuple[str, SemVer]]:
    lo_major, lo_minor, lo_patch, lo_pre = _partial(left)
    result: list[tuple[str, SemVer]] = []
    if lo_major is not None:
        result.append((">=", _v(lo_major, lo_minor or 0, lo_patch or 0, lo_pre)))
    hi_major, hi_minor, hi_patch, hi_pre = _partial(right)
    if hi_major is None:
        return result
    if hi_minor is None:
        result.append(("<", _v(hi_major + 1, 0, 0, _ZERO_PRE)))
    elif hi_patch is None:
        result.append(("<", _v(hi_major, hi_minor + 1, 0, _ZERO_PRE)))
    else:
        result.append(("<=", _v(hi_major, hi_minor, hi_patch, hi_pre)))
    return result


def npm_range(constraint: str) -> list[list[tuple[str, SemVer]]]:
    """Desugar a node-semver range into OR'ed comparator sets."""

    text = str(constraint).strip()
    if (
        re.match(r"^(?:npm:|git[+:]|https?:|file:|link:|workspace:|github:)", text)
        or "/" in text
    ):
        raise UnsupportedConstraint(
            "non_registry_specifier",
            f"{text!r} is an alias, URL, path or workspace reference",
        )
    sets = []
    for alternative in re.split(r"\s*\|\|\s*", text):
        alternative = re.sub(r"(<=|>=|<|>|=|~>|~|\^)\s+", r"\1", alternative.strip())
        hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", alternative)
        if hyphen:
            sets.append(_npm_hyphen(hyphen[1], hyphen[2]))
            continue
        comparators: list[tuple[str, SemVer]] = []
        for token in alternative.split():
            match = re.fullmatch(r"(<=|>=|<|>|=|~>|~|\^)?(.+)", token)
            op, rest = (match[1] or ""), match[2]
            if op in {"~", "~>"}:
                comparators += _npm_tilde(rest)
            elif op == "^":
                comparators += _npm_caret(rest)
            else:
                comparators += _npm_primitive(op, rest)
        sets.append(comparators)
    return sets


def _cargo_comparator(token: str) -> list[tuple[str, SemVer]]:
    match = re.fullmatch(r"(<=|>=|<|>|=|~|\^)?\s*(.+)", token.strip())
    if match is None:
        raise UnsupportedConstraint(
            "unsupported_constraint", f"{token!r} is not a Cargo comparator"
        )
    op, text = match[1] or "", match[2].strip()
    major, minor, patch, pre = _partial(text)
    if major is None:
        if op:
            raise UnsupportedConstraint(
                "unsupported_constraint", "a wildcard takes no operator"
            )
        return []
    wildcard = bool(re.search(r"[xX*]", text))
    if wildcard and op:
        raise UnsupportedConstraint(
            "unsupported_constraint", "a wildcard takes no operator"
        )
    if op in {"", "^"} and not wildcard:
        low = _v(major, minor or 0, patch or 0, pre)
        if major > 0 or minor is None:
            high = _v(major + 1, 0, 0)
        elif minor > 0 or patch is None:
            high = _v(0, minor + 1, 0)
        else:
            high = _v(0, 0, patch + 1)
        return [(">=", low), ("<", high)]
    if op == "~":
        low = _v(major, minor or 0, patch or 0, pre)
        high = _v(major + 1, 0, 0) if minor is None else _v(major, minor + 1, 0)
        return [(">=", low), ("<", high)]
    if op in {"", "="}:  # "=" or a wildcard
        if minor is None:
            return [(">=", _v(major, 0, 0)), ("<", _v(major + 1, 0, 0))]
        if patch is None:
            return [(">=", _v(major, minor, 0)), ("<", _v(major, minor + 1, 0))]
        return [("=", _v(major, minor, patch, pre))]
    if patch is not None:
        return [(op, _v(major, minor, patch, pre))]
    if op == ">":
        return [
            (">=", _v(major + 1, 0, 0) if minor is None else _v(major, minor + 1, 0))
        ]
    if op == ">=":
        return [(">=", _v(major, minor or 0, 0))]
    if op == "<":
        return [("<", _v(major, minor or 0, 0))]
    return [
        ("<", _v(major + 1, 0, 0) if minor is None else _v(major, minor + 1, 0))
    ]  # "<="


def cargo_requirement(constraint: str) -> list[tuple[str, SemVer]]:
    text = str(constraint).strip()
    if not text:
        raise UnsupportedConstraint(
            "unsupported_constraint", "an empty Cargo requirement"
        )
    comparators: list[tuple[str, SemVer]] = []
    for part in text.split(","):
        comparators += _cargo_comparator(part)
    return comparators


# ----------------------------------------------------------------- Maven


_QUALIFIERS = ("alpha", "beta", "milestone", "rc", "snapshot", "", "sp")
_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
_RELEASE_INDEX = str(_QUALIFIERS.index(""))


def _qualifier_key(value: str) -> str:
    return (
        str(_QUALIFIERS.index(value))
        if value in _QUALIFIERS
        else f"{len(_QUALIFIERS)}-{value}"
    )


class _Int:
    def __init__(self, value: int) -> None:
        self.value = value

    def is_null(self) -> bool:
        return self.value == 0

    def compare(self, other: Any) -> int:
        if other is None:
            return 0 if self.value == 0 else 1
        if isinstance(other, _Int):
            return _cmp(self.value, other.value)
        return 1  # an integer outranks a qualifier and a sub-list


class _Str:
    def __init__(self, value: str, followed_by_digit: bool) -> None:
        if followed_by_digit and len(value) == 1:
            value = {"a": "alpha", "b": "beta", "m": "milestone"}.get(value, value)
        self.value = _ALIASES.get(value, value)

    def is_null(self) -> bool:
        return _qualifier_key(self.value) == _RELEASE_INDEX

    def compare(self, other: Any) -> int:
        if other is None:
            return _cmp(_qualifier_key(self.value), _RELEASE_INDEX)
        if isinstance(other, _Str):
            return _cmp(_qualifier_key(self.value), _qualifier_key(other.value))
        return -1


class _List(list):
    def is_null(self) -> bool:
        return len(self) == 0

    def normalise(self) -> None:
        for index in range(len(self) - 1, -1, -1):
            item = self[index]
            if item.is_null():
                del self[index]
            elif not isinstance(item, _List):
                break

    def compare(self, other: Any) -> int:
        if other is None:
            return 0 if not self else self[0].compare(None)
        if isinstance(other, _Int):
            return -1
        if isinstance(other, _Str):
            return 1
        for index in range(max(len(self), len(other))):
            left = self[index] if index < len(self) else None
            right = other[index] if index < len(other) else None
            if left is None:
                result = 0 if right is None else -right.compare(None)
            else:
                result = left.compare(right)
            if result:
                return result
        return 0


def _maven_item(is_digit: bool, text: str) -> Any:
    return _Int(int(text)) if is_digit else _Str(text, False)


@functools.lru_cache(maxsize=4096)
def maven_version(version: str) -> _List:
    """Parse a Maven version into ComparableVersion items."""

    text = str(version).strip().lower()
    if not text:
        raise UnsupportedConstraint("invalid_version", "an empty Maven version")
    items = current = _List()
    stack = [items]
    is_digit, start = False, 0
    for index, char in enumerate(text):
        if char in ".-":
            current.append(
                _Int(0) if index == start else _maven_item(is_digit, text[start:index])
            )
            start = index + 1
            if char == "-":
                child = _List()
                current.append(child)
                current = child
                stack.append(child)
        elif char.isdigit():
            if not is_digit and index > start:
                current.append(_Str(text[start:index], True))
                start = index
                child = _List()
                current.append(child)
                current = child
                stack.append(child)
            is_digit = True
        else:
            if is_digit and index > start:
                current.append(_maven_item(True, text[start:index]))
                start = index
                child = _List()
                current.append(child)
                current = child
                stack.append(child)
            is_digit = False
    if len(text) > start:
        current.append(_maven_item(is_digit, text[start:]))
    while stack:
        stack.pop().normalise()
    return items


@functools.total_ordering
class _MavenKey:
    def __init__(self, version: str) -> None:
        self.items = maven_version(version)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _MavenKey) and self.items.compare(other.items) == 0

    def __lt__(self, other: _MavenKey) -> bool:
        return self.items.compare(other.items) < 0

    def __hash__(self) -> int:
        return 0


@dataclass(frozen=True)
class MavenRestriction:
    lower: str | None
    lower_inclusive: bool
    upper: str | None
    upper_inclusive: bool

    def contains(self, version: str) -> bool:
        key = _MavenKey(version)
        if self.lower is not None:
            low = _MavenKey(self.lower)
            if key < low or (key == low and not self.lower_inclusive):
                return False
        if self.upper is not None:
            high = _MavenKey(self.upper)
            if high < key or (key == high and not self.upper_inclusive):
                return False
        return True


def maven_range(spec: str) -> tuple[str | None, list[MavenRestriction]]:
    """``(soft_version, restrictions)``: a plain version is a soft requirement with no restriction."""

    text = str(spec).strip()
    if not text:
        raise UnsupportedConstraint(
            "unsupported_constraint", "an empty Maven version specification"
        )
    if "${" in text:
        raise UnsupportedConstraint(
            "unresolved_property",
            f"{text!r} references a property that is not resolved",
        )
    if text[0] not in "[(":
        if any(ch in text for ch in "[](),"):
            raise UnsupportedConstraint(
                "unsupported_constraint", f"{text!r} is not a Maven version range"
            )
        return text, []
    restrictions = []
    rest = text
    while rest:
        if rest[0] not in "[(":
            raise UnsupportedConstraint(
                "unsupported_constraint", f"{text!r} is not a Maven version range"
            )
        close = min((i for i in (rest.find("]"), rest.find(")")) if i >= 0), default=-1)
        if close < 0:
            raise UnsupportedConstraint(
                "unsupported_constraint", f"{text!r} has an unclosed range"
            )
        body = rest[1:close]
        lower_inclusive, upper_inclusive = rest[0] == "[", rest[close] == "]"
        if "," not in body:
            if not (lower_inclusive and upper_inclusive) or not body.strip():
                raise UnsupportedConstraint(
                    "unsupported_constraint",
                    f"{text!r} has an invalid single-version range",
                )
            restrictions.append(
                MavenRestriction(body.strip(), True, body.strip(), True)
            )
        else:
            low, high = (part.strip() or None for part in body.split(",", 1))
            if (
                low is not None
                and high is not None
                and _MavenKey(high) < _MavenKey(low)
            ):
                raise UnsupportedConstraint(
                    "unsupported_constraint", f"{text!r} has an inverted range"
                )
            restrictions.append(
                MavenRestriction(
                    low,
                    lower_inclusive and low is not None,
                    high,
                    upper_inclusive and high is not None,
                )
            )
        rest = rest[close + 1 :].lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
    return None, restrictions


# ----------------------------------------------------------------- public API


def sort_key(ecosystem: str, version: str) -> Any:
    """A key ordering versions by the ecosystem's own rules; raises for invalid versions."""

    if ecosystem == "pypi":
        try:
            return Version(str(version))
        except InvalidVersion as exc:
            raise UnsupportedConstraint(
                "invalid_version", f"{version!r} is not a PEP 440 version"
            ) from exc
    if ecosystem == "npm":
        try:
            return _npm_key(str(version).strip())
        except ValueError as exc:
            raise UnsupportedConstraint(
                "invalid_version", f"{version!r} is not a semantic version"
            ) from exc
    if ecosystem == "cargo":
        return SemVer.parse(version).key
    if ecosystem == "maven":
        return _MavenKey(str(version))
    return version_key(str(version))


def compare(ecosystem: str, left: str, right: str) -> int:
    a, b = sort_key(ecosystem, left), sort_key(ecosystem, right)
    return (a > b) - (a < b)


def is_prerelease(ecosystem: str, version: str) -> bool:
    if ecosystem == "pypi":
        return Version(str(version)).is_prerelease
    if ecosystem in {"npm", "cargo"}:
        return bool(SemVer.parse(version).pre)
    return "snapshot" in str(version).lower()


def exact_pin(ecosystem: str, constraint: str | None) -> str | None:
    """The single version a constraint pins exactly (``==``/``===`` on PyPI, ``=``/``[v]`` elsewhere)."""

    text = str(constraint or "").strip()
    if ecosystem == "pypi":
        match = re.fullmatch(r"(?:===|==)\s*([^\s,*]+)", text)
        return match[1] if match else None
    if ecosystem in {"npm", "cargo"}:
        match = re.fullmatch(r"=?\s*v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", text)
        return (
            match[1] if match and (ecosystem == "npm" or text.startswith("=")) else None
        )
    if ecosystem == "maven":
        match = re.fullmatch(r"\[([^,\[\]()]+)\]", text)
        return match[1].strip() if match else None
    return None


def satisfies(ecosystem: str, constraint: str | None, version: str) -> bool:
    """Whether ``version`` satisfies ``constraint`` under the ecosystem's own rules."""

    text = "" if constraint is None else str(constraint).strip()
    if ecosystem == "pypi":
        try:
            spec = SpecifierSet(text)
            names_pre = False
            for item in spec:
                try:
                    names_pre = (
                        names_pre or Version(item.version.rstrip(".*")).is_prerelease
                    )
                except InvalidVersion:
                    continue
            # PEP 440: pre-releases are excluded unless the specifier itself names one.
            return spec.contains(Version(str(version)), prereleases=names_pre)
        except (InvalidSpecifier, InvalidVersion) as exc:
            raise UnsupportedConstraint(
                "unsupported_constraint", f"{text!r} is not a PEP 440 specifier"
            ) from exc
    if ecosystem == "npm":
        target = SemVer.parse(version)
        if text in {"", "*"}:
            return not target.pre
        if re.fullmatch(r"[A-Za-z][\w.-]*", text) and text not in {"x", "X"}:
            raise UnsupportedConstraint(
                "dist_tag", f"{text!r} is a dist-tag, not a range"
            )
        return any(_semver_test(cset, target) for cset in npm_range(text))
    if ecosystem == "cargo":
        target = SemVer.parse(version)
        return _semver_test(cargo_requirement(text or "*"), target)
    if ecosystem == "maven":
        soft, restrictions = maven_range(text)
        if soft is not None:
            return compare("maven", soft, version) == 0
        return any(r.contains(str(version)) for r in restrictions)
    raise UnsupportedConstraint(
        "unsupported_ecosystem", f"no constraint grammar for {ecosystem!r}"
    )


def is_soft_requirement(ecosystem: str, constraint: str | None) -> bool:
    if ecosystem != "maven" or not constraint:
        return False
    try:
        return maven_range(constraint)[0] is not None
    except UnsupportedConstraint:
        return False


__all__ = [
    "ECOSYSTEMS",
    "MavenRestriction",
    "SemVer",
    "UnsupportedConstraint",
    "cargo_requirement",
    "compare",
    "exact_pin",
    "is_prerelease",
    "is_soft_requirement",
    "maven_range",
    "maven_version",
    "normalise_version",
    "npm_range",
    "satisfies",
    "sort_key",
]
