from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import StateType

from .const import DOMAIN

HUB_DIAGNOSTIC_FIELDS: list[tuple[str, str]] = [
    ("description", "Description"),
    ("firmware_revision", "Firmware revision"),
    ("model_name", "Model name"),
    ("object_identifier", "Object identifier"),
    ("object_name", "Object name"),
    ("system_status", "System status"),
    ("vendor_identifier", "Vendor identifier"),
    ("vendor_name", "Vendor name"),
    ("ip_address", "IP address"),
    ("ip_subnet_mask", "IP subnet mask"),
    ("mac_address_raw", "MAC address"),
]
HUB_DIAGNOSTIC_SCAN_INTERVAL = timedelta(seconds=60)
CLIENT_DISCOVERY_TIMEOUT_SECONDS = 3.0
# Fork v1.0.4 : 2.5 s -> 6.0 s. Sur une liaison MS/TP derrière un routeur BACnet
# (BASrouter), les lectures de propriétés individuelles dépassaient régulièrement
# 2,5 s : la propriété était alors stockée à None, et l'objet perdait son nom ou
# son unité. Valeur alignée sur CLIENT_POINT_REFRESH_TIMEOUT_SECONDS.
CLIENT_READ_TIMEOUT_SECONDS = 6.0
CLIENT_OBJECTLIST_SCAN_LIMIT = 16
# v1.0.6 : doit rester STRICTEMENT superieur au budget de transaction
# bacpypes3 (apduTimeout 2000 ms x (1 + numberOfApduRetries=1) = 4,0 s).
# A 0,6 s, wait_for abandonnait la lecture pendant que la SSM bacpypes3
# poursuivait : la reponse tardive retombait sur un future deja resolu.
CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS = 4.5
CLIENT_POINT_REFRESH_TIMEOUT_SECONDS = 6.0
CLIENT_POINT_SCAN_LIMIT = 128
CLIENT_REDISCOVERY_INTERVAL = timedelta(minutes=15)
CLIENT_REFRESH_MIN_SECONDS = 55.0
CLIENT_COV_LEASE_SECONDS = 600
# Fraction du lease à laquelle on RENOUVELLE la souscription COV (make-before-break).
# 0.8 = on ré-enregistre à 80% du lease, avant expiration, pour éviter le trou
# d'indisponibilité (la nouvelle souscription est posée pendant que l'ancienne est
# encore valide). Corrige le "yoyo" disponible/indisponible des entités.
CLIENT_COV_RENEW_FRACTION = 0.8

CLIENT_DIAGNOSTIC_FIELDS: list[tuple[str, str]] = list(HUB_DIAGNOSTIC_FIELDS)
NETWORK_DIAGNOSTIC_KEYS = {"ip_address", "ip_subnet_mask", "mac_address_raw"}
CLIENT_POINT_SUPPORTED_TYPES: dict[str, tuple[str, str]] = {
    "analoginput": ("ai", "analog-input"),
    "analogoutput": ("ao", "analog-output"),
    "analogvalue": ("av", "analog-value"),
    "binaryinput": ("bi", "binary-input"),
    "binaryoutput": ("bo", "binary-output"),
    "binaryvalue": ("bv", "binary-value"),
    "multistatevalue": ("mv", "multi-state-value"),
    "characterstringvalue": ("csv", "characterstring-value"),
}


def _to_state(value: Any) -> StateType:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _object_identifier_text(value: Any) -> str | None:
    if isinstance(value, tuple) and len(value) == 2:
        obj_type, inst = value
        type_txt = str(obj_type or "").strip().replace("-", "_").upper()
        if type_txt and _to_int(inst) is not None:
            return f"OBJECT_{type_txt}:{int(inst)}"
    text = str(value or "").strip()
    return text or None


def _object_identifier_instance_text(value: Any, fallback: int | None = None) -> str | None:
    if isinstance(value, tuple) and len(value) == 2:
        inst = _to_int(value[1])
        if inst is not None:
            return str(inst)
    text = str(value or "").strip()
    if text:
        m = re.search(r":\s*(\d+)\s*$", text)
        if m:
            return m.group(1)
        if text.isdigit():
            return text
    if fallback is not None:
        return str(int(fallback))
    return None


def _normalize_system_status(value: Any) -> tuple[int | None, str | None]:
    labels = {
        0: "operational",
        1: "operational_read_only",
        2: "download_required",
        3: "download_in_progress",
        4: "non_operational",
        5: "backup_in_progress",
    }
    if value is None:
        return None, None

    for raw in (getattr(value, "value", None), value):
        try:
            code = int(raw)  # type: ignore[arg-type]
            if code in labels:
                return code, labels[code]
        except Exception:
            pass

    text = str(value).strip()
    if not text:
        return None, None
    norm = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

    m = re.search(r"(?<!\d)(\d+)(?!\d)", text)
    if m:
        code = _to_int(m.group(1))
        if code is not None and code in labels:
            return code, labels[code]

    aliases = {
        "operational": 0,
        "operational_read_only": 1,
        "operational_readonly": 1,
        "download_required": 2,
        "download_in_progress": 3,
        "non_operational": 4,
        "backup_in_progress": 5,
    }
    for token, code in aliases.items():
        if token in norm:
            return code, labels[code]

    return None, None


def _mac_hex(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    text = str(value).strip()
    if not text:
        return None
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(hex_only) >= 12 and len(hex_only) % 2 == 0:
        return hex_only.upper()
    return None


def _mac_colon(value: Any) -> str | None:
    hex_text = _mac_hex(value)
    if not hex_text:
        return None
    return ":".join(hex_text[idx:idx + 2] for idx in range(0, len(hex_text), 2))


def _bacnet_mac_from_ip_port(ip_address: Any, udp_port: Any) -> str | None:
    ip = str(ip_address or "").strip()
    port = _to_int(udp_port)
    parts = ip.split(".")
    if len(parts) != 4 or port is None or port < 0 or port > 65535:
        return None
    try:
        octets = [int(part) for part in parts]
    except Exception:
        return None
    if any(octet < 0 or octet > 255 for octet in octets):
        return None
    return "".join(f"{octet:02X}" for octet in octets) + f"{port:04X}"


def _to_ipv4_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) == 4:
            return ".".join(str(octet) for octet in raw)
        return None
    try:
        raw = bytes(value)
        if len(raw) == 4:
            return ".".join(str(octet) for octet in raw)
    except Exception:
        pass
    text = str(value).strip()
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", text):
        return text
    return None


def _client_id(instance: int) -> str:
    return f"client_{int(instance)}"


