# custom_components/braiins_os_plus/api.py
"""Braiins OS+ integration API client for token management and miner control."""

import asyncio
import logging
import re
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    API_MODE_LEGACY_GRAPHQL,
    LEGACY_S9_MAX_FREQUENCY,
    LEGACY_S9_MAX_VOLTAGE,
)

_LOGGER = logging.getLogger(__name__)

LEGACY_GRAPHQL_LOGIN = """
mutation ($username: String!, $password: String!) {
  auth {
    login(username: $username, password: $password) {
      ... on Error {
        message
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

LEGACY_GRAPHQL_EXTEND = """
mutation {
  auth {
    extend {
      __typename
      ... on AuthError {
        message
      }
    }
  }
}
"""

LEGACY_GRAPHQL_STATS = """
query {
  bosminer {
    metadata {
      hashChain {
        voltagePerHashChain
        frequency { default min max step unit }
        voltage { default min max step unit }
      }
    }
    info {
      modelName
        summary {
        realHashrate { mhsAv mhs5S mhs1M mhs5M mhs15M mhs24H }
        poolStatus
        shares {
          acceptedDifficulty
          acceptedSolutions
          rejectedDifficulty
          rejectedSolutions
          rejectedRatio
          staleDifficulty
          staleSolutions
          staleRatio
        }
        foundBlocks
        bestShare
        power { limitW approxConsumptionW efficiencyWMhs }
        temperature { name degreesC }
      }
      workSolver {
        name
        childSolvers {
          name
          realHashrate { mhsAv mhs5S mhs1M mhs5M mhs15M mhs24H }
          temperatures { name degreesC }
        }
      }
      fans { name speed rpm }
    }
    config {
      __typename
      ... on BosminerConfig {
        hashChainGlobal { frequency voltage asicBoost }
        hashChains { name enabled frequency voltage }
      }
    }
  }
}
"""

LEGACY_GRAPHQL_ACTION = """
mutation {
  bosminer {
    ACTION {
      __typename
      ... on VoidResult { void }
      ... on BosminerError { message }
    }
  }
}
"""

LEGACY_GRAPHQL_PERFORMANCE = """
mutation ($perfInput: PerformanceIn!, $apply: Boolean!) {
  bosminer {
    config {
      updatePerformance(input: $perfInput, apply: $apply) {
        ... on AttributeError {
          message
          __typename
        }
        ... on PerformanceError {
          message
          hashChains {
            ... on HashChainError {
              name
              voltage
              frequency
              __typename
            }
            __typename
          }
          globalVoltage
          globalFrequency
          __typename
        }
        ... on PerformanceOut {
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}
"""


def _legacy_solver_matches_hash_chain(solver_name: str, chain_name: str) -> bool:
    """Match legacy work-solver names to hash-chain names."""
    normalized_solver = "".join(char.lower() for char in solver_name if char.isalnum())
    normalized_chain = "".join(char.lower() for char in chain_name if char.isalnum())
    if not normalized_solver or not normalized_chain:
        return False
    if normalized_solver == normalized_chain:
        return True
    solver_digits = "".join(char for char in normalized_solver if char.isdigit())
    chain_digits = "".join(char for char in normalized_chain if char.isdigit())
    return bool(solver_digits and chain_digits and solver_digits == chain_digits)


def _is_legacy_s9_model(model_name: Any) -> bool:
    """Return whether a legacy model name identifies an Antminer S9."""
    return bool(re.search(r"\bs9[a-z0-9-]*\b", str(model_name or "").lower()))


def _apply_legacy_model_limits(metadata: dict[str, Any], model_name: Any) -> dict[str, Any]:
    """Apply conservative model-specific limits to legacy metadata."""
    if not _is_legacy_s9_model(model_name):
        return metadata

    limited = dict(metadata)
    for key, maximum in (
        ("frequency", LEGACY_S9_MAX_FREQUENCY),
        ("voltage", LEGACY_S9_MAX_VOLTAGE),
    ):
        setting = dict(limited.get(key) or {})
        if setting.get("max") is None or float(setting["max"]) > maximum:
            setting["max"] = maximum
        if setting.get("default") is not None and float(setting["default"]) > maximum:
            setting["default"] = maximum
        limited[key] = setting
    return limited


def _validate_legacy_performance_limits(
    performance: dict[str, Any], model_name: Any
) -> None:
    """Reject unsafe S9 frequency or voltage values before a GraphQL write."""
    if not _is_legacy_s9_model(model_name):
        return

    checks = [
        ("globalFrequency", performance.get("globalFrequency"), LEGACY_S9_MAX_FREQUENCY, "MHz"),
        ("globalVoltage", performance.get("globalVoltage"), LEGACY_S9_MAX_VOLTAGE, "V"),
    ]
    for chain in performance.get("hashChains") or []:
        name = chain.get("name", "unknown")
        checks.extend(
            [
                (f"Hashboard {name} frequency", chain.get("frequency"), LEGACY_S9_MAX_FREQUENCY, "MHz"),
                (f"Hashboard {name} voltage", chain.get("voltage"), LEGACY_S9_MAX_VOLTAGE, "V"),
            ]
        )
    for label, value, maximum, unit in checks:
        if value is not None and float(value) > maximum:
            raise HomeAssistantError(
                f"S9 limit exceeded: {label} must not exceed {maximum:g} {unit}."
            )


def _legacy_temperature_value(
    temperatures: list[dict[str, Any]], preferred_words: tuple[str, ...]
) -> float | None:
    """Return the highest matching legacy work-solver temperature."""
    preferred = [
        float(item["degreesC"])
        for item in temperatures
        if item.get("degreesC") is not None
        and any(
            word in str(item.get("name", "")).lower()
            for word in preferred_words
        )
    ]
    if preferred:
        return max(preferred)
    values = [
        float(item["degreesC"])
        for item in temperatures
        if item.get("degreesC") is not None
    ]
    return max(values) if values else None


def _legacy_hashboard_data(
    hash_chains: list[dict[str, Any]], work_solver: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Normalize legacy work-solver child data to the REST hashboard shape."""
    if not hash_chains:
        return []

    solvers = (work_solver or {}).get("childSolvers") or []
    boards = []
    for index, hash_chain in enumerate(hash_chains):
        chain_name = str(hash_chain.get("name", index + 1))
        solver = next(
            (
                candidate
                for candidate in solvers
                if _legacy_solver_matches_hash_chain(
                    str(candidate.get("name", "")), chain_name
                )
            ),
            solvers[index]
            if len(solvers) == len(hash_chains) and index < len(solvers)
            else {},
        )
        real_hashrate = solver.get("realHashrate") or {}
        mhs_5s = real_hashrate.get("mhs5S")
        temperatures = solver.get("temperatures") or []
        board_temperature = _legacy_temperature_value(temperatures, ("board", "pcb"))
        chip_temperature = _legacy_temperature_value(
            temperatures, ("chip", "asic", "die", "core")
        )
        board = {
            "id": chain_name,
            "enabled": hash_chain.get("enabled"),
            "stats": {
                "real_hashrate": {
                    "last_5s": {
                        "gigahash_per_second": (
                            float(mhs_5s) / 1_000 if mhs_5s is not None else None
                        )
                    }
                }
            },
        }
        if board_temperature is not None:
            board["board_temp"] = {"degree_c": board_temperature}
        if chip_temperature is not None:
            board["highest_chip_temp"] = {
                "temperature": {"degree_c": chip_temperature}
            }
        boards.append(board)
    return boards


def _legacy_fan_data(fans: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize legacy GraphQL fan data to the REST cooling shape."""
    normalized = []
    for index, fan in enumerate(fans or [], start=1):
        name = str(fan.get("name") or f"Fan {index}")
        match = re.search(r"(\d+)", name)
        position = int(match.group(1)) if match else index
        speed = fan.get("speed")
        if speed is not None and float(speed) > 1:
            speed = float(speed) / 100
        normalized.append(
            {
                "position": position,
                "name": name,
                "rpm": fan.get("rpm"),
                "target_speed_ratio": speed,
            }
        )
    return normalized


async def async_legacy_login(
    session: aiohttp.ClientSession,
    url: str,
    username: str,
    password: str,
) -> tuple[bool, str | None]:
    """Log in to the legacy GraphQL API and return its session cookie."""
    try:
        async with asyncio.timeout(10):
            async with session.post(
                url,
                json={
                    "query": LEGACY_GRAPHQL_LOGIN,
                    "variables": {"username": username, "password": password},
                },
            ) as response:
                payload = await response.json(content_type=None)

                if response.status != 200:
                    return False, f"HTTP {response.status}"

                errors = payload.get("errors") or []
                if errors:
                    return False, errors[0].get("message", "GraphQL login failed")

                login_result = (
                    payload.get("data", {}).get("auth", {}).get("login", {})
                )
                if login_result.get("__typename") != "VoidResult":
                    return False, login_result.get("message", "GraphQL login failed")

                session_cookie = response.cookies.get("session_id")
                if session_cookie is None:
                    return False, "GraphQL login returned no session_id cookie"

                # Keep the cookie in the shared HA session where possible. The
                # explicit Cookie header below is still used as a fallback for
                # IP-based miners whose cookie jar rejects host-only cookies.
                session.cookie_jar.update_cookies(response.cookies, response.url)

                return True, session_cookie.value
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        return False, str(err)


class BraiinsAPI:
    """A class for handling API calls and token renewal."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, session: aiohttp.ClientSession
    ) -> None:
        """Initialize the API object."""
        self._hass = hass
        self._entry = entry
        self._session = session
        miner_ip = self._entry.data["miner_ip"]
        self._legacy = self._entry.data.get("api_mode") == API_MODE_LEGACY_GRAPHQL
        self._base_url = f"http://{miner_ip}/api/v1"
        self._graphql_url = f"http://{miner_ip}/graphql"
        self._token = self._entry.data.get("token")
        self._legacy_session_id: str | None = None
        self._legacy_performance_error: str | None = None
        self._legacy_model_name: str | None = None
        self._headers = {"Authorization": self._token}
        self._lock = asyncio.Lock()
        self._last_data = {}

    def get_cached_value(self, key: str) -> Any:
        """Public method to get a value from the internal cache."""
        return self._last_data.get(key)

    @property
    def is_legacy(self) -> bool:
        """Return whether this miner uses the legacy GraphQL API."""
        return self._legacy

    def update_last_data(self, key: str, value: Any) -> None:
        """Public method to update the internal cache for optimistic UI updates."""
        if self._last_data is not None:
            self._last_data[key] = value

    def update_pending_performance(
        self, values: dict[str, float]
    ) -> dict[str, Any]:
        """Keep locally staged legacy performance values across coordinator polls."""
        performance = dict(self._last_data.get("legacy_performance", {}))
        pending = dict(performance.get("pending", {}))
        pending.update(values)

        current = performance.get("current", {})
        global_chain_values = {
            key: value
            for key, value in (
                ("frequency", values.get("globalFrequency")),
                ("voltage", values.get("globalVoltage")),
            )
            if value is not None
        }
        if global_chain_values:
            chains = {
                chain.get("name"): dict(chain)
                for chain in current.get("hashChains", [])
                if chain.get("name")
            }
            for chain in pending.get("hashChains", []):
                if chain.get("name"):
                    chains[chain["name"]] = dict(chain)
            for chain in chains.values():
                chain.update(global_chain_values)
            if chains:
                pending["hashChains"] = list(chains.values())

        performance["pending"] = pending
        self._last_data["legacy_performance"] = performance
        return performance

    def update_pending_hash_chain(
        self, name: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep locally staged values for one legacy hash chain."""
        performance = dict(self._last_data.get("legacy_performance", {}))
        pending = dict(performance.get("pending", {}))
        current = performance.get("current", {})
        for key in ("globalFrequency", "globalVoltage"):
            if key not in pending and current.get(key) is not None:
                pending[key] = current[key]
        chains = {
            chain.get("name"): dict(chain)
            for chain in pending.get("hashChains", [])
            if chain.get("name")
        }
        current_chain = next(
            (
                chain
                for chain in current.get("hashChains", [])
                if chain.get("name") == name
            ),
            {},
        )
        chain = chains.setdefault(
            name,
            {
                key: current_chain[key]
                for key in ("name", "enabled", "frequency", "voltage")
                if current_chain.get(key) is not None
            }
            or {"name": name},
        )
        chain.update(values)
        pending["hashChains"] = list(chains.values())
        performance["pending"] = pending
        self._last_data["legacy_performance"] = performance
        return performance

    async def async_relogin(self) -> bool:
        """Perform a login to get a new token."""
        if self._legacy:
            success, session_id = await async_legacy_login(
                self._session,
                self._graphql_url,
                self._entry.data["username"],
                self._entry.data["password"],
            )
            if success:
                self._legacy_session_id = session_id
                _LOGGER.info("Successfully authenticated with legacy Braiins GraphQL API")
                return True

            _LOGGER.warning(
                "Failed to authenticate with legacy Braiins GraphQL API: %s",
                session_id,
            )
            return False

        url = f"{self._base_url}/auth/login"
        payload = {
            "username": self._entry.data["username"],
            "password": self._entry.data["password"],
        }
        try:
            async with asyncio.timeout(10):
                async with self._session.post(url, json=payload) as response:
                    response.raise_for_status()
                    data = await response.json()

                    new_token = data["token"]
                    new_timeout = data.get("timeout_s", 3600)
                    new_expires_at = time.time() + new_timeout - 60

                    _LOGGER.info(
                        "Successfully re-authenticated with Braiins OS+ and got a new token"
                    )

                    self._token = new_token
                    self._headers = {"Authorization": self._token}

                    new_data = {
                        **self._entry.data,
                        "token": new_token,
                        "expires_at": new_expires_at,
                    }
                    self._hass.config_entries.async_update_entry(
                        self._entry, data=new_data
                    )

                    return True

        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Failed to re-authenticate with Braiins OS+: %s", err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "An unexpected error occurred during re-authentication: %s", err
            )
            return False

    async def _is_token_valid_and_renew(self) -> bool:
        """Helper to check token validity and renew if needed."""
        if self._legacy:
            if self._legacy_session_id is None:
                return await self.async_relogin()
            return True

        async with self._lock:
            if time.time() > self._entry.data["expires_at"]:
                _LOGGER.info("Token expired based on time, attempting re-login")
                return await self.async_relogin()
        return True

    async def _make_get_request(self, endpoint: str) -> dict[str, Any] | None:
        """Make a GET request and return the JSON response, or None on failure."""
        if not await self._is_token_valid_and_renew():
            return None

        url = f"{self._base_url}/{endpoint}"
        _LOGGER.debug("Sending GET request to %s", url)
        try:
            async with asyncio.timeout(10):
                async with self._session.get(url, headers=self._headers) as response:
                    if response.status == 401:
                        _LOGGER.info(
                            "Token rejected by miner (401), attempting re-login"
                        )
                        async with self._lock:
                            if await self.async_relogin():
                                _LOGGER.info(
                                    "Re-login successful, retrying request for %s", url
                                )
                                async with self._session.get(
                                    url, headers=self._headers
                                ) as retry_response:
                                    retry_response.raise_for_status()
                                    return await retry_response.json()

                            _LOGGER.warning(
                                "Re-login failed after 401, aborting request for %s",
                                url,
                            )
                            return None

                    if response.status == 500:
                        _LOGGER.debug(
                            "Miner returned 500 at %s (likely reconfiguring)", endpoint
                        )
                        return None

                    response.raise_for_status()
                    return await response.json()

        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Failed to get data from %s: %s", url, err)
            return None
        except Exception as err:  # noqa: F841
            _LOGGER.exception(
                "An unexpected error occurred while getting data from %s", url
            )
            return None

    async def async_update_data(self) -> dict[str, Any]:
        """Fetch data from all endpoints and combine them. Raise UpdateFailed only if all fail."""
        if self._legacy:
            return await self._async_update_legacy_data()

        results = await asyncio.gather(
            self._make_get_request("miner/details"),
            self._make_get_request("configuration/constraints"),
            self._make_get_request("miner/hw/hashboards"),
            self._make_get_request("miner/stats"),
            self._make_get_request("performance/mode"),
            self._make_get_request("cooling/state"),
        )

        details, constraints, hashboards_raw, stats, mode, cooling = results

        # If all heavy endpoints return 500, the miner is reconfiguring.
        # Return the last successful data to prevent the UI from reverting.
        if mode is None and stats is None and hashboards_raw is None:
            if self._last_data:
                _LOGGER.info(
                    "Miner is reconfiguring; using cached data to prevent UI revert"
                )
                return self._last_data
            raise UpdateFailed("Miner is busy and no cached data is available.")

        if not any([details, constraints, hashboards_raw, stats, mode, cooling]):
            raise UpdateFailed("Failed to fetch any data from the miner.")

        combined_data = {
            "details": details or self._last_data.get("details", {}),
            "constraints": constraints or self._last_data.get("constraints", {}),
            "cooling": cooling or self._last_data.get("cooling", {}),
            "hashboards": (hashboards_raw.get("hashboards") if hashboards_raw else None)
            or self._last_data.get("hashboards", []),
            "stats": stats or self._last_data.get("stats", {}),
            "performance_mode": self._last_data.get("performance_mode"),
            "power_target": self._last_data.get("power_target"),  # From Cache
            "hashrate_target": self._last_data.get("hashrate_target"),  # From Cache
        }

        if mode:
            try:
                tuner_target = mode.get("tunermode", {}).get("target", {})

                # Detect the active mode bucket
                if "powertarget" in tuner_target:
                    combined_data["performance_mode"] = "Power Target"
                    watt = (
                        tuner_target.get("powertarget", {})
                        .get("power_target", {})
                        .get("watt")
                    )
                    if watt is not None:
                        combined_data["power_target"] = int(
                            watt
                        )  # Overwrite Cache with Fresh

                elif "hashratetarget" in tuner_target:
                    combined_data["performance_mode"] = "Hashrate Target"
                    th = (
                        tuner_target.get("hashratetarget", {})
                        .get("hashrate_target", {})
                        .get("terahash_per_second")
                    )
                    if th is not None:
                        combined_data["hashrate_target"] = int(
                            th
                        )  # Overwrite Cache with Fresh

                # Extract Power Target
                watt = (
                    tuner_target.get("powertarget", {})
                    .get("power_target", {})
                    .get("watt")
                )
                if watt is not None:
                    combined_data["power_target"] = watt

                # Extract Hashrate Target
                th = (
                    tuner_target.get("hashratetarget", {})
                    .get("hashrate_target", {})
                    .get("terahash_per_second")
                )
                if th is not None:
                    combined_data["hashrate_target"] = th
            except (KeyError, AttributeError):
                pass

        self._last_data = combined_data
        return combined_data

    async def _async_graphql_request(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any] | None:
        """Execute a legacy GraphQL request using the session cookie."""
        if not await self._is_token_valid_and_renew():
            return None

        headers = {"Content-Type": "application/json"}
        if self._legacy_session_id:
            headers["Cookie"] = f"session_id={self._legacy_session_id}"

        try:
            async with asyncio.timeout(10):
                async with self._session.post(
                    self._graphql_url,
                    headers=headers,
                    json={"query": query, "variables": variables or {}},
                ) as response:
                    payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.warning("Failed to query legacy Braiins GraphQL API: %s", err)
            return None

        errors = payload.get("errors") or []
        unauthorized = any(
            error.get("extensions", {}).get("code") == "UNAUTHORIZED"
            or error.get("message") == "Unauthorized access"
            for error in errors
        )
        if unauthorized and retry_auth:
            self._legacy_session_id = None
            if await self.async_relogin():
                return await self._async_graphql_request(query, variables, False)

        if errors:
            _LOGGER.warning(
                "Legacy Braiins GraphQL request failed: %s",
                "; ".join(error.get("message", "unknown error") for error in errors),
            )
            return None

        return payload.get("data")

    async def _async_update_legacy_data(self) -> dict[str, Any]:
        """Fetch and normalize stats from the legacy GraphQL API."""
        data = await self._async_graphql_request(LEGACY_GRAPHQL_STATS)
        if not data:
            if self._last_data:
                return self._last_data
            raise UpdateFailed("Failed to fetch data from the legacy Braiins GraphQL API.")

        bosminer = data.get("bosminer", {})
        info = bosminer.get("info", {})
        model_name = info.get("modelName")
        self._legacy_model_name = model_name
        summary = info.get("summary", {})
        work_solver = info.get("workSolver") or {}
        real_hashrate = summary.get("realHashrate", {})
        shares = summary.get("shares", {})
        power = summary.get("power") or {}
        temperature = summary.get("temperature") or {}
        fans = _legacy_fan_data(info.get("fans"))

        metadata = _apply_legacy_model_limits(
            bosminer.get("metadata", {}).get("hashChain") or {}, model_name
        )
        config = bosminer.get("config") or {}
        hash_chain_global = config.get("hashChainGlobal") or {}
        hash_chains = []
        for hash_chain in config.get("hashChains") or []:
            # The legacy API returns null for a board field when that board
            # inherits the global setting. Normalize to the effective value;
            # metadata defaults are only the allowed defaults, not the live
            # configuration.
            normalized_chain = dict(hash_chain)
            if normalized_chain.get("frequency") is None:
                normalized_chain["frequency"] = hash_chain_global.get("frequency")
            if normalized_chain.get("voltage") is None:
                normalized_chain["voltage"] = hash_chain_global.get("voltage")
            hash_chains.append(normalized_chain)
        previous_performance = self._last_data.get("legacy_performance", {})

        mhs_5s = real_hashrate.get("mhs5S")
        legacy_stats = {
            "miner_stats": {
                "real_hashrate": {
                    "last_5s": {
                        "terahash_per_second": (
                            float(mhs_5s) / 1_000_000 if mhs_5s is not None else None
                        )
                    }
                },
                "found_blocks": summary.get("foundBlocks"),
                "best_share": summary.get("bestShare"),
            },
            "pool_stats": {
                "accepted_shares": shares.get("acceptedSolutions"),
                "accepted_difficulty": shares.get("acceptedDifficulty"),
                "rejected_shares": shares.get("rejectedSolutions"),
                "rejected_difficulty": shares.get("rejectedDifficulty"),
                "rejected_ratio": shares.get("rejectedRatio"),
                "stale_shares": shares.get("staleSolutions"),
                "stale_difficulty": shares.get("staleDifficulty"),
                "stale_ratio": shares.get("staleRatio"),
            },
            "power_stats": {
                "approximated_consumption": {"watt": power.get("approxConsumptionW")},
                "efficiency": {
                    "joule_per_terahash": (
                        float(power["efficiencyWMhs"]) * 1_000_000
                        if power.get("efficiencyWMhs") is not None
                        else None
                    )
                },
            },
        }

        legacy_cooling = {}
        if temperature:
            legacy_cooling["highest_temperature"] = {
                "temperature": {"degree_c": temperature.get("degreesC")}
            }
        if fans:
            legacy_cooling["fans"] = fans

        combined_data = {
            "details": {
                "hostname": model_name or "Braiins OS+ Legacy Miner",
                "miner_identity": {"miner_model": model_name},
            },
            "constraints": {},
            "cooling": legacy_cooling,
            "hashboards": _legacy_hashboard_data(hash_chains, work_solver),
            "stats": legacy_stats,
            "performance_mode": self._last_data.get("performance_mode"),
            "power_target": power.get("limitW"),
            "hashrate_target": self._last_data.get("hashrate_target"),
            "legacy_pool_status": summary.get("poolStatus"),
            "legacy_hashrate": real_hashrate,
            "legacy_performance": {
                "metadata": metadata,
                "current": {
                    "globalFrequency": hash_chain_global.get("frequency"),
                    "globalVoltage": hash_chain_global.get("voltage"),
                    "hashChains": hash_chains,
                },
                "pending": previous_performance.get("pending", {}),
            },
        }
        self._last_data = combined_data
        return combined_data

    async def _async_legacy_action(self, action: str) -> bool:
        """Run a start, stop, or restart action on a legacy miner."""
        data = await self._async_graphql_request(
            LEGACY_GRAPHQL_ACTION.replace("ACTION", action)
        )
        result = (data or {}).get("bosminer", {}).get(action, {})
        if result.get("__typename") == "VoidResult":
            return True
        _LOGGER.error("Legacy Braiins action %s failed: %s", action, result.get("message"))
        return False

    async def update_performance(
        self, performance: dict[str, Any], apply: bool = True
    ) -> bool:
        """Update legacy frequency/voltage settings through GraphQL."""
        if not self._legacy:
            return False

        _validate_legacy_performance_limits(performance, self._legacy_model_name)
        self._legacy_performance_error = None
        data = await self._async_graphql_request(
            LEGACY_GRAPHQL_PERFORMANCE,
            {"perfInput": performance, "apply": apply},
        )
        result = (
            (data or {}).get("bosminer", {})
            .get("config", {})
            .get("updatePerformance", {})
        )
        if result.get("__typename") == "PerformanceOut":
            return True

        error = result.get("message") or "Legacy performance update failed"
        self._legacy_performance_error = error
        _LOGGER.error("Legacy performance update failed: %s", error)
        raise HomeAssistantError(error)

    async def _make_request(
        self, method: str, endpoint: str, data: dict | None = None
    ) -> bool:
        """Make a PUT or PATCH request for button presses."""
        if self._legacy:
            _LOGGER.warning("Endpoint %s is not supported by the legacy GraphQL API", endpoint)
            return False

        if not await self._is_token_valid_and_renew():
            return False

        url = f"{self._base_url}/{endpoint}"
        _LOGGER.debug(
            "Sending %s request to %s with data: %s", method.upper(), url, data
        )
        try:
            async with asyncio.timeout(10):
                async with self._session.request(
                    method, url, headers=self._headers, json=data
                ) as response:
                    if response.status == 401:
                        _LOGGER.info(
                            "Token rejected by miner (401) for command, attempting re-login"
                        )
                        async with self._lock:
                            if await self.async_relogin():
                                _LOGGER.info(
                                    "Re-login successful, retrying command for %s", url
                                )
                                async with self._session.request(
                                    method, url, headers=self._headers, json=data
                                ) as retry_response:
                                    if retry_response.status == 422:
                                        response_text = await retry_response.text()
                                        _LOGGER.error(
                                            "Unprocessable Entity on retry for %s. Miner Response: %s",
                                            url,
                                            response_text,
                                        )
                                    retry_response.raise_for_status()
                                    _LOGGER.info(
                                        "Successfully sent command to %s on retry", url
                                    )
                                    return True

                            _LOGGER.error(
                                "Re-login failed after 401, aborting command for %s",
                                url,
                            )
                            return False

                    if response.status == 422:
                        response_text = await response.text()
                        _LOGGER.error(
                            "Unprocessable Entity for %s. Miner Response: %s",
                            url,
                            response_text,
                        )
                    response.raise_for_status()
                    _LOGGER.info("Successfully sent command to %s", url)
                    return True
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Failed to send command to %s: %s", url, err)
            return False
        except Exception as err:  # noqa: F841
            _LOGGER.exception(
                "An unexpected error occurred while sending command to %s", url
            )
            return False

    async def set_hashrate_target(self, th: int) -> bool:
        """Set the miner hashrate target."""
        return await self._make_request(
            "put", "performance/hashrate-target", {"terahash_per_second": th}
        )

    async def increment_hashrate_target(self, value: int) -> bool:
        """Increase the miner hashrate target."""
        return await self._make_request(
            "patch",
            "performance/hashrate-target/increment",
            {"terahash_per_second": value},
        )

    async def decrement_hashrate_target(self, value: int) -> bool:
        """Decrease the miner hashrate target."""
        return await self._make_request(
            "patch",
            "performance/hashrate-target/decrement",
            {"terahash_per_second": value},
        )

    async def increment_power_target(self, value: int) -> bool:
        """Increase the miner power target by the given watt value."""
        return await self._make_request(
            "patch", "performance/power-target/increment", {"watt": value}
        )

    async def decrement_power_target(self, value: int) -> bool:
        """Decrease the miner power target by the given watt value."""
        return await self._make_request(
            "patch", "performance/power-target/decrement", {"watt": value}
        )

    async def set_power_target(self, watt: int) -> bool:
        """Set the miner power target to the specified watt value."""
        return await self._make_request(
            "put", "performance/power-target", {"watt": watt}
        )

    async def pause_mining(self) -> bool:
        """Pause mining on the miner."""
        if self._legacy:
            return await self._async_legacy_action("stop")
        return await self._make_request("put", "actions/pause")

    async def resume_mining(self) -> bool:
        """Resume mining on the miner."""
        if self._legacy:
            return await self._async_legacy_action("start")
        return await self._make_request("put", "actions/resume")

    async def restart_mining(self) -> bool:
        """Restart BOSminer on a legacy miner."""
        if self._legacy:
            return await self._async_legacy_action("restart")
        return False

    async def set_performance_mode(self, mode: str, value: float) -> bool:
        """Switch mode and send a specific target value."""
        if mode == "Power Target":
            payload = {
                "tunermode": {
                    "target": {"powertarget": {"power_target": {"watt": int(value)}}}
                }
            }
        else:
            payload = {
                "tunermode": {
                    "target": {
                        "hashratetarget": {
                            "hashrate_target": {"terahash_per_second": float(value)}
                        }
                    }
                }
            }

        return await self._make_request("put", "performance/mode", payload)
