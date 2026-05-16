"""Exploration test for Bug 1.3 — SIGTERM 不转发导致丢数据.

run.sh 当前使用 ``exec python3 /app/web_ui.py`` 把 PID 1 交给 Web UI，
导致 bash 的 trap 永远不会被触发，SIGTERM 无法转发给后台 smart-service
supervisor，preference_learner 来不及 flush 就被 SIGKILL。

修复后的架构应为：
  * tini -g（进程组信号转发）
  * bash 保持前台，用 ``wait -n`` 等待子进程
  * ``trap _shutdown INT TERM`` 捕获信号并优雅关闭

本测试在修复前 **预期 FAIL**，证明 bug 真实存在。

Validates: Requirements 1.3
"""

from pathlib import Path


# Paths relative to the repository root
_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_SH = _REPO_ROOT / "sleep_classifier" / "run.sh"
_DOCKERFILE = _REPO_ROOT / "sleep_classifier" / "Dockerfile"


def test_run_sh_uses_wait_n_not_exec():
    """run.sh must NOT exec away and must use wait-n + trap pattern."""
    content = _RUN_SH.read_text(encoding="utf-8")

    # 1) run.sh 末尾不应有 exec python3（exec 会替换 bash 进程，trap 失效）
    assert "exec python3" not in content, (
        "run.sh still contains 'exec python3' — bash trap will never fire, "
        "SIGTERM cannot be forwarded to background children"
    )

    # 2) 应包含 wait -n 等待子进程退出
    assert 'wait -n "$PID_WEB"' in content, (
        "run.sh missing 'wait -n \"$PID_WEB\"' — bash must stay in foreground "
        "with wait to receive signals via trap"
    )

    # 3) 应包含 trap _shutdown INT TERM 捕获信号
    assert "trap _shutdown INT TERM" in content, (
        "run.sh missing 'trap _shutdown INT TERM' — no graceful shutdown handler"
    )


def test_dockerfile_tini_group_signal():
    """Dockerfile ENTRYPOINT must use tini -g for process-group signal forwarding."""
    content = _DOCKERFILE.read_text(encoding="utf-8")

    # tini -g 确保 SIGTERM 发送到整个进程组，而非仅 PID 1
    assert 'ENTRYPOINT ["/sbin/tini", "-g", "--"]' in content, (
        "Dockerfile ENTRYPOINT missing '-g' flag — tini won't forward signals "
        "to the entire process group, only to PID 1 child"
    )
