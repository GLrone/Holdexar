"""Publisher-signed update payloads for isolated update-host tests."""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core import update_signing
from app.core.app_info import APP_NAME, MANIFEST_SCHEMA

_KEY_ID = "update-test-fixture"
_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_PUBLIC_KEY = _KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
).hex()


def seal_staging(staging: Path, tag: str, *, ready: bool = True) -> None:
    """Write the same signed release manifest shape consumed by production."""
    payload = staging / APP_NAME
    archive = staging / "update.zip"
    files: dict[str, str] = {}
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                relative = path.relative_to(payload).as_posix()
                content = path.read_bytes()
                files[relative] = hashlib.sha256(content).hexdigest()
                bundle.writestr(f"{APP_NAME}/{relative}", content)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "app": APP_NAME,
        "version": tag.lstrip("vV"),
        "tag": tag,
        "assets": [{
            "role": "app",
            "name": f"{APP_NAME}-win64-{tag}.zip",
            "size": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "files": files,
        }],
    }
    signature = _KEY.sign(update_signing.canonical_manifest_bytes(manifest))
    update_signing.TRUSTED_PUBKEYS[_KEY_ID] = _PUBLIC_KEY
    release = {
        **manifest,
        update_signing.SIGNATURE_FIELD: {
            "alg": update_signing.SIGNATURE_ALG,
            "keyId": _KEY_ID,
            "sig": base64.b64encode(signature).decode("ascii"),
        },
    }
    (staging / "manifest.json").write_text(json.dumps({
        "tag": tag,
        "ready": ready,
        "release_manifest": release,
    }), encoding="utf-8")
