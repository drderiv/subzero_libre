# Changelog

## 1.0.0

Initial public release.

### Features
* Real-time telemetry over Sub-Zero's Azure SignalR hub, with a periodic REST check (default 5 minutes) for active faults, the appliance list and authentication health.
* Cloud control of temperature setpoints (Refrigerator, Freezer, Crisper), Mode, Ice Maker and Humidity Control selects, and Night Mode / Air Purification / Automatic Crisper Temperature switches. The Crisper setpoint keeps its value visible but rejects changes while the automatic setting is on.
* Automatic discovery of whatever the appliance reports: sensors, switches, selects and a door sensor per `<door>_door_ajar` property.
* In-browser Azure AD B2C (PKCE) sign-in with OAuth `state` validation; rotated refresh tokens are persisted; standard re-authentication flow.
* Entities become `unavailable` only when the SignalR stream is down and the REST poll fails; the Cloud Connection sensor stays available.
* Diagnostics download with redaction; access tokens are redacted from logs.
* Out-of-order SignalR updates are ignored per property.
