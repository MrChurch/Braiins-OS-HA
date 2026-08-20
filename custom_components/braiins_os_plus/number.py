# custom_components/braiins_os_plus/number.py
"""Braiins OS+ integration number entities."""

import re

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HASHRATE_STEP,
    CONF_POWER_STEP,
    DEFAULT_HASHRATE_STEP,
    DEFAULT_POWER_STEP,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Number platform for Braiins OS."""
    # Unpack the dictionary created in __init__.py
    domain_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = domain_data["coordinator"]
    api = domain_data["api"]

    entities = [
        BraiinsPowerTargetNumber(coordinator, api, entry),
        BraiinsPowerStepNumber(coordinator, entry),
        BraiinsHashrateTargetNumber(coordinator, api, entry),
        BraiinsHashrateStepNumber(coordinator, entry),
    ]

    if api.is_legacy:
        performance = coordinator.data.get("legacy_performance", {})
        metadata = performance.get("metadata", {})
        frequency_metadata = metadata.get("frequency", {})
        voltage_metadata = metadata.get("voltage", {})
        metadata_complete = all(
            item.get("default") is not None
            and item.get("min") is not None
            and item.get("max") is not None
            for item in (frequency_metadata, voltage_metadata)
        )
        if metadata_complete:
            entities.extend(
                [
                    BraiinsLegacyFrequencyNumber(coordinator, api, entry),
                    BraiinsLegacyVoltageNumber(coordinator, api, entry),
                ]
            )
            for hash_chain in performance.get("current", {}).get("hashChains", []):
                name = hash_chain.get("name")
                if name:
                    entities.extend(
                        [
                            BraiinsLegacyHashChainFrequencyNumber(
                                coordinator, api, entry, name
                            ),
                            BraiinsLegacyHashChainVoltageNumber(
                                coordinator, api, entry, name
                            ),
                        ]
                    )

    async_add_entities(entities)


class BraiinsLegacyPerformanceNumber(CoordinatorEntity, NumberEntity):
    """Locally staged legacy performance value."""

    performance_key: str
    metadata_key: str
    entity_suffix: str
    _attr_unit: str

    def __init__(self, coordinator, api, entry: ConfigEntry) -> None:
        """Initialize a staged legacy performance number."""
        super().__init__(coordinator)
        self._api = api
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self.entity_suffix}"
        self._attr_has_entity_name = True
        self._attr_mode = NumberMode.BOX
        self._attr_native_unit_of_measurement = self._attr_unit
        data = coordinator.data.get("details", {})
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": data.get("hostname", "Braiins Miner"),
        }

    @property
    def _performance_data(self) -> dict:
        """Return current legacy performance metadata and values."""
        return self.coordinator.data.get("legacy_performance", {})

    @property
    def _metadata(self) -> dict:
        """Return metadata for this value."""
        return self._performance_data.get("metadata", {}).get(self.metadata_key, {})

    @property
    def native_min_value(self) -> float:
        """Return the device-provided minimum value."""
        value = self._metadata.get("min")
        if value is None:
            value = self._metadata.get("default")
        return float(value)

    @property
    def native_max_value(self) -> float:
        """Return the device-provided maximum value."""
        value = self._metadata.get("max")
        if value is None:
            value = self._metadata.get("default")
        return float(value)

    @property
    def native_step(self) -> float:
        """Return the device-provided step size."""
        return float(self._metadata.get("step") or 1.0)

    @property
    def native_value(self) -> float | None:
        """Return a staged value, then the current value, then the device default."""
        performance = self._performance_data
        pending = performance.get("pending", {})
        current = performance.get("current", {})
        value = pending.get(self.performance_key, current.get(self.performance_key))
        if value is None:
            value = self._metadata.get("default")
        return float(value) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Stage a value locally; the Apply button performs the miner write."""
        if float(value) > self.native_max_value:
            raise HomeAssistantError(
                f"{self._attr_name} must not exceed {self.native_max_value:g} "
                f"{self._attr_native_unit_of_measurement}."
            )
        new_data = dict(self.coordinator.data)
        performance = dict(new_data.get("legacy_performance", {}))
        pending = dict(performance.get("pending", {}))
        pending[self.performance_key] = float(value)
        performance["pending"] = pending
        staged_performance = self._api.update_pending_performance(
            {self.performance_key: float(value)}
        )
        if staged_performance:
            performance = staged_performance
        new_data["legacy_performance"] = performance
        self.coordinator.async_set_updated_data(new_data)


class BraiinsLegacyFrequencyNumber(BraiinsLegacyPerformanceNumber):
    """Locally staged legacy global frequency."""

    performance_key = "globalFrequency"
    metadata_key = "frequency"
    entity_suffix = "legacy_frequency"
    _attr_name = "Frequency"
    _attr_unit = "MHz"
    _attr_icon = "mdi:sine-wave"


