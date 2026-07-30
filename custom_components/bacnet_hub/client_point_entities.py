from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import StateType

from .const import DOMAIN, client_display_name
from .client_runtime import (
    CLIENT_COV_LEASE_SECONDS,
    CLIENT_COV_RENEW_FRACTION,
    _client_cache_get,
    _client_cov_signal,
    _client_points_get,
    _client_points_set,
    _client_points_signal,
    _client_rescan_signal,
    _cov_process_identifier,
    _entry_points_signal,
    _normalize_bacnet_unit,
    _point_entity_id,
    _point_is_writable,
    _point_native_value_from_payload,
    _point_unique_id,
    _property_slug,
    _safe_text,
    _sensor_device_class_from_unit,
    _to_int,
)
from .client_runtime import (
    _forget_client_written_point,
    _open_cov_subscription_context,
    _read_remote_property,
    _record_client_written_point,
    _write_client_point_present_value,
)

_LOGGER = logging.getLogger(__name__)

# Fork v1.0.4 — relecture forcée du Present Value après écriture.
#
# Une écriture (valeur ou Null) ne déclenche pas systématiquement de notification
# COV côté automate. Sans relecture, l'entité HA reste figée sur la dernière valeur
# COMMANDÉE jusqu'au cycle COV suivant (retard mesuré : plus de 3 minutes sur un
# Distech ECB-203 après un relâchement). L'entité affiche alors une valeur fausse.
#
# Deux passes : la première juste après l'écriture, la seconde en filet de sécurité
# pour les automates qui appliquent la valeur de façon différée. La seconde passe
# ne republie rien si la valeur n'a pas changé.
REFRESH_AFTER_WRITE_DELAYS: tuple[float, ...] = (0.5, 3.0)


def _point_is_on(point: dict[str, Any]) -> bool | None:
    value = point.get("present_value")
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"active", "on", "true", "1"}:
        return True
    if text in {"inactive", "off", "false", "0"}:
        return False
    try:
        return bool(int(text))
    except Exception:
        return None


