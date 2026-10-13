from __future__ import annotations

import json
import ssl
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, unquote

import httpx

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
    finish_reason: str | None = None

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


def validate_url(value: str, *, proxy: bool = False) -> str:
    """Errors name the field only, never reflect credential-bearing input."""
    label = "proxy URL" if proxy else "base URL"
    try:
        parsed = urlsplit(value)
        schemes = {"http", "https", "socks5", "socks5h"} if proxy else {"http", "https"}
        if parsed.scheme not in schemes or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError()
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError()
        if proxy and parsed.path not in {"", "/"}:
            raise ValueError()
        if not proxy and (parsed.username is not None or parsed.password is not None):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {label}: check scheme, host and port") from None
    return value.rstrip("/")


def redact_error(value: object, settings: Settings) -> str:
    text = str(value)
    secrets = [settings.api_key, settings.openrouter_api_key, settings.model_proxy_url]
    if settings.model_proxy_url:
        try:
            p = urlsplit(settings.model_proxy_url)
            secrets.extend([p.username, p.password, unquote(p.username or ""), unquote(p.password or "")])
        except ValueError:
            pass
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return re.sub(r"((?:https?|socks5h?)://)[^\s/\"']+@", r"\1[redacted]@", text)


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
        self.settings = settings
        self.api_key = settings.api_key
        self.base_url = validate_url(settings.base_url)
        self.proxy_url = validate_url(settings.model_proxy_url, proxy=True) if settings.model_proxy_url else ""
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.ssl_context = ssl.create_default_context()
        if settings.model_ca_file:
            self.ssl_context.load_verify_locations(cafile=settings.model_ca_file)
            if settings.model_ca_allow_legacy:
                # Legacy private CAs can omit a critical Basic Constraints bit.
                # Keep trust-chain and hostname verification, relaxing only the
                # extra OpenSSL strict-profile checks for an explicitly supplied CA.
                self.ssl_context.verify_flags &= ~ssl.VERIFY_X509_STRICT

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
                if self.proxy_url:
                    # Explicit proxy routes both /models and completions; SOCKS
                    # authentication is performed by httpcore/socksio.
                    with httpx.Client(proxy=self.proxy_url, verify=self.ssl_context,
                                      timeout=self.timeout, trust_env=False) as transport:
                        response = transport.request(method, self.base_url + path,
                                                     content=body, headers=dict(request.header_items()))
                        if response.status_code >= 400:
                            last_error = GatewayError(redact_error(f"HTTP {response.status_code}: {response.text[:1000]}", self.settings))
                            if response.status_code < 500 and response.status_code != 429:
                                break
                        else:
                            return response.json()
                    if attempt < self.max_attempts - 1:
                        time.sleep(min(1.0 * (2**attempt), 8.0))
                    continue
                with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                    return json.load(response)
            except HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", "replace")
                last_error = GatewayError(redact_error(f"HTTP {exc.code}: {detail}", self.settings))
                if exc.code < 500 and exc.code != 429:
                    break
            except URLError as exc:
                last_error = GatewayError(redact_error(f"gateway connection failed: {exc.reason}", self.settings))
            except (httpx.HTTPError, OSError) as exc:
                last_error = GatewayError(redact_error(f"gateway connection failed: {exc}", self.settings))
            except (ValueError, json.JSONDecodeError):
                last_error = GatewayError("gateway returned invalid JSON")
                break
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
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise TypeError("Expected text content")
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
                content=content,
                tool_calls=calls,
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                finish_reason=response["choices"][0].get("finish_reason"),
            )
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            # Malformed usage/tool fields can themselves echo credentials.
            # No raw provider payload or parsing exception reaches persisted logs.
            raise GatewayError("invalid Chat Completions response") from None