def _client_diag_signal(entry_id: str, client_id: str) -> str:
    return f"{DOMAIN}_client_diag_{entry_id}_{client_id}"


def _hub_diag_signal(entry_id: str) -> str:
    return f"{DOMAIN}_hub_diag_{entry_id}"


def _client_points_signal(entry_id: str, client_id: str) -> str:
    return f"{DOMAIN}_client_points_{entry_id}_{client_id}"


def _entry_points_signal(entry_id: str) -> str:
    return f"{DOMAIN}_entry_points_{entry_id}"


def _client_cov_signal(entry_id: str, client_id: str) -> str:
    return f"{DOMAIN}_client_cov_{entry_id}_{client_id}"


def _client_rescan_signal(entry_id: str) -> str:
    return f"{DOMAIN}_client_rescan_{entry_id}"


def _diag_field_slug(key: str) -> str:
    text = str(key or "").strip().lower()
    if text == "mac_address_raw":
        return "mac_address"
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_") or "value"


def _doi_entity_id(instance: int | str | None, field_key: str, *, network: bool) -> str:
    inst = _to_int(instance)
    if inst is None:
        inst = 0
    prefix = "net_" if network else ""
    return f"sensor.bacnet_doi_{int(inst)}_{prefix}{_diag_field_slug(field_key)}"


def _point_entity_id(
    client_instance: int,
    type_slug: str,
    object_instance: int,
    *,
    entity_domain: str = "sensor",
) -> str:
    return (
        f"{str(entity_domain or 'sensor').strip().lower()}."
        f"bacnet_doi_{int(client_instance)}_{type_slug}_{int(object_instance)}"
    )


def _point_unique_id(entry_id: str, client_id: str, type_slug: str, object_instance: int) -> str:
    return f"{entry_id}-{client_id}-point-{type_slug}-{int(object_instance)}"


def _cov_process_identifier(entry_id: str, client_id: str, point_key: str) -> int:
    seed = f"{entry_id}:{client_id}:{point_key}"
    return (abs(hash(seed)) % 4194303) + 1


def _client_cache_root(hass: HomeAssistant) -> dict[str, dict[str, dict[str, Any]]]:
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault("client_diag_cache", {})


def _client_cache_get(hass: HomeAssistant, entry_id: str, client_id: str) -> dict[str, Any]:
    per_entry = _client_cache_root(hass).setdefault(entry_id, {})
    return per_entry.setdefault(client_id, {})


def _client_cache_set(hass: HomeAssistant, entry_id: str, client_id: str, payload: dict[str, Any]) -> None:
    cache = _client_cache_get(hass, entry_id, client_id)
    cache.update(payload or {})


def _client_points_root(hass: HomeAssistant) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault("client_point_cache", {})


def _client_points_get(hass: HomeAssistant, entry_id: str, client_id: str) -> dict[str, dict[str, Any]]:
    per_entry = _client_points_root(hass).setdefault(entry_id, {})
    return per_entry.setdefault(client_id, {})