class BacnetClientPointEntityBase:
    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = False
    _POINT_UNAVAILABLE_KEY = "_cov_unavailable"
    _POINT_UNAVAILABLE_REASON_KEY = "_cov_unavailable_reason"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
        *,
        entity_domain: str,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._client_id = client_id
        self._client_instance = int(client_instance)
        self._point_key = str(point_key)
        self._entity_domain = str(entity_domain).strip().lower()

        self._unsub_points_dispatcher: Callable[[], None] | None = None
        self._unsub_cov_dispatcher: Callable[[], None] | None = None
        self._cov_context: Any | None = None
        self._cov_task: asyncio.Task | None = None
        self._cov_lease_unsub: Callable[[], None] | None = None
        self._cov_lock = asyncio.Lock()
        self._cov_registered = False
        self._cov_last_target: tuple[str, str] | None = None
        self._cov_retry_delay_seconds: float = 10.0
        self._cov_retry_not_before_ts: float = 0.0
        self._cov_rescan_not_before_ts: float = 0.0
        # Fork v1.0.4 : tâches de relecture du Present Value en cours (annulées au retrait).
        self._refresh_tasks: set[asyncio.Task] = set()

        cache = _client_points_get(hass, entry_id, client_id).get(self._point_key, {})
        type_slug = str(cache.get("type_slug") or "point")
        object_instance = _to_int(cache.get("object_instance")) or 0

        self._attr_unique_id = _point_unique_id(entry_id, client_id, type_slug, object_instance)
        self.entity_id = _point_entity_id(
            self._client_instance,
            type_slug,
            object_instance,
            entity_domain=self._entity_domain,
        )
        description = _safe_text(cache.get("description"))
        object_name = _safe_text(cache.get("object_name"))
        self._attr_name = str(description or object_name or f"{type_slug.upper()} {object_instance}")
        self._attr_available = not bool(cache.get(self._POINT_UNAVAILABLE_KEY, False))

    @property
    def device_info(self) -> DeviceInfo:
        diag_cache = _client_cache_get(self.hass, self._entry_id, self._client_id)
        device_data = dict(diag_cache.get("device", {}) or {})
        return DeviceInfo(
            identifiers={(DOMAIN, self._client_id)},
            via_device=(DOMAIN, self._entry_id),
            name=str(diag_cache.get("name") or client_display_name(self._client_instance)),
            manufacturer=_safe_text(device_data.get("vendor_name")),
            model=_safe_text(device_data.get("model_name")),
            sw_version=_safe_text(device_data.get("firmware_revision")),
            hw_version=_safe_text(device_data.get("hardware_revision")),
            serial_number=_safe_text(device_data.get("serial_number")),
        )

    async def async_added_to_hass(self) -> None:
        signal = _client_points_signal(self._entry_id, self._client_id)
        self._unsub_points_dispatcher = async_dispatcher_connect(
            self.hass,
            signal,
            self._handle_points_update,
        )
        cov_signal = _client_cov_signal(self._entry_id, self._client_id)
        self._unsub_cov_dispatcher = async_dispatcher_connect(
            self.hass,
            cov_signal,
            self._handle_cov_reregister,
        )
        self._handle_points_update()
        await self._async_register_cov()
        self._handle_points_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_points_dispatcher is not None:
            self._unsub_points_dispatcher()
            self._unsub_points_dispatcher = None
        if self._unsub_cov_dispatcher is not None:
            self._unsub_cov_dispatcher()
            self._unsub_cov_dispatcher = None
        # Fork v1.0.4 : stopper les relectures planifiées avant de retirer l'entité.
        for task in list(self._refresh_tasks):
            if not task.done():
                task.cancel()
        self._refresh_tasks.clear()
        async with self._cov_lock:
            await self._async_stop_cov_runtime()

    def _get_point(self) -> dict[str, Any]:
        return dict(
            _client_points_get(self.hass, self._entry_id, self._client_id).get(self._point_key, {}) or {}
        )

    def _set_client_points_unavailable(self, unavailable: bool, *, reason: str | None = None) -> None:
        point_cache = _client_points_get(self.hass, self._entry_id, self._client_id)
        if not point_cache:
            return

        payload: dict[str, dict[str, Any]] = {}
        changed = False
        for point_key, raw_point in point_cache.items():
            point = dict(raw_point or {})
            prev_unavailable = bool(point.get(self._POINT_UNAVAILABLE_KEY, False))
            if prev_unavailable != unavailable:
                point[self._POINT_UNAVAILABLE_KEY] = unavailable
                changed = True

            if unavailable:
                reason_text = str(reason or "cov_register_failed")
                if str(point.get(self._POINT_UNAVAILABLE_REASON_KEY) or "") != reason_text:
                    point[self._POINT_UNAVAILABLE_REASON_KEY] = reason_text
                    changed = True
            else:
                if self._POINT_UNAVAILABLE_REASON_KEY in point:
                    point.pop(self._POINT_UNAVAILABLE_REASON_KEY, None)
                    changed = True

            payload[str(point_key)] = point

        if not changed:
            return

        _client_points_set(self.hass, self._entry_id, self._client_id, payload)
        async_dispatcher_send(self.hass, _client_points_signal(self._entry_id, self._client_id))
        async_dispatcher_send(
            self.hass,
            _entry_points_signal(self._entry_id),
            {"client_id": self._client_id},
        )

    @callback
    def _handle_cov_reregister(self) -> None:
        self.hass.async_create_task(self._async_reregister_cov())

    async def _async_reregister_cov(self) -> None:
        try:
            await self._async_register_cov()
        except asyncio.CancelledError:
            raise
        except BaseException:
            _LOGGER.debug("COV re-register failed for %s", self._point_key, exc_info=True)

    async def _async_stop_cov_runtime(self) -> None:
        if self._cov_lease_unsub is not None:
            try:
                self._cov_lease_unsub()
            except BaseException:
                pass
            self._cov_lease_unsub = None
        if self._cov_task is not None and not self._cov_task.done():
            self._cov_task.cancel()
            try:
                await self._cov_task
            except asyncio.CancelledError:
                pass
            except BaseException:
                pass
        self._cov_task = None
        if self._cov_context is not None:
            await self._async_cleanup_cov_context(self._cov_context, call_aexit=True)
        self._cov_context = None
        self._cov_registered = False

    async def _async_cleanup_cov_context(self, context_obj: Any, *, call_aexit: bool) -> None:
        if context_obj is None:
            return

        if call_aexit:
            try:
                await context_obj.__aexit__(None, None, None)
            except BaseException:
                pass

        embedded_tasks: list[asyncio.Task] = []
        embedded_handles: list[Any] = []
        values: list[Any] = []
        seen_ids: set[int] = set()

        try:
            for value in vars(context_obj).values():
                vid = id(value)
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)
                values.append(value)
        except BaseException:
            pass

        for attr_name in dir(context_obj):
            if attr_name.startswith("__"):
                continue
            low = attr_name.lower()
            if "task" not in low and "handle" not in low and "timer" not in low:
                continue
            try:
                value = getattr(context_obj, attr_name)
            except BaseException:
                continue
            vid = id(value)
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            values.append(value)

        for value in values:
            if isinstance(value, asyncio.Task):
                embedded_tasks.append(value)
                continue
            cancel_fn = getattr(value, "cancel", None)
            if callable(cancel_fn):
                embedded_handles.append(value)

        for handle in embedded_handles:
            try:
                handle.cancel()
            except BaseException:
                pass

        for task in embedded_tasks:
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException:
                pass

    async def _async_register_cov(self) -> None:
        point = self._get_point()
        object_identifier = _safe_text(point.get("object_identifier"))
        address = _safe_text(point.get("client_address"))
        if not object_identifier or not address:
            self._set_client_points_unavailable(True, reason="cov_target_missing")
            return
        now = time.monotonic()
        target = (str(address), str(object_identifier))
        if self._cov_last_target != target:
            self._cov_last_target = target
            self._cov_retry_not_before_ts = 0.0
            self._cov_retry_delay_seconds = 10.0
        if now < self._cov_retry_not_before_ts:
            return

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            self._set_client_points_unavailable(True, reason="bacnet_app_unavailable")
            return

        cov_factory = getattr(app, "change_of_value", None)
        if not callable(cov_factory):
            self._cov_registered = False
            self._set_client_points_unavailable(True, reason="cov_not_supported")
            return

        process_id = _cov_process_identifier(self._entry_id, self._client_id, self._point_key)
        async with self._cov_lock:
            await self._async_stop_cov_runtime()

            async def _cleanup_failed_context(context_obj: Any) -> None:
                await self._async_cleanup_cov_context(context_obj, call_aexit=False)

            opened_context, last_err = await _open_cov_subscription_context(
                app,
                address=address,
                object_identifier=object_identifier,
                process_id=process_id,
                lifetime=CLIENT_COV_LEASE_SECONDS,
                cleanup_context=_cleanup_failed_context,
                max_offset_attempts=3,
            )
            self._cov_context = opened_context
            self._cov_registered = False
            if last_err is not None:
                exc_info = (type(last_err), last_err, last_err.__traceback__)
                _LOGGER.debug(
                    "COV subscribe failed for %s (%s)",
                    object_identifier,
                    address,
                    exc_info=exc_info,
                )
                now_fail = time.monotonic()
                if now_fail >= self._cov_rescan_not_before_ts:
                    self._cov_rescan_not_before_ts = now_fail + 10.0
                    async_dispatcher_send(
                        self.hass,
                        _client_rescan_signal(self._entry_id),
                        {"instance": self._client_instance},
                    )
                self._cov_retry_not_before_ts = time.monotonic() + self._cov_retry_delay_seconds
                self._cov_retry_delay_seconds = min(self._cov_retry_delay_seconds * 2.0, 300.0)
                self._set_client_points_unavailable(True, reason="cov_register_failed")
                return

            if self._cov_context is None:
                self._set_client_points_unavailable(True, reason="cov_register_failed")
                return

            self._cov_registered = True
            self._cov_retry_not_before_ts = 0.0
            self._cov_retry_delay_seconds = 10.0
            self._set_client_points_unavailable(False)
            self._cov_task = self.hass.async_create_task(self._async_cov_receive_loop())
            self._schedule_cov_lease_reregister()

    def _schedule_cov_lease_reregister(self) -> None:
        if self._cov_lease_unsub is not None:
            try:
                self._cov_lease_unsub()
            except BaseException:
                pass
            self._cov_lease_unsub = None

        # Make-before-break : on renouvelle AVANT l'expiration (à 80% du lease),
        # pour que la nouvelle souscription soit posée tant que l'ancienne est encore
        # valide -> supprime le trou d'indisponibilité (yoyo) à chaque renouvellement.
        fraction = float(CLIENT_COV_RENEW_FRACTION)
        if not (0.1 <= fraction <= 0.95):
            fraction = 0.8
        delay = max(1.0, float(CLIENT_COV_LEASE_SECONDS) * fraction)

        @callback
        def _lease_expired(_now) -> None:
            self._cov_lease_unsub = None
            self.hass.async_create_task(self._async_reregister_cov())

        self._cov_lease_unsub = async_call_later(self.hass, delay, _lease_expired)

    async def _async_cov_receive_loop(self) -> None:
        while True:
            context = self._cov_context
            if context is None:
                return
            try:
                prop, value = await context.get_value()
            except asyncio.CancelledError:
                raise
            except BaseException:
                self._cov_registered = False
                self._set_client_points_unavailable(True, reason="cov_receive_failed")
                self._handle_points_update()
                _LOGGER.debug("COV receive loop failed for %s", self._point_key, exc_info=True)
                return

            key = _property_slug(prop)
            if not key:
                continue
            if key not in {
                "presentvalue",
                "statusflags",
                "outofservice",
                "reliability",
                "description",
                "objectname",
                "statetext",
                "activetext",
                "inactivetext",
            }:
                continue

            point = self._get_point()
            if not point:
                continue

            if key == "presentvalue":
                point["present_value"] = value
            elif key == "statusflags":
                point["status_flags"] = _safe_text(value)
            elif key == "outofservice":
                point["out_of_service"] = value
            elif key == "reliability":
                point["reliability"] = _safe_text(value)
            elif key == "description":
                point["description"] = _safe_text(value)
            elif key == "objectname":
                point["object_name"] = _safe_text(value)
            elif key == "statetext":
                if isinstance(value, (list, tuple)):
                    point["state_text"] = [str(item) for item in value]
                else:
                    try:
                        point["state_text"] = [str(item) for item in list(value)]
                    except Exception:
                        pass
            elif key == "activetext":
                point["active_text"] = _safe_text(value)
            elif key == "inactivetext":
                point["inactive_text"] = _safe_text(value)

            _client_points_set(
                self.hass,
                self._entry_id,
                self._client_id,
                {self._point_key: point},
            )
            async_dispatcher_send(self.hass, _client_points_signal(self._entry_id, self._client_id))
            async_dispatcher_send(
                self.hass,
                _entry_points_signal(self._entry_id),
                {"client_id": self._client_id},
            )

    def _schedule_present_value_refresh(self) -> None:
        """Fork v1.0.4 : planifie la relecture du Present Value réel après écriture.

        Non bloquant : le service HA (`set_value`, `select_option`, `press`…) rend la
        main immédiatement, la relecture se fait en tâche de fond.

        Appelé depuis l'event loop (services d'entité) -> `hass.async_create_task`.
        NE PAS utiliser `hass.create_task`, qui retourne `None`.
        """
        try:
            task = self.hass.async_create_task(self._async_refresh_present_value())
        except BaseException:
            _LOGGER.debug(
                "Planification de la relecture impossible pour %s",
                self._point_key,
                exc_info=True,
            )
            return
        if task is None:
            return
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def _async_refresh_present_value(self) -> None:
        """Fork v1.0.4 : relit le Present Value sur l'automate et publie l'état réel.

        Ne lève jamais : une relecture ratée ne doit pas faire échouer l'écriture qui
        vient d'aboutir. En cas d'échec, l'état reste celui d'avant, corrigé plus tard
        par le COV.
        """
        point = self._get_point()
        if not point:
            return

        address = _safe_text(point.get("client_address"))
        object_type = _safe_text(point.get("object_type"))
        object_instance = _to_int(point.get("object_instance"))
        if not address or not object_type or object_instance is None:
            return

        # Même construction d'identifiant que le chemin d'écriture.
        objid = f"{object_type},{int(object_instance)}"

        for delay in REFRESH_AFTER_WRITE_DELAYS:
            if float(delay) > 0:
                await asyncio.sleep(float(delay))

            server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
            app = getattr(server, "app", None) if server is not None else None
            if app is None:
                return

            try:
                value = await _read_remote_property(app, address, objid, "presentValue")
            except asyncio.CancelledError:
                raise
            except BaseException:
                _LOGGER.debug(
                    "Relecture du Present Value échouée pour %s (%s)",
                    objid,
                    address,
                    exc_info=True,
                )
                continue

            current = self._get_point()
            if not current:
                return

            try:
                unchanged = current.get("present_value") == value
            except BaseException:
                unchanged = False
            if unchanged:
                continue

            current["present_value"] = value
            _client_points_set(
                self.hass,
                self._entry_id,
                self._client_id,
                {self._point_key: current},
            )
            async_dispatcher_send(self.hass, _client_points_signal(self._entry_id, self._client_id))
            async_dispatcher_send(
                self.hass,
                _entry_points_signal(self._entry_id),
                {"client_id": self._client_id},
            )
            _LOGGER.debug(
                "Present Value relu sur %s (%s) : %s",
                objid,
                address,
                value,
            )

    async def _async_write_present_value(self, value: Any) -> None:
        point = self._get_point()
        if not point:
            raise HomeAssistantError("Point payload unavailable")

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            raise HomeAssistantError("BACnet app unavailable")

        address = _safe_text(point.get("client_address"))
        object_type = _safe_text(point.get("object_type"))
        object_instance = _to_int(point.get("object_instance"))
        if not address or not object_type or object_instance is None:
            raise HomeAssistantError("Point addressing incomplete")

        type_slug = str(point.get("type_slug") or "").strip().lower()
        # Fork v1.0.5 — NIVEAU 1 : la priorité d'écriture ne dépend PLUS de
        # has_priority_array, donc plus d'une lecture réseau. Une lecture ratée
        # faisait partir l'écriture sans priorité : acceptée par l'automate,
        # écrasée aussitôt, et sans erreur affichée.
        if type_slug in {"ao", "bo", "av", "bv", "mv"}:
            from .write_priority import get_write_priority

            write_priority = get_write_priority(
                self.hass, self._entry_id, self._client_id
            )
        else:
            write_priority = None

        await _write_client_point_present_value(
            app,
            address,
            object_type,
            int(object_instance),
            value,
            priority=write_priority,
        )

        # Fork v1.0.5 : l'ecriture a abouti et occupe desormais un niveau de
        # priorite sur l'automate. On le memorise pour pouvoir le relacher a
        # l'arret de Home Assistant : l'ECB-203 ne rend jamais la main seul.
        if write_priority is not None:
            _record_client_written_point(
                self.hass,
                self._entry_id,
                client_id=self._client_id,
                point_key=self._point_key,
                address=address,
                object_type=object_type,
                object_instance=int(object_instance),
                priority=write_priority,
            )

        point["present_value"] = value
        _client_points_set(
            self.hass,
            self._entry_id,
            self._client_id,
            {self._point_key: point},
        )
        async_dispatcher_send(self.hass, _client_points_signal(self._entry_id, self._client_id))
        async_dispatcher_send(
            self.hass,
            _entry_points_signal(self._entry_id),
            {"client_id": self._client_id},
        )

        # Fork v1.0.4 : la valeur ci-dessus est OPTIMISTE (affichage immédiat).
        # On relit le Present Value réel pour publier la vérité de l'automate.
        self._schedule_present_value_refresh()

    async def _async_release_present_value(self) -> None:
        """Fork : relâche la commande en écrivant Null à la priorité configurée.

        L'automate (ex. Distech) reprend alors la main avec sa propre valeur
        inscrite à un niveau de priorité inférieur. Indispensable pour rendre
        la main en fin de mode (ex. mode été) ou en sécurité (sonde indisponible).
        """
        point = self._get_point()
        if not point:
            raise HomeAssistantError("Point payload unavailable")

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            raise HomeAssistantError("BACnet app unavailable")

        address = _safe_text(point.get("client_address"))
        object_type = _safe_text(point.get("object_type"))
        object_instance = _to_int(point.get("object_instance"))
        if not address or not object_type or object_instance is None:
            raise HomeAssistantError("Point addressing incomplete")

        type_slug = str(point.get("type_slug") or "").strip().lower()
        # Fork v1.0.5 — NIVEAU 1 : refus fondé sur le seul type d'objet.
        if type_slug not in {"ao", "bo", "av", "bv", "mv"}:
            raise HomeAssistantError(
                "Ce point ne gère pas de Priority Array : relâchement impossible"
            )

        from .write_priority import get_write_priority

        write_priority = get_write_priority(self.hass, self._entry_id, self._client_id)

        try:
            from bacpypes3.primitivedata import Null

            null_value: Any = Null(())
        except Exception as err:  # pragma: no cover
            raise HomeAssistantError(f"Null BACnet indisponible : {err}") from err

        await _write_client_point_present_value(
            app,
            address,
            object_type,
            int(object_instance),
            null_value,
            priority=write_priority,
        )

        # Fork v1.0.5 : le point n'occupe plus de niveau de priorite.
        _forget_client_written_point(
            self.hass,
            self._entry_id,
            client_id=self._client_id,
            point_key=self._point_key,
        )

        _LOGGER.debug(
            "Relâchement (Null) écrit sur %s,%s à la priorité %s",
            object_type,
            object_instance,
            write_priority,
        )

        # Fork v1.0.4 : après un relâchement, l'automate reprend la main avec SA valeur.
        # Sans relecture, l'entité resterait figée sur la dernière valeur commandée par HA.
        self._schedule_present_value_refresh()

    @callback
    def _handle_points_update(self) -> None:
        point = self._get_point()
        if not point:
            return

        self._attr_available = not bool(point.get(self._POINT_UNAVAILABLE_KEY, False))

        description = _safe_text(point.get("description"))
        object_name = _safe_text(point.get("object_name"))
        if description:
            self._attr_name = description
        elif object_name:
            self._attr_name = object_name

        self._apply_point_state(point)
        self.async_write_ha_state()

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        raise NotImplementedError


