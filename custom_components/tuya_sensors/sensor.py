"""Sensor platform for Tuya sensors integration."""
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

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

# Tuya API error codes
_TUYA_ERR_FUNCTION_NOT_SUPPORT = 2003
_TUYA_ERR_DATA_CENTER_SUSPENDED = 28841107

async def _fetch_properties(
    hass: HomeAssistant, tuya_api: Any, device_ids: List[str]
) -> Tuple[List[str], List[str]]:
    """Fetch all available property codes for the given device IDs.

    This method attempts to discover available properties by querying multiple
    Tuya API endpoints, as different device types report their capabilities
    differently.
    """
    property_codes: Set[str] = set()
    errors_found: Set[str] = set()

    for device_id in device_ids:
        # Define endpoints to try for discovery
        endpoints = [
            (f"/v1.0/devices/{device_id}/status", "status", "list"),
            (f"/v2.0/cloud/thing/{device_id}/shadow/properties", "properties", "dict"),
            (f"/v1.0/devices/{device_id}/functions", "functions", "dict"),
            (f"/v1.0/devices/{device_id}/specifications", "specifications", "dict"),
            (f"/v1.0/devices/{device_id}", "device info", "dict"),
            (f"/v1.0/iot-03/devices/{device_id}/status", "iot-03 status", "list"),
        ]

        for path, name, result_type in endpoints:
            _LOGGER.debug("Device %s: trying %s endpoint to discover properties", device_id, name)
            try:
                response = await hass.async_add_executor_job(tuya_api.get, path)
                _LOGGER.debug("Device %s: %s discovery response: %s", device_id, name, response)

                if not response.get("success", False):
                    code = response.get("code")
                    if code == _TUYA_ERR_DATA_CENTER_SUSPENDED:
                        if name == "status":
                            _LOGGER.error(
                                "Device %s: Tuya Cloud Error %s - The data center is suspended or no permission. "
                                "Please ensure the correct Data Center is enabled in your Tuya IoT Platform project settings.",
                                device_id,
                                code,
                            )
                        errors_found.add("data_center_error")
                    continue

                res = response.get("result")
                if not res:
                    continue

                # Parse property codes based on endpoint structure
                if name == "specifications":
                    for key in ["functions", "status"]:
                        for item in res.get(key, []):
                            if isinstance(item, dict) and "code" in item:
                                property_codes.add(item["code"])
                elif name == "device info":
                    for item in res.get("status", []):
                        if isinstance(item, dict) and "code" in item:
                            property_codes.add(item["code"])
                elif name == "properties" and isinstance(res, dict):
                    for item in res.get("properties", []):
                        if isinstance(item, dict) and "code" in item:
                            property_codes.add(item["code"])
                elif name == "functions" and isinstance(res, dict):
                    for item in res.get("functions", []):
                        if isinstance(item, dict) and "code" in item:
                            property_codes.add(item["code"])
                elif isinstance(res, list):
                    for item in res:
                        if isinstance(item, dict) and "code" in item:
                            property_codes.add(item["code"])

            except Exception as e:
                _LOGGER.error("Error fetching %s for device %s: %s", name, device_id, e)

    if not property_codes:
        _LOGGER.warning(
            "Discovery finished: No properties found for device IDs: %s. Check debug logs for API responses.",
            device_ids,
        )

    return sorted(list(property_codes)), list(errors_found)


def _normalise_status(result: Any) -> List[Dict[str, Any]]:
    """Return a flat [{code, value}] list from a /status result payload.

    The /status endpoint returns result as a list directly:
        [{"code": "temp_current", "value": 235}, ...]
    """
    if isinstance(result, list):
        return result
    _LOGGER.warning("Unexpected /status result shape: %s", result)
    return []