def _entry_client_points(hass: HomeAssistant, entry_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    return _client_points_root(hass).setdefault(entry_id, {})


def _client_points_set(
    hass: HomeAssistant,
    entry_id: str,
    client_id: str,
    payload: dict[str, dict[str, Any]],
) -> None:
    cache = _client_points_get(hass, entry_id, client_id)
    cache.update(payload or {})


def _client_locks_root(hass: HomeAssistant) -> dict[str, dict[str, asyncio.Lock]]:
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault("client_diag_locks", {})


def _client_lock_get(hass: HomeAssistant, entry_id: str, client_id: str) -> asyncio.Lock:
    per_entry = _client_locks_root(hass).setdefault(entry_id, {})
    lock = per_entry.get(client_id)
    if lock is None:
        lock = asyncio.Lock()
        per_entry[client_id] = lock
    return lock


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _object_identifier_instance(value: Any, object_type: str) -> int | None:
    if isinstance(value, tuple) and len(value) == 2:
        obj_type, inst = value
        if str(obj_type).replace("-", "").lower() == object_type.replace("-", "").lower():
            return _to_int(inst)
    text = str(value or "").strip()
    m = re.search(rf"{object_type}[:\s,]+(\d+)$", text, flags=re.IGNORECASE)
    if m:
        return _to_int(m.group(1))
    return None


def _normalize_object_type_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _supported_point_type(value: Any) -> tuple[str, str] | None:
    if isinstance(value, tuple) and len(value) == 2:
        return _supported_point_type(value[0])
    raw = str(value or "").strip()
    key = _normalize_object_type_key(raw)
    if key in CLIENT_POINT_SUPPORTED_TYPES:
        return CLIENT_POINT_SUPPORTED_TYPES[key]

    for prefix in ("objecttype", "object", "enum", "bacnetobjecttype"):
        if key.startswith(prefix):
            trimmed = key[len(prefix):]
            if trimmed in CLIENT_POINT_SUPPORTED_TYPES:
                return CLIENT_POINT_SUPPORTED_TYPES[trimmed]

    low = raw.lower()
    if "analog" in low and "input" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("analoginput")
    if "analog" in low and "output" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("analogoutput")
    if "analog" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("analogvalue")
    if "binary" in low and "input" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("binaryinput")
    if "binary" in low and "output" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("binaryoutput")
    if "binary" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("binaryvalue")
    if "multistate" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("multistatevalue")
    if "characterstring" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("characterstringvalue")
    return None


def _object_instance(value: Any) -> int | None:
    if isinstance(value, tuple) and len(value) == 2:
        return _to_int(value[1])
    text = str(value or "").strip()
    m = re.search(r"(\d+)\s*$", text)
    if m:
        return _to_int(m.group(1))
    return None


def _parse_object_list_item(value: Any) -> tuple[str, int] | None:
    if isinstance(value, tuple) and len(value) == 2:
        inst = _to_int(value[1])
        if inst is not None:
            return str(value[0]), int(inst)

    object_type = (
        getattr(value, "objectType", None)
        or getattr(value, "object_type", None)
        or getattr(value, "type", None)
    )
    object_instance = (
        getattr(value, "instance", None)
        or getattr(value, "objectInstance", None)
        or getattr(value, "object_instance", None)
        or getattr(value, "instanceNumber", None)
    )
    inst = _to_int(object_instance)
    if object_type is not None and inst is not None:
        return str(object_type), int(inst)

    text = str(value or "").strip()
    if not text:
        return None

    m = re.search(r"([A-Za-z0-9_\- ]+)\s*[:,]\s*(\d+)\s*$", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"([A-Za-z0-9_\- ]+)\s+(\d+)\s*$", text)
    if m:
        return m.group(1), int(m.group(2))
    return None


def _object_identifier_compact(value: Any, fallback_type: str, fallback_instance: int) -> str:
    if isinstance(value, tuple) and len(value) == 2:
        return f"{value[0]},{int(value[1])}"
    text = str(value or "").strip()
    return text or f"{fallback_type},{int(fallback_instance)}"


def _normalize_bacnet_unit(value: Any) -> str | None:
    raw_text = str(value or "").strip()
    if not raw_text:
        return None

    norm = re.sub(r"[^a-z0-9]+", "", raw_text.lower())
    mapping = {
        "degreescelsius": "\u00b0C",
        "degreecelsius": "\u00b0C",
        "degreesfahrenheit": "\u00b0F",
        "degreefahrenheit": "\u00b0F",
        "percent": "%",
        "partspermillion": "ppm",
        "pascals": "Pa",
        "kilopascals": "kPa",
        "watts": "W",
        "kilowatts": "kW",
        "wattshour": "Wh",
        "watthours": "Wh",
        "kilowatthours": "kWh",
        "volts": "V",
        "amperes": "A",
        "hertz": "Hz",
    }
    if norm in mapping:
        return mapping[norm]
    if norm in {"c", "degc"}:
        return "\u00b0C"
    if norm in {"f", "degf"}:
        return "\u00b0F"
    return raw_text


def _sensor_device_class_from_unit(unit: str | None) -> SensorDeviceClass | None:
    normalized = _normalize_bacnet_unit(unit)
    u = str(normalized or "").strip().lower()
    if u in {"\u00b0c", "\u00b0f"}:
        return SensorDeviceClass.TEMPERATURE
    if u in {"w", "kw"}:
        return SensorDeviceClass.POWER
    if u in {"wh", "kwh"}:
        return SensorDeviceClass.ENERGY
    if u == "v":
        return SensorDeviceClass.VOLTAGE
    if u == "a":
        return SensorDeviceClass.CURRENT
    if u == "hz":
        return SensorDeviceClass.FREQUENCY
    return None


def _point_native_value_from_payload(point: dict[str, Any]) -> StateType:
    value = point.get("present_value")
    if value is None:
        return None

    type_slug = str(point.get("type_slug") or "")
    if type_slug in {"ai", "ao", "av"}:
        try:
            rounded = round(float(value), 3)
            if rounded == -0.0:
                rounded = 0.0
            return rounded
        except Exception:
            pass
    if type_slug in {"bi", "bo", "bv"}:
        text = str(value).strip().lower()
        if text in ("active", "on", "true", "1"):
            active_text = _safe_text(point.get("active_text"))
            return active_text or "active"
        if text in ("inactive", "off", "false", "0"):
            inactive_text = _safe_text(point.get("inactive_text"))
            return inactive_text or "inactive"
        return str(value)

    if type_slug == "mv":
        idx = _to_int(value)
        texts = point.get("state_text")
        if idx is not None and isinstance(texts, (list, tuple)):
            pos = idx - 1
            if 0 <= pos < len(texts):
                label = _safe_text(texts[pos])
                if label:
                    return label

    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def _property_slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.split(".")[-1]
    return re.sub(r"[^a-z0-9]+", "", text)


def _point_has_priority_array(point: dict[str, Any]) -> bool:
    raw = point.get("has_priority_array")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, (list, tuple, set, dict)):
        return len(raw) > 0
    text = str(raw).strip().lower()
    return bool(text and text not in {"none", "null", "false", "0", "[]", "()"})


def _point_is_writable(point: dict[str, Any]) -> bool:
    """Fork v1.0.5 : décision d'écriture fondée sur le TYPE d'objet uniquement.

    NIVEAU 1 — ne doit jamais casser. Auparavant, `ao` et `bo` dépendaient de
    `_point_has_priority_array()`, donc du succès d'une lecture réseau de la
    propriété `priorityArray`. Une lecture qui dépassait le délai suffisait à
    déclarer le point non inscriptible : l'entité `number` redevenait un simple
    `sensor`, le bouton Relâcher disparaissait, et les écritures partaient sans
    priorité — silencieusement écrasées par l'automate.

    Le type d'objet provient de l'identifiant de l'objet lui-même : il ne peut
    pas se dégrader. Si un `ao`/`bo` n'a réellement pas de Priority Array,
    l'automate renverra une erreur d'écriture — visible, donc préférable au
    désarmement silencieux.
    """
    type_slug = str(point.get("type_slug") or "").strip().lower()
    return type_slug in {"ao", "bo", "av", "bv", "mv", "csv"}


