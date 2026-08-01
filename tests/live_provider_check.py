"""通过一次真实 DeepSeek 请求验证 OpenAI-compatible Provider 连通性。

任务流位置：这是默认测试集之外的显式在线验证入口；它从环境读取密钥、创建
真实客户端并调用 Provider，但不进入 Tool Registry 或完整 Agent Loop。
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from openai import OpenAI

from harness.config import Settings
from harness.providers.openai_compatible import OpenAICompatibleProvider


def test_real_provider_returns_live_response(workspace: Path) -> None:
    """执行一次需显式启用的真实 Provider 冒烟测试。"""

    settings = Settings.from_env(
        workspace=workspace,
        model=os.getenv("CODEFORGE_MODEL") or "deepseek-v4-pro",
        base_url=(
            os.getenv("CODEFORGE_BASE_URL")
            or "https://api.deepseek.com"
        ),
    )
    http_client = httpx.Client(
        timeout=settings.request_timeout,
        trust_env=False,
    )
    client = OpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        http_client=http_client,
    )
    provider = OpenAICompatibleProvider(
        model=settings.model,
        api_key="",
        max_tokens=min(settings.max_tokens, 128),
        client=client,
    )

    print(f"[LIVE PROVIDER] model={settings.model}")
    print(f"[LIVE PROVIDER] base_url={settings.base_url or '(SDK default)'}")

    try:
        turn = provider.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "This is a provider connectivity test. No tools are "
                        "available. Follow the response instruction exactly."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Reply with exactly CODEFORGE_LIVE_OK and nothing "
                        "else."
                    ),
                },
            ],
            [],
        )
    finally:
        client.close()

    print(f"[LIVE RESPONSE] {turn.content!r}")
    print(f"[LIVE USAGE] {turn.usage}")

    assert turn.content.strip() == "CODEFORGE_LIVE_OK"
    assert not turn.tool_calls
