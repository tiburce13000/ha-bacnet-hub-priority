"""Plateforme `button` — Fork magliaral/ha-bacnet-hub.

Deux familles de boutons :

- « Relâcher », un par point BACnet commandable (ao, bo, av, bv, mv). Écrit Null à
  la priorité configurée par le sélecteur « Priorité d'écriture » du device,
  rendant la main à l'automate.
- « Lire le Priority Array » (v1.1.0), un par device. Lit à la demande les
  16 niveaux de chaque point commandable et les expose en attributs.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client_runtime import _entry_client_points, _entry_points_signal, _to_int
from .priority_array_button import BacnetClientPriorityArrayButton
from .release_button_entity import (
    COMMANDABLE_TYPE_SLUGS,
    BacnetClientPointReleaseButton,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crée les boutons « Relâcher » (par point) et « Lire le Priority Array » (par device)."""

    added: set[tuple[str, str]] = set()
    added_devices: set[str] = set()

    @callback
    def _add_missing(_payload=None) -> None:
        entities: list[Any] = []
        per_entry = _entry_client_points(hass, entry.entry_id)
        for client_id, point_cache in per_entry.items():
            # v1.1.0 — un bouton de lecture du Priority Array par device, créé dès
            # qu'un point commandable existe. Comme le bouton « Relâcher », il ne
            # dépend PAS d'une lecture réussie de priorityArray : c'est ce couplage
            # qui faisait disparaître des entités en v1.0.4.
            device_instance: int | None = None
            has_commandable = False

            for point_key, point in sorted(point_cache.items()):
                data = dict(point or {})
                type_slug = str(data.get("type_slug") or "").strip().lower()
                # Fork v1.0.5 — NIVEAU 1 : création fondée sur le seul type d'objet.
                # Auparavant, une lecture ratée de priorityArray faisait disparaître
                # le bouton Relâcher de l'appareil.
                if type_slug not in COMMANDABLE_TYPE_SLUGS:
                    continue

                client_instance = _to_int(data.get("client_instance"))
                if client_instance is None:
                    client_instance = _to_int(str(client_id).split("_")[-1]) or 0

                # Relevé avant le filtre `added` : le bouton de device doit pouvoir
                # être créé même sur un rappel où tous les points sont déjà connus.
                has_commandable = True
                if device_instance is None:
                    device_instance = int(client_instance)

                key = (str(client_id), str(point_key))
                if key in added:
                    continue

                entities.append(
                    BacnetClientPointReleaseButton(
                        hass=hass,
                        entry_id=entry.entry_id,
                        client_id=str(client_id),
                        client_instance=int(client_instance),
                        point_key=str(point_key),
                    )
                )
                added.add(key)

            if has_commandable and str(client_id) not in added_devices:
                entities.append(
                    BacnetClientPriorityArrayButton(
                        hass=hass,
                        entry_id=entry.entry_id,
                        client_id=str(client_id),
                        client_instance=int(device_instance or 0),
                    )
                )
                added_devices.add(str(client_id))

        if entities:
            async_add_entities(entities)

    _add_missing()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, _entry_points_signal(entry.entry_id), _add_missing
        )
    )
