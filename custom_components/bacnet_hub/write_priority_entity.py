"""Entité select 'Priorité d'écriture' (device-level) pour chaque device client.

Fork magliaral/ha-bacnet-hub. Modèle repris de CervezaStallone :
- Une entité select par device Distech découvert.
- Désactivée par défaut (l'utilisateur l'active manuellement s'il veut changer la priorité).
- Catégorie CONFIG. Options 8..16, défaut 16.
- Persistée via RestoreEntity (survit aux redémarrages).
- Changer la valeur met à jour la priorité utilisée par TOUTES les écritures
  commandables de ce device.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, client_display_name
from .client_runtime import _client_cache_get
from .write_priority import (
    DEFAULT_WRITE_PRIORITY,
    WRITE_PRIORITY_OPTIONS,
    get_write_priority,
    set_write_priority,
)

_LOGGER = logging.getLogger(__name__)

_PRIORITY_OPTIONS: list[str] = [str(p) for p in WRITE_PRIORITY_OPTIONS]


class BacnetClientWritePrioritySelect(SelectEntity, RestoreEntity):
    """Sélecteur de priorité d'écriture BACnet, un par device client (device-level)."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:priority-high"
    _attr_options = _PRIORITY_OPTIONS

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

        self._attr_unique_id = f"{self._entry_id}-{self._client_id}-write-priority"
        self._attr_name = "Priorité d'écriture"

        current = get_write_priority(hass, self._entry_id, self._client_id)
        self._attr_current_option = str(current)

    @property
    def device_info(self) -> DeviceInfo:
        diag_cache = _client_cache_get(self.hass, self._entry_id, self._client_id)
        return DeviceInfo(
            identifiers={(DOMAIN, self._client_id)},
            via_device=(DOMAIN, self._entry_id),
            name=str(diag_cache.get("name") or client_display_name(self._client_instance)),
        )

    async def async_added_to_hass(self) -> None:
        """Restaure la dernière priorité choisie au démarrage."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in _PRIORITY_OPTIONS:
            validated = set_write_priority(
                self.hass, self._entry_id, self._client_id, last_state.state
            )
            self._attr_current_option = str(validated)
            _LOGGER.debug(
                "Priorité d'écriture restaurée à %s pour le device %s",
                validated,
                self._client_id,
            )
        else:
            # Aligne le store sur la valeur par défaut affichée.
            set_write_priority(
                self.hass, self._entry_id, self._client_id, self._attr_current_option
            )

    async def async_select_option(self, option: str) -> None:
        """Met à jour la priorité d'écriture pour ce device."""
        validated = set_write_priority(self.hass, self._entry_id, self._client_id, option)
        self._attr_current_option = str(validated)
        self.async_write_ha_state()
        _LOGGER.debug(
            "Priorité d'écriture changée à %s pour le device %s",
            validated,
            self._client_id,
        )
