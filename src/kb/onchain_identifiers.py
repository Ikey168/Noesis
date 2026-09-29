"""Chain, address and transaction identifiers for the On-chain Observations pack (#2055).

Stdlib only. Every identifier is normalised the same way on both sides of any
match, so a lookup, a store key, a cluster edge and a monitor all agree:

* chains are CAIP-2 ids: ``eip155:1`` (Ethereum mainnet) and
  ``bip122:000000000019d6689c085ae165831e93`` (Bitcoin mainnet). The same
  address string on two chains is two different records.
* EVM addresses are stored lower-case with the ``0x`` prefix; a mixed-case
  input must carry a valid EIP-55 checksum (Keccak-256 is implemented here
  because the standard library only ships SHA3, whose padding differs).
  :func:`checksum_address` renders the EIP-55 form for display and explorer
  locators.
* Bitcoin addresses are validated by their own checksums: Base58Check for
  P2PKH/P2SH and BIP-173 bech32 / BIP-350 bech32m for segwit outputs; segwit
  addresses are stored lower-case, Base58 addresses as written (they are case
  sensitive).
* transaction hashes are 32 bytes of hex: ``0x``-prefixed lower-case on EVM
  chains, bare lower-case on Bitcoin (a stray ``0x`` is removed).

Names (ENS and the like), e-mail addresses, handles and free text are refused:
acquisition and queries take one explicit ledger identifier, never a name.
"""

from __future__ import annotations

import hashlib
import re

ETHEREUM = "eip155:1"
BITCOIN = "bip122:000000000019d6689c085ae165831e93"
CHAINS = {
    ETHEREUM: {"family": "evm", "name": "Ethereum mainnet", "finality_depth": 64},
    BITCOIN: {"family": "utxo", "name": "Bitcoin mainnet", "finality_depth": 6},
}
CHAIN_ALIASES = {
    "ethereum": ETHEREUM,
    "eth": ETHEREUM,
    "1": ETHEREUM,
    "bitcoin": BITCOIN,
    "btc": BITCOIN,
}


class IdentifierError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- Keccak-256

_RC = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)
# Rotation offsets r[x][y].
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1


def _rol(value: int, shift: int) -> int:
    shift %= 64
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _keccak_f(state: list[int]) -> None:
    for rc in _RC:
        c = [
            state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
            for x in range(5)
        ]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for i in range(25):
            state[i] ^= d[i % 5]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(state[x + 5 * y], _ROT[x][y])
        for y in range(5):
            row = b[5 * y : 5 * y + 5]
            for x in range(5):
                state[x + 5 * y] = row[x] ^ ((~row[(x + 1) % 5]) & row[(x + 2) % 5])
        state[0] ^= rc


def _sponge(data: bytes, pad: int, rate: int = 136, out_len: int = 32) -> bytes:
    message = bytearray(data)
    message.append(pad)
    while len(message) % rate:
        message.append(0)
    message[-1] |= 0x80
    state = [0] * 25
    for offset in range(0, len(message), rate):
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(
                message[offset + 8 * i : offset + 8 * i + 8], "little"
            )
        _keccak_f(state)
    return b"".join(lane.to_bytes(8, "little") for lane in state)[:out_len]


def keccak256(data: bytes) -> bytes:
    """Keccak-256 as used by Ethereum (original Keccak padding ``0x01``)."""
    return _sponge(bytes(data), 0x01)


def _sha3_256(data: bytes) -> bytes:
    """FIPS-202 SHA3-256 through the same permutation; tests compare it to hashlib."""
    return _sponge(bytes(data), 0x06)


# --------------------------------------------------------------------------- chains


def normalize_chain(value: object) -> str:
    raw = str(value or "").strip().lower()
    raw = CHAIN_ALIASES.get(raw, raw)
    if raw not in CHAINS:
        raise IdentifierError(
            "unsupported_chain",
            f"unsupported chain {value!r}; supported: {sorted(CHAINS)}",
        )
    return raw


def family(chain_id: str) -> str:
    return CHAINS[normalize_chain(chain_id)]["family"]


# --------------------------------------------------------------------------- EVM

_HEX40 = re.compile(r"^(0x)?[0-9a-fA-F]{40}$")
_HEX64 = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def checksum_address(address: str) -> str:
    """The EIP-55 mixed-case form of an EVM address."""
    lower = str(address).lower().removeprefix("0x")
    digest = keccak256(lower.encode("ascii")).hex()
    return "0x" + "".join(
        ch.upper() if ch.isalpha() and int(digest[i], 16) >= 8 else ch
        for i, ch in enumerate(lower)
    )


def _normalize_evm_address(raw: str) -> str:
    if not _HEX40.match(raw):
        raise IdentifierError(
            "invalid_address", "an EVM address is 0x followed by 40 hex digits"
        )
    body = raw[2:] if raw.lower().startswith("0x") else raw
    if body != body.lower() and body != body.upper():
        if checksum_address(body)[2:] != body:
            raise IdentifierError(
                "invalid_checksum", "the mixed-case address fails its EIP-55 checksum"
            )
    return "0x" + body.lower()


# --------------------------------------------------------------------------- Bitcoin

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32M_CONST = 0x2BC830A3


