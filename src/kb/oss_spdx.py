"""SPDX licence-expression parsing and normalisation against a pinned SPDX License List.

A declared licence is quoted as published and normalised into an SPDX
expression with a receipt naming the rule applied and the list version used.
Nothing is interpreted: no compatibility, permissiveness or compliance
statement is derived.

* **Grammar (SPDX 2.3 Annex D).** ``AND``, ``OR`` and ``WITH`` with
  parentheses; ``+`` after a licence id; ``LicenseRef-…`` (optionally
  ``DocumentRef-…:``) and ``AdditionRef-…``. Precedence ``+`` > ``WITH`` >
  ``AND`` > ``OR``. Licence and exception ids are matched case-insensitively
  against the list and rewritten in the list's case; operators are matched in
  upper case, lower-case operators are accepted and noted in the receipt
  (verify against Annex D before relying on it).
* **Normalised form.** ``expression`` is the canonical rendering (list case,
  single spaces, parentheses only where precedence needs them); the
  ``comparison_key`` additionally flattens nested operators and sorts and
  de-duplicates ``AND``/``OR`` operands, so ``MIT OR Apache-2.0`` and
  ``(Apache-2.0 OR MIT)`` compare equal. Comparisons always use the key.
* **Never guessed.** Free text maps only through an exact licence *name* in
  the pinned list, an exact PyPI trove classifier or an exact POM licence name
  in the small alias tables below (each noted in the receipt). Anything else -
  an ambiguous classifier ("Apache Software License"), several classifiers or
  POM licences whose combination the source does not state, npm
  ``SEE LICENSE IN …`` / ``UNLICENSED``, or an id the list does not know - is
  ``unparseable`` with the raw text kept. Deprecated ids are kept as written
  and flagged, never rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Trove classifiers whose SPDX id is unambiguous (verify against the classifier list).
CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: MIT No Attribution License (MIT-0)": "MIT-0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "License :: OSI Approved :: Zero-Clause BSD (0BSD)": "0BSD",
    "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
    "License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "License :: OSI Approved :: European Union Public Licence 1.2 (EUPL 1.2)": "EUPL-1.2",
    "License :: OSI Approved :: Boost Software License 1.0 (BSL-1.0)": "BSL-1.0",
    "License :: CC0 1.0 Universal (CC0 1.0) Public Domain Dedication": "CC0-1.0",
}
# Classifiers that name a licence family without a version or an only/or-later choice.
AMBIGUOUS_CLASSIFIERS = frozenset(
    {
        "License :: OSI Approved :: Apache Software License",
        "License :: OSI Approved :: BSD License",
        "License :: OSI Approved :: GNU General Public License (GPL)",
        "License :: OSI Approved :: GNU General Public License v2 (GPLv2)",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "License :: OSI Approved :: GNU Lesser General Public License v2 (LGPLv2)",
        "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
        "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)",
        "License :: OSI Approved :: Python Software Foundation License",
        "License :: Other/Proprietary License",
        "License :: Freely Distributable",
        "License :: Public Domain",
    }
)
# Exact POM <license><name> values that are not SPDX list names (verify each).
NAME_ALIASES = {
    "the apache software license, version 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "the mit license": "MIT",
    "eclipse public license - v 2.0": "EPL-2.0",
    "eclipse public license - v 1.0": "EPL-1.0",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
}
_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.\-+:]+)")
_IDSTRING = re.compile(r"^[A-Za-z0-9.\-]+$")
_REF = re.compile(r"^(?:DocumentRef-[A-Za-z0-9.\-]+:)?LicenseRef-[A-Za-z0-9.\-]+$")
_ADDITION_REF = re.compile(
    r"^(?:DocumentRef-[A-Za-z0-9.\-]+:)?AdditionRef-[A-Za-z0-9.\-]+$"
)


class SpdxError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class SpdxList:
    """One pinned SPDX License List release."""

    version: str
    licences: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # casefolded id -> entry
    exceptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # casefolded name -> id

    @classmethod
    def from_content(cls, content: Mapping[str, Any]) -> SpdxList:
        licences = {
            str(e["id"]).casefold(): dict(e) for e in content.get("licences") or []
        }
        exceptions = {
            str(e["id"]).casefold(): dict(e) for e in content.get("exceptions") or []
        }
        names = {
            str(e["name"]).casefold(): str(e["id"])
            for e in content.get("licences") or []
            if e.get("name")
        }
        return cls(str(content["list_version"]), licences, exceptions, names)


# ----------------------------------------------------------------- parsing


class _Parser:
    def __init__(self, text: str, spdx: SpdxList, notes: list[str]) -> None:
        self.tokens = self._tokens(text)
        self.index = 0
        self.spdx = spdx
        self.notes = notes
        self.deprecated: list[str] = []

    @staticmethod
    def _tokens(text: str) -> list[str]:
        tokens, position = [], 0
        text = text.strip()
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None or match.end() == position:
                raise SpdxError(
                    "unparseable", f"unexpected character at {position} in {text!r}"
                )
            tokens.append(match[1])
            position = match.end()
        return tokens

    def _peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _operator(self, name: str) -> bool:
        token = self._peek()
        if token is None or token.upper() != name:
            return False
        if token != name:
            self.notes.append(f"operator {token!r} read as {name}")
        self.index += 1
        return True

    def parse(self) -> Any:
        if not self.tokens:
            raise SpdxError("unparseable", "empty expression")
        node = self._or()
        if self._peek() is not None:
            raise SpdxError("unparseable", f"unexpected {self._peek()!r}")
        return node

    def _or(self) -> Any:
        items = [self._and()]
        while self._operator("OR"):
            items.append(self._and())
        return items[0] if len(items) == 1 else ("OR", items)

    def _and(self) -> Any:
        items = [self._with()]
        while self._operator("AND"):
            items.append(self._with())
        return items[0] if len(items) == 1 else ("AND", items)

    def _with(self) -> Any:
        token = self._peek()
        if token == "(":
            self.index += 1
            node = self._or()
            if self._peek() != ")":
                raise SpdxError("unparseable", "unbalanced parenthesis")
            self.index += 1
            if self._peek() and self._peek().upper() == "WITH":
                raise SpdxError(
                    "unparseable", "WITH applies to a single licence, not an expression"
                )
            return node
        node = self._licence()
        if self._operator("WITH"):
            node = ("WITH", node, self._exception())
        return node

    def _licence(self) -> Any:
        token = self._peek()
        if (
            token is None
            or token in {"(", ")"}
            or token.upper() in {"AND", "OR", "WITH"}
        ):
            raise SpdxError("unparseable", f"expected a licence id, found {token!r}")
        self.index += 1
        if _REF.fullmatch(token):
            return ("REF", token)
        plus = token.endswith("+")
        ident = token[:-1] if plus else token
        if not _IDSTRING.fullmatch(ident):
            raise SpdxError("unparseable", f"{token!r} is not an SPDX licence id")
        entry = self.spdx.licences.get(ident.casefold())
        if entry is None:
            code = (
                "exception_as_licence"
                if ident.casefold() in self.spdx.exceptions
                else "unknown_identifier"
            )
            raise SpdxError(
                code,
                f"{ident!r} is not a licence id in SPDX License List {self.spdx.version}",
            )
        if entry.get("deprecated"):
            self.deprecated.append(entry["id"])
        return ("LIC", entry["id"], plus)

    def _exception(self) -> str:
        token = self._peek()
        if token is None:
            raise SpdxError("unparseable", "WITH needs an exception id")
        self.index += 1
        if _ADDITION_REF.fullmatch(token):
            return token
        entry = self.spdx.exceptions.get(token.casefold())
        if entry is None:
            raise SpdxError(
                "unknown_identifier",
                f"{token!r} is not an exception id in SPDX License List {self.spdx.version}",
            )
        if entry.get("deprecated"):
            self.deprecated.append(entry["id"])
        return str(entry["id"])


def render(node: Any, *, parent: str | None = None) -> str:
    kind = node[0]
    if kind == "LIC":
        return node[1] + ("+" if node[2] else "")
    if kind == "REF":
        return node[1]
    if kind == "WITH":
        return f"{render(node[1])} WITH {node[2]}"
    text = f" {kind} ".join(render(child, parent=kind) for child in node[1])
    return f"({text})" if parent == "AND" and kind == "OR" else text


def _flatten(node: Any) -> Any:
    if node[0] not in {"AND", "OR"}:
        return node
    items: list[Any] = []
    for child in node[1]:
        child = _flatten(child)
        items.extend(child[1] if child[0] == node[0] else [child])
    unique = {comparison(child): child for child in items}
    ordered = [unique[key] for key in sorted(unique)]
    return ordered[0] if len(ordered) == 1 else (node[0], ordered)


def comparison(node: Any) -> str:
    """The order-insensitive comparison key of a parsed expression."""

    node = _flatten(node) if node[0] in {"AND", "OR"} else node
    if node[0] in {"AND", "OR"}:
        return "(" + f" {node[0]} ".join(comparison(child) for child in node[1]) + ")"
    return render(node)


def parse_expression(
    text: str, spdx: SpdxList, *, legacy_slash: bool = False
) -> dict[str, Any]:
    """Parse one SPDX expression; raises :class:`SpdxError` when it does not parse under the list."""

    notes: list[str] = []
    source = str(text).strip()
    if legacy_slash and "/" in source:
        source = re.sub(r"\s*/\s*", " OR ", source)
        notes.append("legacy '/' read as OR (Cargo manifest convention, verify)")
    parser = _Parser(source, spdx, notes)
    node = parser.parse()
    key = comparison(node)
    return {
        "expression": render(node),
        "comparison_key": key[1:-1]
        if key.startswith("(") and key.endswith(")") and node[0] in {"AND", "OR"}
        else key,
        "deprecated_ids": sorted(set(parser.deprecated)),
        "notes": notes,
    }


# ----------------------------------------------------------------- declarations


def _text_candidate(
    value: str, spdx: SpdxList, *, legacy_slash: bool
) -> tuple[dict[str, Any] | None, str]:
    """An expression from free text: a parseable expression, a list name, or an alias name."""

    text = str(value).strip()
    if not text:
        return None, "empty"
    upper = text.upper()
    if upper == "UNLICENSED":
        return (
            None,
            "npm 'UNLICENSED' is not an SPDX expression (the publisher grants no licence per npm's documentation)",
        )
    if upper.startswith("SEE LICENSE IN") or upper.startswith("SEE LICENCE IN"):
        return (
            None,
            "the licence text is in a file of the package; not an SPDX expression",
        )
    try:
        parsed = parse_expression(text, spdx, legacy_slash=legacy_slash)
        return {**parsed, "rule": "spdx-expression"}, "parsed"
    except SpdxError as exc:
        reason = str(exc)
    folded = " ".join(text.split()).casefold()
    if folded in spdx.names:
        parsed = parse_expression(spdx.names[folded], spdx)
        return {
            **parsed,
            "rule": "spdx-list-name",
            "notes": [f"exact licence name in SPDX list {spdx.version}"],
        }, "named"
    if folded in NAME_ALIASES:
        parsed = parse_expression(NAME_ALIASES[folded], spdx)
        return {
            **parsed,
            "rule": "licence-name-alias",
            "notes": ["exact name in the pack's alias table (verify)"],
        }, "named"
    return None, reason


def normalise_declaration(
    raw: Mapping[str, Any], spdx: SpdxList | None, *, ecosystem: str
) -> dict[str, Any]:
    """Normalise a licence declaration as published into an SPDX expression with a receipt.

    ``raw`` holds only the licence fields as the source published them:
    ``expression`` (PEP 639 ``license_expression``, npm/Cargo ``license``,
    deps.dev licences joined as the source states), ``text`` (a free-text
    licence field), ``classifiers`` (PyPI trove classifiers) and ``names``
    (POM ``<license><name>`` values, legacy npm ``licenses[].type``).
    """

    if spdx is None:
        return {
            "status": "no_spdx_list",
            "reason": "no SPDX License List release has been acquired",
        }
    legacy = ecosystem == "cargo"
    receipt: dict[str, Any] = {
        "spdx_list_version": spdx.version,
        "inputs": {k: raw[k] for k in sorted(raw)},
    }

    def done(candidate: dict[str, Any], field_name: str) -> dict[str, Any]:
        status = (
            "normalised_with_deprecated_ids"
            if candidate["deprecated_ids"]
            else "normalised"
        )
        return {
            "status": status,
            "expression": candidate["expression"],
            "comparison_key": candidate["comparison_key"],
            "spdx_list_version": spdx.version,
            "receipt": {
                **receipt,
                "field": field_name,
                "rule": candidate["rule"],
                "deprecated_ids": candidate["deprecated_ids"],
                "notes": candidate["notes"],
            },
        }

    def fail(reason: str) -> dict[str, Any]:
        return {
            "status": "unparseable",
            "reason": reason,
            "spdx_list_version": spdx.version,
            "receipt": receipt,
        }

    if raw.get("expression"):
        candidate, reason = _text_candidate(
            str(raw["expression"]), spdx, legacy_slash=legacy
        )
        if candidate is None:
            return fail(f"declared expression does not parse: {reason}")
        return done(candidate, "expression")
    candidates: list[tuple[str, dict[str, Any]]] = []
    reasons: list[str] = []
    if raw.get("text"):
        candidate, reason = _text_candidate(str(raw["text"]), spdx, legacy_slash=legacy)
        if candidate is not None:
            candidates.append(("text", candidate))
        else:
            reasons.append(f"licence text: {reason}")
    classifiers = [
        c for c in raw.get("classifiers") or [] if str(c).startswith("License ::")
    ]
    if len(classifiers) > 1:
        return fail(
            "several licence classifiers; the combination is not stated by the source"
        )
    for classifier in classifiers:
        if classifier in CLASSIFIERS:
            parsed = parse_expression(CLASSIFIERS[classifier], spdx)
            candidates.append(
                (
                    "classifiers",
                    {
                        **parsed,
                        "rule": "trove-classifier",
                        "notes": [
                            "exact trove classifier in the pack's table (verify)"
                        ],
                    },
                )
            )
        elif classifier in AMBIGUOUS_CLASSIFIERS:
            reasons.append(f"classifier {classifier!r} does not name one SPDX licence")
        else:
            reasons.append(f"classifier {classifier!r} is not in the classifier table")
    names = [str(n) for n in raw.get("names") or [] if str(n).strip()]
    if len(names) > 1:
        return fail(
            "several licences listed; the combination (AND/OR) is not stated by the source"
        )
    for name in names:
        candidate, reason = _text_candidate(name, spdx, legacy_slash=legacy)
        if candidate is not None:
            candidates.append(("names", candidate))
        else:
            reasons.append(f"licence name {name!r}: {reason}")
    if not candidates:
        return fail("; ".join(reasons) or "no licence declared")
    keys = {c["comparison_key"] for _, c in candidates}
    if len(keys) > 1:
        return fail("licence fields disagree: " + ", ".join(sorted(keys)))
    field_name, candidate = candidates[0]
    return done(candidate, field_name)


def comparison_key_of(normalisation: Mapping[str, Any] | None) -> str | None:
    if not normalisation or not str(normalisation.get("status", "")).startswith(
        "normalised"
    ):
        return None
    return str(normalisation["comparison_key"])


def list_content(
    list_version: str,
    release_date: str | None,
    licences: Iterable[Mapping[str, Any]],
    exceptions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "list_version": list_version,
        **({"release_date": release_date} if release_date else {}),
        "licences": sorted(
            (
                {
                    "id": str(e["id"]),
                    "name": str(e.get("name") or ""),
                    "deprecated": bool(e.get("deprecated")),
                }
                for e in licences
            ),
            key=lambda e: e["id"],
        ),
        "exceptions": sorted(
            (
                {
                    "id": str(e["id"]),
                    "name": str(e.get("name") or ""),
                    "deprecated": bool(e.get("deprecated")),
                }
                for e in exceptions
            ),
            key=lambda e: e["id"],
        ),
    }


__all__ = [
    "AMBIGUOUS_CLASSIFIERS",
    "CLASSIFIERS",
    "NAME_ALIASES",
    "SpdxError",
    "SpdxList",
    "comparison",
    "comparison_key_of",
    "list_content",
    "normalise_declaration",
    "parse_expression",
    "render",
]