def _normalise_properties(result: Any) -> List[Dict[str, Any]]:
    """Return a flat [{code, value}] list from a /properties result payload.

    The /properties endpoint wraps data one level deeper:
        {"properties": [{"code": "va_temperature", "value": 235, "time": ...}, ...]}
    """
    if isinstance(result, dict):
        props = result.get("properties", [])
        return [
            {"code": p["code"], "value": p["value"]}
            for p in props
            if isinstance(p, dict) and "code" in p and "value" in p
        ]
    _LOGGER.warning("Unexpected /properties result shape: %s", result)
    return []


async def _async_fetch_status(
    hass: HomeAssistant, tuya_api: Any, device_id: str
) -> Tuple[List[Dict[str, Any]], str]:
    """Fetch device status from both /status and /properties to find the best source.

    Returns (normalised_data, endpoint_used) where endpoint_used is either
    "status" or "properties".
    """
    status_data: List[Dict[str, Any]] = []
    properties_data: List[Dict[str, Any]] = []
    
    # 1. Try /status
    _LOGGER.debug("Device %s: trying /status endpoint", device_id)
    try:
        response = await hass.async_add_executor_job(
            tuya_api.get, f"/v1.0/devices/{device_id}/status"
        )
        _LOGGER.debug("Device %s: /status raw response: %s", device_id, response)
        if response.get("success", False):
            status_data = _normalise_status(response.get("result", []))
    except Exception as e:
        _LOGGER.debug("Device %s: /status call failed: %s", device_id, e)

    # 2. Try /properties
    _LOGGER.debug("Device %s: trying /properties endpoint", device_id)
    try:
        response = await hass.async_add_executor_job(
            tuya_api.get, f"/v2.0/cloud/thing/{device_id}/shadow/properties"
        )
        _LOGGER.debug("Device %s: /properties raw response: %s", device_id, response)
        if response.get("success", False):
            properties_data = _normalise_properties(response.get("result", {}))
    except Exception as e:
        _LOGGER.debug("Device %s: /properties call failed: %s", device_id, e)

    # Decision logic:
    # If both have data, pick the one with more data points.
    # If they have the same amount, prefer /status (legacy/standard).
    if len(properties_data) > len(status_data):
        _LOGGER.debug("Device %s: choosing 'properties' endpoint (more data: %d vs %d)", 
                     device_id, len(properties_data), len(status_data))
        return properties_data, "properties"
    
    if status_data:
        _LOGGER.debug("Device %s: choosing 'status' endpoint (%d data points)", device_id, len(status_data))
        return status_data, "status"
        
    if properties_data:
        _LOGGER.debug("Device %s: choosing 'properties' endpoint (%d data points)", device_id, len(properties_data))
        return properties_data, "properties"

    raise UpdateFailed(f"Failed to get status for device {device_id} from both endpoints")


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Optional[DiscoveryInfoType] = None,
) -> None:
    """Set up the Tuya sensor platform from configuration.yaml."""
    domain_config = hass.data[DOMAIN]
    await _async_setup(hass, domain_config, async_add_entities)


