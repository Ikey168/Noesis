"""Chain, address and hash identifiers (#2055)."""

from __future__ import annotations

import hashlib
import os

import pytest

from src.kb import onchain_identifiers as ids
from src.kb.onchain_identifiers import BITCOIN, ETHEREUM, IdentifierError


def test_keccak_matches_known_vectors_and_the_permutation_matches_hashlib_sha3():
    assert (
        ids.keccak256(b"").hex()
        == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    for size in (0, 1, 135, 136, 137, 272, 500):
        data = os.urandom(size)
        assert ids._sha3_256(data) == hashlib.sha3_256(data).digest()


@pytest.mark.parametrize(
    "address",
    [
        # EIP-55 specification test vectors.
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ],
)
def test_eip55_checksums(address):
    assert ids.checksum_address(address.lower()) == address
    assert ids.normalize_address(ETHEREUM, address) == address.lower()


def test_evm_normalisation_is_the_same_on_both_sides_of_a_match():
    checksummed = ids.fixture_evm_address("id-test")
    assert ids.normalize_address("ethereum", checksummed) == ids.normalize_address(
        ETHEREUM, checksummed.lower()
    )
    assert ids.normalize_address(ETHEREUM, checksummed[2:]) == checksummed.lower()
    broken = checksummed[:-1] + (
        checksummed[-1].lower()
        if checksummed[-1].isupper()
        else checksummed[-1].upper()
    )
    if broken[-1].isalpha():
        with pytest.raises(IdentifierError) as exc:
            ids.normalize_address(ETHEREUM, broken)
        assert exc.value.code == "invalid_checksum"


def test_bitcoin_addresses_are_checksum_validated():
    segwit = ids.fixture_btc_address("id-test")
    assert ids.normalize_address(BITCOIN, segwit.upper()) == segwit
    assert (
        ids.normalize_address(BITCOIN, "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4")
        == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    )  # BIP-173 test vector
    base58 = ids.b58check_encode(b"\x00" + hashlib.sha256(b"id-test").digest()[:20])
    assert ids.normalize_address(BITCOIN, base58) == base58
    with pytest.raises(IdentifierError):
        ids.normalize_address(
            BITCOIN, segwit[:-1] + ("q" if segwit[-1] != "q" else "p")
        )
    with pytest.raises(IdentifierError):
        ids.normalize_address(
            BITCOIN, base58[:-1] + ("2" if base58[-1] != "2" else "3")
        )


def test_hashes_names_and_person_identifiers():
    body = "AB" * 32
    assert ids.normalize_tx_hash(ETHEREUM, body) == "0x" + body.lower()
    assert ids.normalize_tx_hash(BITCOIN, "0x" + body) == body.lower()
    with pytest.raises(IdentifierError):
        ids.normalize_tx_hash(ETHEREUM, "0x123")
    for value, code in (
        ("vitalik.eth", "name_lookup_refused"),
        ("a@b.example", "person_identifier_refused"),
        ("", "invalid_address"),
    ):
        with pytest.raises(IdentifierError) as exc:
            ids.normalize_address(ETHEREUM, value)
        assert exc.value.code == code
    with pytest.raises(IdentifierError):
        ids.normalize_chain("eip155:137")


def test_explorer_urls_use_checksummed_addresses():
    address = ids.fixture_evm_address("id-url").lower()
    assert ids.explorer_url(ETHEREUM, "address", address).endswith(
        ids.checksum_address(address)
    )
    assert (
        ids.explorer_url(BITCOIN, "tx", "ab" * 32)
        == "https://blockstream.info/tx/" + "ab" * 32
    )
