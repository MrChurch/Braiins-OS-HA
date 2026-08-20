# Braiins OS+ Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Project Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)](https://github.com/aleixps/Braiins-OS-HA)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![AI-Assisted](https://img.shields.io/badge/Development-AI--Assisted-blueviolet.svg)](#ai-assistance)

This is a custom integration for Home Assistant that allows you to control and monitor your cryptocurrency miners running **Braiins OS+**. It connects directly to your miner's local API, providing advanced tuning controls and detailed telemetry.

## Features

-   **Local Control**: Connects directly to your miner via its local IP address. No cloud services are required.
-   **Detailed Monitoring**: Provides sensor entities for key metrics with a rapid 5-second update interval.
    -   Total hashrate (TH/s).
    -   Real-time power consumption (W) and energy efficiency (J/TH).
    -   Highest chip and board temperatures.
    -   Per-hashboard hashrate, chip temperature, and board temperature.
	-	Monitor RPM and Target Speed (%) for every fan detected.
-   **Simple Controls**: Provides button entities to perform key actions:
    -   Pause and Resume mining operations.
    -   Increment and Decrement the power target.
	-   Fine-tune how much the increment/decrement buttons change your targets via dedicated configuration entities.
-   **Tuner Management**: 
    -   Set specific **Power Targets** (Watts) or **Hashrate Targets** (TH/s).
    -   Dynamic slider limits: Sliders automatically adjust their min/max range based on your specific miner's hardware constraints.
-   **Robust Authentication**: Automatically handles the renewal of authentication tokens to ensure the connection is always active.
-   **Legacy Support**: Braiins OS+ 22.08.x miners can use the legacy GraphQL API with a `session_id` cookie instead of the newer REST API.


## Prerequisites

-   A miner running a recent version of Braiins OS+.
-   Home Assistant (Version 2023.11.0 or newer).
-   HACS (Home Assistant Community Store) installed and running.

## Installation

This integration is best installed via HACS.

### HACS (Recommended Method)

1.  Navigate to the HACS section in your Home Assistant.
2.  Go to "Integrations", then click the three-dots menu in the top right and select **"Custom repositories"**.
3.  Paste the following URL into the "Repository" field:
    ```
    https://github.com/aleixps/Braiins-OS-HA
    ```
4.  Select **"Integration"** as the category.
5.  Click **"Add"**.
6.  The "Braiins OS+" integration will now be available in HACS. Find it and click **"Install"**.
7.  Restart Home Assistant after the installation is complete.

### Manual Installation

