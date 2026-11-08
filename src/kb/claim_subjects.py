"""What a claim is about, for gating contradictions to same-subject claims.

Two install hooks from two different campaigns are not in conflict, however
similar their wording. :func:`claim_subjects` recognises the things a
security claim is about:

* **indicators** — file hashes (MD5/SHA-1/SHA-256), hosts (domains and IPv4,
  defanged forms included), file names, wallet addresses (Bitcoin,
  Ethereum), CVE identifiers and package names (``name@1.2.3``, "the ``x``
  package", ``npm install x``);
* **names** — malware families, campaigns and actor groups named next to a
  type word ("the Lumma stealer", "tracked as UNC1234", "dubbed GhostLoader");
* **entities** — other proper names (CamelCase, ALL-CAPS, digit-bearing or
  multi-word capitalised names; sentence-initial common words, days and
  months excluded).

:func:`subject_gate` then decides whether two claims may be linked:

``contradicts`` (strict)
    Allowed only when both claims name a shared subject. When both name
    indicators, an indicator must be shared; names and entities match by
    value whatever their kind. A claim naming something and a claim naming
    nothing do not conflict (``subject_unmatched``). When neither names
    anything, ``allow_unidentified`` decides (default True, so generic
    corpora keep their existing behaviour).
``supports`` / ``duplicate`` (lenient)
    Blocked only when both claims name indicators and share none
    (``different_subjects``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DEFANG = (
    (re.compile(r"\[\.\]|\(\.\)|\{\.\}|\[dot\]|\(dot\)", re.IGNORECASE), "."),
    (re.compile(r"\bhxxp", re.IGNORECASE), "http"),
    (re.compile(r"\[:\]"), ":"),
    (re.compile(r"\[@\]|\[at\]", re.IGNORECASE), "@"),
)

_HASH = re.compile(r"(?<![0-9a-fA-FxX])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40}|[0-9a-fA-F]{32})(?![0-9a-fA-F])")
_CVE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_ETH = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
_BTC = re.compile(r"\b(?:bc1[ac-hj-np-z02-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_TLDS = (
    "com|net|org|io|co|ai|app|dev|info|biz|xyz|top|site|online|store|shop|cloud|live|"
    "me|cc|tk|ml|ga|cf|gq|ru|su|cn|uk|de|fr|nl|eu|us|in|br|ir|kp|ua|pl|it|es|jp|kr|"
    "tv|ws|pw|link|club|click|icu|vip|work|zip|mov|onion|sh|gg|so|to|ly|is|lol|fun"
)
_DOMAIN = re.compile(
    rf"\b(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+(?:{_TLDS})\b(?![.-]?[a-z0-9])", re.IGNORECASE
)
_FILE = re.compile(
    r"\b[\w][\w.-]*\.(?:exe|dll|sys|scr|js|mjs|cjs|py|pyc|ps1|psm1|sh|bat|cmd|vbs|vbe|hta|jar|"
    r"msi|apk|dmg|pkg|deb|rpm|zip|rar|7z|iso|img|lnk|docm|xlsm|pptm|one|wsf|elf|so|dylib|bin)\b",
    re.IGNORECASE,
)
_PKG_CHARS = r"@?[a-z0-9][\w.-]*(?:/[a-z0-9][\w.-]*)?"
_PACKAGE_PATTERNS = (
    re.compile(rf"(?<![\w@])({_PKG_CHARS})@\d+(?:\.\d+)+[\w.-]*", re.IGNORECASE),
    re.compile(rf"\b(?:npm|pip3?|pipx|gem|cargo|go|yarn|pnpm|composer|nuget)\s+(?:install|add|get)\s+(?:-\S+\s+)*({_PKG_CHARS})", re.IGNORECASE),
    re.compile(rf"\b(?:npm|pypi|pip|rubygems|gem|crate|crates\.io|nuget|maven|packagist|go)\s+(?:package|module|library|crate|gem)s?\s+[`'\"“]?({_PKG_CHARS})", re.IGNORECASE),
    re.compile(rf"\b(?:package|module|library|crate|gem)s?\s+[`'\"“]({_PKG_CHARS})[`'\"”]", re.IGNORECASE),
    re.compile(rf"\bthe\s+[`'\"“]?({_PKG_CHARS})[`'\"”]?\s+(?:npm\s+|pypi\s+|python\s+|node\s+|ruby\s+)?(?:package|module|library|crate|gem)\b", re.IGNORECASE),
    # Unquoted "package event-stream": only names that look like package
    # names (a separator or digit), so "package was removed" is not one.
    re.compile(r"\b(?:package|module|library|crate|gem)s?\s+(@?[a-z0-9]+(?:[-_.@/][a-z0-9]+)+|[a-z]+\d[\w-]*)\b", re.IGNORECASE),
)
_PACKAGE_STOP = {
    "malicious", "compromised", "rogue", "fake", "typosquatted", "typosquatting", "same", "new",
    "popular", "legitimate", "affected", "this", "that", "these", "those", "python", "npm", "node",
    "ruby", "java", "open-source", "open", "source", "official", "original", "vulnerable", "trojanized",
    "backdoored", "infected", "first", "second", "latest", "older", "a", "an", "the", "other", "its",
    "their", "our", "your", "one", "two", "three", "several", "many", "multiple", "dependency",
    "update", "updated", "install", "-g", "--save", "package", "packages",
}
_TYPE_WORDS = (
    "malware|ransomware|stealer|infostealer|loader|botnet|trojan|backdoor|rat|worm|wiper|dropper|"
    "downloader|implant|rootkit|miner|cryptominer|spyware|keylogger|exploit kit|campaign|operation|"
    "family|variant|strain|apt|group|actor|threat actor|gang|crew|cluster"
)
_NAME_TOKEN = r"[A-Z0-9][\w.-]*[A-Za-z0-9]|[A-Z]"
_NAMED = (
    re.compile(rf"\b(?:[Tt]he\s+)?((?:{_NAME_TOKEN})(?:\s+(?:{_NAME_TOKEN})){{0,2}})\s+(?i:{_TYPE_WORDS})\b"),
    re.compile(rf"\b(?i:{_TYPE_WORDS})\s+(?i:called|named|dubbed|known as|tracked as)?\s*[`'\"“]?((?:{_NAME_TOKEN})(?:\s+(?:{_NAME_TOKEN})){{0,2}})"),
    re.compile(rf"\b(?:dubbed|codenamed|code-named|tracked as|known as|named|called)\s+[`'\"“]?((?:{_NAME_TOKEN})(?:\s+(?:{_NAME_TOKEN})){{0,2}})"),
)
_STOP_NAMES = {
    "the", "a", "an", "this", "that", "these", "those", "it", "its", "we", "they", "he", "she", "i",
    "our", "their", "his", "her", "in", "on", "at", "for", "from", "by", "with", "however", "but",
    "and", "or", "if", "when", "while", "after", "before", "since", "according", "also", "as",
    "there", "here", "what", "which", "who", "how", "why", "all", "some", "many", "most", "new",
    "one", "two", "three", "first", "second", "last", "next", "today", "yesterday", "tomorrow",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "researchers", "attackers", "security", "update", "notably",
    "additionally", "meanwhile", "previously", "currently", "once", "then", "now", "both", "each",
    "any", "no", "not", "unlike", "like", "such", "other", "another", "several", "said", "says",
}
_GENERIC_CAPS = {"I", "A", "OK", "US", "UK", "EU", "UN", "AM", "PM", "AND", "OR", "THE", "IT", "ID"}
#: Ecosystems, platforms and algorithm names are context, not subjects: two
#: claims about different npm packages both "mention npm".
_CONTEXT_NAMES = {
    "npm", "pypi", "pip", "github", "gitlab", "bitbucket", "docker", "docker hub", "rubygems",
    "crates.io", "nuget", "maven", "windows", "linux", "macos", "android", "ios", "javascript",
    "typescript", "python", "node.js", "node", "java", "go", "rust", "ruby", "php", "powershell",
    "bash", "sha-256", "sha256", "sha-1", "sha1", "md5", "ip", "ips", "url", "urls", "dns", "http",
    "https", "api", "cve", "ioc", "iocs", "c2", "c&c", "tls", "ssl", "vpn", "html", "json", "yaml",
}
_ENTITY = re.compile(r"\b[A-Z][\w.-]*[A-Za-z0-9](?:\s+[A-Z][\w.-]*[A-Za-z0-9])*|\b[A-Z]{2,}\d*\b")
_SENTENCE_START = re.compile(r"(?:^|[.!?]\s+|[:;]\s+|\n)$")


@dataclass(frozen=True)
class Subjects:
    """Recognised subjects. ``indicators`` are ``kind:value``; names/entities are values."""

    indicators: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    #: Sentence-initial capitalised words ("Lumma does not ..."): they can
    #: confirm a shared subject but never block a pair on their own.
    weak: frozenset[str] = frozenset()

    @property
    def any(self) -> bool:
        return bool(self.indicators or self.names or self.entities)

    def keys(self) -> frozenset[str]:
        return self.indicators | {f"name:{v}" for v in self.names | self.entities}


def _defang(text: str) -> str:
    for pattern, replacement in _DEFANG:
        text = pattern.sub(replacement, text)
    return text


def _clean_name(value: str) -> str | None:
    words = [w for w in re.split(r"\s+", value.strip(" `'\"“”.,;:()[]")) if w]
    while words and words[0].lower() in _STOP_NAMES:
        words.pop(0)
    while words and words[-1].lower() in _STOP_NAMES:
        words.pop()
    if not words:
        return None
    name = " ".join(words).lower()
    if len(name) < 2 or name in _STOP_NAMES or name in _CONTEXT_NAMES or name.isdigit():
        return None
    return name


def claim_subjects(text: str) -> Subjects:
    raw = _defang(text or "")
    indicators: set[str] = set()
    spans: list[tuple[int, int]] = []

    def add(kind: str, value: str, match: re.Match | None = None, group: int = 0) -> None:
        indicators.add(f"{kind}:{value}")
        if match is not None:
            spans.append(match.span(group))

    for match in _HASH.finditer(raw):
        add("hash", match.group(0).lower(), match)
    for match in _CVE.finditer(raw):
        add("cve", match.group(0).upper(), match)
    for match in _ETH.finditer(raw):
        add("wallet", match.group(0).lower(), match)
    for match in _BTC.finditer(raw):
        token = match.group(0)
        if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
            add("wallet", token, match)
    for match in _IPV4.finditer(raw):
        add("host", match.group(0), match)
    for match in _FILE.finditer(raw):
        add("file", match.group(0).lower(), match)
    covered = [span for span in spans]
    for match in _DOMAIN.finditer(raw):
        if any(s <= match.start() < e for s, e in covered):
            continue
        add("host", match.group(0).lower().removeprefix("www."), match)
    for pattern in _PACKAGE_PATTERNS:
        for match in pattern.finditer(raw):
            name = match.group(1).strip("`'\"“”.,;:").lower()
            if name and name not in _PACKAGE_STOP and not name.isdigit() and len(name) > 1:
                add("pkg", name, match, 1)

    names: set[str] = set()
    for pattern in _NAMED:
        for match in pattern.finditer(raw):
            name = _clean_name(match.group(1))
            if name and not re.fullmatch(_TYPE_WORDS, name, re.IGNORECASE):
                names.add(name)
                spans.append(match.span(1))

    entities: set[str] = set()
    weak: set[str] = set()
    for match in _ENTITY.finditer(raw):
        start, end = match.span()
        if any(s <= start < e or s < end <= e for s, e in spans):
            continue
        value = match.group(0)
        tokens = value.split()
        at_start = bool(_SENTENCE_START.search(raw[:start]))
        distinctive = (
            len(tokens) > 1
            or any(ch.isdigit() for ch in value)
            or (value.isupper() and len(value) >= 2)
            or re.search(r"[a-z][A-Z]", value) is not None
        )
        if value in _GENERIC_CAPS:
            continue
        name = _clean_name(value)
        if not name or name in names:
            continue
        (weak if at_start and not distinctive else entities).add(name)
    return Subjects(frozenset(indicators), frozenset(names), frozenset(entities),
                    frozenset(weak - entities - names))


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str | None
    shared: frozenset[str]

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "shared": sorted(self.shared)}


def subject_gate(
    text_a: str, text_b: str, relation: str, *, allow_unidentified: bool = True,
    subjects_a: Subjects | None = None, subjects_b: Subjects | None = None,
) -> GateDecision:
    """May ``relation`` link two claims, given what each is about?"""
    a = subjects_a or claim_subjects(text_a)
    b = subjects_b or claim_subjects(text_b)
    indicator_overlap = a.indicators & b.indicators
    name_overlap = (a.names | a.entities | a.weak) & (b.names | b.entities | b.weak)
    shared = frozenset(indicator_overlap | {f"name:{v}" for v in name_overlap})
    if a.indicators and b.indicators and not indicator_overlap:
        return GateDecision(False, "different_subjects", frozenset())
    if relation != "contradicts":
        return GateDecision(True, None, shared)
    if shared:
        return GateDecision(True, None, shared)
    if not a.any and not b.any:
        return GateDecision(allow_unidentified, None if allow_unidentified else "unidentified_subjects", shared)
    if a.any and b.any:
        return GateDecision(False, "different_subjects", shared)
    return GateDecision(False, "subject_unmatched", shared)
