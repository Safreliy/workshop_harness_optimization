from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .settings import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ModelTurn:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class GatewayError(RuntimeError):
    pass


class ModelClient:
    """Minimal Chat Completions adapter without vendor-specific SDK headers."""

    def __init__(
        self,
        settings: Settings,
        timeout: float = 120.0,
        max_attempts: int = 5,
    ):
        missing = settings.validate_model()
        if missing:
            raise ValueError(f"missing model settings: {', '.join(missing)}")
        self.model_name = settings.model_name
        self.api_key = settings.api_key
        self.base_url = settings.base_url
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "postgres-meta-harness-lab/0.1",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", "replace")
                last_error = GatewayError(f"HTTP {exc.code}: {detail}")
                if exc.code < 500 and exc.code != 429:
                    break
            except URLError as exc:
                last_error = GatewayError(f"gateway connection failed: {exc.reason}")
            if attempt < self.max_attempts - 1:
                time.sleep(min(1.0 * (2**attempt), 8.0))
        raise last_error or GatewayError("unknown gateway error")

    def list_models(self) -> list[str]:
        payload = self._request("GET", "/models")
        return [
            item["id"]
            for item in payload.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> ModelTurn:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        response = self._request("POST", "/chat/completions", payload)
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayError(f"invalid Chat Completions response: {response!r}") from exc
        calls = [
            ToolCall(
                id=call["id"],
                name=call["function"]["name"],
                arguments=call["function"].get("arguments") or "{}",
            )
            for call in (message.get("tool_calls") or [])
        ]
        usage = response.get("usage") or {}
        return ModelTurn(
            content=message.get("content") or "",
            tool_calls=calls,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )
