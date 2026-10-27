"""更新清单数字签名：Ed25519，公钥内嵌于本模块，签名只认代码里的信任表。

信任模型：镜像与下载通道只是字节运输，不产生任何信任；清单必须由内嵌公钥
验签通过才可信，清单自带的一切字段（包括资产 sha256）都以签名为前提。
发布侧签名入口是 scripts/build_manifest.py（私钥在发布机、仓库外）。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# 信任表：keyId → 公钥原始 32 字节的 hex。客户端只认这里的公钥；
# 清单里出现的任何公钥字段一律忽略。轮换密钥时新增条目、保留旧条目。
TRUSTED_PUBKEYS: dict[str, str] = {
    "k1": "a2a39fde4347156b2353746a144f2dcdd1591fc77c43104206ab44641d898f8d",
}

SIGNATURE_FIELD = "signature"
SIGNATURE_ALG = "ed25519"


def canonical_manifest_bytes(manifest: dict) -> bytes:
    """签名覆盖对象的规范化字节：剔除签名字段后排序紧凑序列化。

    发布端与客户端共用本函数，两端字节必须逐位一致。
    """
    payload = {k: v for k, v in manifest.items() if k != SIGNATURE_FIELD}
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def verify_manifest(manifest: dict) -> tuple[bool, str]:
    """验签更新清单。返回 (是否可信, 失败原因)；可信时原因为空串。"""
    sig = manifest.get(SIGNATURE_FIELD)
    if not isinstance(sig, dict):
        return False, "清单缺少签名"
    if sig.get("alg") != SIGNATURE_ALG:
        return False, f"签名算法不支持：{sig.get('alg')!r}"
    key_id = str(sig.get("keyId") or "")
    pubkey_hex = TRUSTED_PUBKEYS.get(key_id)
    if not pubkey_hex:
        return False, f"签名公钥不在信任表内：{key_id!r}"
    value = sig.get("sig")
    if not isinstance(value, str) or not value:
        return False, "签名为空"
    try:
        signature = base64.b64decode(value, validate=True)
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        key.verify(signature, canonical_manifest_bytes(manifest))
    except (InvalidSignature, ValueError, TypeError) as exc:
        return False, f"签名校验失败：{exc}"
    return True, ""


def sign_manifest(manifest: dict, private_key_pem: Path) -> dict:
    """给清单补签名字段（发布侧专用）。返回带签名的清单副本。"""
    private_key = serialization.load_pem_private_key(
        private_key_pem.read_bytes(), password=None
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519（只支持更新签名密钥）")
    signature = private_key.sign(canonical_manifest_bytes(manifest))
    signed = dict(manifest)
    signed[SIGNATURE_FIELD] = {
        "alg": SIGNATURE_ALG,
        "keyId": _key_id_of(private_key),
        "sig": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def _key_id_of(private_key: Ed25519PrivateKey) -> str:
    """按公钥字节反查信任表里的 keyId；不在表内说明拿错了密钥。"""
    pub_hex = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    for key_id, trusted_hex in TRUSTED_PUBKEYS.items():
        if trusted_hex == pub_hex:
            return key_id
    raise ValueError("该私钥的公钥不在内嵌信任表内（先用 --generate-key 登记公钥）")


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519（只支持更新签名密钥）")
    return key


def generate_keypair(output: Path) -> str:
    """生成新密钥对，私钥写 output（不覆盖已有），返回公钥 hex 供登记。"""
    if output.exists():
        raise FileExistsError(f"私钥已存在，拒绝覆盖：{output}")
    key = Ed25519PrivateKey.generate()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