class BacnetClientPointSensor(BacnetClientPointEntityBase, SensorEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="sensor",
        )
        self._attr_native_value: StateType = None
        self._attr_native_unit_of_measurement: str | None = None
        self._attr_device_class: SensorDeviceClass | None = None
        self._attr_state_class: SensorStateClass | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        self._attr_native_unit_of_measurement = _normalize_bacnet_unit(point.get("unit"))
        self._attr_device_class = _sensor_device_class_from_unit(self._attr_native_unit_of_measurement)
        native_value = _point_native_value_from_payload(point)
        self._attr_state_class = None
        if str(point.get("type_slug") or "") in {"ai", "ao", "av"} and isinstance(native_value, (int, float)):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_value = native_value
        self._attr_extra_state_attributes = {}


class BacnetClientPointBinarySensor(BacnetClientPointEntityBase, BinarySensorEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="binary_sensor",
        )
        self._attr_is_on: bool | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        self._attr_is_on = _point_is_on(point)
        self._attr_extra_state_attributes = {}


class BacnetClientPointNumber(BacnetClientPointEntityBase, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_native_step = 0.1

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="number",
        )
        self._attr_native_value: float | None = None
        self._attr_native_unit_of_measurement: str | None = None
        self._attr_device_class: SensorDeviceClass | None = None

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        self._attr_native_unit_of_measurement = _normalize_bacnet_unit(point.get("unit"))
        self._attr_device_class = _sensor_device_class_from_unit(self._attr_native_unit_of_measurement)
        value = point.get("present_value")
        try:
            self._attr_native_value = round(float(value), 1) if value is not None else None
        except Exception:
            self._attr_native_value = None

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write_present_value(round(float(value), 1))


