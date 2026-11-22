"""接口测试：健康检查与鉴权开关"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api as api_module


def test_health():
    """/health 不查库，服务起来就该返回 ok"""
    client = TestClient(api_module.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_key_disabled_by_default(monkeypatch):
    """未配置 API_KEY 时不校验，单机部署零负担"""
    monkeypatch.setattr(api_module, "API_KEY", "")
    assert api_module.verify_api_key(None) is None


def test_api_key_rejects_missing(monkeypatch):
    """配置了 Key 但请求没带，返回 401"""
    monkeypatch.setattr(api_module, "API_KEY", "secret")
    with pytest.raises(HTTPException) as exc:
        api_module.verify_api_key(None)
    assert exc.value.status_code == 401


def test_api_key_rejects_wrong(monkeypatch):
    """Key 不匹配返回 401"""
    monkeypatch.setattr(api_module, "API_KEY", "secret")
    with pytest.raises(HTTPException) as exc:
        api_module.verify_api_key("wrong")
    assert exc.value.status_code == 401


def test_api_key_accepts_correct(monkeypatch):
    """Key 匹配时放行"""
    monkeypatch.setattr(api_module, "API_KEY", "secret")
    assert api_module.verify_api_key("secret") is None
