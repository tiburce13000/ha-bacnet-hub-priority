"""Bouton « Lire le Priority Array » — une entité par device client.

Fork magliaral/ha-bacnet-hub.

CAS BACnet Explorer affiche les 16 niveaux du Priority Array d'un objet. Home
Assistant n'exposait rien de tel : la donnée transitait déjà (`priorityArray`
figure dans les propriétés demandées à l'import) mais elle était réduite à un
booléen puis jetée.

Ce bouton en fait une **photo à la demande** : à la pression, il lit le Priority
Array de tous les points commandables du device et l'expose en attributs.

Deux règles de conception, héritées de la régression v1.0.4 :

- **Aucune lecture réseau ne conditionne une écriture.** Ce bouton est un outil
  de diagnostic ; il ne pilote rien et n'est appelé par aucun chemin d'écriture.
- **Un échec de lecture renvoie `None` franc**, jamais une valeur de repli. La
  lecture précédente est alors conservée pour le point concerné, et l'attribut
  `errors` signale ce qui a échoué.

La lecture est **séquentielle** : sur une liaison MS/TP, des requêtes simultanées
vers un même automate allongent les aller-retours au lieu de les raccourcir.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .const import DOMAIN, client_display_name
from .client_runtime import (
    PRIORITY_ARRAY_SIZE,
    _client_cache_get,
    _client_points_signal,
    _entry_client_points,
    _read_client_point_priority_array,
    _safe_text,
)
from .release_button_entity import COMMANDABLE_TYPE_SLUGS

_LOGGER = logging.getLogger(__name__)

# Respiration entre deux points. Laisse le jeton MS/TP circuler.
_INTER_POINT_DELAY_SECONDS = 0.2


class BacnetClientPriorityArrayButton(ButtonEntity):
    """Lit à la demande le Priority Array des points commandables d'un device."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:format-list-numbered"
    _attr_should_poll = False

    def __init__(
        self,
        hass,
        entry_id: str,
        client_id: str,
        client_instance: int,
    ) -> None:
        self.hass = hass
        self._entry_id = str(entry_id)
        self._client_id = str(client_id)
        self._client_instance = int(client_instance)

        self._attr_unique_id = f"{self._entry_id}-{self._client_id}-priority-array"
        self._attr_name = "Lire le Priority Array"

        # Dernière photo connue, par point. Conservée d'une lecture à l'autre :
        # un point qui échoue garde sa valeur précédente plutôt que de disparaître.
        self._points: dict[str, dict[str, Any]] = {}
        self._last_read: str | None = None
        self._last_duration: float | None = None
        self._errors: list[str] = []
        self._reading = False

    @property
    def device_info(self) -> DeviceInfo:
        diag_cache = _client_cache_get(self.hass, self._entry_id, self._client_id)
        return DeviceInfo(
            identifiers={(DOMAIN, self._client_id)},
            via_device=(DOMAIN, self._entry_id),
            name=str(
                diag_cache.get("name") or client_display_name(self._client_instance)
            ),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_read": self._last_read,
            "read_duration_seconds": self._last_duration,
            "points_count": len(self._points),
            "errors": list(self._errors),
            "points": dict(self._points),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                _client_points_signal(self._entry_id, self._client_id),
                self._handle_points_updated,
            )
        )

    @callback
    def _handle_points_updated(self, _payload=None) -> None:
        """Le nom du device peut avoir changé ; rien à recalculer ici."""
        self.async_write_ha_state()

    def _commandable_points(self) -> list[tuple[str, dict[str, Any]]]:
        per_entry = _entry_client_points(self.hass, self._entry_id)
        point_cache = per_entry.get(self._client_id) or {}
        result: list[tuple[str, dict[str, Any]]] = []
        for point_key, point in sorted(point_cache.items()):
            data = dict(point or {})
            type_slug = str(data.get("type_slug") or "").strip().lower()
            if type_slug in COMMANDABLE_TYPE_SLUGS:
                result.append((str(point_key), data))
        return result

    async def async_press(self) -> None:
        """Lit le Priority Array de chaque point commandable, séquentiellement."""
        if self._reading:
            _LOGGER.debug(
                "Lecture du Priority Array déjà en cours pour %s, appui ignoré",
                self._client_id,
            )
            return

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            self._errors = ["bacnet_app_unavailable"]
            self.async_write_ha_state()
            _LOGGER.warning(
                "Priority Array : application BACnet indisponible pour %s",
                self._client_id,
            )
            return

        points = self._commandable_points()
        if not points:
            self._errors = ["no_commandable_point"]
            self.async_write_ha_state()
            return

        self._reading = True
        errors: list[str] = []
        started = time.monotonic()
        try:
            for index, (point_key, data) in enumerate(points):
                object_identifier = _safe_text(data.get("object_identifier"))
                address = _safe_text(data.get("client_address"))
                if not object_identifier or not address:
                    errors.append(f"{point_key}: target_missing")
                    continue

                values = await _read_client_point_priority_array(
                    app, address, object_identifier
                )

                if values is None:
                    # Échec franc : la photo précédente de CE point est conservée.
                    errors.append(f"{point_key}: read_failed")
                    continue

                active = [
                    level
                    for level in range(1, PRIORITY_ARRAY_SIZE + 1)
                    if values[level - 1] is not None
                ]
                self._points[point_key] = {
                    "object": object_identifier,
                    "name": _safe_text(data.get("object_name")),
                    "priorities": values,
                    "active_levels": active,
                    "highest_active": active[0] if active else None,
                }

                if index < len(points) - 1:
                    await asyncio.sleep(_INTER_POINT_DELAY_SECONDS)
        finally:
            self._reading = False

        self._last_duration = round(time.monotonic() - started, 2)
        self._last_read = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._errors = errors
        self.async_write_ha_state()

        _LOGGER.debug(
            "Priority Array lu pour %s : %d point(s), %d échec(s), %.2f s",
            self._client_id,
            len(points) - len(errors),
            len(errors),
            self._last_duration,
        )
