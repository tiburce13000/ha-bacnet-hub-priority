"""Bouton « Relâcher » pour les points BACnet commandables.

Fork magliaral/ha-bacnet-hub.

Écrit **Null** dans le Priority Array du point, à la priorité configurée par le
sélecteur « Priorité d'écriture » du device. L'automate (ex. Distech) reprend alors
la main avec sa propre valeur inscrite à un niveau inférieur.

Indispensable pour :
- rendre la main à l'automate en fin de mode (ex. sortie du mode été) ;
- la sécurité : si une sonde de référence devient indisponible, on relâche
  plutôt que de laisser une commande figée.

Créé uniquement pour les points commandables disposant d'un Priority Array
(ao, bo, av, bv, mv). Désactivé par défaut.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory

from .client_point_entities import BacnetClientPointEntityBase

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
        # Identifiant et nom dérivés du point, suffixés pour ne pas entrer en
        # collision avec l'entité principale (number/switch/select) du même point.
        base_uid = self._attr_unique_id
        self._attr_unique_id = f"{base_uid}-release"
        base_name = self._attr_name or "Point"
        self._attr_name = f"{base_name} — Relâcher"

    async def async_press(self) -> None:
        """Relâche la commande : écrit Null à la priorité configurée."""
        await self._async_release_present_value()
        _LOGGER.debug("Relâchement demandé pour %s", self._point_key)