async def async_setup_entry(
    hass: HomeAssistant, entry: Any, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Tuya sensor platform from a config entry."""
    domain_config = hass.data[DOMAIN].get(entry.entry_id)
    await _async_setup(hass, domain_config, async_add_entities, entry.entry_id)


async def _async_setup(
    hass: HomeAssistant,
    domain_config: Dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    entry_id: Optional[str] = None,
) -> None:
    """Internal method to set up the Tuya sensor platform."""
    from tuya_connector import TUYA_LOGGER, TuyaOpenAPI
    
    # Set up logging for tuya_connector
    TUYA_LOGGER.setLevel(logging.INFO)

    api_key = domain_config["api_key"]
    api_secret = domain_config["api_secret"]
    device_ids = domain_config.get("device_ids", [])
    sensors_config = domain_config.get("sensors", [])
    region = domain_config["region"]
    scan_interval = domain_config["scan_interval"]

    # Set appropriate endpoint based on region
    endpoint = f"https://openapi.tuya{region}.com"

    # Initialize API connection
    tuya_api = TuyaOpenAPI(
        access_id=api_key, access_secret=api_secret, endpoint=endpoint
    )

    try:
        # Get access token
        response = await hass.async_add_executor_job(tuya_api.connect)

        if not response.get("success", False):
            _LOGGER.error("Failed to get access token: %s", response)
            return

        all_devices: List[Dict[str, Any]] = []

        # Fetch device information
        if device_ids:
            for device_id in device_ids:
                try:
                    response = await hass.async_add_executor_job(
                        tuya_api.get, f"/v1.0/devices/{device_id}"
                    )

                    if not response.get("success", False):
                        _LOGGER.error(
                            "Failed to get device info for %s: %s", device_id, response
                        )
                        continue

                    all_devices.append(response.get("result", {}))
                except Exception as e:
                    _LOGGER.error("Error processing device %s: %s", device_id, str(e))
        else:
            response = await hass.async_add_executor_job(tuya_api.get, "/v1.0/devices")
            if not response.get("success", False):
                _LOGGER.error("Failed to get devices: %s", response)
                return
            all_devices = response.get("result", [])

        _LOGGER.debug(
            "Processing %d device(s): %s",
            len(all_devices),
            [d.get("id") for d in all_devices],
        )

        sensor_entities: List[TuyaSensor] = []

        # Process each device to discover sensors
        for device in all_devices:
            device_id = device.get("id")
            if not device_id:
                continue

            device_name = device.get("name", f"Device {device_id}")
            _LOGGER.debug(
                "Device %s (%s): starting sensor discovery", device_id, device_name
            )

            try:
                # Fetch status via /status, falling back to /properties if needed.
                status_data, endpoint_used = await _async_fetch_status(
                    hass, tuya_api, device_id
                )
                _LOGGER.debug(
                    "Device %s: using '%s' endpoint (%d data points)",
                    device_id,
                    endpoint_used,
                    len(status_data),
                )
            except UpdateFailed as e:
                _LOGGER.warning("%s", e)
                continue

            # Create a coordinator for this device
            coordinator = TuyaDataCoordinator(
                hass, _LOGGER, tuya_api, device_id, scan_interval, endpoint_used
            )

            # Seed the coordinator with the initial data we just fetched
            # so that sensors have state immediately on startup.
            coordinator.async_set_updated_data(status_data)

            # Process configured sensors
            for sensor_cfg in sensors_config:
                code = sensor_cfg.get("code")

                # Check if this sensor data point exists in the device status
                if not any(s.get("code") == code for s in status_data):
                    _LOGGER.debug(
                        "Device %s: code '%s' not found in status data, skipping",
                        device_id,
                        code,
                    )
                    continue

                _LOGGER.debug(
                    "Device %s: creating entity for code '%s' name='%s'",
                    device_id,
                    code,
                    sensor_cfg["name"],
                )
                sensor_entities.append(
                    TuyaSensor(
                        coordinator=coordinator,
                        device_name=device_name,
                        code=code,
                        name=sensor_cfg["name"],
                        device_class=sensor_cfg.get("device_class"),
                        unit=sensor_cfg.get("unit"),
                        state_class=sensor_cfg.get("state_class"),
                        factor=sensor_cfg.get("factor", 1.0),
                        entry_id=entry_id,
                    )
                )

        if sensor_entities:
            _LOGGER.info("Found %d Tuya sensors", len(sensor_entities))
            async_add_entities(sensor_entities, update_before_add=False)
        else:
            _LOGGER.warning("No compatible sensors found in your Tuya account")

    except Exception as e:
        _LOGGER.error("Error setting up Tuya sensors integration: %s", str(e))


class TuyaDataCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Tuya data from the Cloud API.

    This coordinator handles polling for a single Tuya device, ensuring that
    battery-powered devices use the appropriate /properties endpoint while
    mains-powered devices use /status.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        tuya_api: Any,
        device_id: str,
        scan_interval: Any,
        endpoint_used: str = "status",
    ) -> None:
        """Initialize the Tuya data coordinator."""
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

    async def _async_update_data(self) -> List[Dict[str, Any]]:
        """Fetch data from Tuya API."""
        try:
            if self._endpoint_used == "properties":
                # Go straight to /properties — we know /status returns 2003 for this device
                _LOGGER.debug("Coordinator %s: polling /properties", self._device_id)
                response = await self.hass.async_add_executor_job(
                    self._tuya_api.get,
                    f"/v2.0/cloud/thing/{self._device_id}/shadow/properties",
                )
                _LOGGER.debug(
                    "Coordinator %s: /properties poll response: %s",
                    self._device_id,
                    response,
                )

                if not response.get("success", False):
                    raise UpdateFailed(
                        f"Failed to get properties for device {self._device_id}: {response}"
                    )

                data = _normalise_properties(response.get("result", {}))
                _LOGGER.debug(
                    "Coordinator %s: normalised poll data: %s", self._device_id, data
                )
                return data

            # Standard /status path
            _LOGGER.debug("Coordinator %s: polling /status", self._device_id)
            response = await self.hass.async_add_executor_job(
                self._tuya_api.get, f"/v1.0/devices/{self._device_id}/status"
            )
            _LOGGER.debug(
                "Coordinator %s: /status poll response: %s", self._device_id, response
            )

            if not response.get("success", False):
                # Check if we should switch to properties (happens if device switched from powered to battery?)
                if response.get("code") == _TUYA_ERR_FUNCTION_NOT_SUPPORT:
                    _LOGGER.info(
                        "Device %s switched to properties endpoint", self._device_id
                    )
                    self._endpoint_used = "properties"
                    return await self._async_update_data()
                raise UpdateFailed(
                    f"Failed to get status for device {self._device_id}: {response}"
                )

            data = _normalise_status(response.get("result", []))
            _LOGGER.debug(
                "Coordinator %s: normalised poll data: %s", self._device_id, data
            )
            return data

        except UpdateFailed:
            raise
        except Exception as e:
            raise UpdateFailed(f"Error communicating with Tuya API: {e}") from e


class TuyaSensor(SensorEntity):
    """Representation of a Tuya Sensor.

    These sensors are created dynamically based on properties discovered on
    the Tuya Cloud platform.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TuyaDataCoordinator,
        device_name: str,
        code: str,
        name: str,
        device_class: Optional[str],
        unit: Optional[str],
        state_class: Optional[str],
        factor: float = 1.0,
        entry_id: Optional[str] = None,
    ) -> None:
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._device_id = coordinator._device_id
        self._code = code
        self._name = f"{device_name} {name}"
        self._factor = factor

        # Set entity properties
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit

        # Use entry_id in unique_id if available to avoid collisions between multiple installations
        if entry_id:
            self._attr_unique_id = f"tuya_{entry_id}_{self._device_id}_{code}"
        else:
            self._attr_unique_id = f"tuya_{self._device_id}_{code}"

        # Set should_poll to False as we use a DataUpdateCoordinator
        self._attr_should_poll = False

        # Add device info to group sensors under a single device in HA
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": device_name,
            "manufacturer": "Tuya",
            "model": "Tuya Cloud Sensor",
        }

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._name

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None

        # Extract value for this specific sensor
        for state in self.coordinator.data:
            if state.get("code") == self._code:
                value = state.get("value")
                try:
                    return float(value) * self._factor
                except (TypeError, ValueError):
                    return value
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        # We always return True to avoid 'unavailable' states in history.
        # As long as the coordinator has some data, the sensor will show the last value.
        return self.coordinator.data is not None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return the state attributes."""
        return {
            "device_id": self._device_id,
            "code": self._code,
            "endpoint": self.coordinator._endpoint_used,
            "last_updated": self.coordinator.last_update_success,
            "poll_success": self.coordinator.last_update_success,
        }

    async def async_added_to_hass(self) -> None:
        """Connect to dispatcher listening for entity data notifications."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )