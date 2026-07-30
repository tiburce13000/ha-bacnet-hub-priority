"""Plateforme `button` — Fork magliaral/ha-bacnet-hub.

Crée un bouton « Relâcher » pour chaque point BACnet commandable disposant d'un
Priority Array (ao, bo, av, bv, mv). Le bouton écrit Null à la priorité configurée
par le sélecteur « Priorité d'écriture » du device, rendant la main à l'automate.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client_runtime import _entry_client_points, _entry_points_signal, _to_int
from .release_button_entity import (
    COMMANDABLE_TYPE_SLUGS,
    BacnetClientPointReleaseButton,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crée les boutons « Relâcher » pour les points commandables."""

    added: set[tuple[str, str]] = set()

    @callback
    def _add_missing(_payload=None) -> None:
        entities: list[Any] = []
        per_entry = _entry_client_points(hass, entry.entry_id)
        for client_id, point_cache in per_entry.items():
            for point_key, point in sorted(point_cache.items()):
                data = dict(point or {})
                type_slug = str(data.get("type_slug") or "").strip().lower()
                # Fork v1.0.5 — NIVEAU 1 : création fondée sur le seul type d'objet.
                # Auparavant, une lecture ratée de priorityArray faisait disparaître
                # le bouton Relâcher de l'appareil.
                if type_slug not in COMMANDABLE_TYPE_SLUGS:
                    continue

                key = (str(client_id), str(point_key))
                if key in added:
                    continue

                client_instance = _to_int(data.get("client_instance"))
                if client_instance is None:
                    client_instance = _to_int(str(client_id).split("_")[-1]) or 0

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

        if entities:
            async_add_entities(entities)

    _add_missing()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, _entry_points_signal(entry.entry_id), _add_missing
        )
    )
