# Sub-Zero Libre — Home Assistant Custom Integration

A native, high-performance Home Assistant integration for **Sub-Zero Smart Refrigerators and Appliances** that implements nothing more than the free mobile application's cloud telemetry and authentication. No additional hardware is required.

Provides real-time telemetry streaming, temperature monitoring, door ajar alerts, filter life tracking, and appliance control without third-party cloud bridges.

---

## Key Features

* **Real-Time SignalR Telemetry Streaming:** Connects directly to Sub-Zero's Azure SignalR hub (`connectedappliances`) for instant push updates when doors open, modes toggle, or setpoints change.
* **Full Coexistence with Official Mobile App:** The official Sub-Zero mobile app continues to function normally over Wi-Fi and Bluetooth. Unlike community ESP32/Bluetooth workarounds that require removing the refrigerator from your account or disabling Bluetooth on your phone, both the official mobile app and Home Assistant operate simultaneously in complete harmony.
* **Bidirectional Cloud Appliance Control:**
  * **Temperature Controls (`number`):** Directly adjust Refrigerator, Freezer, and Crisper target setpoints from Home Assistant dashboards and automations.
  * **Mode Controls (`select`):** The same choices as the mobile app: **Mode** (Normal, High Usage, Short Vacation, Long Vacation, Sabbath), **Ice Maker** (Normal, Max Ice, Night Ice, Off) and **Humidity Control** (Normal, Enhanced). The appliance applies its own side effects, e.g., vacation modes turn the ice maker off and restore it on exit, and High Usage turns on a timed Max Ice.
  * **Switches (`switch`):** Night Mode, Air Purification and Automatic Crisper Temperature. While the automatic crisper setting is on, the appliance holds the crisper 2°F below the Refrigerator setpoint and the Crisper setpoint keeps showing its value but rejects changes until you turn the automatic setting off (Home Assistant cannot grey out a number control as the mobile app does).
* **Instant Safety & State Alerts (`binary_sensor`):**
  * Door Open / Ajar Alerts for every door the appliance reports (Refrigerator, Freezer, and e.g., a wine door), discovered automatically
  * Service Required Warning Alert (wrench icon)
  * Ice Maker Cleaning Required Alert
  * Active Appliance Faults with diagnostic error attributes
  * Cloud Connection Health Status
  * Main Power Status
* **Filter Life & Connectivity Monitoring (`sensor`):**
  * Water Filter Remaining Life (%)
  * Air Purification Filter Remaining Life (%)
  * Appliance Wi-Fi Signal Strength (dBm)
* **Automatic Rolling Token Refresh:** Uses standard Azure AD B2C OAuth2 token refresh headlessly in the background and stores each rotated refresh token, so sessions survive Home Assistant restarts. If Sub-Zero ever rejects the stored sign-in, Home Assistant shows a standard **Re-authenticate** prompt instead of failing silently.
* **Resilient Outage Handling & Quiet Logs:**
  * **Honest Availability:** Entities keep their last known values while the real-time SignalR stream is up. If the stream is down *and* the REST poll is failing, entities become `unavailable` so automations never act on stale data. The **Cloud Connection** sensor stays available to report the outage itself.
  * **Back-off Reconnect:** The SignalR stream reconnects with exponential back-off and jitter.
  * **Quiet Logs:** Home Assistant logs a failing poll once and its recovery once. Access tokens are redacted from all log output.
* **Diagnostics:** **Download diagnostics** on the integration card provides a redacted report (tokens, account details and serial numbers removed) that is safe to attach to bug reports.
* **Zero Event Loop Blocking:** Built entirely on native asynchronous `aiohttp` and Home Assistant's managed client session with thread-pool executor offloading for all filesystem checks.

---

## Installation

### Method 1: HACS (Recommended)

