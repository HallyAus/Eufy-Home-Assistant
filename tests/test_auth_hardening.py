"""Offline regression tests for mailbox verification and account-bound auth caches."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]

CLOUD_SPEC = importlib.util.spec_from_file_location(
    "eufy_cloud", ROOT / "bridge/eufy_cloud.py"
)
cloud = importlib.util.module_from_spec(CLOUD_SPEC)
sys.modules[CLOUD_SPEC.name] = cloud
assert CLOUD_SPEC.loader is not None
CLOUD_SPEC.loader.exec_module(cloud)

AUTH_SPEC = importlib.util.spec_from_file_location(
    "auth_login_hardening", ROOT / "bridge/auth_login.py"
)
auth_login = importlib.util.module_from_spec(AUTH_SPEC)
assert AUTH_SPEC.loader is not None
AUTH_SPEC.loader.exec_module(auth_login)


def challenge() -> dict:
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "auth_token": "provisional-token",
            "fa_info": {"step": 26052},
            "server_secret_info": {"public_key": "server-public-key"},
        },
    }


@pytest.mark.asyncio
async def test_challenge_requests_code_through_official_push_endpoint():
    post = AsyncMock(
        side_effect=[challenge(), {"code": 0, "msg": "sent", "data": {}}]
    )
    with patch.object(cloud, "encrypted_post", post):
        with pytest.raises(cloud.EufyVerificationRequired, match="email verification"):
            await cloud.login(
                "owner@example.com", "correct horse battery staple",
                key_obj=object(),
            )

    send_call = post.await_args_list[1]
    assert send_call.args[0] == "/app/sendmsg/verify_code"
    assert send_call.args[1] == {
        "biz_type": 1004,
        "message_type": 2,
        "client_secret_info": {"public_key": "server-public-key"},
    }
    assert send_call.args[2] == "provisional-token"
    assert send_call.kwargs["service"] == "push"
    assert send_call.kwargs["extra_headers"]["X-Auth-Token"] == "provisional-token"


@pytest.mark.asyncio
async def test_six_digit_code_repeats_login_with_provisional_token():
    post = AsyncMock(
        side_effect=[
            challenge(),
            {
                "code": 0,
                "msg": "ok",
                "data": {"auth_token": "final-token", "user_id": "owner-id"},
            },
        ]
    )
    with patch.object(cloud, "encrypted_post", post):
        result = await cloud.login(
            "owner@example.com", "correct horse battery staple",
            verification_code="123456", key_obj=object(),
        )

    verify_call = post.await_args_list[1]
    assert verify_call.args[0] == "/passport/login"
    assert verify_call.args[1]["verify_code"] == "123456"
    assert verify_call.args[2] == "provisional-token"
    assert result["auth_token"] == "final-token"


@pytest.mark.asyncio
async def test_malformed_verification_code_is_rejected_locally():
    with patch.object(cloud, "encrypted_post", AsyncMock(return_value=challenge())):
        with pytest.raises(cloud.EufyCloudError, match="exactly six digits"):
            await cloud.login(
                "owner@example.com", "correct horse battery staple",
                verification_code="12ab", key_obj=object(),
            )


def test_cache_is_bound_to_account_region_and_country(tmp_path: Path):
    email, region, country = "Owner@Example.com", "us-pr", "AU"
    fingerprint = auth_login.account_fingerprint(email, region, country)
    cache = tmp_path / "auth.json"
    cache.write_text(
        json.dumps(
            {
                "cacheVersion": 1,
                "accountFingerprint": fingerprint,
                "authToken": "secret",
                "stationSn": "T8N000000000",
            }
        ),
        encoding="utf-8",
    )

    assert auth_login.cache_matches(str(cache), email.lower(), region, country)
    assert not auth_login.cache_matches(str(cache), "other@example.com", region, country)
    assert not auth_login.cache_matches(str(cache), email, "eu-pr", country)
    assert not auth_login.cache_matches(str(cache), email, region, "US")
    assert "owner@example.com" not in fingerprint


def test_legacy_or_incomplete_cache_is_never_used_as_fallback(tmp_path: Path):
    cache = tmp_path / "auth.json"
    cache.write_text(
        json.dumps({"authToken": "old-token", "stationSn": "T8N000000000"}),
        encoding="utf-8",
    )
    assert not auth_login.cache_matches(
        str(cache), "owner@example.com", "us-pr", "US"
    )
