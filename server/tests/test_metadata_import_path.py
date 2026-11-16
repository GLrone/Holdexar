"""metadata 导入目录收容：请求给定的 source_dir 必须落在数据目录内。

回归锚：任意路径（如 C:/Windows）可被遍历且期望文件名可披露——目录闸
统一文案「导入目录不存在或不可用」，不回显请求路径。环境变量默认目录是
运营者自有配置（与 API 攻击面不同信任级），不在此闸内。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app.domains.metadata import router as metadata_router
from app.domains.metadata import service as metadata_service
from app.domains.metadata.router import ImportRequest


class _StubSettings:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir


@pytest.fixture
def confined(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(metadata_service, "get_settings",
                        lambda: _StubSettings(data_dir))
    return data_dir


def test_inside_data_dir_allowed(confined):
    inside = confined / "import"
    inside.mkdir()
    assert metadata_service.constrain_source_dir(str(inside)) == inside.resolve()


def test_default_source_dir_bypasses_gate(confined):
    """缺省（环境变量目录）不经闸——运营者自有配置，行为不变。"""
    assert metadata_service.constrain_source_dir(None) == metadata_service.DEFAULT_SOURCE_DIR


def test_outside_data_dir_rejected_without_echo(confined):
    escapes = [
        str(confined.parent / "elsewhere"),
        "C:/Windows",
        str(confined / ".." / ".."),
    ]
    for bad in escapes:
        with pytest.raises(metadata_service.SourceDirRejected) as ei:
            metadata_service.constrain_source_dir(bad)
        assert "导入目录不存在或不可用" in str(ei.value)
        # 不回显请求路径
        assert "elsewhere" not in str(ei.value)
        assert "Windows" not in str(ei.value)
        assert str(confined) not in str(ei.value)


def test_resolve_source_path_gated(confined):
    with pytest.raises(metadata_service.SourceDirRejected):
        metadata_service.resolve_source_path("C:/Windows", metadata_service._EPIC_FILE)


@pytest.mark.asyncio
async def test_import_endpoint_422_on_escape(confined):
    with pytest.raises(HTTPException) as ei:
        await metadata_router.import_metadata(
            ImportRequest(source="epic", source_dir="C:/Windows"))
    assert ei.value.status_code == 422
    assert "导入目录不存在或不可用" in ei.value.detail
    assert "Windows" not in ei.value.detail


@pytest.mark.asyncio
async def test_status_endpoint_422_on_escape(confined):
    with pytest.raises(HTTPException) as ei:
        await metadata_router.metadata_status(source_dir="C:/Windows")
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_import_endpoint_inside_data_dir_passes_gate(confined):
    """收容内的目录放行到导入器：文件缺失走既有「找不到数据文件」响应，非 422。"""
    inside = confined / "import"
    inside.mkdir()
    result = await metadata_router.import_metadata(
        ImportRequest(source="epic", source_dir=str(inside)))
    assert result["source"] == "epic"
    assert result["ok"] is False
