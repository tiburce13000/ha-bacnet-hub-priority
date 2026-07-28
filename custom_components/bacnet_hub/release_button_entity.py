"""Bouton « Relâcher » pour les points BACnet commandables.

Fork magliaral/ha-bacnet-hub.

Écrit **Null** dans le Priority Array du point, à la priorité configurée par le
sélecteur « Priorité d'écriture » du device. L'automate (ex. Distech) reprend alors
la main avec sa propre valeur inscrite à un niveau inférieur.

Indispensable pour :
- rendre la main à l'automate en fin de mode (ex. sortie du mode été) ;
- la sécurité : si une sonde de référence devient indisponible, on relâche
  plutôt que de laisser une commande figée.

Note d'implémentation : ce bouton réutilise l'adressage et le suivi de
disponibilité de la classe de base, mais PAS sa machinerie d'état :
- il n'enregistre pas de souscription COV (inutile pour une action, et cela
  ferait doublon avec l'entité principale du même point) ;
- il n'a pas d'état à appliquer (`_apply_point_state` est un no-op) ;
- il conserve son propre nom (la base réécrit sinon le nom depuis l'objet BACnet).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .client_point_entities import BacnetClientPointEntityBase
from .client_runtime import _client_points_signal

_LOGGER = logging.getLogger(__name__)

# Types d'objets BACnet disposant d'un Priority Array (commandables).
COMMANDABLE_TYPE_SLUGS: set[str] = {"ao", "bo", "av", "bv", "mv"}


class BacnetClientPointReleaseButton(BacnetClientPointEntityBase, ButtonEntity):
    """Bouton qui relâche la commande (écrit Null) sur un point commandable."""

    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:lock-open-variant-outline"

    def __init__(
        self,
        hass,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass=hass,
            entry_id=entry_id,
            client_id=client_id,
            client_instance=client_instance,
            point_key=point_key,
            entity_domain="button",
        )
        # Identifiant suffixé pour ne pas entrer en collision avec l'entité
        # principale (number/switch/select) du même point.
        self._attr_unique_id = f"{self._attr_unique_id}-release"
        base_name = getattr(self, "_attr_name", None) or "Point"
        self._release_name = f"{base_name} — Relâcher"
        self._attr_name = self._release_name

    async def async_added_to_hass(self) -> None:
        """Suivi de disponibilité uniquement, sans souscription COV.

        On n'appelle PAS `super().async_added_to_hass()` : la base enregistre une
        souscription COV et réécrit le nom de l'entité, ce qui n'a pas de sens
        pour un bouton d'action.
        """
        self._unsub_points_dispatcher = async_dispatcher_connect(
            self.hass,
            _client_points_signal(self._entry_id, self._client_id),
            self._handle_points_update,
        )
        self._handle_points_update()

    @callback
    def _handle_points_update(self) -> None:
        """Met à jour la seule disponibilité, en conservant le nom du bouton."""
        point = self._get_point()
        if not point:
            return
        self._attr_available = not bool(point.get(self._POINT_UNAVAILABLE_KEY, False))
        self._attr_name = self._release_name
        self.async_write_ha_state()

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        """Un bouton n'a pas d'état à appliquer."""
        return None

    async def async_press(self) -> None:
        """Relâche la commande : écrit Null à la priorité configurée."""
        await self._async_release_present_value()
        _LOGGER.debug("Relâchement demandé pour %s", self._point_key)