def _b58decode(text: str) -> bytes:
    value = 0
    for ch in text:
        index = _B58.find(ch)
        if index < 0:
            raise IdentifierError("invalid_address", "not a Base58 string")
        value = value * 58 + index
    body = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def b58check_encode(payload: bytes) -> str:
    """Base58Check encoding (used to build fictional fixture addresses)."""
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    value = int.from_bytes(data, "big")
    out = ""
    while value:
        value, rem = divmod(value, 58)
        out = _B58[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def _polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(
    data: list[int], frombits: int, tobits: int, pad: bool
) -> list[int] | None:
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        return None
    return out


def segwit_encode(hrp: str, witver: int, program: bytes) -> str:
    """BIP-173/350 encoding (used to build fictional fixture addresses)."""
    data = [witver] + (_convertbits(list(program), 8, 5, True) or [])
    const = 1 if witver == 0 else _BECH32M_CONST
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


def _normalize_segwit(raw: str) -> str:
    if raw != raw.lower() and raw != raw.upper():
        raise IdentifierError("invalid_address", "bech32 addresses are not mixed case")
    text = raw.lower()
    pos = text.rfind("1")
    hrp, payload = text[:pos], text[pos + 1 :]
    if hrp != "bc" or len(payload) < 6 or any(c not in _BECH32 for c in payload):
        raise IdentifierError("invalid_address", "not a Bitcoin mainnet segwit address")
    data = [_BECH32.find(c) for c in payload]
    const = _polymod(_hrp_expand(hrp) + data)
    witver = data[0]
    if const not in (1, _BECH32M_CONST) or (witver == 0) != (const == 1):
        raise IdentifierError(
            "invalid_checksum", "the segwit address fails its bech32/bech32m checksum"
        )
    program = _convertbits(data[1:-6], 5, 8, False)
    if (
        program is None
        or not 2 <= len(program) <= 40
        or witver > 16
        or (witver == 0 and len(program) not in (20, 32))
    ):
        raise IdentifierError("invalid_address", "invalid segwit program")
    return text


def _normalize_base58(raw: str) -> str:
    data = _b58decode(raw)
    if len(data) != 25:
        raise IdentifierError("invalid_address", "a Base58 address decodes to 25 bytes")
    payload, check = data[:-4], data[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != check:
        raise IdentifierError(
            "invalid_checksum", "the Base58 address fails its checksum"
        )
    if payload[0] not in (0x00, 0x05):
        raise IdentifierError(
            "invalid_address", "not a Bitcoin mainnet P2PKH or P2SH address"
        )
    return raw


# --------------------------------------------------------------------------- public


def _screen(raw: str) -> None:
    if not raw:
        raise IdentifierError("invalid_address", "an address is required")
    if "@" in raw:
        raise IdentifierError(
            "person_identifier_refused",
            "e-mail addresses and handles are not ledger identifiers",
        )
    if raw.lower().endswith((".eth", ".crypto", ".sol")) or " " in raw or "." in raw:
        raise IdentifierError(
            "name_lookup_refused",
            "names are never resolved; pass an explicit ledger address",
        )


def normalize_address(chain_id: str, value: object) -> str:
    """The canonical stored form of an address on ``chain_id``, or :class:`IdentifierError`."""
    chain = normalize_chain(chain_id)
    raw = str(value or "").strip()
    _screen(raw)
    if CHAINS[chain]["family"] == "evm":
        return _normalize_evm_address(raw)
    if raw.lower().startswith("bc1"):
        return _normalize_segwit(raw)
    return _normalize_base58(raw)


def normalize_tx_hash(chain_id: str, value: object) -> str:
    chain = normalize_chain(chain_id)
    raw = str(value or "").strip()
    if not _HEX64.match(raw):
        raise IdentifierError(
            "invalid_tx_hash", "a transaction hash is 32 bytes of hex"
        )
    body = (raw[2:] if raw.lower().startswith("0x") else raw).lower()
    return "0x" + body if CHAINS[chain]["family"] == "evm" else body


def display_address(chain_id: str, address: str) -> str:
    return checksum_address(address) if family(chain_id) == "evm" else address


def explorer_url(chain_id: str, kind: str, identifier: str) -> str:
    """The public explorer page an observation is cited to (a human-readable locator)."""
    if family(chain_id) == "evm":
        base = "https://etherscan.io"
        path = {"tx": "tx", "address": "address", "block": "block"}[kind]
        ident = checksum_address(identifier) if kind == "address" else identifier
        return f"{base}/{path}/{ident}"
    return f"https://blockstream.info/{kind}/{identifier}"


def is_evm_address(value: object) -> bool:
    return bool(_HEX40.match(str(value or "")))


def fixture_evm_address(label: str) -> str:
    """A fictional, correctly shaped EVM address derived from a label (tests and fixtures only)."""
    return checksum_address(keccak256(("noesis-fixture:" + label).encode()).hex()[-40:])


def fixture_tx_hash(label: str, *, evm: bool = True) -> str:
    body = hashlib.sha256(("noesis-fixture-tx:" + label).encode()).hexdigest()
    return "0x" + body if evm else body


def fixture_btc_address(label: str) -> str:
    """A fictional P2WPKH address with a valid bech32 checksum (tests and fixtures only)."""
    return segwit_encode(
        "bc", 0, hashlib.sha256(("noesis-fixture-btc:" + label).encode()).digest()[:20]
    )