class BacnetClientPointSwitch(BacnetClientPointEntityBase, SwitchEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="switch",
        )
        self._attr_is_on: bool = False

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        value = _point_is_on(point)
        self._attr_is_on = bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write_present_value(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write_present_value(0)


class BacnetClientPointSelect(BacnetClientPointEntityBase, SelectEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="select",
        )
        self._attr_options: list[str] = []
        self._attr_current_option: str | None = None

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        texts = point.get("state_text")
        options: list[str] = []
        if isinstance(texts, (list, tuple)):
            options = [str(item).strip() for item in texts if str(item).strip()]
        if not options:
            count = _to_int(point.get("number_of_states")) or 0
            if count > 0:
                options = [str(idx) for idx in range(1, min(count, 128) + 1)]
        self._attr_options = options

        idx = _to_int(point.get("present_value"))
        self._attr_current_option = None
        if idx is not None and options:
            pos = int(idx) - 1
            if 0 <= pos < len(options):
                self._attr_current_option = options[pos]

    async def async_select_option(self, option: str) -> None:
        point = self._get_point()
        texts = point.get("state_text")
        options = list(self.options or [])
        value_index: int | None = None
        if option in options:
            value_index = options.index(option) + 1
        elif isinstance(texts, (list, tuple)):
            normalized = [str(item).strip() for item in texts]
            if option in normalized:
                value_index = normalized.index(option) + 1
        if value_index is None:
            maybe_int = _to_int(option)
            if maybe_int is None:
                raise HomeAssistantError(f"Unsupported option: {option}")
            value_index = int(maybe_int)
        await self._async_write_present_value(int(value_index))


class BacnetClientPointText(BacnetClientPointEntityBase, TextEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="text",
        )
        self._attr_native_value: str | None = None

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        value = _point_native_value_from_payload(point)
        self._attr_native_value = None if value is None else str(value)

    async def async_set_value(self, value: str) -> None:
        point = self._get_point()
        if not _point_is_writable(point):
            raise HomeAssistantError("Point is read-only")
        await self._async_write_present_value(str(value))


