"""Regression test for rank13: OPC-UA must not offer security policies its
``_async_connect`` cannot actually complete a handshake for.

``_async_connect`` calls ``client.set_security_string(f"{security},Sign,,")``
— empty cert/key paths — for any non-"None" policy. asyncua requires a
client certificate + private key for every encrypted policy, so picking
``Basic256Sha256`` (or any of the other previously-listed policies) could
never connect. Plan A: collapse the exposed choices to ``("None",)`` until
certificate/key upload is wired in, and say so in the help text.
"""
from __future__ import annotations

from acquisition.protocols.opcua import OPCUAProtocol, _SECURITY_POLICIES


def test_security_policies_collapsed_to_none_only():
    assert _SECURITY_POLICIES == ("None",)


def test_device_field_choices_match():
    by_name = {f.name: f for f in OPCUAProtocol.DEVICE_FIELDS}
    spec = by_name["security_policy"]
    assert spec.choices == ("None",)
    assert spec.default == "None"
    # Must explain *why* — otherwise it just looks like a missing feature.
    assert "证书" in spec.help_text or "加密" in spec.help_text


def test_validate_device_rejects_previously_offered_encrypted_policy():
    """Basic256Sha256 used to be a valid choice; it must now fail validation
    instead of silently being accepted and failing at connect time."""
    errors = OPCUAProtocol.validate_device(
        {"endpoint_url": "opc.tcp://10.0.0.1:4840", "security_policy": "Basic256Sha256"}
    )
    assert errors, "an unsupported security policy must be flagged at import/validate time"


def test_validate_device_accepts_none_policy():
    errors = OPCUAProtocol.validate_device(
        {"endpoint_url": "opc.tcp://10.0.0.1:4840", "security_policy": "None"}
    )
    assert errors == []


def test_validate_device_accepts_missing_policy_defaults_to_none():
    errors = OPCUAProtocol.validate_device({"endpoint_url": "opc.tcp://10.0.0.1:4840"})
    assert errors == []