1.  Go to the [latest release](https://github.com/aleixps/Braiins-OS-HA/releases/latest) page of this repository.
2.  Download the `braiins_os_plus.zip` file.
3.  Unzip the file.
4.  Copy the `braiins_os_plus` directory into your Home Assistant `config/custom_components/` directory.
5.  Restart Home Assistant.

## Configuration

Once installed, you can add and configure the integration through the Home Assistant UI.

1.  Go to **Settings** > **Devices & Services**.
2.  Click the **"+ Add Integration"** button in the bottom right.
3.  Search for **"Braiins OS+"**.
4.  In the configuration dialog, enter the following details:
    -   **Miner IP**: The local IP address of your miner.
    -   **Username**: The username for your miner's web interface.
    -   **Password**: The password for your miner.
5.  Click **"Submit"**.

The integration will log in and create a new device with all associated entities.


## Entities Created

### Controls & Configuration

| Entity ID | Description |
| :--- | :--- |
| `number.power_target` | Set specific power draw in Watts. Limits are dynamic per model. |
| `number.hashrate_target` | Set specific hashrate target in TH/s. |
| `number.power_adjustment_step` | **Config:** Set how many Watts buttons change (e.g., 100W, 250W). |
| `number.hashrate_adjustment_step`| **Config:** Set how many TH/s buttons change (e.g., 1.0, 5.0). |
| `number.legacy_frequency` | Legacy GraphQL: stage global frequency using miner-provided limits. |
| `number.legacy_voltage` | Legacy GraphQL: stage global voltage using miner-provided limits. |
| `number.legacy_hashchain_frequency_<name>` | Legacy GraphQL: stage frequency for an individual hashboard. |
| `number.legacy_hashchain_voltage_<name>` | Legacy GraphQL: stage voltage for an individual hashboard. |
| `switch.braiins_hashboard_<name>` | Legacy GraphQL: stage enabling or disabling an individual hashboard. |
| `button.increment_power_target` | Increases Power Target by the configured step. |
| `button.decrement_power_target` | Decreases Power Target by the configured step. |
| `button.increment_hashrate_target`| Increases Hashrate Target by the configured step. |
| `button.decrement_hashrate_target`| Decreases Hashrate Target by the configured step. |
| `button.pause_miner` | Pauses mining operations. |
| `button.resume_miner` | Resumes mining operations. |
| `button.apply_performance` | Legacy GraphQL: apply staged frequency and voltage together. |
| `button.hashboard_<name>_use_global_values` | Legacy GraphQL: stage the current global frequency and voltage for one hashboard. |

### Sensors (Updated every 5s)

| Sensor | Description | Unit |
| :--- | :--- | :--- |
| **Total Hashrate** | Combined real-time hashrate of all boards. | TH/s |
| **Hashrate 1 Min** | BOS API/Legacy GraphQL 1-minute hashrate average. | TH/s |
| **Hashrate 15 Min** | BOS API/Legacy GraphQL 15-minute hashrate average. | TH/s |
| **Miner Consumption** | Real-time power draw from the wall. | W |
| **Miner Efficiency** | Real-time efficiency (reports 0.0 when paused). | J/TH |
| **Accepted** | Total number of shares accepted by the pool. | shares |
| **Blocks Found** | Total number of blocks found by the miner. | blocks |
| **Best Share** | Highest share difficulty reported by the miner. | — |
| **Chip Temperature** | The highest chip temperature reported by cooling system. | °C |
| **Board Temperature** | Calculated highest surface temperature among all boards. | °C |
| **Fan Speed** | Actual RPM for each individual fan. | RPM |
| **Fan Speed 1 Min** | Rolling one-minute average RPM for each individual fan. | RPM |
| **Fan Target Speed** | The duty cycle percentage for each fan. | % |

*Per-hashboard sensors for hashrate and temperature are also created automatically.*

### Legacy GraphQL mode

During setup, the integration tries the current REST API first and falls back to the legacy GraphQL API when available. Legacy miners expose `Accepted`, `Blocks Found`, `Best Share`, hashrate, power, temperature, and fan data through `bosminer.info`. Per-hashboard hashrate and temperatures are read from `bosminer.info.workSolver.childSolvers`. Start, stop, and restart use the corresponding legacy GraphQL mutations. Individual hashboard frequency and voltage values are staged locally and applied together with `button.apply_performance`.

For legacy Antminer S9 miners, the integration enforces a maximum frequency of 600 MHz and a maximum voltage of 8.5 V for both global and individual hashboard settings. Changing a global S9 frequency or voltage automatically stages that changed value for every detected hashboard, so old per-board overrides do not remain active. Hashboard enable/disable changes and the per-board “Use Global Values” action are staged locally and applied with `button.apply_performance`.

## Creating an Energy Sensor (kWh)

To track total energy consumption for the **Home Assistant Energy Dashboard**:

1.  Go to **Settings** > **Devices & Services** > **Helpers**.
2.  Create a **"Riemann sum integral sensor"**.
3.  **Input sensor**: `sensor.miner_consumption`.
4.  **Metric prefix**: `k` (kilo).
5.  **Unit of time**: `Hours`.
6.  Use the resulting entity in your Energy Dashboard.

## AI Assistance

This integration was developed with the assistance of Artificial Intelligence tools.

## Contributing

Contributions and bug reports are welcome! Check the [issues page](https://github.com/aleixps/Braiins-OS-HA/issues) to get involved.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
