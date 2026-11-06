"""opencode 全局配置 → Flux 模型目录的读取契约。

手机端「切换 Agent 与模型」只展示 agent.spec.models + key_models；
opencode 的模型（含 Zen 免费模型）必须从它自己的全局配置进目录，
否则手机上只能选到密钥库里的付费模型，免费模型无处可选。
"""

from __future__ import annotations

import json
from pathlib import Path

from flux.config import Settings
from flux.core.agent_runtime.model_catalog import (
    default_model_for_runtime,
    models_for_runtime,
)
from flux.core.agent_runtime.runtimes.base import RUNTIME_OPENCODE

SECRET = "oc_sk_test_secret_value"


def _write_config(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _settings(path: Path) -> Settings:
    return Settings(opencode_config_file=str(path))


def test_free_models_from_global_config_are_selectable(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "provider": {
                "opencode": {
                    "options": {"apiKey": SECRET},
                    "models": {
                        "deepseek-v4.1-flash-free": {"name": "free"},
                        "big-pickle": {"name": "free"},
                    },
                }
            },
            "model": "opencode/big-pickle",
        },
    )
    assert models_for_runtime(_settings(path), RUNTIME_OPENCODE) == [
        "opencode/big-pickle",
        "opencode/deepseek-v4.1-flash-free",
    ]
    assert default_model_for_runtime(_settings(path), RUNTIME_OPENCODE) == "opencode/big-pickle"


def test_provider_secrets_never_enter_the_catalog(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {"provider": {"opencode": {"options": {"apiKey": SECRET}, "models": {"m1": {}}}}},
    )
    models = models_for_runtime(_settings(path), RUNTIME_OPENCODE)
    assert SECRET not in json.dumps(models)


def test_missing_config_keeps_opencode_catalog_empty(tmp_path: Path) -> None:
    settings = Settings(opencode_config_file=str(tmp_path / "missing.json"))
    assert models_for_runtime(settings, RUNTIME_OPENCODE) == []
    assert default_model_for_runtime(settings, RUNTIME_OPENCODE) == ""


def test_malformed_config_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert models_for_runtime(_settings(path), RUNTIME_OPENCODE) == []
