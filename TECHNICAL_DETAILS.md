# Sub-Zero Libre — Technical Details & Architecture

This document provides in-depth technical documentation covering the Sub-Zero cloud protocol, Azure Active Directory B2C authentication, Azure SignalR WebSocket streaming, Azure IoT Hub Direct Method execution, and complete entity reference tables.

---

## Entities Overview

All entities operate in real-time push mode via Azure SignalR WebSockets and bidirectional Azure IoT Hub Direct Methods:

| Domain | Entity Name | Telemetry property | Description |
| :--- | :--- | :--- | :--- |
| **Number** | `number.<name>_refrigerator_temperature_setpoint` | `ref_set_temp` | Refrigerator target temperature (°F / °C) |
| **Number** | `number.<name>_freezer_temperature_setpoint` | `frz_set_temp` | Freezer target temperature (°F / °C) |
| **Number** | `number.<name>_crisper_temperature_setpoint` | `crisp_set_temp` | Crisper drawer target temperature (°F / °C) |
| **Select** | `select.<name>_mode` | `high_use_on`, `short_vacation_on`, `long_vacation_on`, `sabbath_on` | Normal (all off), High Usage, Short Vacation, Long Vacation, Sabbath |
| **Select** | `select.<name>_ice_maker` | `ice_maker_on`, `max_ice_on`, `night_ice_on` | Normal, Max Ice, Night Ice, Off |
| **Select** | `select.<name>_humidity_control` | `humidity_control` | Normal (1) / Enhanced (2) |
| **Switch** | `switch.<name>_night_mode` | `night_mode` | Night Mode (0/1 integer) |
| **Switch** | `switch.<name>_automatic_crisper_temperature` | `crisp_temp_mode` | 1 = crisper follows the refrigerator (setpoint − 2°F), 0 = manual |
| **Switch** | `switch.<name>_air_purification` | `air_filter_on` | PureAir purification system |
| **Sensor** | `sensor.<name>_water_filter_life` | `water_filter_pct_remaining` | Water filter remaining life (%) |
| **Sensor** | `sensor.<name>_air_filter_life` | `air_filter_pct_remaining` | Air filter remaining life (%) |
| **Sensor** | `sensor.<name>_wi_fi_signal_strength` | `ap_rssi` | Wi-Fi signal strength (dBm) |
| **Binary Sensor** | `binary_sensor.<name>_<door>_door` | `<door>_door_ajar` | One per door reported (`ref` → Refrigerator, `frz` → Freezer, e.g. `wine` → Wine) |
| **Binary Sensor** | `binary_sensor.<name>_service_required` | `service_required` | Service required alert |
| **Binary Sensor** | `binary_sensor.<name>_ice_clean_required` | `ice_clean_required` | Ice maker cleaning required (dedicated ice machines) |
| **Binary Sensor** | `binary_sensor.<name>_appliance_fault` | fault notifications | Active faults (details in attributes) |
| **Binary Sensor** | `binary_sensor.<name>_cloud_connection` | SignalR state | Sub-Zero cloud connection status |
| **Binary Sensor** | `binary_sensor.<name>_power_status` | `unit_on` | Appliance main power |

### Observed mode behavior

Observed from the mobile app and Home Assistant against a BI-42SID: the appliance applies side effects itself, so selects only set the property they represent.

* **Short / Long Vacation:** setting the flag turns `ice_maker_on` off; leaving the mode turns it back on.
* **High Usage:** sets `high_use_start_time` / `high_use_end_time` (8 h window) and turns Max Ice on; leaving clears all of it.
* **Max Ice:** `max_ice_on` also sets a 24 h `max_ice_start_time` / `max_ice_end_time` window. **Night Ice** and **Ice Maker Off** are plain flags.
* **Automatic crisper temperature:** with `crisp_temp_mode` 1 the appliance sets `crisp_set_temp` to the refrigerator setpoint − 2°F within about a second of any refrigerator change (observed in °F); with 0 the crisper setpoint is independent and is set normally. The Crisper number keeps displaying the value while the mode is 1, but writes are rejected with a translated error.
* SignalR updates can arrive out of order (`seq` 551 before 550) and sometimes minutes late; the integration discards a device update for a property already set by a newer `seq`.

