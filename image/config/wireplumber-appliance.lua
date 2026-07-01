-- Appliance Bluetooth audio overrides for WirePlumber.
--
-- Loaded after the system bluetooth.lua.d/50-bluez-config.lua rules.
-- table.insert appends to the existing rules array so system defaults
-- are extended, not replaced.
--
-- Deployed by install.sh to:
--   /home/pi/.config/wireplumber/bluetooth.lua.d/51-appliance.lua

bluez_monitor.rules = bluez_monitor.rules or {}

-- Disable AVRCP hardware-volume mirroring for all Bluetooth cards.
--
-- By default WirePlumber syncs the PipeWire node volume with the value the
-- speaker reports via AVRCP (the physical knob position).  On the appliance
-- the knob may be at any position; the result is that PipeWire applies
-- unwanted software attenuation (e.g. 0.16 when the knob is near minimum)
-- on top of what librespot already controls via the PulseAudio API.
-- Disabling hw-volume lets librespot own the full volume range.
table.insert(bluez_monitor.rules, {
    matches = {
        {
            { "device.name", "matches", "bluez_card.*" },
        },
    },
    apply_properties = {
        ["bluez5.hw-volume"] = "[]",
    },
})

-- Keep Bluetooth sink nodes running even when no client is playing audio.
--
-- PipeWire's default is to suspend idle nodes after a few seconds of silence.
-- For a Bluetooth A2DP sink, suspension tears down the AVDTP stream; the next
-- connect attempt then requires a full A2DP re-establishment which the JBL
-- speaker sometimes rejects.  node.pause-on-idle = false keeps the transport
-- alive across track gaps, Spotify queue pauses, and librespot idle periods.
table.insert(bluez_monitor.rules, {
    matches = {
        {
            { "node.name", "matches", "bluez_output.*" },
        },
    },
    apply_properties = {
        ["node.pause-on-idle"] = false,
        ["node.volume"] = 1.0,
    },
})
