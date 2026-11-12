import pytest


@pytest.fixture(autouse=True)
def _isolated_lab_data(tmp_path, monkeypatch):
    # 外部連携の保存先（connections.py）を試験ごとの一時フォルダにし、本物の .lab/ を読み書きしない
    monkeypatch.setenv("LAB_DATA_DIR", str(tmp_path / "lab"))
