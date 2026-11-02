"""Verify the candidate's pinned public bridge is the source under test."""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = "5a3bef581aab1bc6b19970eccd0abe57b3a53b05"
BRIDGE_BLOBS = {
    "bridge/auth_login.py": "c981b3cc433f55626967067c48ec9e5ffa2a2bc4",
    "bridge/eufy_cloud.py": "616a4fed1d47c635b22c5f0e3f955d227ad22779",
    "bridge/eufy_run.py": "c8d8052ad9650d4e37b3465fa1f686fe86023764",
    "bridge/eufy_stream.py": "7b4c53b34e4c613085cb1ceea92adc9897c0545e",
    "bridge/eufy_warm.py": "eded1b53d3c3f151561dd459ae2df84777dc3453",
    "bridge/fetch_deps.js": "8b70f9fee2036d1157567d8ec39c81553f67ac5e",
    "bridge/gen_go2rtc.py": "1134b95be11606603cff72ea09498c31c0fed711",
    "bridge/get_auth.js": "86f2cf95c591c56db80e62313cf3d33bc5d540ba",
    "bridge/go2rtc.example.yaml": "a9f6e7e6faac9a4dc8d253feefa4806a7a289f07",
    "bridge/requirements.txt": "d7cd3f3d1d24fc91d65e481dce19221d89109e9f",
    "bridge/sctp_oracle.js": "2dbcb3ce76f34b3472390e85f8e9408455e8f1eb",
    "bridge/start_bridge.cmd": "9b1633791982482cf5cf9c710eebfa9b8d548878",
    "bridge/start_bridge.sh": "d0a3488a289286b463affb6478a9a20cea1f6003"
}


def test_candidate_bridge_matches_pinned_github_tree():
    dockerfile = (ROOT / "eufy_nvr/Dockerfile").read_text()
    assert re.search(r'^ARG REPO_COMMIT="' + PIN + r'"$', dockerfile, re.MULTILINE)
    actual_paths = {p.relative_to(ROOT).as_posix() for p in (ROOT / "bridge").rglob("*") if p.is_file() and "__pycache__" not in p.parts and "worker" not in p.parts}
    assert actual_paths == set(BRIDGE_BLOBS)
    for path, expected in BRIDGE_BLOBS.items():
        data = (ROOT / path).read_bytes()
        digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        assert digest == expected, path
