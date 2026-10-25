# Changelog

## 1.1.0

### Added
* **Email and password sign-in** inside Home Assistant (with support for the text-message code and image check Sub-Zero may ask for). The password is never stored.
* The sign-in dialog now starts with a choice of **email and password** or **web browser**. If the email/password method cannot complete (an unsupported verification step, a changed sign-in page, repeated wrong answers), the flow switches to the web browser method automatically. Re-authentication offers the same choice.
* Automated tests (login module against a fake sign-in service, config flow, mode logic, telemetry ordering, discovery).

### Changed
* `subzero_auth_test.py` now uses the integration's own login and client code and requires `aiohttp`; it can sign in with either method.

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
