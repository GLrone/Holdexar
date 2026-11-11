import hashlib
import json
import pathlib
import sys
import zipfile


def pack(destination: pathlib.Path) -> str:
    source = pathlib.Path(__file__).with_name("manifest.json")
    manifest = json.loads(source.read_text(encoding="utf-8"))
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    member = zipfile.ZipInfo("manifest.json", (1980, 1, 1, 0, 0, 0))
    member.external_attr = 0o100644 << 16
    with zipfile.ZipFile(destination, "x") as archive:
        archive.writestr(member, payload)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python pack.py OUTPUT.hlx")
    print(pack(pathlib.Path(sys.argv[1])))
