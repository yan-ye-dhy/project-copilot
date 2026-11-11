"""The broker contract cannot carry credentials, prompts or transport metadata."""
import json
from uuid import uuid4

import pytest

from project_copilot.services.messaging import JobMessage, parse_payload


def test_envelope_round_trip_has_only_bounded_dispatch_identifiers():
    message = JobMessage(uuid4(), uuid4(), 7)
    payload = message.to_payload()
    assert len(payload) < 512
    assert json.loads(payload) == {"event_id": str(message.event_id), "job_id": str(message.job_id),
                                  "dispatch_generation": 7, "schema_version": 1}
    assert parse_payload(payload) == message


@pytest.mark.parametrize("change", [
    {"prompt": "private text"}, {"job_id": None}, {"event_id": "not-an-id"},
    {"dispatch_generation": True}, {"dispatch_generation": 0}, {"dispatch_generation": -1},
    {"dispatch_generation": 2**31}, {"schema_version": True}, {"schema_version": 2},
])
def test_envelope_rejects_payload_extensions_and_unsupported_identities(change):
    data = json.loads(JobMessage(uuid4(), uuid4()).to_payload())
    data.update(change)
    with pytest.raises(ValueError, match="^invalid dispatch message$"):
        parse_payload(json.dumps(data).encode())


@pytest.mark.parametrize("payload", [b"", b"x" * 513, b"[]", b"{}", b"null", b"\xff", b"{", "{}"])
def test_envelope_rejects_non_message_bodies(payload):
    with pytest.raises(ValueError, match="^invalid dispatch message$"):
        parse_payload(payload)


def test_envelope_rejects_duplicate_fields_and_noncanonical_uuid():
    message = JobMessage(uuid4(), uuid4())
    payload = message.to_payload()
    duplicate = payload[:-1] + b',"schema_version":1}'
    for body in (duplicate, payload.replace(str(message.job_id).encode(), message.job_id.hex.encode())):
        with pytest.raises(ValueError, match="^invalid dispatch message$"):
            parse_payload(body)