class BraiinsLegacyVoltageNumber(BraiinsLegacyPerformanceNumber):
    """Locally staged legacy global voltage."""

    performance_key = "globalVoltage"
    metadata_key = "voltage"
    entity_suffix = "legacy_voltage"
    _attr_name = "Voltage"
    _attr_unit = "V"
    _attr_icon = "mdi:flash-outline"


class BraiinsLegacyHashChainNumber(BraiinsLegacyPerformanceNumber):
    """Locally staged performance value for one legacy hash chain."""

    def __init__(self, coordinator, api, entry: ConfigEntry, hash_chain_name: str) -> None:
        """Initialize a hash-chain performance number."""
        self._hash_chain_name = hash_chain_name
        super().__init__(coordinator, api, entry)
        suffix = re.sub(r"[^a-z0-9]+", "_", hash_chain_name.lower()).strip("_")
        self._attr_unique_id = (
            f"{entry.entry_id}_{self.entity_suffix}_{suffix or 'unknown'}"
        )
        self._attr_name = f"{self._attr_name} {hash_chain_name}"

    @property
    def _current_hash_chain(self) -> dict:
        """Return the current value for this hash chain."""
        for chain in self._performance_data.get("current", {}).get("hashChains", []):
            if chain.get("name") == self._hash_chain_name:
                return chain
        return {}

    @property
    def _pending_hash_chain(self) -> dict:
        """Return staged values for this hash chain."""
        for chain in self._performance_data.get("pending", {}).get("hashChains", []):
            if chain.get("name") == self._hash_chain_name:
                return chain
        return {}

    @property
    def native_value(self) -> float | None:
        """Return staged, current, or default value for this hash chain."""
        value = self._pending_hash_chain.get(self.performance_key)
        if value is None:
            value = self._current_hash_chain.get(self.performance_key)
        if value is None:
            value = self._metadata.get("default")
        return float(value) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Stage a value locally; the Apply button performs the miner write."""
        if float(value) > self.native_max_value:
            raise HomeAssistantError(
                f"{self._attr_name} must not exceed {self.native_max_value:g} "
                f"{self._attr_native_unit_of_measurement}."
            )
        new_data = dict(self.coordinator.data)
        performance = dict(new_data.get("legacy_performance", {}))
        pending = dict(performance.get("pending", {}))
        chains = {
            chain.get("name"): dict(chain)
            for chain in pending.get("hashChains", [])
            if chain.get("name")
        }
        chain = chains.setdefault(self._hash_chain_name, {"name": self._hash_chain_name})
        chain[self.performance_key] = float(value)
        pending["hashChains"] = list(chains.values())
        performance["pending"] = pending
        staged_performance = self._api.update_pending_hash_chain(
            self._hash_chain_name, {self.performance_key: float(value)}
        )
        if staged_performance:
            performance = staged_performance
        new_data["legacy_performance"] = performance
        self.coordinator.async_set_updated_data(new_data)


class BraiinsLegacyHashChainFrequencyNumber(BraiinsLegacyHashChainNumber):
    """Locally staged frequency for one legacy hash chain."""

    performance_key = "frequency"
    metadata_key = "frequency"
    entity_suffix = "legacy_hashchain_frequency"
    _attr_name = "Hashboard Frequency"
    _attr_unit = "MHz"
    _attr_icon = "mdi:sine-wave"


class BraiinsLegacyHashChainVoltageNumber(BraiinsLegacyHashChainNumber):
    """Locally staged voltage for one legacy hash chain."""

    performance_key = "voltage"
    metadata_key = "voltage"
    entity_suffix = "legacy_hashchain_voltage"
    _attr_name = "Hashboard Voltage"
    _attr_unit = "V"
    _attr_icon = "mdi:flash-outline"


class BraiinsHashrateTargetNumber(CoordinatorEntity, NumberEntity):
    """Number entity to set the Braiins OS hashrate target."""

    _attr_has_entity_name = True
    _attr_name = "Hashrate Target"
    _attr_native_unit_of_measurement = "TH/s"
    _attr_icon = "mdi:speedometer"
    _attr_native_step = 1

    def __init__(self, coordinator, api, entry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self.api = api
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_hashrate_target"
        # Use shared device info logic
        data = self.coordinator.data.get("details", {})
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": data.get("hostname", "Braiins Miner"),
        }

    @property
    def native_min_value(self) -> float:
        """Return min TH/s from tuner_constraints."""
        try:
            # We cast the constraint to int
            return float(
                int(
                    float(
                        self.coordinator.data["constraints"]["tuner_constraints"][
                            "hashrate_target"
                        ]["min"]["terahash_per_second"]
                    )
                )
            )
        except (KeyError, TypeError):
            return 10.0

    @property
    def native_max_value(self) -> float:
        """Return max TH/s from tuner_constraints."""
        try:
            return float(
                int(
                    float(
                        self.coordinator.data["constraints"]["tuner_constraints"][
                            "hashrate_target"
                        ]["max"]["terahash_per_second"]
                    )
                )
            )
        except (KeyError, TypeError):
            return 300.0

    @property
    def native_value(self) -> int | None:
        """Return the current hashrate target fetched from the miner."""
        val = self.coordinator.data.get("hashrate_target")
        return int(val) if val is not None else None

    @property
    def available(self) -> bool:
        """Only available if Hashrate Target mode is active."""
        return (
            super().available
            and self.coordinator.data.get("performance_mode") == "Hashrate Target"
        )

    async def async_set_native_value(self, value: float) -> None:
        """Send the new hashrate target to the miner."""
        target = int(value)
        success = await self.api.set_hashrate_target(target)
        if success and self.coordinator.data:
            new_data = dict(self.coordinator.data)
            new_data["hashrate_target"] = target
            self.coordinator.async_set_updated_data(new_data)


class BraiinsHashrateStepNumber(NumberEntity):
    """Local configuration entity to set the hashrate adjustment step."""

    _attr_has_entity_name = True
    _attr_name = "Hashrate Adjustment Step"
    _attr_native_unit_of_measurement = "TH/s"
    _attr_icon = "mdi:unfold-more-horizontal"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 100.0
    _attr_native_step = 10

    def __init__(self, coordinator, entry) -> None:
        """Initialize the number entity."""
        self.coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_hashrate_step_config"
        data = self.coordinator.data.get("details", {})
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": data.get("hostname", "Braiins Miner"),
        }

    @property
    def native_value(self) -> float:
        """Return the hashrate step value from integration options."""
        return int(self._entry.options.get(CONF_HASHRATE_STEP, DEFAULT_HASHRATE_STEP))

    async def async_set_native_value(self, value: float) -> None:
        """Update the internal hashrate step value in Config Entry options."""
        new_options = dict(self._entry.options)
        new_options[CONF_HASHRATE_STEP] = int(value)
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)


class BraiinsPowerStepNumber(NumberEntity):
    """Local configuration entity to set the increment/decrement step."""

    _attr_has_entity_name = True
    _attr_name = "Power Adjustment Step"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:unfold-more-horizontal"

    # Based on your dps_constraints: min 1, max 1000
    _attr_native_min_value = 1
    _attr_native_max_value = 1000
    _attr_native_step = 1

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        self.coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_power_step_config"
        # Shared device info logic
        data = self.coordinator.data.get("details", {})
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": data.get("hostname", "Braiins Miner"),
        }

    @property
    def native_value(self) -> int:
        """Return the step value from integration options."""
        return self._entry.options.get(CONF_POWER_STEP, DEFAULT_POWER_STEP)

    async def async_set_native_value(self, value: float) -> None:
        """Update the internal step value in Config Entry options."""
        new_options = dict(self._entry.options)
        new_options[CONF_POWER_STEP] = int(value)
        # This saves the value permanently
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)


class BraiinsPowerTargetNumber(CoordinatorEntity, NumberEntity):
    """Number entity to set the Braiins OS power target."""

    _attr_has_entity_name = True
    _attr_name = "Power Target"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:lightning-bolt"

    _attr_native_min_value = 780  # Minimum Watts
    _attr_native_max_value = 6400  # Maximum Watts
    _attr_native_step = 1  # Allows adjustments in increments of 10W

    def __init__(self, coordinator, api, entry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self.api = api

        # Link this entity to the existing Braiins OS device in HA
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}
        self._attr_unique_id = f"{entry.entry_id}_power_target"

    @property
    def native_value(self):
        """Return the current power target fetched from the miner."""
        if self.coordinator.data:
            return self.coordinator.data.get("power_target")
        return None

    @property
    def available(self) -> bool:
        """Only available if Power Target mode is active."""
        return (
            super().available
            and self.coordinator.data.get("performance_mode") == "Power Target"
        )

    async def async_set_native_value(self, value: float) -> None:
        """Send the new power target to the miner."""
        watt_value = int(value)

        success = await self.api.set_power_target(watt_value)

        if success:
            # Optimistically update the coordinator data without a full API poll
            if self.coordinator.data is not None:
                new_data = dict(self.coordinator.data)
                new_data["power_target"] = watt_value

                self.coordinator.async_set_updated_data(new_data)

    @property
    def native_min_value(self) -> float:
        """Return min watts from tuner_constraints."""
        try:
            return float(
                self.coordinator.data["constraints"]["tuner_constraints"][
                    "power_target"
                ]["min"]["watt"]
            )
        except (KeyError, TypeError):
            return 780.0

    @property
    def native_max_value(self) -> float:
        """Return max watts from tuner_constraints."""
        try:
            return float(
                self.coordinator.data["constraints"]["tuner_constraints"][
                    "power_target"
                ]["max"]["watt"]
            )
        except (KeyError, TypeError):
            return 6500.0

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for this miner."""
        data = self.coordinator.data.get("details", {})
        ident = data.get("miner_identity", {})

        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name=data.get("hostname")
            or f"Braiins OS+ Miner ({self.coordinator.config_entry.data['miner_ip']})",
            manufacturer="Braiins",
            model=ident.get("miner_model") or "Miner with Braiins OS+",
            sw_version=data.get("bos_version", {}).get("current"),
        )