Entities are created dynamically: a sensor, number, switch or door sensor appears only once the appliance reports the matching telemetry property, so the roster varies by model. `door_ajar_timeout` (the door alarm delay in minutes) is reported by the appliance but intentionally not exposed. Setpoint ranges are not provided by the cloud; defaults live in `const.py` (`DEFAULT_SETPOINT_RANGES`) with per-model overrides in `MODEL_SETPOINT_RANGES`.

### What the periodic REST check does

Telemetry never comes from polling; it all arrives over SignalR. The coordinator's periodic refresh (default 300 seconds) exists for data the stream does not carry:

* **Active faults** from `/fault-notifications/v1/notifications/device/{serial}` (the only source for the Appliance Fault sensor).
* **Account and appliance metadata** from `/consumerapp/user/devices` (name, temperature unit, appliances added or removed).
* **Health and authentication:** refreshes the id_token near expiry, raises re-authentication on a persistent 401, and provides the REST half of the availability rule (entities are `unavailable` only when the stream is down *and* this check fails).

---

## Architecture & Technical Details

* **Identity Provider**: Azure Active Directory B2C (`login.subzero-wolf.com/SubZeroB2CPrd.onmicrosoft.com`)
  * **Policy**: `B2C_1A_SIGNUP_SIGNIN`
  * **Client ID**: `6eefabd0-49a3-4b92-b329-81b9f638e940`
  * **Scopes**: `openid offline_access`
  * **Redirect URI**: `com.szg.szgdigitalproductexperience://oauth/redirect`
* **API Gateway**: Azure API Management (APIM) at `https://prod.iot.subzero.com`
  * **Subscription Key**: A static key identifying the official mobile app, sent in the `ocp-apim-subscription-key` header (bundled in `const.py`; not reproduced here).
  * **Headers**: Requires Bearer `id_token` and custom `userId` header matching the JWT `sub` claim.
* **Realtime Hub**: Azure SignalR Service
  * **Negotiation**: `POST /signal-r/negotiateUser`
  * **WebSocket Hub**: `wss://sznacasigprod.service.signalr.net/client/?hub=connectedappliances`
  * **Protocol**: SignalR JSON Hub Protocol v1 with heartbeat/ping handling.

---

## Bidirectional Cloud Control & Direct Method Protocol

Sub-Zero appliances utilize Azure IoT Hub Direct Methods for all remote cloud commands. The integration communicates directly with Sub-Zero's Azure APIM gateway:

* **Endpoint**: `POST https://prod.iot.subzero.com/consumerapp/device/{deviceId}/directmethod/executeAPICmd`
* **Mandatory Headers**:
  * `ocp-apim-subscription-key`: `<app_subscription_key>` *(static app key, see `const.py`)*
  * `authorization`: `Bearer <id_token>`
  * `userid`: `<user_uuid>` *(all lowercase)*
  * `user-agent`: `Dart/3.11 (dart:io)`
  * `app_platform`: `android`
  * `app_version`: `4.6.2`
  * `content-type`: `application/json; charset=utf-8`
* **Payload Envelope Format**:
  All Direct Method calls must wrap the command inside a JSON envelope with a unique request UUID (`req_id`) and payload object (`pload`):
  ```json
  {
    "req_id": "1c714024-c50e-411a-8e97-de9a555d038e",
    "pload": {
      "cmd": "set",
      "params": {
        "ref_set_temp": 38
      }
    }
  }
  ```
* **Supported Commands**:
  * **Temperature Setpoints:** `ref_set_temp`, `frz_set_temp`, `crisp_set_temp` (integer degrees).
  * **Appliance Modes:** `ice_maker_on`, `max_ice_on`, `night_ice_on`, `air_filter_on`, `sabbath_on` (boolean).
  * **Cloud Reporting Channel:** `{"cmd": "open_cloud_async"}` (prompts the physical appliance to immediately push full telemetry snapshots over the SignalR stream).
