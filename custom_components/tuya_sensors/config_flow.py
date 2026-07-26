# Define the configuration flow for Tuya integration
# File: custom_components/tuya_sensors/config_flow.py

import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL
import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)

_LOGGER = logging.getLogger(__name__)

# Define schema constants
DOMAIN = "tuya_sensors"
CONF_API_SECRET = "api_secret"
CONF_REGION = "region"
CONF_DEVICE_IDS = "device_ids"
CONF_SENSORS = "sensors"

# Region options
REGIONS = ["us", "eu", "cn", "in"]

class TuyaSensorsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya Sensors integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize."""
        self.data = {}
        self.available_properties = []
        self.selected_property_index = 0

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors_ui = {}

        if user_input is not None:
            # Process device IDs as a list
            device_ids = [id.strip() for id in user_input[CONF_DEVICE_IDS].split(",") if id.strip()]
            
            # Verify credentials and fetch properties
            try:
                # Pre-load tuya_connector in executor
                await self.hass.async_add_executor_job(__import__, "tuya_connector")
                from tuya_connector import TuyaOpenAPI
                endpoint = f"https://openapi.tuya{user_input[CONF_REGION]}.com"
                tuya_api = TuyaOpenAPI(
                    access_id=user_input[CONF_API_KEY],
                    access_secret=user_input[CONF_API_SECRET],
                    endpoint=endpoint
                )
                response = await self.hass.async_add_executor_job(tuya_api.connect)
                if not response.get("success", False):
                    errors_ui["base"] = "invalid_auth"
                else:
                    from .sensor import _fetch_properties
                    self.available_properties, errors = await _fetch_properties(self.hass, tuya_api, device_ids)
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
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): str,
                vol.Required(CONF_API_SECRET): str,
                vol.Required(CONF_DEVICE_IDS): str,
                vol.Required(CONF_REGION, default="us"): vol.In(REGIONS),
                vol.Optional(CONF_SCAN_INTERVAL, default=60): vol.All(
                    vol.Coerce(int), vol.Range(min=30, max=1800)
                ),
            }),
            errors=errors_ui,
        )

    async def async_step_select_sensors(self, user_input=None):
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
            data_schema=vol.Schema({
                vol.Required("selected_codes"): cv.multi_select(
                    {code: code for code in self.available_properties}
                ),
            }),
        )

    async def async_step_configure_sensor(self, user_input=None):
        """Step to configure individual sensor details."""
        if self.selected_property_index >= len(self.selected_codes):
            return self.async_create_entry(
                title=f"Tuya Cloud ({self.data[CONF_REGION]})",
                data=self.data,
            )

        code = self.selected_codes[self.selected_property_index]

        if user_input is not None:
            self.data[CONF_SENSORS].append({
                "code": code,
                "name": user_input["name"],
                "device_class": user_input.get("device_class"),
                "unit": user_input.get("unit"),
                "state_class": user_input.get("state_class"),
            })
            self.selected_property_index += 1
            return await self.async_step_configure_sensor()

        # Get list of device classes and state classes
        device_classes = [None] + sorted([item.value for item in SensorDeviceClass])
        state_classes = [None] + sorted([item.value for item in SensorStateClass])

        return self.async_show_form(
            step_id="configure_sensor",
            data_schema=vol.Schema({
                vol.Required("name", default=code.replace("_", " ").title()): str,
                vol.Optional("device_class"): vol.In(device_classes),
                vol.Optional("unit"): str,
                vol.Optional("state_class"): vol.In(state_classes),
            }),
            description_placeholders={"code": code},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return TuyaOptionsFlowHandler()


class TuyaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Tuya integration options."""

    def __init__(self):
        """Initialize options flow."""
        self.data = {}
        self.available_properties = []
        self.selected_property_index = 0

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors_ui = {}

        if user_input is not None:
            # Update credentials and fetch properties if needed, or just update scan interval
            # For simplicity, let's just allow updating scan interval and device ids here.
            # If they change device IDs, we should ideally re-run discovery.
            
            device_ids = [id.strip() for id in user_input[CONF_DEVICE_IDS].split(",") if id.strip()]
            
            # If device ids changed, we might want to re-discover properties.
            # But let's keep it simple and just update the current ones if we are not re-running the whole flow.
            
            self.data = dict(self.config_entry.data)
            self.data.update(user_input)
            self.data[CONF_DEVICE_IDS] = device_ids
            
            # If user wants to re-configure sensors
            if user_input.get("reconfigure_sensors"):
                try:
                    # Pre-load tuya_connector in executor
                    await self.hass.async_add_executor_job(__import__, "tuya_connector")
                    from tuya_connector import TuyaOpenAPI
                    endpoint = f"https://openapi.tuya{self.data[CONF_REGION]}.com"
                    tuya_api = TuyaOpenAPI(
                        access_id=self.data[CONF_API_KEY],
                        access_secret=self.data[CONF_API_SECRET],
                        endpoint=endpoint
                    )
                    await self.hass.async_add_executor_job(tuya_api.connect)
                    from .sensor import _fetch_properties
                    self.available_properties, errors = await _fetch_properties(self.hass, tuya_api, device_ids)
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
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_IDS, default=", ".join(data.get(CONF_DEVICE_IDS, []))): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=data.get(CONF_SCAN_INTERVAL, 60)): vol.All(
                    vol.Coerce(int), vol.Range(min=30, max=1800)
                ),
                vol.Optional("reconfigure_sensors", default=False): bool,
            }),
            errors=errors_ui,
        )

    async def async_step_select_sensors(self, user_input=None):
        """Step to select which properties to use as sensors."""
        if user_input is not None:
            selected_codes = user_input.get("selected_codes", [])
            self.selected_codes = selected_codes
            self.selected_property_index = 0
            return await self.async_step_configure_sensor()

        return self.async_show_form(
            step_id="select_sensors",
            data_schema=vol.Schema({
                vol.Required("selected_codes"): cv.multi_select(
                    {code: code for code in self.available_properties}
                ),
            }),
        )

    async def async_step_configure_sensor(self, user_input=None):
        """Step to configure individual sensor details."""
        if self.selected_property_index >= len(self.selected_codes):
            return self.async_create_entry(title="", data=self.data)

        code = self.selected_codes[self.selected_property_index]

        if user_input is not None:
            self.data[CONF_SENSORS].append({
                "code": code,
                "name": user_input["name"],
                "device_class": user_input.get("device_class"),
                "unit": user_input.get("unit"),
                "state_class": user_input.get("state_class"),
            })
            self.selected_property_index += 1
            return await self.async_step_configure_sensor()

        device_classes = [None] + sorted([item.value for item in SensorDeviceClass])
        state_classes = [None] + sorted([item.value for item in SensorStateClass])

        return self.async_show_form(
            step_id="configure_sensor",
            data_schema=vol.Schema({
                vol.Required("name", default=code.replace("_", " ").title()): str,
                vol.Optional("device_class"): vol.In(device_classes),
                vol.Optional("unit"): str,
                vol.Optional("state_class"): vol.In(state_classes),
            }),
            description_placeholders={"code": code},
        )