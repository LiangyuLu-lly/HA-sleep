"""Bug 1.10 探索测试 — AppArmor profile 缺失

这是一个 exploration test（探索性测试）。在修复前，此测试 **预期失败**，
因为 sleep_classifier/apparmor.txt 文件不存在，且 config.yaml 中未声明
apparmor == true。

测试失败即证明 Bug 1.10 存在：Add-on 缺少 AppArmor 安全配置文件。
"""

from pathlib import Path

import yaml


def test_apparmor_txt_exists_and_well_formed():
    """apparmor.txt 应存在且包含必要的安全策略声明。"""
    addon_dir = Path(__file__).resolve().parent.parent / "sleep_classifier"

    # 1. 文件必须存在
    apparmor_path = addon_dir / "apparmor.txt"
    assert apparmor_path.exists(), (
        f"AppArmor profile not found at {apparmor_path}"
    )

    # 2. 内容包含必要的策略声明
    content = apparmor_path.read_text(encoding="utf-8")

    assert "profile sleep_classifier" in content, (
        "apparmor.txt must contain 'profile sleep_classifier'"
    )
    assert "/app/** r," in content, (
        "apparmor.txt must contain '/app/** r,'"
    )
    assert "/data/** rwk," in content, (
        "apparmor.txt must contain '/data/** rwk,'"
    )
    assert "/share/** rwk," in content, (
        "apparmor.txt must contain '/share/** rwk,'"
    )
    assert "/dev/tty" in content, (
        "apparmor.txt must contain '/dev/tty'"
    )
    assert "#include <tunables/global>" in content, (
        "apparmor.txt must contain '#include <tunables/global>'"
    )

    # 3. config.yaml 应声明 apparmor == true
    config_path = addon_dir / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config_data.get("apparmor") is True, (
        f"config.yaml 'apparmor' should be true, got {config_data.get('apparmor')!r}"
    )
