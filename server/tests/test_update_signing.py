"""更新清单签名验收：签发/验签/篡改检测/信任表边界（不碰真实私钥）。"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import update_signing  # noqa: E402


@pytest.fixture
def test_key(monkeypatch) -> tuple[Ed25519PrivateKey, dict]:
    """一次性密钥对 + 注入信任表；返回 (私钥, 签名好的样例清单)。"""
    key = Ed25519PrivateKey.generate()
    pub_hex = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    monkeypatch.setattr(update_signing, "TRUSTED_PUBKEYS", {"test": pub_hex})
    return key, {"test": pub_hex}


def _sign(key: Ed25519PrivateKey, manifest: dict, key_id: str = "test") -> dict:
    signed = dict(manifest)
    signature = key.sign(update_signing.canonical_manifest_bytes(manifest))
    signed["signature"] = {
        "alg": "ed25519", "keyId": key_id,
        "sig": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def _sample() -> dict:
    return {
        "schema": 2, "app": "Holdexar", "version": "0.3.6", "tag": "v0.3.6",
        "notes": "更新说明：修复若干问题",
        "assets": [{"role": "app", "name": "x.zip", "size": 10, "sha256": "a" * 64}],
    }


def test_sign_and_verify_roundtrip(test_key) -> None:
    key, _ = test_key
    signed = _sign(key, _sample())
    ok, reason = update_signing.verify_manifest(signed)
    assert ok is True, reason


def test_verify_survives_json_roundtrip(test_key) -> None:
    """客户端拿到的是 JSON 文本：parse 回 dict 后验签必须仍然通过。"""
    key, _ = test_key
    signed = _sign(key, _sample())
    text = json.dumps(signed, ensure_ascii=False, indent=2)
    ok, reason = update_signing.verify_manifest(json.loads(text))
    assert ok is True, reason


def test_tampered_fields_fail_verification(test_key) -> None:
    """签名后改动任何被签字段（版本/资产哈希/删字段）都必须验不过。"""
    key, _ = test_key
    base = _sign(key, _sample())

    tampered = dict(base, version="99.99.99")
    assert update_signing.verify_manifest(tampered)[0] is False

    tampered = json.loads(json.dumps(base))
    tampered["assets"][0]["sha256"] = "b" * 64
    assert update_signing.verify_manifest(tampered)[0] is False

    tampered = dict(base)
    del tampered["notes"]
    assert update_signing.verify_manifest(tampered)[0] is False


def test_unsigned_manifest_rejected(test_key) -> None:
    ok, reason = update_signing.verify_manifest(_sample())
    assert ok is False
    assert "签名" in reason


def test_unknown_key_id_rejected(test_key, monkeypatch) -> None:
    """攻击者自签清单（keyId 不在信任表内）必须被拒——清单不能自带信任锚。"""
    key, trusted = test_key
    attacker = Ed25519PrivateKey.generate()
    signed = _sign(attacker, _sample(), key_id="attacker")
    ok, reason = update_signing.verify_manifest(signed)
    assert ok is False
    assert "信任表" in reason

    # 攻击者把公钥塞进清单字段同样无效：verify 只认代码内嵌信任表
    pub_hex = attacker.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    signed["pubkey"] = pub_hex
    assert update_signing.verify_manifest(signed)[0] is False


def test_wrong_key_same_id_rejected(test_key) -> None:
    """同 keyId 但公钥不同（换钥伪装）：签名校验本身就会失败。"""
    key, trusted = test_key
    other = Ed25519PrivateKey.generate()
    signed = _sign(other, _sample(), key_id="test")
    assert update_signing.verify_manifest(signed)[0] is False


def test_bad_signature_shapes_rejected(test_key) -> None:
    key, _ = test_key
    signed = _sign(key, _sample())

    bad_alg = json.loads(json.dumps(signed))
    bad_alg["signature"]["alg"] = "rs256"
    assert update_signing.verify_manifest(bad_alg)[0] is False

    bad_sig = json.loads(json.dumps(signed))
    bad_sig["signature"]["sig"] = base64.b64encode(b"\x00" * 64).decode()
    assert update_signing.verify_manifest(bad_sig)[0] is False

    empty_sig = json.loads(json.dumps(signed))
    empty_sig["signature"]["sig"] = ""
    assert update_signing.verify_manifest(empty_sig)[0] is False

    assert update_signing.verify_manifest({"version": "1", "signature": "not-a-dict"})[0] is False


def test_signature_field_excluded_from_canonical_bytes() -> None:
    """规范化必须剔除签名字段本身，否则签名自指导致永远验不过。"""
    manifest = _sample()
    data_without = update_signing.canonical_manifest_bytes(manifest)
    manifest["signature"] = {"alg": "ed25519", "keyId": "k", "sig": "x"}
    data_with = update_signing.canonical_manifest_bytes(manifest)
    assert data_without == data_with


def test_canonical_bytes_stable_across_key_order() -> None:
    """键序无关：同一内容的两种构造顺序产出相同规范化字节。"""
    a = {"schema": 2, "version": "1", "assets": [{"sha256": "a", "name": "x"}]}
    b = {"assets": [{"name": "x", "sha256": "a"}], "version": "1", "schema": 2}
    assert update_signing.canonical_manifest_bytes(a) == update_signing.canonical_manifest_bytes(b)


def test_generate_keypair_and_sign_manifest(tmp_path: Path, monkeypatch) -> None:
    """发布侧全链：生成密钥 → 登记 → 签名 → 验签（用临时信任表，不碰真实 k1）。"""
    key_path = tmp_path / "k.pem"
    pub_hex = update_signing.generate_keypair(key_path)
    monkeypatch.setattr(update_signing, "TRUSTED_PUBKEYS", {"gen": pub_hex})

    signed = update_signing.sign_manifest(_sample(), key_path)
    ok, reason = update_signing.verify_manifest(signed)
    assert ok is True, reason
    assert signed["signature"]["keyId"] == "gen"

    with pytest.raises(FileExistsError):
        update_signing.generate_keypair(key_path)
