"""隔离夹具自身的机械防线：本文件未豁免，数据目录必须落在临时目录。"""
import os
import tempfile


def test_data_dir_is_isolated_temp_dir():
    data_dir = os.environ.get("HOLDEXAR_DATA_DIR", "")
    assert data_dir, "隔离夹具未生效：HOLDEXAR_DATA_DIR 未设置"
    assert data_dir.startswith(tempfile.gettempdir()), (
        f"数据目录未隔离进临时目录：{data_dir}"
    )