def _protect_point_metadata(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fork v1.0.5 : empêche un rescan partiel de dégrader un point déjà connu.

    NIVEAU 2 — ne doit pas se dégrader. `_merge_non_none()` ne protège que les
    valeurs remplacées par `None`. Or une lecture ratée produit ici des valeurs
    NON nulles qui écrasent quand même le bon contenu :

    - `object_name` retombe sur le repli généré `"<type> <instance>"` ;
    - `has_priority_array` retombe sur `False`.

    Cette fonction ne fait que CONSERVER l'ancienne valeur : elle ne peut ni
    écrire sur le réseau, ni inventer une valeur, ni bloquer quoi que ce soit.
    """
    merged = dict(current or {})
    prev = dict(previous or {})
    if not prev:
        return merged

    # 1. Nom : ne jamais remplacer un nom réel par le repli généré.
    object_type = str(merged.get("object_type") or "").strip()
    object_instance = _to_int(merged.get("object_instance"))
    if object_type and object_instance is not None:
        fallback_name = f"{object_type} {int(object_instance)}"
        previous_name = _safe_text(prev.get("object_name"))
        current_name = _safe_text(merged.get("object_name"))
        if (
            previous_name
            and previous_name != fallback_name
            and current_name == fallback_name
        ):
            merged["object_name"] = prev.get("object_name")

    # 2. has_priority_array : jamais de rétrogradation True -> False.
    if _point_has_priority_array(prev) and not _point_has_priority_array(merged):
        merged["has_priority_array"] = prev.get("has_priority_array")

    # 3. Recalcul de l'inscriptibilité après protection (dépend du seul type).
    merged["writable_from_ha"] = _point_is_writable(merged)
    return merged


def _point_platform(point: dict[str, Any]) -> str:
    type_slug = str(point.get("type_slug") or "").strip().lower()
    if type_slug == "csv":
        return "text"
    if type_slug in {"ai"}:
        return "sensor"
    if type_slug == "bi":
        return "binary_sensor"
    if type_slug == "mv":
        return "select" if _point_is_writable(point) else "sensor"
    if type_slug in {"av", "ao"}:
        return "number" if _point_is_writable(point) else "sensor"
    if type_slug in {"bv", "bo"}:
        return "switch" if _point_is_writable(point) else "binary_sensor"
    return "sensor"


import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict

from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_INSTANCE, DOMAIN, client_display_name
from .helpers.bacnet import device_instance_from_identifier as _device_instance_from_identifier
_LOGGER = logging.getLogger(__name__)


def _merge_non_none(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous or {})
    for key, value in dict(current or {}).items():
        if value is not None:
            merged[key] = value
        elif key not in merged:
            merged[key] = None
    return merged


async def _read_remote_property(
    app: Any,
    address: str,
    objid: str,
    prop: str,
    array_index: int | None = None,
    timeout: float = CLIENT_READ_TIMEOUT_SECONDS,
) -> Any:
    return await asyncio.wait_for(
        app.read_property(address, objid, prop, array_index=array_index),
        timeout=timeout,
    )


async def _read_remote_properties(
    app: Any,
    address: str,
    objid: str,
    properties: list[str],
    timeout: float = CLIENT_POINT_REFRESH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    unique_props: list[str] = []
    seen: set[str] = set()
    for prop in properties:
        key = str(prop).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_props.append(key)

    rpm = getattr(app, "read_property_multiple", None)
    if callable(rpm):
        for args in (
            (address, objid, unique_props),
            (address, [(objid, unique_props)]),
        ):
            try:
                raw = await asyncio.wait_for(rpm(*args), timeout=timeout)
                if isinstance(raw, dict):
                    for prop in unique_props:
                        if prop in raw:
                            result[prop] = raw.get(prop)
                    if all(prop in result for prop in unique_props):
                        return result
            except asyncio.CancelledError:
                raise
            except BaseException:
                continue

    for prop in unique_props:
        if prop in result:
            continue
        try:
            result[prop] = await _read_remote_property(
                app,
                address,
                objid,
                prop,
                timeout=CLIENT_READ_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            result[prop] = None
    return result


async def _read_remote_property_any_objid(
    app: Any,
    address: str,
    objids: list[str],
    prop: str,
) -> Any:
    last_err: BaseException | None = None
    for objid in objids:
        try:
            return await _read_remote_property(app, address, objid, prop)
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            last_err = err
            continue
    if last_err:
        raise last_err
    return None


async def _write_client_point_present_value(
    app: Any,
    address: str,
    object_type: str,
    object_instance: int,
    value: Any,
    *,
    priority: int | None = None,
    timeout: float = CLIENT_READ_TIMEOUT_SECONDS,
) -> Any:
    objid = f"{str(object_type)}{','}{int(object_instance)}"
    write_fn = getattr(app, "write_property", None)
    if not callable(write_fn):
        raise RuntimeError("write_property is not available on BACnet app")

    attempts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    kwargs_with_priority = {"priority": int(priority)} if priority is not None else {}
    attempts.append(((address, objid, "presentValue", value), kwargs_with_priority))
    if priority is not None:
        attempts.append(((address, objid, "presentValue", value, int(priority)), {}))
        attempts.append(((address, objid, "presentValue", value, None, int(priority)), {}))

    last_err: BaseException | None = None
    for args, kwargs in attempts:
        try:
            return await asyncio.wait_for(write_fn(*args, **kwargs), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            last_err = err
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("Unable to write presentValue")


# ---------------------------------------------------------------------------
# Fork v1.0.5 — suivi des points commandés par HA et relâchement garanti
# ---------------------------------------------------------------------------
# Essais menés le 29/07/2026 sur un Distech ECB-203 :
#   - coupure de l'alimentation 24 VAC : le Priority Array est CONSERVÉ ;
#   - 30 min de perte de communication : le Priority Array est CONSERVÉ ;
#   - aucun chien de garde dans le programme embarqué.
# L'automate ne rend donc JAMAIS la main de lui-même : une valeur écrite par
# Home Assistant reste inscrite indéfiniment si Home Assistant disparaît.
#
# On mémorise ici chaque point réellement commandé par HA, afin de ne relâcher
# QUE ceux-là à l'arrêt de Home Assistant ou au déchargement de l'intégration.
# Relâcher aveuglément tous les objets serait long et hasardeux.
#
# ATTENTION : le relâchement rend la main au niveau de priorité inférieur. Si
# aucun n'est occupé, la sortie prend la valeur de `Relinquish Default`, qui
# vaut 0.00 sur l'AO:7 de l'ECB-203. Le retour à une valeur utile repose sur le
# fait que le programme embarqué occupe la priorité 14.
KEY_CLIENT_WRITTEN_POINTS = "client_written_points"

# Délai par écriture de relâchement. Porté de 3,0 à 4,5 s en v1.0.6 : à 3,0 s
# le wait_for coupait au milieu d'une transaction bacpypes3 (budget 4,0 s), ce
# qui laissait le point occupé sur l'automate. Reste plus court que
# CLIENT_READ_TIMEOUT_SECONDS.
CLIENT_RELEASE_WRITE_TIMEOUT_SECONDS = 4.5

# Budget global. Porté de 15 à 25 s en v1.0.6 pour couvrir 5 points x 4,5 s.
# Ne doit jamais bloquer l'arrêt de Home Assistant.
CLIENT_RELEASE_TOTAL_BUDGET_SECONDS = 25.0


def _client_written_points(hass: HomeAssistant, entry_id: str) -> Dict[str, Dict[str, Any]]:
    """Registre { "<client_id>::<point_key>": {adresse, objet, priorité} }."""
    store = hass.data.setdefault(DOMAIN, {}).setdefault(KEY_CLIENT_WRITTEN_POINTS, {})
    return store.setdefault(str(entry_id), {})


def _record_client_written_point(
    hass: HomeAssistant,
    entry_id: str,
    *,
    client_id: str,
    point_key: str,
    address: Any,
    object_type: Any,
    object_instance: Any,
    priority: Any,
) -> None:
    """Mémorise un point que HA vient d'occuper à un niveau de priorité.

    Ne lève jamais : un défaut de mémorisation ne doit pas faire échouer une
    écriture qui a abouti.
    """
    if priority is None:
        return
    try:
        _client_written_points(hass, entry_id)[f"{client_id}::{point_key}"] = {
            "address": str(address),
            "object_type": str(object_type),
            "object_instance": int(object_instance),
            "priority": int(priority),
        }
    except BaseException:
        _LOGGER.debug("Mémorisation du point commandé impossible", exc_info=True)


def _forget_client_written_point(
    hass: HomeAssistant,
    entry_id: str,
    *,
    client_id: str,
    point_key: str,
) -> None:
    """Retire un point du registre après un relâchement réussi. Ne lève jamais."""
    try:
        _client_written_points(hass, entry_id).pop(f"{client_id}::{point_key}", None)
    except BaseException:
        _LOGGER.debug("Oubli du point commandé impossible", exc_info=True)


async def _release_all_client_written_points(
    hass: HomeAssistant,
    entry_id: str,
    *,
    app: Any,
    reason: str,
) -> int:
    """Écrit Null sur tous les points occupés par HA. NE LÈVE JAMAIS.

    Écritures séquentielles : sur une liaison MS/TP, des écritures simultanées
    se gênent. Le budget global garantit que l'arrêt de Home Assistant n'est
    jamais retardé au-delà de CLIENT_RELEASE_TOTAL_BUDGET_SECONDS.
    """
    try:
        pending = dict(_client_written_points(hass, entry_id))
    except BaseException:
        _LOGGER.debug("Registre des points commandés illisible", exc_info=True)
        return 0

    if not pending:
        return 0

    if app is None:
        _LOGGER.warning(
            "Relâchement impossible (%s) : pile BACnet indisponible. "
            "%d point(s) restent occupés sur l'automate.",
            reason,
            len(pending),
        )
        return 0

    try:
        from bacpypes3.primitivedata import Null

        null_value: Any = Null(())
    except BaseException:
        _LOGGER.warning(
            "Relâchement impossible (%s) : Null BACnet indisponible. "
            "%d point(s) restent occupés sur l'automate.",
            reason,
            len(pending),
            exc_info=True,
        )
        return 0

    released = 0
    deadline = time.monotonic() + CLIENT_RELEASE_TOTAL_BUDGET_SECONDS

    for key, info in pending.items():
        if time.monotonic() >= deadline:
            _LOGGER.warning(
                "Budget de relâchement épuisé (%s) : %d point(s) non relâchés, "
                "ils restent occupés sur l'automate.",
                reason,
                len(pending) - released,
            )
            break
        try:
            await _write_client_point_present_value(
                app,
                info["address"],
                info["object_type"],
                int(info["object_instance"]),
                null_value,
                priority=int(info["priority"]),
                timeout=CLIENT_RELEASE_WRITE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            _LOGGER.warning(
                "Relâchement en échec pour %s (%s) : le point reste occupé sur l'automate.",
                key,
                reason,
                exc_info=True,
            )
            continue

        try:
            _client_written_points(hass, entry_id).pop(key, None)
        except BaseException:
            pass
        released += 1
        _LOGGER.info(
            "Relâchement (Null) écrit sur %s,%s à la priorité %s (%s)",
            info.get("object_type"),
            info.get("object_instance"),
            info.get("priority"),
            reason,
        )

    return released


async def _open_cov_subscription_context(
    app: Any,
    *,
    address: str,
    object_identifier: str,
    process_id: int,
    lifetime: int | float,
    cleanup_context: Callable[[Any], Awaitable[None]] | None = None,
    max_offset_attempts: int = 3,
) -> tuple[Any | None, BaseException | None]:
    cov_factory = getattr(app, "change_of_value", None)
    if not callable(cov_factory):
        return None, RuntimeError("cov_not_supported")

    last_err: BaseException | None = None
    attempts = max(1, int(max_offset_attempts))
    for offset in range(0, attempts):
        context_obj: Any | None = None
        try:
            context_obj = cov_factory(
                Address(address),
                ObjectIdentifier(object_identifier),
                subscriber_process_identifier=((int(process_id) + offset - 1) % 4194303) + 1,
                issue_confirmed_notifications=False,
                lifetime=int(lifetime),
            )
            opened_context = await context_obj.__aenter__()
            return opened_context, None
        except BaseException as err:
            last_err = err
            if context_obj is not None and cleanup_context is not None:
                try:
                    await cleanup_context(context_obj)
                except BaseException:
                    pass
            if isinstance(err, ValueError) and "existing context" in str(err).lower():
                continue
            break

    return None, last_err


async def _read_client_object_list(
    app: Any,
    address: str,
    client_instance: int,
) -> list[tuple[str, int]]:
    device_obj = f"device,{int(client_instance)}"
    object_list: list[tuple[str, int]] = []
    try:
        list_len = _to_int(
            await _read_remote_property(
                app,
                address,
                device_obj,
                "objectList",
                array_index=0,
                timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
            )
        )
    except asyncio.CancelledError:
        raise
    except BaseException:
        list_len = None
    if not list_len or list_len <= 0:
        return object_list

    for idx in range(1, min(list_len, CLIENT_POINT_SCAN_LIMIT) + 1):
        try:
            item = await _read_remote_property(
                app,
                address,
                device_obj,
                "objectList",
                array_index=idx,
                timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            continue
        parsed = _parse_object_list_item(item)
        if parsed is None:
            continue
        item_type, inst = parsed
        type_info = _supported_point_type(item_type)
        if not type_info or inst is None:
            continue
        canonical_type = type_info[1]
        object_list.append((canonical_type, int(inst)))
    return object_list


async def _discover_remote_clients(server: Any) -> list[tuple[int, str]]:
    app = getattr(server, "app", None)
    if app is None:
        _LOGGER.debug("Client discovery skipped: BACnet app not available")
        return []

    responses: list[Any] = []

    async def _who_is_attempt(label: str, **kwargs: Any) -> list[Any]:
        try:
            result = list(await app.who_is(**kwargs) or [])
            _LOGGER.debug("Client discovery %s returned %d responses", label, len(result))
            return result
        except Exception:
            _LOGGER.debug("Client discovery %s failed", label, exc_info=True)
            return []

    # Keep discovery simple and deterministic:
    # 1) default BACpypes who_is()
    # 2) explicit global broadcast (*:*)
    responses.extend(
        await _who_is_attempt(
            "default",
            timeout=CLIENT_DISCOVERY_TIMEOUT_SECONDS,
        )
    )

    if not responses:
        responses.extend(
            await _who_is_attempt(
                "global-broadcast(*:*)",
                address=Address("*:*"),
                timeout=CLIENT_DISCOVERY_TIMEOUT_SECONDS + 2.0,
            )
        )

    local_instance = _to_int(getattr(server, "instance", None))
    clients: dict[tuple[int, str], tuple[int, str]] = {}
    for i_am in responses:
        try:
            dev_ident = getattr(i_am, "iAmDeviceIdentifier", None)
            instance = _device_instance_from_identifier(dev_ident)
            source = _safe_text(
                getattr(i_am, "pduSource", None)
                or getattr(i_am, "source", None)
            )
        except Exception:
            continue
        if instance is None or not source:
            _LOGGER.debug("Ignoring I-Am with unparsable payload: %r", i_am)
            continue
        if local_instance is not None and instance == local_instance:
            continue
        key = (instance, source)
        clients[key] = key

    discovered = sorted(clients.values(), key=lambda item: (item[0], item[1]))
    if discovered:
        _LOGGER.debug("Discovered BACnet clients (%d): %s", len(discovered), discovered)
    else:
        _LOGGER.debug(
            "Client discovery returned no usable I-Am responses "
            "(raw=%d, local_instance=%s)",
            len(responses),
            local_instance,
        )
    return discovered


async def _resolve_client_address(
    app: Any,
    server: Any,
    client_instance: int,
    fallback_address: str | None = None,
) -> str:
    instance = int(client_instance)
    address_text = str(fallback_address or "").strip()

    # I-Am / explicit scan source is authoritative for reconnects.
    # Do not override it with potentially stale device-info cache entries.
    if address_text:
        return address_text

    try:
        device_info = await app.get_device_info(instance)
        device_address = getattr(device_info, "device_address", None) if device_info else None
        if device_address:
            addr = str(device_address).strip()
            if addr:
                return addr
    except Exception:
        pass

    try:
        for found_instance, found_address in await _discover_remote_clients(server):
            if int(found_instance) == instance and str(found_address or "").strip():
                return str(found_address).strip()
    except Exception:
        pass

    return address_text


async def _read_client_runtime(
    app: Any,
    client_instance: int,
    client_address: str,
    network_port_instance_hint: int = 1,
) -> dict[str, Any]:
    instance = int(client_instance)
    address = str(client_address)
    network_port_instance = max(1, int(network_port_instance_hint or 1))
    device_obj = f"device,{instance}"

    async def read_device(prop: str) -> Any:
        try:
            return await _read_remote_property(app, address, device_obj, prop)
        except asyncio.CancelledError:
            raise
        except BaseException:
            return None

    async def read_network(prop: str, inst: int) -> Any:
        try:
            return await _read_remote_property_any_objid(
                app,
                address,
                [f"network-port,{inst}", f"networkPort,{inst}"],
                prop,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            return None

    probe_object_name = _safe_text(await read_device("objectName"))
    if probe_object_name is None:
        probe_object_identifier = _object_identifier_text(await read_device("objectIdentifier"))
        if probe_object_identifier is None:
            return {
                "online": False,
                "has_device_object": False,
                "has_network_object": False,
                "device": {
                    "client_instance": instance,
                    "client_address": address,
                    "object_identifier": str(instance),
                },
                "network": {
                    "client_instance": instance,
                    "client_address": address,
                },
                "network_port_instance": network_port_instance,
                "name": f"BACnet Client {instance}",
            }

    object_name = probe_object_name or _safe_text(await read_device("objectName"))
    description = _safe_text(await read_device("description"))
    model_name = _safe_text(await read_device("modelName"))
    vendor_name = _safe_text(await read_device("vendorName"))
    vendor_identifier = _to_int(await read_device("vendorIdentifier"))
    firmware_revision = _safe_text(await read_device("firmwareRevision"))
    hardware_revision = _safe_text(await read_device("hardwareRevision"))
    application_software_version = _safe_text(await read_device("applicationSoftwareVersion"))
    serial_number = _safe_text(await read_device("serialNumber"))
    object_identifier = _object_identifier_instance_text(
        await read_device("objectIdentifier"),
        fallback=instance,
    )
    raw_system_status = await read_device("systemStatus")
    _, system_status = _normalize_system_status(raw_system_status)

    net_object_identifier = await read_network("objectIdentifier", network_port_instance)
    if net_object_identifier is None and network_port_instance == 1:
        try:
            list_len = _to_int(
                await _read_remote_property(
                    app,
                    address,
                    device_obj,
                    "objectList",
                    array_index=0,
                    timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
                )
            )
            if list_len and list_len > 0:
                for idx in range(1, min(list_len, CLIENT_OBJECTLIST_SCAN_LIMIT) + 1):
                    oid = await _read_remote_property(
                        app,
                        address,
                        device_obj,
                        "objectList",
                        array_index=idx,
                        timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
                    )
                    inst = _object_identifier_instance(oid, "network-port")
                    if inst is not None:
                        network_port_instance = int(inst)
                        break
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass
        net_object_identifier = await read_network("objectIdentifier", network_port_instance)
    has_network_object = net_object_identifier is not None

    ip_address_raw = await read_network("ipAddress", network_port_instance)
    ip_subnet_mask_raw = await read_network("ipSubnetMask", network_port_instance)
    udp_port = _to_int(await read_network("bacnetIPUDPPort", network_port_instance))
    mac_raw = _mac_hex(await read_network("macAddress", network_port_instance))

    ip_address = _to_ipv4_text(ip_address_raw) or _safe_text(ip_address_raw)
    ip_subnet_mask = _to_ipv4_text(ip_subnet_mask_raw) or _safe_text(ip_subnet_mask_raw)
    if not mac_raw:
        mac_raw = _bacnet_mac_from_ip_port(ip_address, udp_port)
    mac_colon = _mac_colon(mac_raw)

    device_data = {
        "client_instance": instance,
        "client_address": address,
        "description": description,
        "firmware_revision": firmware_revision,
        "hardware_revision": hardware_revision,
        "application_software_version": application_software_version,
        "model_name": model_name,
        "object_identifier": object_identifier or str(instance),
        "object_name": object_name,
        "system_status": system_status,
        "vendor_identifier": vendor_identifier,
        "vendor_name": vendor_name,
        "serial_number": serial_number,
    }
    network_data = {
        "client_instance": instance,
        "client_address": address,
        "ip_address": ip_address,
        "ip_subnet_mask": ip_subnet_mask,
        "mac_address_raw": mac_colon,
    }
    online = any(
        value is not None
        for value in (
            object_name,
            model_name,
            vendor_name,
            firmware_revision,
            ip_address,
            mac_raw,
        )
    )
    return {
        "online": online,
        "has_device_object": True,
        "has_network_object": has_network_object,
        "device": device_data,
        "network": network_data,
        "network_port_instance": network_port_instance,
        "name": client_display_name(instance, object_name),
    }


# ---------------------------------------------------------------------------
# v1.1.0 — Lecture du Priority Array à la demande
# ---------------------------------------------------------------------------

PRIORITY_ARRAY_SIZE = 16

# Délai par point. Doit rester au-dessus du budget de transaction bacpypes3
# (apduTimeout × (1 + numberOfApduRetries) = 4,0 s), cf. v1.0.6.
CLIENT_PRIORITY_ARRAY_TIMEOUT_SECONDS = 6.0


def _normalize_priority_value(item: Any) -> float | None:
    """Réduit un `PriorityValue` bacpypes3 à un `float` ou `None`.

    Un `PriorityValue` est un *choice* : `null`, `real`, `binary`, `integer`…
    Le cas `null` (niveau libre) doit ressortir en `None`, et non en `0.0` —
    confondre les deux rendrait un niveau libre indiscernable d'une commande à
    zéro, ce qui est exactement l'information recherchée.
    """
    if item is None:
        return None

    # bacpypes3 expose les branches du choice en attributs ; seule celle qui est
    # renseignée est non nulle.
    for attr in ("real", "integer", "unsigned", "binaryValue", "enumerated", "double"):
        value = getattr(item, attr, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

    if getattr(item, "null", None) is not None:
        return None

    # Certaines piles renvoient directement un scalaire.
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return float(item)
    if isinstance(item, bool):
        return float(int(item))

    text = str(item).strip().lower()
    if text in ("", "null", "none"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_priority_array(raw: Any) -> list[float | None] | None:
    """Normalise un Priority Array brut en 16 valeurs `None` / `float`.

    Renvoie **`None` franc** si la donnée est inexploitable — jamais `[]` ni
    `[None] * 16`. C'est la condition pour que `_merge_non_none` protège la
    valeur précédente : le piège de la v1.0.4 était précisément un repli non nul
    (`has_priority_array` en `bool`, `object_name` avec valeur par défaut), que
    cette protection ne voyait pas.
    """
    if raw is None:
        return None
    try:
        items = list(raw)
    except TypeError:
        return None
    if not items:
        return None

    values = [_normalize_priority_value(item) for item in items[:PRIORITY_ARRAY_SIZE]]
    # Un automate peut renvoyer moins de 16 niveaux ; on complète pour que
    # l'index reste la priorité BACnet.
    while len(values) < PRIORITY_ARRAY_SIZE:
        values.append(None)
    return values


async def _read_client_point_priority_array(
    app: Any,
    address: str,
    object_identifier: str,
) -> list[float | None] | None:
    """Lit le Priority Array d'un point commandable. `None` si la lecture échoue.

    Une seule requête, une seule passe.

    v1.1.0 comportait un repli lisant les 16 niveaux un par un via `array_index`.
    Il visait les automates dont la réponse complète dépasse
    `Max Apdu Length Accepted` alors que `Segmentation Supported = None`
    (`Abort from device: 6`). Ce cas ne s'est jamais présenté, alors que le repli
    se déclenchait sur **toute** erreur de lecture — y compris, et surtout, sur
    les objets ne possédant tout simplement pas de propriété `priorityArray`.
    Chacun de ces objets coûtait alors 16 aller-retours inutiles sur un bus
    série. Repli retiré en v1.2.0 : un échec est désormais un échec franc.

    16 `Real` tiennent dans 480 octets ; un automate qui ne sait pas répondre à
    cette lecture n'a pas de Priority Array exploitable de toute façon.
    """
    try:
        raw = await _read_remote_property(
            app,
            address,
            object_identifier,
            "priorityArray",
            timeout=CLIENT_PRIORITY_ARRAY_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except BaseException:
        return None

    # `None` franc si la donnée est inexploitable : la photo précédente du point
    # est alors conservée par l'appelant.
    return _normalize_priority_array(raw)


async def _read_client_point_payload(
    app: Any,
    address: str,
    client_instance: int,
    object_type: str,
    object_instance: int,
) -> dict[str, Any]:
    supported = _supported_point_type(object_type)
    if not supported:
        return {}
    type_slug, canonical_type = supported
    objid = f"{canonical_type},{int(object_instance)}"
    props = [
        "objectIdentifier",
        "objectName",
        "description",
        "presentValue",
        "units",
        "statusFlags",
        "outOfService",
        "reliability",
        "stateText",
        "numberOfStates",
        "activeText",
        "inactiveText",
        "priorityArray",
    ]
    values = await _read_remote_properties(app, address, objid, props)
    state_text_value = values.get("stateText")
    state_text: list[str] | None = None
    if isinstance(state_text_value, (list, tuple)):
        state_text = [str(item) for item in state_text_value]
    elif state_text_value is not None and not isinstance(state_text_value, str):
        try:
            state_text = [str(item) for item in list(state_text_value)]
        except Exception:
            state_text = None
    object_identifier_raw = values.get("objectIdentifier")
    object_identifier = _object_identifier_compact(
        object_identifier_raw,
        canonical_type,
        int(object_instance),
    )
    point_key = f"{type_slug}_{int(object_instance)}"
    has_priority_array = values.get("priorityArray") is not None
    writable_from_ha = _point_is_writable(
        {
            "type_slug": type_slug,
            "has_priority_array": has_priority_array,
        }
    )
    return {
        "point_key": point_key,
        "client_instance": int(client_instance),
        "client_address": str(address),
        "object_type": canonical_type,
        "type_slug": type_slug,
        "object_instance": int(object_instance),
        "object_identifier": object_identifier,
        "object_name": _safe_text(values.get("objectName")) or f"{canonical_type} {int(object_instance)}",
        "description": _safe_text(values.get("description")),
        "present_value": values.get("presentValue"),
        "unit": _normalize_bacnet_unit(values.get("units")),
        "status_flags": _safe_text(values.get("statusFlags")),
        "out_of_service": values.get("outOfService"),
        "reliability": _safe_text(values.get("reliability")),
        "state_text": state_text,
        "number_of_states": _to_int(values.get("numberOfStates")),
        "active_text": _safe_text(values.get("activeText")),
        "inactive_text": _safe_text(values.get("inactiveText")),
        "has_priority_array": has_priority_array,
        "writable_from_ha": writable_from_ha,
    }


def _hub_diagnostics(server: Any, merged: Dict[str, Any]) -> Dict[str, Any]:
    device_obj = getattr(server, "device_object", None) if server is not None else None
    network_obj = getattr(server, "network_port_object", None) if server is not None else None

    instance = _to_int(getattr(server, "instance", None)) if server is not None else _to_int(merged.get(CONF_INSTANCE))

    object_name = _safe_text(getattr(device_obj, "objectName", None))
    description = _safe_text(getattr(device_obj, "description", None))
    model_name = _safe_text(getattr(device_obj, "modelName", None))
    vendor_name = _safe_text(getattr(device_obj, "vendorName", None))
    vendor_identifier = _to_int(getattr(device_obj, "vendorIdentifier", None))

    firmware_revision = _safe_text(getattr(device_obj, "firmwareRevision", None))
    integration_version = _safe_text(getattr(device_obj, "applicationSoftwareVersion", None))
    hardware_revision = _safe_text(getattr(device_obj, "hardwareRevision", None))

    _, status_label = _normalize_system_status(getattr(device_obj, "systemStatus", None))
    system_status = status_label

    object_identifier = _object_identifier_instance_text(
        getattr(device_obj, "objectIdentifier", None),
        fallback=instance,
    )

    ip_address = _to_ipv4_text(getattr(network_obj, "ipAddress", None))
    subnet_mask = _to_ipv4_text(getattr(network_obj, "ipSubnetMask", None))
    udp_port = _to_int(getattr(network_obj, "bacnetIPUDPPort", None))

    mac_address_raw = _mac_hex(getattr(network_obj, "macAddress", None))
    if not mac_address_raw:
        mac_address_raw = _bacnet_mac_from_ip_port(ip_address, udp_port)
    mac_colon = _mac_colon(mac_address_raw)

    address = None

    return {
        "device_object_instance": instance,
        "object_identifier": object_identifier,
        "object_name": object_name,
        "description": description,
        "model_name": model_name,
        "vendor_identifier": vendor_identifier,
        "vendor_name": vendor_name,
        "system_status": system_status,
        "firmware_revision": firmware_revision,
        "integration_version": integration_version,
        "firmware": integration_version,
        "application_software_version": integration_version,
        "hardware_revision": hardware_revision,
        "address": address,
        "ip_address": ip_address,
        "ip_subnet_mask": subnet_mask,
        "subnet_mask": subnet_mask,
        "mac_address": mac_colon,
        "mac_address_raw": mac_colon,
    }


def _client_offline_payload(
    client_instance: int,
    client_address: str,
    network_port_instance_hint: int = 1,
) -> dict[str, Any]:
    instance = int(client_instance)
    address = str(client_address)
    return {
        "online": False,
        "has_device_object": True,
        "has_network_object": False,
        "device": {
            "client_instance": instance,
            "client_address": address,
            "object_identifier": str(instance),
        },
        "network": {
            "client_instance": instance,
            "client_address": address,
        },
        "network_port_instance": int(network_port_instance_hint or 1),
        "name": client_display_name(instance, None),
    }


async def _refresh_client_cache(
    hass: HomeAssistant,
    entry_id: str,
    client_id: str,
    client_instance: int,
    client_address: str,
    network_port_hints: dict[str, int],
    client_targets: dict[str, tuple[int, str]] | None = None,
    force: bool = False,
) -> None:
    cache = _client_cache_get(hass, entry_id, client_id)
    now = time.monotonic()
    last_refresh = float(cache.get("_last_refresh_ts") or 0.0)
    if not force and (now - last_refresh) < CLIENT_REFRESH_MIN_SECONDS:
        return

    lock = _client_lock_get(hass, entry_id, client_id)
    async with lock:
        cache = _client_cache_get(hass, entry_id, client_id)
        previous_device = dict(cache.get("device", {}) or {})
        previous_network = dict(cache.get("network", {}) or {})
        previous_name = _safe_text(cache.get("name"))
        now = time.monotonic()
        last_refresh = float(cache.get("_last_refresh_ts") or 0.0)
        if not force and (now - last_refresh) < CLIENT_REFRESH_MIN_SECONDS:
            return

        server = hass.data.get(DOMAIN, {}).get("servers", {}).get(entry_id)
        app = getattr(server, "app", None) if server is not None else None
        hint = int(network_port_hints.get(client_id, 1))
        resolved_address = str(client_address or "").strip()
        if app is not None:
            resolved_address = await _resolve_client_address(
                app=app,
                server=server,
                client_instance=int(client_instance),
                fallback_address=resolved_address,
            )
        data = _client_offline_payload(client_instance, resolved_address, hint)
        if app is not None:
            try:
                data = await _read_client_runtime(
                    app,
                    int(client_instance),
                    resolved_address,
                    network_port_instance_hint=hint,
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                _LOGGER.debug(
                    "Client runtime read failed for %s (%s)",
                    client_instance,
                    resolved_address,
                    exc_info=True,
                )
                data = _client_offline_payload(client_instance, resolved_address, hint)
                if previous_device:
                    data["device"] = _merge_non_none(previous_device, dict(data.get("device", {}) or {}))
                if previous_network:
                    data["network"] = _merge_non_none(previous_network, dict(data.get("network", {}) or {}))
                if previous_name:
                    data["name"] = previous_name
                data["has_device_object"] = bool(
                    data.get("has_device_object")
                    or previous_device.get("object_identifier")
                )
                data["has_network_object"] = bool(
                    data.get("has_network_object")
                    or previous_network.get("ip_address")
                    or previous_network.get("ip_subnet_mask")
                    or previous_network.get("mac_address_raw")
                )
            else:
                if previous_device:
                    data["device"] = _merge_non_none(previous_device, dict(data.get("device", {}) or {}))
                if previous_network:
                    data["network"] = _merge_non_none(previous_network, dict(data.get("network", {}) or {}))
                if previous_name and not _safe_text(data.get("name")):
                    data["name"] = previous_name
                data["has_device_object"] = bool(
                    data.get("has_device_object")
                    or previous_device.get("object_identifier")
                )
                data["has_network_object"] = bool(
                    data.get("has_network_object")
                    or previous_network.get("ip_address")
                    or previous_network.get("ip_subnet_mask")
                    or previous_network.get("mac_address_raw")
                )

        network_port_hints[client_id] = int(data.get("network_port_instance") or hint or 1)
        if client_targets is not None:
            client_targets[client_id] = (
                int(client_instance),
                str(data.get("device", {}).get("client_address") or resolved_address),
            )
        data["_last_refresh_ts"] = now
        _client_cache_set(hass, entry_id, client_id, data)
    async_dispatcher_send(hass, _client_diag_signal(entry_id, client_id))

