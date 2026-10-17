"""Windows PE 版本资源生成与校验测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from desktop.version_resource import (
    COMPANY_NAME,
    ROLE_META,
    generate_version_info_text,
    numeric_version,
    numeric_version_str,
    read_pe_properties,
    verify_exe_version_resources,
    write_version_file,
)


def test_numeric_version_conversion() -> None:
    assert numeric_version("0.3.5-beta") == (0, 3, 5, 0)
    assert numeric_version_str("0.3.5-beta") == "0.3.5.0"
    assert numeric_version("1.2.3.4") == (1, 2, 3, 4)
    assert numeric_version_str("1.2.3.4") == "1.2.3.4"
    assert numeric_version("0.1.0") == (0, 1, 0, 0)


def test_version_info_generation_all_roles(tmp_path: Path) -> None:
    for role in ("launcher", "updater", "desktop"):
        text = generate_version_info_text(role, "0.3.5-beta")
        meta = ROLE_META[role]
        assert meta["description"] in text
        assert meta["internal_name"] in text
        assert meta["original_filename"] in text
        assert COMPANY_NAME in text
        assert "0.3.5.0" in text
        assert "0.3.5-beta" in text

        out_file = tmp_path / f"{role}_version.txt"
        write_version_file(out_file, role, "0.3.5-beta")
        assert out_file.is_file()
        assert out_file.read_text(encoding="utf-8") == text


def test_verify_exe_version_resources_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="可执行文件缺失"):
        verify_exe_version_resources(tmp_path, "0.3.5-beta")


@pytest.mark.skipif(os.name != "nt", reason="PE 版本资源仅在 Windows 平台支持")
def test_read_pe_properties_on_python_exe() -> None:
    import sys

    props = read_pe_properties(Path(sys.executable))
    assert props.get("ProductName") == "Python"
    assert "FileVersion" in props
    assert "ProductVersion" in props
