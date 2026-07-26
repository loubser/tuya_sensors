"""Config flow for Tuya Sensors integration."""
import logging
from typing import Any, Dict, List, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import CONF_API_KEY, CONF_REGION, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from . import DOMAIN
from .sensor import _fetch_properties

_LOGGER = logging.getLogger(__name__)

# Schema constants
CONF_API_SECRET = "api_secret"
CONF_DEVICE_IDS = "device_ids"
CONF_SENSORS = "sensors"

# Region options
REGIONS = ["us", "eu", "cn", "in"]


class TuyaSensorsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya Sensors integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.data: Dict[str, Any] = {}
        self.available_properties: List[str] = []
        self.selected_codes: List[str] = []
        self.selected_property_index: int = 0

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle the initial step where the user enters Tuya credentials."""
        errors_ui: Dict[str, str] = {}

        if user_input is not None:
            # Process device IDs as a list
            device_ids = [
                id_str.strip()
                for id_str in user_input[CONF_DEVICE_IDS].split(",")
                if id_str.strip()
            ]

            # Verify credentials and fetch properties
            try:
                # Pre-load tuya_connector in executor to avoid event loop blocking
                await self.hass.async_add_executor_job(__import__, "tuya_connector")
                from tuya_connector import TuyaOpenAPI

                endpoint = f"https://openapi.tuya{user_input[CONF_REGION]}.com"
                tuya_api = TuyaOpenAPI(
                    access_id=user_input[CONF_API_KEY],
                    access_secret=user_input[CONF_API_SECRET],
                    endpoint=endpoint,
                )
                response = await self.hass.async_add_executor_job(tuya_api.connect)

                if not response.get("success", False):
                    errors_ui["base"] = "invalid_auth"
                else:
                    self.available_properties, errors = await _fetch_properties(
                        self.hass, tuya_api, device_ids
                    )
                    if not self.available_properties:
                        if "data_center_error" in errors:
                            errors_ui["base"] = "data_center_error"
                        else:
                            errors_ui["base"] = "no_properties_found"
                    else:
                        self.data = user_input
                        self.data[CONF_DEVICE_IDS] = device_ids
                        self.data[CONF_SENSORS] = []
                        return await self.async_step_select_sensors()
            except Exception as e:
                _LOGGER.error("Failed to connect to Tuya Cloud: %s", e, exc_info=True)
                errors_ui["base"] = "cannot_connect"

        # Show form for credentials
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Required(CONF_API_SECRET): str,
                    vol.Required(CONF_DEVICE_IDS): str,
                    vol.Required(CONF_REGION, default="us"): vol.In(REGIONS),
                    vol.Optional(CONF_SCAN_INTERVAL, default=60): vol.All(
                        vol.Coerce(int), vol.Range(min=30, max=1800)
                    ),
                }
            ),
            errors=errors_ui,
        )

    async def async_step_select_sensors(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step to select which properties to use as sensors."""
        if user_input is not None:
            selected_codes = user_input.get("selected_codes", [])
            self.selected_codes = selected_codes
            if not selected_codes:
                return await self.async_step_select_sensors()

            self.selected_property_index = 0
            return await self.async_step_configure_sensor()

        return self.async_show_form(
            step_id="select_sensors",
            data_schema=vol.Schema(
                {
                    vol.Required("selected_codes"): cv.multi_select(
                        {code: code for code in self.available_properties}
                    ),
                }
            ),
        )

    async def async_step_configure_sensor(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step to configure individual sensor details (name, class, unit, factor)."""
        if self.selected_property_index >= len(self.selected_codes):
            return self.async_create_entry(
                title=f"Tuya Cloud ({self.data[CONF_REGION]})",
                data=self.data,
            )

        code = self.selected_codes[self.selected_property_index]

        if user_input is not None:
            self.data[CONF_SENSORS].append(
                {
                    "code": code,
                    "name": user_input["name"],
                    "device_class": user_input.get("device_class"),
                    "unit": user_input.get("unit"),
                    "state_class": user_input.get("state_class"),
                    "factor": user_input.get("factor", 1.0),
                }
            )
            self.selected_property_index += 1
            return await self.async_step_configure_sensor()

        # Get list of device classes and state classes for dropdowns
        device_classes = [None] + sorted([item.value for item in SensorDeviceClass])
        state_classes = [None] + sorted([item.value for item in SensorStateClass])

        # Suggest a default factor based on property code name
        default_factor = 1.0
        if any(word in code.lower() for word in ["temp", "temperature", "humidity", "pressure"]):
            default_factor = 0.1

        return self.async_show_form(
            step_id="configure_sensor",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=code.replace("_", " ").title()): str,
                    vol.Optional("device_class"): vol.In(device_classes),
                    vol.Optional("unit"): str,
                    vol.Optional("state_class"): vol.In(state_classes),
                    vol.Optional("factor", default=default_factor): vol.Coerce(float),
                }
            ),
            description_placeholders={"code": code},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "TuyaOptionsFlowHandler":
        """Get the options flow for this handler."""
        return TuyaOptionsFlowHandler(config_entry)


class TuyaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Tuya integration options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.data: Dict[str, Any] = {}
        self.available_properties: List[str] = []
        self.selected_codes: List[str] = []
        self.selected_property_index: int = 0

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Manage the options, allowing updates to device IDs and scan interval."""
        errors_ui: Dict[str, str] = {}

        if user_input is not None:
            device_ids = [
                id_str.strip()
                for id_str in user_input[CONF_DEVICE_IDS].split(",")
                if id_str.strip()
            ]

            self.data = dict(self.config_entry.data)
            self.data.update(user_input)
            self.data[CONF_DEVICE_IDS] = device_ids

            # If user wants to re-configure sensors, start discovery
            if user_input.get("reconfigure_sensors"):
                try:
                    await self.hass.async_add_executor_job(__import__, "tuya_connector")
                    from tuya_connector import TuyaOpenAPI

                    endpoint = f"https://openapi.tuya{self.data[CONF_REGION]}.com"
                    tuya_api = TuyaOpenAPI(
                        access_id=self.data[CONF_API_KEY],
                        access_secret=self.data[CONF_API_SECRET],
                        endpoint=endpoint,
                    )
                    await self.hass.async_add_executor_job(tuya_api.connect)

                    self.available_properties, errors = await _fetch_properties(
                        self.hass, tuya_api, device_ids
                    )
                    if not self.available_properties:
                        if "data_center_error" in errors:
                            errors_ui["base"] = "data_center_error"
                        else:
                            errors_ui["base"] = "no_properties_found"
                    else:
                        self.data[CONF_SENSORS] = []
                        return await self.async_step_select_sensors()
                except Exception:
                    errors_ui["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="", data=self.data)

        data = self.config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_IDS, default=", ".join(data.get(CONF_DEVICE_IDS, []))
                    ): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=data.get(CONF_SCAN_INTERVAL, 60)
                    ): vol.All(vol.Coerce(int), vol.Range(min=30, max=1800)),
                    vol.Optional("reconfigure_sensors", default=False): bool,
                }
            ),
            errors=errors_ui,
        )

    async def async_step_select_sensors(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step to select which properties to use as sensors during reconfiguration."""
        if user_input is not None:
            selected_codes = user_input.get("selected_codes", [])
            self.selected_codes = selected_codes
            self.selected_property_index = 0
            return await self.async_step_configure_sensor()

        return self.async_show_form(
            step_id="select_sensors",
            data_schema=vol.Schema(
                {
                    vol.Required("selected_codes"): cv.multi_select(
                        {code: code for code in self.available_properties}
                    ),
                }
            ),
        )

    async def async_step_configure_sensor(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step to configure individual sensor details during reconfiguration."""
        if self.selected_property_index >= len(self.selected_codes):
            return self.async_create_entry(title="", data=self.data)

        code = self.selected_codes[self.selected_property_index]

        if user_input is not None:
            self.data[CONF_SENSORS].append(
                {
                    "code": code,
                    "name": user_input["name"],
                    "device_class": user_input.get("device_class"),
                    "unit": user_input.get("unit"),
                    "state_class": user_input.get("state_class"),
                    "factor": user_input.get("factor", 1.0),
                }
            )
            self.selected_property_index += 1
            return await self.async_step_configure_sensor()

        device_classes = [None] + sorted([item.value for item in SensorDeviceClass])
        state_classes = [None] + sorted([item.value for item in SensorStateClass])

        # Suggest a default factor
        default_factor = 1.0
        if any(word in code.lower() for word in ["temp", "temperature", "humidity", "pressure"]):
            default_factor = 0.1

        return self.async_show_form(
            step_id="configure_sensor",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=code.replace("_", " ").title()): str,
                    vol.Optional("device_class"): vol.In(device_classes),
                    vol.Optional("unit"): str,
                    vol.Optional("state_class"): vol.In(state_classes),
                    vol.Optional("factor", default=default_factor): vol.Coerce(float),
                }
            ),
            description_placeholders={"code": code},
        )