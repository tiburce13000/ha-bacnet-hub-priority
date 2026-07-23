"""Gestion de la priorité d'écriture BACnet par device client (device-level).

Fork magliaral/ha-bacnet-hub : reprend le modèle "Write Priority" device-level de
CervezaStallone. Un seul sélecteur de priorité par device Distech, désactivé par
défaut, qui s'applique à toutes les écritures commandables (AO/BO/AV/BV/MV avec
Priority Array) de ce device.

Défaut = 16 (comportement d'origine inchangé tant que l'utilisateur ne touche à rien).
Niveaux proposés = 8..16 (8 = Manual Operator).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Niveau par défaut : 16 (le plus bas / le plus sûr). Comportement d'origine.
DEFAULT_WRITE_PRIORITY = 16
# Niveaux proposés dans le sélecteur (8 = Manual Operator ... 16 = le plus bas).
WRITE_PRIORITY_OPTIONS: list[int] = [8, 9, 10, 11, 12, 13, 14, 15, 16]

_STORE_KEY = "write_priority"


def _store(hass: HomeAssistant) -> dict[str, dict[str, int]]:
    """Racine de stockage : {entry_id: {client_id: priorité}}."""
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault(_STORE_KEY, {})


def get_write_priority(hass: HomeAssistant, entry_id: str, client_id: str) -> int:
    """Priorité d'écriture courante pour ce device client (défaut 16)."""
    per_entry = _store(hass).get(str(entry_id), {})
    value = per_entry.get(str(client_id))
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WRITE_PRIORITY
    if ivalue in WRITE_PRIORITY_OPTIONS:
        return ivalue
    return DEFAULT_WRITE_PRIORITY


def set_write_priority(
    hass: HomeAssistant, entry_id: str, client_id: str, priority: Any
) -> int:
    """Enregistre la priorité pour ce device client. Retourne la valeur validée."""
    try:
        ivalue = int(priority)
    except (TypeError, ValueError):
        ivalue = DEFAULT_WRITE_PRIORITY
    if ivalue not in WRITE_PRIORITY_OPTIONS:
        ivalue = DEFAULT_WRITE_PRIORITY
    per_entry = _store(hass).setdefault(str(entry_id), {})
    per_entry[str(client_id)] = ivalue
    return ivalue
