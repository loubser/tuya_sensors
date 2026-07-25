"""Sensor platform for Tuya sensors integration."""
import logging
from datetime import timedelta
import re
import asyncio

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfTemperature,
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfPower,
    UnitOfEnergy,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import DOMAIN, _LOGGER

# Tuya API error code returned when a device does not support the /status
# endpoint (typical of battery-powered sensors — use /properties instead)
_TUYA_ERR_FUNCTION_NOT_SUPPORT = 2003

async def _fetch_properties(hass, tuya_api, device_ids):
    """Fetch all available property codes for the given device IDs."""
    property_codes = set()
    errors_found = set()
    for device_id in device_ids:
        # 1. Try /status first (v1.0)
        _LOGGER.debug("Device %s: trying /status endpoint to discover properties", device_id)
        try:
            response = await hass.async_add_executor_job(
                tuya_api.get, f"/v1.0/devices/{device_id}/status"
            )
            _LOGGER.debug("Device %s: /status discovery response: %s", device_id, response)
            if not response.get("success", False):
                code = response.get("code")
                if code == 28841107:
                    _LOGGER.error("Device %s: Tuya Cloud Error 28841107 - The data center is suspended or no permission. "
                                  "Please ensure the correct Data Center is enabled in your Tuya IoT Platform project settings.", device_id)
                    errors_found.add("data_center_error")
            if response.get("success", False):
                res = response.get("result", [])
                items = res if isinstance(res, list) else []
                for item in items:
                    if isinstance(item, dict) and "code" in item:
                        property_codes.add(item["code"])
        except Exception as e:
            _LOGGER.error("Error fetching /status for device %s: %s", device_id, e)
        
        # 2. Try /properties (v2.0)
        _LOGGER.debug("Device %s: trying /properties endpoint to discover properties", device_id)
        try:
            response = await hass.async_add_executor_job(
                tuya_api.get, f"/v2.0/cloud/thing/{device_id}/shadow/properties"
            )
            _LOGGER.debug("Device %s: /properties discovery response: %s", device_id, response)
            if not response.get("success", False):
                code = response.get("code")
                if code == 28841107:
                    errors_found.add("data_center_error")
            if response.get("success", False):
                res = response.get("result", {})
                # result might be the dict with 'properties' or the list directly
                props = res.get("properties", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
                for p in props:
                    if isinstance(p, dict) and "code" in p:
                        property_codes.add(p["code"])
        except Exception as e:
            _LOGGER.error("Error fetching /properties for device %s: %s", device_id, e)

        # 3. Try /functions (v1.0) - another common source for property codes
        _LOGGER.debug("Device %s: trying /functions endpoint to discover properties", device_id)
        try:
            response = await hass.async_add_executor_job(
                tuya_api.get, f"/v1.0/devices/{device_id}/functions"
            )
            _LOGGER.debug("Device %s: /functions discovery response: %s", device_id, response)
            if not response.get("success", False):
                code = response.get("code")
                if code == 28841107:
                    errors_found.add("data_center_error")
            if response.get("success", False):
                res = response.get("result", {})
                # result might be the list directly or a dict containing 'functions'
                funcs = res.get("functions", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
                for f in funcs:
                    if isinstance(f, dict) and "code" in f:
                        property_codes.add(f["code"])
        except Exception as e:
            _LOGGER.error("Error fetching /functions for device %s: %s", device_id, e)

        # 4. Try /specifications (v1.0) - yet another one
        _LOGGER.debug("Device %s: trying /specifications endpoint to discover properties", device_id)
        try:
            response = await hass.async_add_executor_job(
                tuya_api.get, f"/v1.0/devices/{device_id}/specifications"
            )
            _LOGGER.debug("Device %s: /specifications discovery response: %s", device_id, response)
            if not response.get("success", False):
                code = response.get("code")
                if code == 28841107:
                    errors_found.add("data_center_error")
            if response.get("success", False):
                res = response.get("result", {})
                # Specifications often have functions and status
                for key in ["functions", "status"]:
                    items = res.get(key, [])
                    for item in items:
                        if isinstance(item, dict) and "code" in item:
                            property_codes.add(item["code"])
        except Exception as e:
            _LOGGER.error("Error fetching /specifications for device %s: %s", device_id, e)

        # 5. Try device info 'status' field (v1.0)
        _LOGGER.debug("Device %s: trying /v1.0/devices/{id} endpoint to discover properties", device_id)
        try:
            response = await hass.async_add_executor_job(
                tuya_api.get, f"/v1.0/devices/{device_id}"
            )
            _LOGGER.debug("Device %s: device info discovery response: %s", device_id, response)
            if not response.get("success", False):
                code = response.get("code")
                if code == 28841107:
                    errors_found.add("data_center_error")
            if response.get("success", False):
                res = response.get("result", {})
                status = res.get("status", [])
                for item in status:
                    if isinstance(item, dict) and "code" in item:
                        property_codes.add(item["code"])
        except Exception as e:
            _LOGGER.error("Error fetching device info for device %s: %s", device_id, e)

        # 6. Try /v1.0/iot-03/devices/{id}/status - another variation
        _LOGGER.debug("Device %s: trying /v1.0/iot-03/devices/{id}/status endpoint", device_id)
        try:
            response = await hass.async_add_executor_job(
                tuya_api.get, f"/v1.0/iot-03/devices/{device_id}/status"
            )
            _LOGGER.debug("Device %s: /v1.0/iot-03 discovery response: %s", device_id, response)
            if not response.get("success", False):
                code = response.get("code")
                if code == 28841107:
                    errors_found.add("data_center_error")
            if response.get("success", False):
                res = response.get("result", [])
                items = res if isinstance(res, list) else []
                for item in items:
                    if isinstance(item, dict) and "code" in item:
                        property_codes.add(item["code"])
        except Exception as e:
            _LOGGER.debug("Error fetching iot-03 status for device %s: %s", device_id, e)
    
    # If still no properties found, log a summary
    if not property_codes:
        _LOGGER.warning("Discovery finished: No properties found for device IDs: %s. Check debug logs for API responses.", device_ids)

    return sorted(list(property_codes)), list(errors_found)

def _normalise_status(result):
    """Return a flat [{code, value}] list from a /status result payload.

    The /status endpoint returns result as a list directly:
        [{"code": "temp_current", "value": 235}, ...]
    """
    if isinstance(result, list):
        return result
    _LOGGER.warning("Unexpected /status result shape: %s", result)
    return []


def _normalise_properties(result):
    """Return a flat [{code, value}] list from a /properties result payload.

    The /properties endpoint wraps data one level deeper:
        {"properties": [{"code": "va_temperature", "value": 235, "time": ...}, ...]}
    We strip the extra level so the rest of the code sees the same shape as
    /status responses.
    """
    if isinstance(result, dict):
        props = result.get("properties", [])
        # Keep only code + value; discard the timestamp field
        return [{"code": p["code"], "value": p["value"]} for p in props if "code" in p and "value" in p]
    _LOGGER.warning("Unexpected /properties result shape: %s", result)
    return []


async def _async_fetch_status(hass, tuya_api, device_id):
    """Fetch device status, falling back to /properties on error 2003.

    Returns (normalised_data, endpoint_used) where endpoint_used is either
    "status" or "properties". Raises UpdateFailed if both endpoints fail.
    """
    # Try /status first
    _LOGGER.debug("Device %s: trying /status endpoint", device_id)
    response = await hass.async_add_executor_job(
        tuya_api.get, f"/v1.0/devices/{device_id}/status"
    )
    _LOGGER.debug("Device %s: /status raw response: %s", device_id, response)

    if response.get("success", False):
        data = _normalise_status(response.get("result", []))
        _LOGGER.debug("Device %s: /status returned %d data points: %s", device_id, len(data), data)
        return data, "status"

    if response.get("code") != _TUYA_ERR_FUNCTION_NOT_SUPPORT:
        # A genuine error — don't try the other endpoint
        raise UpdateFailed(f"Failed to get status for device {device_id}: {response}")

    # /status returned 2003 — device uses property-based reporting (e.g. battery-powered sensors)
    # Note: properties are served from the v2.0 API, not v1.0
    _LOGGER.debug(
        "Device %s does not support /status (code 2003); trying /properties", device_id
    )
    response = await hass.async_add_executor_job(
        tuya_api.get, f"/v2.0/cloud/thing/{device_id}/shadow/properties"
    )
    _LOGGER.debug("Device %s: /properties raw response: %s", device_id, response)

    if response.get("success", False):
        data = _normalise_properties(response.get("result", {}))
        _LOGGER.debug("Device %s: /properties returned %d data points: %s", device_id, len(data), data)
        return data, "properties"

    raise UpdateFailed(
        f"Failed to get status for device {device_id} via both /status and "
        f"/properties. Last error: {response}"
    )


async def async_setup_platform(
    hass,
    config,
    async_add_entities,
    discovery_info = None,
) -> None:
    # Get config from hass.data
    domain_config = hass.data[DOMAIN]
    await _async_setup(hass, domain_config, async_add_entities)

async def async_setup_entry(
    hass,
    entry,
    async_add_entities
) -> None:
    # Get config from hass.data
    domain_config = hass.data[DOMAIN].get(entry.entry_id)
    await _async_setup(hass, domain_config, async_add_entities)

async def _async_setup(
    hass,
    domain_config,
    async_add_entities
) -> None:
    """Set up the Tuya sensor."""
    try:
        from tuya_connector import TuyaOpenAPI, TUYA_LOGGER
    except ImportError:
        _LOGGER.error("Failed to import tuya_connector. Make sure it's installed.")
        return

    # Set up logging for tuya_connector
    TUYA_LOGGER.setLevel(logging.INFO)
    
    api_key = domain_config["api_key"]
    api_secret = domain_config["api_secret"]
    device_ids = domain_config["device_ids"]
    sensors_config = domain_config.get("sensors", [])
    region = domain_config["region"]
    scan_interval = domain_config["scan_interval"]

    # Set appropriate endpoint based on region
    endpoint = f"https://openapi.tuya{region}.com"

    # Initialize API connection
    tuya_api = TuyaOpenAPI(
        access_id=api_key,
        access_secret=api_secret,
        endpoint=endpoint
    )
    
    try:
        # Get access token
        response = await hass.async_add_executor_job(tuya_api.connect)
        
        if not response.get("success", False):
            _LOGGER.error("Failed to get access token: %s", response)
            return

        sensor_entities = []
        all_devices = []
        
        # If specific device IDs are provided, use them
        if device_ids:
            for device_id in device_ids:
                try:
                    # Get device info
                    response = await hass.async_add_executor_job(
                        tuya_api.get, f"/v1.0/devices/{device_id}"
                    )
                    
                    if not response.get("success", False):
                        _LOGGER.error("Failed to get device info for %s: %s", device_id, response)
                        continue
                    
                    device_info = response.get("result", {})
                    all_devices.append(device_info)
                except Exception as e:
                    _LOGGER.error("Error processing device %s: %s", device_id, str(e))
        else:
            # If no specific devices are provided, discover all devices
            response = await hass.async_add_executor_job(tuya_api.get, "/v1.0/devices")
            
            if not response.get("success", False):
                _LOGGER.error("Failed to get devices: %s", response)
                return
                
            all_devices = response.get("result", [])

        _LOGGER.debug("Processing %d device(s): %s", len(all_devices), [d.get("id") for d in all_devices])

        # Process each device to discover sensors
        for device in all_devices:
            device_id = device.get("id")
            device_name = device.get("name", f"Device {device_id}")
            _LOGGER.debug("Device %s (%s): starting sensor discovery", device_id, device_name)

            try:
                # Fetch status via /status, falling back to /properties on error 2003.
                # endpoint_used is stored on the coordinator so subsequent polls go
                # directly to the correct endpoint without retrying /status each time.
                status_data, endpoint_used = await _async_fetch_status(
                    hass, tuya_api, device_id
                )
                _LOGGER.debug(
                    "Device %s: using '%s' endpoint (%d data points)",
                    device_id, endpoint_used, len(status_data)
                )
            except UpdateFailed as e:
                _LOGGER.warning("%s", e)
                continue

            # Create a coordinator for this device
            coordinator = TuyaDataCoordinator(
                hass,
                _LOGGER,
                tuya_api,
                device_id,
                scan_interval,
                endpoint_used
            )

            # Process configured sensors
            for sensor_cfg in sensors_config:
                code = sensor_cfg.get("code")
                
                # Check if this sensor data point exists in the device status
                if not any(s.get("code") == code for s in status_data):
                    _LOGGER.debug("Device %s: code '%s' not found in status data, skipping", device_id, code)
                    continue

                _LOGGER.debug("Device %s: creating entity for code '%s' name='%s'", device_id, code, sensor_cfg["name"])
                sensor_entity = TuyaSensor(
                    coordinator=coordinator,
                    device_name=device_name,
                    code=code,
                    name=sensor_cfg["name"],
                    device_class=sensor_cfg.get("device_class"),
                    unit=sensor_cfg.get("unit"),
                    state_class=sensor_cfg.get("state_class")
                )

                sensor_entities.append(sensor_entity)

        # Add all discovered sensor entities
        if sensor_entities:
            _LOGGER.info("Found %d Tuya sensors", len(sensor_entities))
            async_add_entities(sensor_entities, update_before_add=True)

        else:
            _LOGGER.warning("No compatible sensors found in your Tuya account")
            
    except Exception as e:
        _LOGGER.error("Error setting up Tuya sensors integration: %s", str(e))


class TuyaDataCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Tuya data."""

    def __init__(self, hass, logger, tuya_api, device_id, scan_interval, endpoint_used="status"):
        """Initialize."""

        if isinstance(scan_interval, int):
            scan_interval = timedelta(seconds=scan_interval)

        super().__init__(
            hass,
            logger,
            name=f"tuya_{device_id}",
            update_interval=scan_interval,
        )
        self._tuya_api = tuya_api
        self._device_id = device_id
        # Stored at discovery time so each poll goes directly to the correct
        # endpoint without retrying /status on battery-powered devices
        self._endpoint_used = endpoint_used

    async def _async_update_data(self):
        """Fetch data from Tuya API."""
        try:
            if self._endpoint_used == "properties":
                # Go straight to /properties — we know /status returns 2003 for this device
                _LOGGER.debug("Coordinator %s: polling /properties", self._device_id)
                response = await self.hass.async_add_executor_job(
                    self._tuya_api.get, f"/v2.0/cloud/thing/{self._device_id}/shadow/properties"
                )
                _LOGGER.debug("Coordinator %s: /properties poll response: %s", self._device_id, response)

                if not response.get("success", False):
                    raise UpdateFailed(f"Failed to get properties for device {self._device_id}: {response}")

                data = _normalise_properties(response.get("result", {}))
                _LOGGER.debug("Coordinator %s: normalised poll data: %s", self._device_id, data)
                return data
            else:
                # Standard /status path
                _LOGGER.debug("Coordinator %s: polling /status", self._device_id)
                response = await self.hass.async_add_executor_job(
                    self._tuya_api.get, f"/v1.0/devices/{self._device_id}/status"
                )
                _LOGGER.debug("Coordinator %s: /status poll response: %s", self._device_id, response)

                if not response.get("success", False):
                    # Check if we should switch to properties (happens if device switched from powered to battery?)
                    if response.get("code") == _TUYA_ERR_FUNCTION_NOT_SUPPORT:
                         _LOGGER.info("Device %s switched to properties endpoint", self._device_id)
                         self._endpoint_used = "properties"
                         return await self._async_update_data()
                    raise UpdateFailed(f"Failed to get status for device {self._device_id}: {response}")

                data = _normalise_status(response.get("result", []))
                _LOGGER.debug("Coordinator %s: normalised poll data: %s", self._device_id, data)
                return data

        except UpdateFailed:
            raise
        except Exception as e:
            raise UpdateFailed(f"Error communicating with Tuya API: {e}")

class TuyaSensor(SensorEntity):
    """Representation of a Tuya Sensor."""
    
    def __init__(self, coordinator, device_name, code, name, device_class, unit, state_class):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._device_id = coordinator._device_id
        self._code = code
        self._name = f"{device_name} {name}"
        
        # Set entity properties
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"tuya_{self._device_id}_{code}"
        
    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name
        
    @property
    def native_value(self):
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None
            
        # Extract value for this specific sensor
        for state in self.coordinator.data:
            if state.get("code") == self._code:
                value = state.get("value")
                # Temperature values from both endpoints are reported in tenths
                # of a degree (e.g. 235 → 23.5 °C)
                # We check the device class to decide if we should divide by 10.
                # In HA 2023.1+, SensorDeviceClass values are strings.
                if self._attr_device_class == SensorDeviceClass.TEMPERATURE or self._attr_device_class == "temperature":
                    try:
                        return int(value) / 10
                    except (TypeError, ValueError):
                        return None
                return value
        return None
        
    @property
    def available(self):
        """Return True if entity is available."""
        return self.coordinator.last_update_success
        
    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "device_id": self._device_id,
            "code": self._code,
            "endpoint": self.coordinator._endpoint_used,
            "last_updated": self.coordinator.last_update_success
        }
        
    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
        
    async def async_update(self):
        """Update entity."""
        await self.coordinator.async_request_refresh()