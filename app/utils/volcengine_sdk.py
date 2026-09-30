from __future__ import annotations

import importlib
from typing import Any

import volcenginesdkcore

_SERVICE_MODULE_BY_NAME = {
    "dns": "volcenginesdkdns",
    "cdn": "volcenginesdkcdn",
    "clb": "volcenginesdkclb",
    "certificate_service": "volcenginesdkcertificateservice",
}

_DEFAULT_CLASS_NAME_BY_MODULE = {
    "volcenginesdkdns": "DNSApi",
    "volcenginesdkcdn": "CDNApi",
    "volcenginesdkclb": "CLBApi",
    "volcenginesdkcertificateservice": "CERTIFICATESERVICEApi",
}


def _resolve_service_class(module_path: str, service_class_name: str | None = None) -> type[Any]:
    module = importlib.import_module(module_path)
    class_name = service_class_name or _DEFAULT_CLASS_NAME_BY_MODULE.get(module_path)
    if not class_name:
        raise ValueError(f"No default volcengine API class configured for module: {module_path}")
    resolved = getattr(module, class_name, None)
    if resolved is None:
        raise AttributeError(f"Unable to resolve class {class_name} in {module_path}")
    return resolved


def _build_sdk_configuration(
    access_key_id: str,
    access_key_secret: str,
    region: str,
) -> volcenginesdkcore.Configuration:
    configuration = volcenginesdkcore.Configuration()
    configuration.ak = access_key_id
    configuration.sk = access_key_secret
    configuration.region = region
    return configuration


def _to_compat_dict(value: Any) -> Any:
    if isinstance(value, list):
        return [_to_compat_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_compat_dict(item) for key, item in value.items()}

    to_dict = getattr(value, "to_dict", None)
    attribute_map = getattr(value, "attribute_map", None)
    if callable(to_dict):
        raw = to_dict()
        if isinstance(raw, dict):
            converted = {}
            for key, item in raw.items():
                normalized = _to_compat_dict(item)
                converted[key] = normalized
                if isinstance(attribute_map, dict):
                    api_key = attribute_map.get(key)
                    if api_key and api_key != key:
                        converted[api_key] = normalized
            return converted
        return raw
    return value


class _VolcengineServiceAdapter:
    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, item: str) -> Any:
        target = getattr(self._client, item)
        if not callable(target):
            return target

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            result = target(*args, **kwargs)
            return _to_compat_dict(result)

        return _wrapped


def build_credentials(
    access_key_id: str,
    access_key_secret: str,
    service: str,
    region: str,
):
    # service is preserved for compatibility with existing call sites.
    return _build_sdk_configuration(access_key_id, access_key_secret, region)


def build_volcengine_service(
    *,
    access_key_id: str,
    access_key_secret: str,
    service: str,
    region: str,
    service_cls: Any | None = None,
    module_path: str | None = None,
    service_class_name: str | None = None,
) -> Any:
    configuration = build_credentials(access_key_id, access_key_secret, service, region)

    if service_cls is None:
        resolved_module_path = module_path or _SERVICE_MODULE_BY_NAME.get(service)
        if not resolved_module_path:
            raise ValueError(f"Unsupported volcengine service: {service}")
        service_cls = _resolve_service_class(resolved_module_path, service_class_name)

    client = service_cls(api_client=volcenginesdkcore.ApiClient(configuration))
    return _VolcengineServiceAdapter(client)


DNSApi = _resolve_service_class("volcenginesdkdns")
CDNApi = _resolve_service_class("volcenginesdkcdn")
CLBApi = _resolve_service_class("volcenginesdkclb")
CERTIFICATESERVICEApi = _resolve_service_class("volcenginesdkcertificateservice")
