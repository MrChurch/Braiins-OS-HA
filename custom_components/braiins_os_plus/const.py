"""Constants for the Braiins OS+ integration."""

DOMAIN = "braiins_os_plus"

API_MODE = "api_mode"
API_MODE_REST = "rest"
API_MODE_LEGACY_GRAPHQL = "legacy_graphql"

# List of platforms that this integration will support
PLATFORMS = ["button", "number", "select", "sensor", "switch"]

CONF_POWER_STEP = "power_step"
DEFAULT_POWER_STEP = 250

CONF_HASHRATE_STEP = "hashrate_step"
DEFAULT_HASHRATE_STEP = 10

# Conservative legacy limits for Antminer S9 hash-chain tuning.
LEGACY_S9_MAX_FREQUENCY = 350.0
LEGACY_S9_MAX_VOLTAGE = 8.5
