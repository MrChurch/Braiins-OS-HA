"""Braiins OS+ legacy hashboard switch entities."""

import re

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BraiinsAPI
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up legacy hashboard enable switches."""
    data = hass.data[DOMAIN][entry.entry_id]
    api: BraiinsAPI = data["api"]
    coordinator = data["coordinator"]

    if not api.is_legacy:
        async_add_entities([])
        return

    entities = []
    for hash_chain in coordinator.data.get("legacy_performance", {}).get(
        "current", {}
    ).get("hashChains", []):
        name = hash_chain.get("name")
        if name:
            entities.append(BraiinsLegacyHashChainSwitch(coordinator, api, entry, name))
    async_add_entities(entities)


class BraiinsLegacyHashChainSwitch(CoordinatorEntity, SwitchEntity):
    """Locally staged enable/disable switch for one legacy hashboard."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:power"

    def __init__(self, coordinator, api: BraiinsAPI, entry: ConfigEntry, name: str) -> None:
        """Initialize a hashboard enable switch."""
        super().__init__(coordinator)
        self._api = api
        self._entry = entry
        self._hash_chain_name = name
        suffix = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        self._attr_unique_id = f"{entry.entry_id}_hashchain_enabled_{suffix or 'unknown'}"
        self._attr_name = f"Hashboard {name}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": coordinator.data.get("details", {}).get(
                "hostname", "Braiins Miner"
            ),
        }

    @property
    def _performance_data(self) -> dict:
        """Return the current and staged performance data."""
        return self.coordinator.data.get("legacy_performance", {})

    def _find_hash_chain(self, key: str) -> dict:
        """Find this hash chain in current or pending data."""
        for chain in self._performance_data.get(key, {}).get("hashChains", []):
            if chain.get("name") == self._hash_chain_name:
                return chain
        return {}

    @property
    def is_on(self) -> bool | None:
        """Return the staged or current enabled state."""
        pending = self._find_hash_chain("pending")
        current = self._find_hash_chain("current")
        value = pending.get("enabled", current.get("enabled"))
        return bool(value) if value is not None else None

    async def async_turn_on(self, **kwargs) -> None:
        """Stage this hashboard as enabled."""
        await self._stage_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Stage this hashboard as disabled."""
        await self._stage_enabled(False)

    async def _stage_enabled(self, enabled: bool) -> None:
        """Stage an enabled value without writing to the miner yet."""
        new_data = dict(self.coordinator.data)
        performance = dict(new_data.get("legacy_performance", {}))
        pending = dict(performance.get("pending", {}))
        chains = {
            chain.get("name"): dict(chain)
            for chain in pending.get("hashChains", [])
            if chain.get("name")
        }
        chain = chains.setdefault(self._hash_chain_name, {"name": self._hash_chain_name})
        chain["enabled"] = enabled
        pending["hashChains"] = list(chains.values())
        performance["pending"] = pending
        self._api.update_pending_hash_chain(
            self._hash_chain_name, {"enabled": enabled}
        )
        new_data["legacy_performance"] = performance
        self.coordinator.async_set_updated_data(new_data)

