"""模型菜单必须来自后端配置，且只向浏览器提供展示所需字段。"""

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.model.config import get_model_profile


def test_models_endpoint_uses_the_configured_profiles_and_default(monkeypatch):
    profile = replace(
        get_model_profile("ustc-deepseek-flash"),
        name="custom-provider",
        display_name="新模型服务",
        api_key_env="PRIVATE_API_KEY",
        base_url_env="PRIVATE_BASE_URL",
        supports_reasoning_effort=False,
    )
    monkeypatch.setattr(routes, "MODEL_PROFILES", {profile.name: profile})
    monkeypatch.setattr(routes, "DEFAULT_MODEL_NAME", profile.name)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.get("/api/models")
    assert response.status_code == 200
    assert response.json() == {
        "default_model": "custom-provider",
        "models": [{
            "name": "custom-provider",
            "display_name": "新模型服务",
            "supports_thinking": True,
            "supports_reasoning_effort": False,
        }],
    }
