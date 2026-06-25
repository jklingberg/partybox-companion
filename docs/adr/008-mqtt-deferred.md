# ADR-008: MQTT Deferred to Post-v1.0

**Status:** Accepted

---

## Context

MQTT is widely used in home automation. Home Assistant, Node-RED, and many other systems consume MQTT natively. Adding MQTT to the daemon would make it integrate seamlessly with these ecosystems without requiring an HTTP client.

## Decision

MQTT is deferred to post-v1.0. It may be added as an optional adapter after v1.0 if there is demand.

For v1.0, Home Assistant and other automation systems connect to the REST API over HTTP, the same as any other client.

## Consequences

**Benefits of deferral:**
- Simpler v1.0. No additional dependency (`aiomqtt` or similar), no broker configuration, no MQTT topic design to stabilise.
- REST + WebSocket covers all current use cases. Polling the REST API is sufficient for HA automations. WebSocket subscriptions cover real-time needs.
- MQTT topic naming and payload format are design decisions that benefit from real-world feedback. Getting them wrong in v1.0 would create a migration burden.

**Accepted trade-offs:**
- Users who have an existing MQTT-based home automation setup cannot use MQTT with this project until post-v1.0.
- HA users will need a REST-based integration rather than a native MQTT integration for v1.0.

**Future path:** A post-v1.0 MQTT adapter would be an optional component that subscribes to device events via WebSocket or the internal event bus and re-publishes them as MQTT messages. It would not require changes to the core daemon.

**Rejected alternative:** Including MQTT in v1.0. Rejected because it adds complexity and a required external dependency (an MQTT broker) without enabling any use case that HTTP + WebSocket cannot serve.
