"""Session-only connection profiles shared by the two local workshop UIs."""
from dataclasses import replace
import threading
from pathlib import Path
import ssl
from uuid import uuid4

from .model import ModelClient, redact_error, validate_url
from .settings import Settings


class ConnectionProfiles:
    def __init__(self, settings: Settings):
        self._lock = threading.RLock()
        self._profiles = {"env": ("Из .env", "env", settings)}
        self.selected = "env"
        self.defaults = settings
        if settings.openrouter_api_key:
            # The configured auditor is a general LLM; its server-held credential
            # can also back a main-agent profile. Decision models are not chat agents.
            model = settings.auditor_model.strip()
            if model.lower().startswith(("typesafe/", "jev")):
                model = ""
            router = replace(settings, api_key=settings.openrouter_api_key,
                             base_url="https://openrouter.ai/api/v1", model_name=model,
                             model_ca_file="", model_ca_allow_legacy=False)
            self._profiles["openrouter-auditor"] = (
                "OpenRouter · ключ аудитора", "openrouter", router
            )

    def settings(self) -> Settings:
        with self._lock:
            return self._profiles[self.selected][2]

    def snapshot(self):
        with self._lock:
            return self.selected, self._profiles[self.selected][2]

    def public(self):
        with self._lock:
            return {"selected": self.selected, "profiles": [
                {"id": key, "name": name, "provider": provider,
                 "base_url": redact_error(s.base_url, s), "model": s.model_name,
                 "key_configured": bool(s.api_key), "proxy_configured": bool(s.model_proxy_url),
                 "proxy_enabled": bool(s.model_proxy_url), "model_ca_file": s.model_ca_file,
                 "model_ca_allow_legacy": s.model_ca_allow_legacy}
                for key, (name, provider, s) in self._profiles.items()],
                "persistence": "Профили хранятся до перезапуска сервера; постоянное подключение задайте в .env."}

    def update(self, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a connection object")
        action = body.get("action", "save")
        if not isinstance(action, str) or not isinstance(body.get("profile_id", ""), str):
            raise ValueError("Invalid connection operation or profile ID")
        with self._lock:
            if action == "select":
                key = body.get("profile_id")
                if key not in self._profiles:
                    raise ValueError("Unknown connection profile")
                self.selected = key
                return self.public()
            if action == "models":
                settings = self.settings()
            elif action == "save":
                key = body.get("profile_id") or uuid4().hex[:12]
                previous = self._profiles.get(key)
                provider = body.get("provider", "openai_compatible")
                if not isinstance(provider, str) or provider not in {"env", "openrouter", "openai_compatible"}:
                    raise ValueError("Unknown provider")
                for field in ("name", "base_url", "model", "api_key", "proxy_url", "model_ca_file"):
                    if field in body and (not isinstance(body[field], str) or len(body[field]) > 4096):
                        raise ValueError(f"Invalid {field} field")
                for field in ("proxy_enabled", "model_ca_allow_legacy"):
                    if field in body and not isinstance(body[field], bool):
                        raise ValueError(f"Invalid {field} field: expected boolean")
                name = body.get("name", "Моя модель").strip()
                if not name or len(name) > 100:
                    raise ValueError("Profile name must be 1–100 characters")
                base_url = validate_url(body.get("base_url", "").strip())
                model = body.get("model", "").strip()
                if len(model) > 200:
                    raise ValueError("Model name must be at most 200 characters")
                api_key = body.get("api_key", "").strip()
                if not api_key and previous:
                    if base_url != previous[2].base_url.rstrip("/"):
                        raise ValueError("Enter a new API key when changing the endpoint; saved keys are not forwarded automatically")
                    api_key = previous[2].api_key
                if not api_key and provider == "env" and base_url == self.defaults.base_url.rstrip("/"):
                    api_key = self.defaults.api_key
                if not api_key and provider == "openrouter" and base_url == "https://openrouter.ai/api/v1":
                    api_key = self.defaults.openrouter_api_key
                if not api_key:
                    raise ValueError("API key is required; use a placeholder for a local server without authentication")
                proxy = body.get("proxy_url", "").strip()
                if body.get("proxy_enabled", False):
                    proxy = proxy or (previous[2].model_proxy_url if previous else "")
                    if not proxy:
                        raise ValueError("Enter proxy URL or disable proxy")
                    proxy = validate_url(proxy, proxy=True)
                else:
                    proxy = ""
                same_endpoint = previous and base_url == previous[2].base_url.rstrip("/")
                ca = body.get("model_ca_file", previous[2].model_ca_file if same_endpoint else "").strip()
                legacy = body.get("model_ca_allow_legacy", previous[2].model_ca_allow_legacy if same_endpoint else False)
                if ca:
                    if not Path(ca).is_file():
                        raise ValueError("CA file does not exist on the server")
                    try:
                        ssl.create_default_context().load_verify_locations(cafile=ca)
                    except (OSError, ssl.SSLError):
                        raise ValueError("CA file must contain valid PEM certificates") from None
                if legacy and not ca:
                    raise ValueError("Legacy CA compatibility requires an explicit CA file")
                settings = replace(self.defaults, api_key=api_key, base_url=base_url,
                                   model_name=model, model_proxy_url=proxy,
                                   model_ca_file=ca, model_ca_allow_legacy=legacy)
                self._profiles[key] = (name, provider, settings)
                self.selected = key
                return self.public()
            else:
                raise ValueError("Unknown connection operation")
        # Network calls do not block profile editing; snapshot binds one request.
        # GET /models does not send a model; an incomplete profile can discover
        # IDs before choosing one. Completions still require an explicit model.
        models = ModelClient(replace(settings, model_name=settings.model_name or "__models_list__"), timeout=20, max_attempts=1).list_models()
        return {"models": [redact_error(m, settings) for m in models]}


def profiles_for(server, settings):
    """Handlers share the server's state, not a process-global credential store."""
    if not hasattr(server, "connection_profiles"):
        # Constructors are called by server request threads; a common lock avoids
        # discarding the first saved profile during concurrent initialization.
        with _INITIALIZE:
            if not hasattr(server, "connection_profiles"):
                server.connection_profiles = ConnectionProfiles(settings)
    return server.connection_profiles


_INITIALIZE = threading.Lock()


def same_origin(handler):
    origin = handler.headers.get("Origin")
    return not origin or origin in {f"http://127.0.0.1:{handler.server.server_port}",
                                    f"http://localhost:{handler.server.server_port}"}
