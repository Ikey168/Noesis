"""Direct tests for the fail-closed person classifier (OS-A safety hardening).

``common.is_person`` backs the "never identify a person" guardrail on the gated
geolocation tool and the entity dossier. These pin the branches that used to
fail *open* (subset test + a 5-word role vocabulary) now failing *closed*.
"""

from src.osint import common


def _actors(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS document_actors ("
        "document_id VARCHAR, source_type VARCHAR, actor_name VARCHAR, "
        "entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO document_actors (document_id, actor_name, entity_id, role) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


# --- explicit type wins ------------------------------------------------------


def test_person_type_is_person(conn):
    assert common.is_person(conn, "Anyone", entity_type="person") is True


def test_nonperson_type_overrides_role_inference(conn):
    # An outlet tagged with a person-ish role, but the caller asserts it is an
    # organisation: the explicit non-person type wins.
    _actors(conn, [("d1", "Wire Co", "org:w", "author")])
    assert common.is_person(conn, "Wire Co", entity_type="organization") is False


def test_person_id_prefix_is_person(conn):
    assert common.is_person(conn, "person:jr") is True


# --- ANY-match, not subset (the core fail-open fix) --------------------------


def test_office_role_is_person_any_match(conn):
    # "president" is not in the old 5-word whitelist; under a subset test it
    # slipped the guard. ANY overlap with a person role now classifies it.
    _actors(conn, [("d1", "Sam Cole", "person:sc", "president")])
    assert common.is_person(conn, "Sam Cole") is True


def test_mixed_roles_including_a_person_role_is_person(conn):
    # A person tagged both "subject" and "official" — subset test would fail
    # (official was absent); ANY-match catches it.
    _actors(conn, [
        ("d1", "Pat Lane", "person:pl", "official"),
        ("d2", "Pat Lane", "person:pl", "subject"),
    ])
    assert common.is_person(conn, "Pat Lane") is True


def test_org_only_roles_is_not_person(conn):
    _actors(conn, [("d1", "Grid Authority", "org:ga", "organization")])
    assert common.is_person(conn, "Grid Authority") is False


# --- unknown handling: fail-closed by default, positive-only for free text ---


def test_unknown_entity_defaults_to_person(conn):
    _actors(conn, [("d1", "Someone", "person:s", "speaker")])
    # A name absent from the actor layer cannot be classified -> fail closed.
    assert common.is_person(conn, "Absent Name") is True


def test_unknown_entity_is_not_person_for_free_text_scan(conn):
    _actors(conn, [("d1", "Someone", "person:s", "speaker")])
    # A topic substring that names nobody must not be refused as a person.
    assert common.is_person(conn, "flooding", unknown_is_person=False) is False


def test_no_actor_layer_falls_back_to_unknown_policy(conn):
    # No document_actors table at all.
    assert common.is_person(conn, "Anyone") is True
    assert common.is_person(conn, "Anyone", unknown_is_person=False) is False
