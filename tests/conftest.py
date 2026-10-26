"""离线采集器使用临时合成凭据；正式历史归档始终只读。"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest


@pytest.fixture
def bind_current_sources():
    """合成向量/假模型测试绑定当前源码；不替正式旧运行重签或放宽源码门禁。"""
    root = Path(__file__).resolve().parents[1]

    def bind(receipt):
        updated = deepcopy(receipt)
        for name in updated["source_sha256"]:
            path = root / name
            assert path.resolve().is_relative_to(root)
            updated["source_sha256"][name] = sha256(path.read_bytes()).hexdigest()
        return updated

    return bind
