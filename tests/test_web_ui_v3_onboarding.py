"""Web UI v3.0.0 onboarding 用户画像单元测试 (Task 7.2).

围绕 ``sleep_classifier/web_ui.py`` 中第 3 步（用户画像）落盘逻辑：

* 合法画像 → 写入 ``v3_user_profile`` 子字段 (R8.2 / R8.3)
* 缺失 / 非法字段 → 折叠到 ``""``，下游视为 ``unspecified`` / ``neutral`` (R8.2)
* ``prior_weight_lock_zero=True`` → ``prior_weight_lock=0.0`` 持久化 (R8.5)
* v2.x 旧 ``web_ui_overrides.json`` 加载时无 ``v3_user_profile`` 字段
  且后续保存不破坏已有 v2.x 字段 (R8.7 / PR6)
* Skip 路径（POST 不含任何 v3 profile 字段）→ 不创建 ``v3_user_profile``

aiohttp ``TestClient`` + ``tmp_path`` monkeypatch 驱动 ``make_app()``,
与 ``tests/test_web_ui*.py`` 现有约定保持一致。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from aiohttp.test_utils import TestClient, TestServer

# Add-on 目录不在 src 树里，需要手动加 sys.path 才能 import web_ui
_ADDON_ROOT = Path(__file__).resolve().parents[1] / "sleep_classifier"
if str(_ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADDON_ROOT))

import web_ui  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    """把 /data 重定向到 ``tmp_path`` 避免污染真实文件系统。"""
    monkeypatch.setattr(web_ui, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(
        web_ui, "_OVERRIDES_PATH", tmp_path / "web_ui_overrides.json",
    )
    monkeypatch.setattr(web_ui, "_OPTIONS_PATH", tmp_path / "options.json")
    return tmp_path


@pytest.fixture
def app(monkeypatch):
    """构建一个不连真实 HA 的 web UI app 用于测试。"""
    async def _fake_fetch():
        return []
    monkeypatch.setattr(web_ui, "_fetch_states", _fake_fetch)
    # Ingress IP guard 由专门的 test_web_ui_ip_guard.py 覆盖；这里关掉以专注画像逻辑
    monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)
    return web_ui.make_app()


@pytest.fixture
async def client(app):
    """轻量 aiohttp TestClient，避免引入 pytest-aiohttp。"""
    async with TestClient(TestServer(app)) as c:
        yield c


def _read_overrides(tmp_path: Path) -> Dict[str, Any]:
    return json.loads(
        (tmp_path / "web_ui_overrides.json").read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# 1. 合法画像 → 写入 v3_user_profile 子字段
# ---------------------------------------------------------------------------


_ISO8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)$"
)


class TestLegalProfilePersistence:
    """R8.2 / R8.3: 合法画像保存到 v3_user_profile 子字段。"""

    async def test_legal_profile_persists_to_v3_user_profile_subfield(
        self, client, isolate_data_dir,
    ) -> None:
        body = {
            "age_band": "26-35",
            "sex": "F",
            "chronotype": "evening",
            "prior_weight_lock_zero": False,
        }
        resp = await client.post("/api/onboarding/save", json=body)
        assert resp.status == 200

        data = _read_overrides(isolate_data_dir)
        assert "v3_user_profile" in data, (
            "v3_user_profile 子字段未被写入 web_ui_overrides.json"
        )
        profile = data["v3_user_profile"]
        assert profile["age_band"] == "26-35"
        assert profile["sex"] == "F"
        assert profile["chronotype"] == "evening"
        # prior_weight_lock_zero=False ⇒ prior_weight_lock 保持 None（不锁定）
        assert profile["prior_weight_lock"] is None
        # set_at 必须是 ISO-8601 UTC（design §4.3 schema）
        assert "set_at" in profile, "v3_user_profile 缺少 set_at 时间戳"
        assert _ISO8601_UTC_RE.match(profile["set_at"]), (
            f"set_at 不是合法 ISO-8601 UTC: {profile['set_at']}"
        )


# ---------------------------------------------------------------------------
# 2. 缺失 / 非法字段 → 折叠到 "" （unspecified / neutral 兜底）
# ---------------------------------------------------------------------------


class TestMissingOrInvalidFields:
    """R8.2: 缺失 / 非法字段 → ``""``，下游视为 unspecified / neutral。"""

    async def test_missing_or_invalid_fields_fall_back_to_empty_string(
        self, client, isolate_data_dir,
    ) -> None:
        # age_band 给一个非法值；sex 留空；chronotype 完全缺失但
        # 因为 prior_weight_lock_zero 在 body 中，会触发 v3 profile 构建
        body = {
            "age_band": "not-a-real-bucket",
            "sex": "",
            "prior_weight_lock_zero": False,
            # chronotype 故意缺失
        }
        resp = await client.post("/api/onboarding/save", json=body)
        assert resp.status == 200

        data = _read_overrides(isolate_data_dir)
        profile = data["v3_user_profile"]
        # 三个枚举字段全部回落到 ""（_coerce_enum 行为）
        assert profile["age_band"] == ""
        assert profile["sex"] == ""
        assert profile["chronotype"] == ""
        assert profile["prior_weight_lock"] is None

    async def test_wrong_type_fields_fall_back_to_empty_string(
        self, client, isolate_data_dir,
    ) -> None:
        # 故意传入非 string 类型，_coerce_enum 应安全降级到 ""
        body = {
            "age_band": 26,            # int 而不是 string
            "sex": ["F"],              # list 而不是 string
            "chronotype": None,        # None 而不是 string
            "prior_weight_lock_zero": False,
        }
        resp = await client.post("/api/onboarding/save", json=body)
        assert resp.status == 200

        profile = _read_overrides(isolate_data_dir)["v3_user_profile"]
        assert profile["age_band"] == ""
        assert profile["sex"] == ""
        assert profile["chronotype"] == ""


# ---------------------------------------------------------------------------
# 3. prior_weight_lock=0 持久化
# ---------------------------------------------------------------------------


class TestPriorWeightLockZero:
    """R8.5: 用户在 Web UI 锁定 prior_weight 到 0 时，落盘为 0.0。"""

    async def test_prior_weight_lock_zero_persists_correctly(
        self, client, isolate_data_dir,
    ) -> None:
        body = {
            "age_band": "36-50",
            "sex": "M",
            "chronotype": "morning",
            "prior_weight_lock_zero": True,
        }
        resp = await client.post("/api/onboarding/save", json=body)
        assert resp.status == 200

        profile = _read_overrides(isolate_data_dir)["v3_user_profile"]
        assert profile["prior_weight_lock"] == 0.0
        # 显式校验是 float 0.0 而不是 int 0 / False / None
        assert isinstance(profile["prior_weight_lock"], float)


# ---------------------------------------------------------------------------
# 4. v2.x 老 web_ui_overrides.json 兼容性 (PR6 / R8.7)
# ---------------------------------------------------------------------------


class TestLegacyV2xCompatibility:
    """PR6: 老 v2.x overrides 文件加载时不破坏未引用字段。"""

    async def test_legacy_v2x_overrides_loaded_without_v3_field(
        self, client, isolate_data_dir,
    ) -> None:
        """v2.1.0 老文件不含 v3_user_profile，``_load_existing`` 不应注入。"""
        legacy = {
            "sleep_stage_source": "sensor.bedroom_sleep_stage",
            "temperature_source": "sensor.bedroom_temp",
            "light_targets": ["light.bedroom_main"],
            # v2.1.0 三个 feature flag
            "onboarding_skipped": True,
            "telemetry_enabled": False,
            "upgrade_notifications_enabled": True,
        }
        (isolate_data_dir / "web_ui_overrides.json").write_text(
            json.dumps(legacy), encoding="utf-8",
        )

        loaded = web_ui._load_existing()
        # PR6: 不引用的 / 未来字段不会被自动注入
        assert "v3_user_profile" not in loaded, (
            "_load_existing 不应给 v2.x 老文件凭空注入 v3_user_profile"
        )
        # 但 v2.x 既有字段应原样保留
        assert loaded["sleep_stage_source"] == "sensor.bedroom_sleep_stage"
        assert loaded["temperature_source"] == "sensor.bedroom_temp"
        assert loaded["light_targets"] == ["light.bedroom_main"]
        # v2.1.0 feature flag 通过 apply_v2_1_0_defaults 保留
        assert loaded["onboarding_skipped"] is True
        assert loaded["telemetry_enabled"] is False
        assert loaded["upgrade_notifications_enabled"] is True

    async def test_save_after_legacy_load_preserves_v2x_fields(
        self, client, isolate_data_dir,
    ) -> None:
        """加载 v2.x 老文件后再保存，应保留原有 v2.x 字段（不被剥离）。"""
        legacy = {
            "sleep_stage_source": "sensor.legacy_stage",
            "temperature_source": "sensor.legacy_temp",
            "light_targets": ["light.legacy_a", "light.legacy_b"],
            "telemetry_enabled": False,
            "upgrade_notifications_enabled": True,
        }
        (isolate_data_dir / "web_ui_overrides.json").write_text(
            json.dumps(legacy), encoding="utf-8",
        )

        # 提交一份只含 v3 画像的 onboarding，模拟「老用户首次升级填画像」
        resp = await client.post(
            "/api/onboarding/save",
            json={
                "age_band": "51-65",
                "sex": "unspecified",
                "chronotype": "neutral",
                "prior_weight_lock_zero": False,
            },
        )
        assert resp.status == 200

        data = _read_overrides(isolate_data_dir)
        # v2.x 既有字段被保留
        assert data["sleep_stage_source"] == "sensor.legacy_stage"
        assert data["temperature_source"] == "sensor.legacy_temp"
        assert data["light_targets"] == ["light.legacy_a", "light.legacy_b"]
        assert data["telemetry_enabled"] is False
        assert data["upgrade_notifications_enabled"] is True
        # v3 画像新增
        profile = data["v3_user_profile"]
        assert profile["age_band"] == "51-65"
        assert profile["sex"] == "unspecified"
        assert profile["chronotype"] == "neutral"


# ---------------------------------------------------------------------------
# 5. Skip 路径不创建 v3_user_profile
# ---------------------------------------------------------------------------


class TestSkipPath:
    """Skip / 完全空 body → 不应在落盘文件里凭空生成 v3_user_profile。"""

    async def test_skip_path_does_not_create_v3_user_profile_field(
        self, client, isolate_data_dir,
    ) -> None:
        # 模拟 skip 按钮（前端发送 {skip: true}），不携带任何 v3 profile 字段
        resp = await client.post("/api/onboarding/save", json={"skip": True})
        assert resp.status == 200

        data = _read_overrides(isolate_data_dir)
        assert "v3_user_profile" not in data, (
            "Skip 路径不应创建 v3_user_profile 子字段"
        )

    async def test_empty_body_does_not_create_v3_user_profile_field(
        self, client, isolate_data_dir,
    ) -> None:
        resp = await client.post("/api/onboarding/save", json={})
        assert resp.status == 200

        data = _read_overrides(isolate_data_dir)
        assert "v3_user_profile" not in data