1. Ensure [HACS](https://hacs.xyz/) is installed in your Home Assistant instance.
2. In Home Assistant, navigate to **HACS** > **Integrations**.
3. Click the three dots in the upper right corner > **Custom repositories**.
4. Paste the repository URL:
   ```text
   https://github.com/drderiv/subzero_libre
   ```
5. Select category **Integration** and click **Add**.
6. Search for **Sub-Zero Libre**, click **Download**, and restart Home Assistant.

### Method 2: Manual Installation

1. Copy the `custom_components/subzero_libre` directory into your Home Assistant `<config>/custom_components/` directory:
   ```bash
   cp -r /path/to/sub-zero_libre/custom_components/subzero_libre /path/to/homeassistant/config/custom_components/
   ```
2. Restart Home Assistant.

### Removal

1. In Home Assistant, navigate to **Settings** > **Devices & Services**.
2. On the **Sub-Zero Libre** card, click the three vertical dots (`⋮`) and select **Delete**.
3. Confirm deletion when prompted. All configured entities, devices, and stored authentication tokens are cleanly removed from Home Assistant.
4. If you installed via HACS, navigate to **HACS** > **Sub-Zero Libre** > three dots (`⋮`) > **Remove**, then restart Home Assistant.

---

## Setup & Authentication

Setting up Sub-Zero Libre is a one-time action performed directly through Home Assistant's web interface. After the one-time setup, tokens are automatically refreshed in the background for seamless, continuous operation.

1. In Home Assistant, navigate to **Settings** > **Devices & Services** > **Add Integration**.
2. Search for **Sub-Zero Libre**.
3. Choose how to sign in: **Email and password** (recommended), or **Web browser (advanced)**.

### Option 1: Email and password (recommended)

1. Enter the email and password you use in the Sub-Zero mobile app and click **Submit**.
2. If Sub-Zero asks for extra verification, Home Assistant will show it: type the characters from an **image check**, and/or the **code Sub-Zero sends you by text message**.
3. Home Assistant completes the sign-in and discovers your appliances.

Your password is sent only to Sub-Zero's own sign-in service (`login.subzero-wolf.com`), exactly as it would be from a browser, and it is **never stored**. Home Assistant keeps only the renewable sign-in tokens Sub-Zero returns (protect your Home Assistant backups accordingly).

If your account needs a verification step this method cannot complete, or Sub-Zero changes its sign-in page, Home Assistant tells you and switches to the web browser method automatically.

### Option 2: Web browser (for those who prefer not to enter their password into Home Assistant)

> [!IMPORTANT]
> **Desktop Browser Required:** Please perform this on a desktop browser (such as Chrome, Edge, or Firefox). Mobile browsers (and the Home Assistant mobile companion app) lack Developer Tools required to capture the redirect URL. The browser does not need to be on the same machine as Home Assistant.

1. In the Home Assistant dialog, click **Click Here to Log In with Sub-Zero** to open the official Wolf / Sub-Zero sign-in page in a new tab, and **wait for the page to finish loading completely**.
2. **Capture the Redirect URL:**
   * Once the page has loaded, open **Developer Tools** (press **F12** or **Ctrl+Shift+I**, or use the browser menu) and switch to the **Network** tab. Doing this *after* the page loads keeps the list nearly empty, so the row you need is easy to spot.
   * Enter your Sub-Zero mobile app email and password, then click **Sign In**.
   * The page will remain on the login screen (depending on your setup, you may see something like "Page Not Found" or "Unable to open address" or a box pop up asking if you wish to open another application — just ignore any of these). A few new rows appear in the **Network** tab; the last one, at the bottom, is highlighted in **red** (desktop browsers cannot open the mobile app link, so the request fails).
   * Right-click that red row and choose **Copy** > **Copy URL**. The copied text starts with:
     ```text
     com.szg.szgdigitalproductexperience://oauth/redirect?code=...
     ```
3. Return to the Home Assistant dialog, paste the copied URL into the field, and click **Submit**.

Either way, Home Assistant then establishes the real-time SignalR connection and discovers your Sub-Zero appliances, sensors, setpoint controls, and switches.

*(Optional / Advanced: If you already have a `refresh_token` or `subzero_tokens.json`, you can paste either directly into the browser method's field. The developer tool `python3 subzero_auth_test.py` (needs `pip install aiohttp`) can sign in with either method and list your appliances.)*

---

## Configuration & Options

Click **Configure** on the Sub-Zero Libre card in Home Assistant at any time to adjust:
* **Cloud Check Interval**: How often Home Assistant re-checks **active appliance faults** and the **appliance list** (names, units, newly added or removed appliances) over REST, and confirms the sign-in is still valid (slider from **15 seconds** to **900 seconds**, default: **300 seconds**). All live state (temperatures, doors, modes, switches) arrives instantly over the real-time SignalR connection regardless of this setting, so a new fault is reported within one interval at most.

### Managing Multiple Appliances & Locations

Sub-Zero's cloud infrastructure organizes connected appliances strictly by **user account** rather than physical location. Consequently, authenticating with your credentials imports all smart appliances associated with that account in the official mobile app into Home Assistant.

If your account includes appliances located across multiple properties (e.g., a primary residence and a vacation home) and you do not wish to monitor an appliance in a specific Home Assistant instance:
1. In Home Assistant, navigate to **Settings** > **Devices & Services** > **Sub-Zero Libre**.
2. Select the appliance you wish to exclude.
3. Click the three vertical dots (`⋮`) in the top right corner of the device card.
4. Select **Disable this device** and confirm.

Home Assistant will instantly disable all entities for that appliance, hiding them from dashboards, stopping recorder database tracking, and excluding them from automations, while leaving your other appliances operating at full fidelity.

---

## Known Behavior & Limitations

* **Changes are confirmed by the appliance, with a delay.** Every command goes through Sub-Zero's cloud, and the appliance's confirmation usually arrives within a few seconds but can occasionally take a minute or two. The official mobile app has the same lag, which is why its screens sometimes update late. Home Assistant shows your change immediately and then reconciles it with the appliance's confirmation.
* **Rapid toggling can briefly flip back and forth.** If you change the same control again before the first change has been confirmed (for example, clicking a switch twice within a few seconds), the late confirmation of the first change can briefly override the second one, and the **Activity** log may show extra on/off entries. This is cosmetic. Every command is sent exactly once, and the control settles on your last choice once the confirmations arrive. Allow a few seconds between changes.
* **Some modes change other settings on their own.** The appliance applies these side effects itself: Short and Long Vacation turn the Ice Maker off (and restore it when you leave the mode), and High Usage turns on a timed Max Ice. The Ice Maker control will therefore change by itself when you change Mode.
* **Automatic Crisper Temperature controls the Crisper setpoint.** While it is on, the appliance keeps the crisper 2°F below the Refrigerator setpoint, and the Crisper setpoint rejects manual changes. Turn the automatic setting off first.
* **Faults are checked periodically, not instantly.** Everything else is live, but active faults are re-checked at the Cloud Check Interval (default 5 minutes).
* **Cloud dependency.** Home Assistant talks to your appliance through Sub-Zero's cloud, so internet or Sub-Zero service outages make entities `unavailable` (the Cloud Connection sensor reports this) until the connection returns.

---

## Supported Appliances

Developed and tested against a **BI-42SID/S/TH** (42" Built-in Side-by-Side with Dispenser). The integration identifies these model IDs and shows their names on the device page; other Sub-Zero smart appliances on your account will also be set up, and are named `Sub-Zero Model <id>` until added to the list:

| Appliance ID | Model |
| :--- | :--- |
| 17.11.3.3 | BI-42SID/S/TH (42" Built-in Side-by-Side with Dispenser) |
| 17.11.2.0 | BI-42S/S/TH (42" Built-in Side-by-Side) |
| 17.7.1.1 | BI-36U (36" Built-in Over-and-Under) |
| 17.7.2.1 | BI-36UID (36" Built-in Over-and-Under with Dispenser) |
| 17.10.0 | BI-48S (48" Built-in Side-by-Side) |
| 17.3.0 | BI-36R (36" Built-in All Refrigerator) |

Entities are created from what each appliance actually reports, so the available controls and sensors vary by model. Only the BI-42SID has been verified; if you have another model or a wine unit and something is missing or wrong, please open an issue and attach the downloaded diagnostics.

---

## Example Automations & Use Cases

Replace the entity IDs below with your own (**Settings** > **Devices & Services** > **Entities**, search for "Sub-Zero"). They start with your appliance's name, for example `binary_sensor.42_side_by_side_refrigerator_door`.

### 1. Door Left Ajar Alert

Send a notification if either door stays open for more than 3 minutes:

```yaml
alias: "Sub-Zero: Door Left Ajar"
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.42_side_by_side_refrigerator_door
      - binary_sensor.42_side_by_side_freezer_door
    to: "on"
    for:
      minutes: 3
actions:
  - action: notify.notify
    data:
      title: "Sub-Zero door ajar"
      message: "{{ trigger.to_state.name }} has been open for over 3 minutes."
```

### 2. Vacation Mode When Nobody Is Home

Switch to Short Vacation mode (the appliance also turns the ice maker off) when the house has been empty for 4 hours, and back to Normal when someone returns:

```yaml
alias: "Sub-Zero: Vacation Mode When Away"
triggers:
  - trigger: state
    entity_id: zone.home
    to: "0"
    for:
      hours: 4
    id: away
  - trigger: numeric_state
    entity_id: zone.home
    above: 0
    id: home
actions:
  - action: select.select_option
    target:
      entity_id: select.42_side_by_side_mode
    data:
      option: "{{ 'short_vacation' if trigger.id == 'away' else 'normal' }}"
```

Select options use these values: `normal`, `high_use`, `short_vacation`, `long_vacation`, `sabbath` (Mode); `normal`, `max_ice`, `night_ice`, `off` (Ice Maker); `normal`, `enhanced` (Humidity Control).

### 3. Appliance Fault Notification

```yaml
alias: "Sub-Zero: Fault Reported"
triggers:
  - trigger: state
    entity_id: binary_sensor.42_side_by_side_appliance_fault
    to: "on"
actions:
  - action: notify.notify
    data:
      title: "Sub-Zero fault"
      message: "The appliance reported a fault. Check the Appliance Fault sensor's attributes for details."
```

Faults are re-checked at the Cloud Check Interval (default 5 minutes), so allow for that delay.

---

## Troubleshooting

* **Email and password sign-in says it is unavailable.** Home Assistant switches to the web browser method (see *Option 2* above) when Sub-Zero asks for a step the email/password method cannot complete, or after repeated wrong verification answers. Nothing is wrong with your account; use the browser steps. If you see "Sub-Zero is limiting sign-in attempts", wait a while before trying again.
* **I can't find the redirect URL during web browser sign-in.** Open the sign-in link and let the page finish loading **before** opening Developer Tools (F12 or Ctrl+Shift+I) and the **Network** tab, then sign in. The page stays on the login screen, and you may see "Page Not Found", "Unable to open address" or a prompt to open another application; ignore these. Right-click the last (red) row in the Network tab and choose **Copy** > **Copy URL**.
* **Re-authentication required.** If Sub-Zero rejects the stored sign-in, Home Assistant shows a **Re-authenticate** prompt on the integration. Choose a sign-in method again (email and password, or web browser); your entities, automations and history are kept.
* **Entities are missing or show `Unknown` after setup.** Entities are created from what the appliance reports, and the first full snapshot can take a little while. If it doesn't arrive, force-stop and reopen the official Sub-Zero mobile app, which prompts the cloud to request one. The entities then appear automatically.
* **Entities are `unavailable`, or Cloud Connection shows off.** Entities become unavailable only when the real-time stream is down *and* the periodic cloud check is failing. The stream reconnects automatically with back-off. Make sure the Home Assistant host can reach `login.subzero-wolf.com`, `prod.iot.subzero.com` and `*.service.signalr.net` on port 443. Brief network or DNS blips are logged once and recover on their own.
* **The Crisper setpoint won't change.** Turn off **Automatic Crisper Temperature** first; while it is on, the appliance sets the crisper 2°F below the refrigerator.
* **A control briefly flips back after I change it.** See *Known Behavior & Limitations* above; wait a few seconds between changes.
* **See more detail in the logs.** Home Assistant shows only warnings and errors by default. To see recovery messages and connection events, add this to `configuration.yaml` and restart (use `debug` instead of `info` for full detail, which is verbose):
  ```yaml
  logger:
    default: warning
    logs:
      custom_components.subzero_libre: info
  ```
* **Reporting a problem.** Download diagnostics from **Settings** > **Devices & Services** > **Sub-Zero Libre** > **Download diagnostics**. Account details, tokens, serial numbers and network identifiers are redacted, but please skim the file before posting it publicly.

---

## Technical Details & Architecture

For complete entity lists, comprehensive protocol specifications, etc., please see [TECHNICAL_DETAILS.md](TECHNICAL_DETAILS.md).

---

## Future Potential Enhancements

* **Notification-Triggered Fault Refresh:**  
  The real-time stream emits a notification message (`notif_type` / `notif_seq`) when the appliance raises a notification. Refreshing the active-fault list as soon as one arrives would make fault reporting instant and turn the periodic cloud check into a slow safety net. This needs verification that those notifications reliably correspond to faults.

* **Centralized Hub-and-Spoke Relay Architecture:**  
  For advanced power users with multiple Sub-Zero connected appliances spanning multiple physical properties and distinct Home Assistant instances:
  * A lightweight central gateway / proxy daemon (the **Hub**) can be deployed to maintain the single authorized Azure SignalR WebSocket connection and REST token rotation with Sub-Zero's cloud.
  * The various distributed Home Assistant installations (the **Spokes**) can be configured to connect directly to the Hub across a secure local network or VPN instead of reaching out independently to Sub-Zero's cloud.
  * This architecture prevents multiple Home Assistant instances from competing for the same cloud WebSocket session, eliminates redundant polling, minimizes external cloud traffic, and allows each spoke instance to subscribe selectively to only the appliances physically present at that location.

---

## License and Legal

This project is licensed under the [Apache License 2.0](LICENSE).  
Portions of the entity boilerplate are adapted from Home Assistant Core under the Apache License 2.0.
This integration is an independent open-source project and is not affiliated with, endorsed by, or supported by Sub-Zero Group, Inc., Wolf Appliance, Inc. or their respective affiliates. All product names, trademarks, and registered trademarks are property of their respective owners.