"""Check the DER fingerprint override without contacting an NVR."""

import ast
import hashlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from OpenSSL import crypto
from aiortc import rtcdtlstransport


def _validator():
    path = Path(__file__).resolve().parents[1] / "bridge/eufy_stream.py"
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_peer_identity_via_der"
    )
    namespace = {
        "_ossl_crypto": crypto,
        "_hashlib": hashlib,
        "_PEER_DIGESTS": {"sha-256": hashlib.sha256, "sha-384": hashlib.sha384, "sha-512": hashlib.sha512},
        "_dtls_mod": rtcdtlstransport,
        "time": time,
        "sys": sys,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function.name]


def _certificate():
    key = crypto.PKey()
    key.generate_key(crypto.TYPE_RSA, 2048)
    certificate = crypto.X509()
    certificate.get_subject().CN = "test-nvr"
    certificate.set_serial_number(1)
    certificate.gmtime_adj_notBefore(0)
    certificate.gmtime_adj_notAfter(60)
    certificate.set_issuer(certificate.get_subject())
    certificate.set_pubkey(key)
    certificate.sign(key, "sha256")
    return certificate


def _transport(certificate):
    transport = SimpleNamespace(
        _ssl=SimpleNamespace(get_peer_certificate=lambda: certificate),
        state=None,
    )
    transport._set_state = lambda state: setattr(transport, "state", state)
    return transport


def test_dtls_accepts_matching_der_fingerprint_and_rejects_mismatch():
    validate = _validator()
    certificate = _certificate()
    digest = hashlib.sha256(crypto.dump_certificate(crypto.FILETYPE_ASN1, certificate)).hexdigest().upper()
    fingerprint = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
    transport = _transport(certificate)

    validate(transport, SimpleNamespace(fingerprints=[SimpleNamespace(algorithm="sha-256", value=fingerprint)]))
    assert transport.state is None

    validate(transport, SimpleNamespace(fingerprints=[SimpleNamespace(algorithm="sha-256", value="00")]))
    assert transport.state == rtcdtlstransport.State.FAILED


def test_dtls_fails_closed_for_missing_certificate_or_supported_fingerprint():
    validate = _validator()
    missing = _transport(None)
    validate(missing, SimpleNamespace(fingerprints=[]))
    assert missing.state == rtcdtlstransport.State.FAILED

    unsupported = _transport(_certificate())
    validate(unsupported, SimpleNamespace(fingerprints=[SimpleNamespace(algorithm="sha-1", value="00")]))
    assert unsupported.state == rtcdtlstransport.State.FAILED
