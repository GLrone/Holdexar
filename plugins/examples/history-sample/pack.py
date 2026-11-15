import hashlib
import json
import pathlib
import sys
import zipfile


def pack(destination: pathlib.Path) -> str:
    source = pathlib.Path(__file__).parent
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    observations = (source / "observations.json").read_bytes()
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    with zipfile.ZipFile(destination, "x") as archive:
        manifest_member = zipfile.ZipInfo("manifest.json", (1980, 1, 1, 0, 0, 0))
        manifest_member.external_attr = 0o100644 << 16
        archive.writestr(manifest_member, payload)
        data_member = zipfile.ZipInfo("observations.json", (1980, 1, 1, 0, 0, 0))
        data_member.external_attr = 0o100644 << 16
        archive.writestr(data_member, observations)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python pack.py OUTPUT.hlx")
    print(pack(pathlib.Path(sys.argv[1])))
