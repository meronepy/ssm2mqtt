"""ssm2mqtt - A bridge between Sesame 5 smart locks and MQTT.

This script connects to a Sesame 5 lock via Bluetooth Low Energy (BLE)
and publishes its mechanical status to an MQTT broker, while also
listening for commands to lock, unlock, or toggle the lock state.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import logging
import json
import os
import signal
import sys
from typing import AsyncGenerator
import aiomqtt
from gomalock.scanner import scan_sesame
from gomalock.sesame5 import Sesame5, Sesame5MechStatus

if sys.platform.lower() == "win32" or os.name.lower() == "nt":
    from asyncio import set_event_loop_policy, WindowsSelectorEventLoopPolicy

    set_event_loop_policy(WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO)
logging.getLogger("bleak").setLevel(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_json(filename: str):
    """Load a JSON configuration file from the module directory.

    Args:
        filename (str): Name of the JSON file to load.

    Returns:
        dict: Parsed JSON data.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file content is not valid JSON.
    """
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(config_path, "r", encoding="utf8") as f:
        return json.load(f)


@asynccontextmanager
async def sesame5_client(
    address: str, secret_key: str
) -> AsyncGenerator[Sesame5, None]:
    """Context manager to create and manage a Sesame5 client connection.

    This function scans for a Sesame 5 lock by its Bluetooth address,
    connects to it, and waits for it to log in using the provided secret key.

    Args:
        address (str): The Bluetooth address of the Sesame 5 lock.
        secret_key (str): The secret key for the Sesame 5 lock.

    Yields:
        Sesame5: An instance of the Sesame5 client connected to the lock.

    Raises:
        ConnectionError: If the device is already connected.
        RuntimeError: If an unexpected internal error occurs during scanning and
            if GATT characteristics are not found and if login command fails.
        TimeoutError: If the device is not found and if the connection attempt fails or times out.
        ValueError: If secret_key is not a 32-character hex string.

    """
    sesame5 = await scan_sesame(address, 20)
    await sesame5.connect()
    await sesame5.login(secret_key)
    try:
        yield sesame5
    finally:
        if sesame5.is_connected:
            await sesame5.disconnect()


@dataclass
class MqttConfig:
    """Configuration for MQTT connection and topics.

    Attributes:
        mqtt_host (str): IP address of the MQTT broker.
        mqtt_port (int): Port number of the MQTT broker.
        topic_publish (str): MQTT topic to publish the Sesame lock status.
        topic_subscribe (str): MQTT topic to subscribe for commands.
        mqtt_user (str | None): Username for MQTT authentication, if required.
        mqtt_password (str | None): Password for MQTT authentication, if required.
    """

    mqtt_host: str
    mqtt_port: int
    topic_publish: str
    topic_subscribe: str
    mqtt_user: str | None
    mqtt_password: str | None


@dataclass
class SesameConfig:
    """Configuration for Sesame.

    Attributes:
        sesame_address (str): Bluetooth address of the Sesame.
        sesame_secret_key (str): Secret key for the Sesame.
        history_name (str): History name for the operations.
        lock_command (str): Command to lock the Sesame and indicates in lock range.
        unlock_command (str): Command to unlock the Sesame and indicates in unlock range.
        toggle_command (str): Command to toggle the lock state.
    """

    sesame_address: str
    sesame_secret_key: str
    history_name: str
    lock_command: str
    unlock_command: str
    toggle_command: str


# Making class is reasonable to handle multiple devices, so pylint: disable-next=too-few-public-methods
class Ssm2MqttBridge:
    """Bridge between Sesame 5 smart locks and MQTT.

    This class handles the connection to the Sesame 5 lock and the MQTT broker,
    publishes the mechanical status of the lock, and listens for commands to
    lock, unlock, or toggle the lock state.

    Attributes:
        mqtt_config (MqttConfig): Configuration for MQTT connection and topics.
        sesame_config (SesameConfig): Configuration for Sesame lock.
    """

    def __init__(self, mqtt_config: MqttConfig, sesame_config: SesameConfig) -> None:
        self._mqtt_config = mqtt_config
        self._sesame_config = sesame_config

    async def _mechstatus_publisher(
        self,
        mqttc: aiomqtt.Client,
        topic_publish: str,
        mech_status: Sesame5MechStatus,
    ) -> None:
        lock_status = (
            self._sesame_config.lock_command
            if mech_status.is_in_lock_range
            else self._sesame_config.unlock_command
        )
        payload = json.dumps(
            {
                "position": mech_status.position,
                "lockCurrentState": lock_status,
                "batteryVoltage": mech_status.battery_voltage,
                "batteryLevel": mech_status.battery_percentage,
                "chargingState": "NOT_CHARGEABLE",
                "statusLowBattery": mech_status.is_battery_critical,
            }
        )
        await mqttc.publish(topic_publish, payload)

    async def _mqtt_command_handler(
        self, mqttc: aiomqtt.Client, sesame: Sesame5, topic_subscribe: str
    ) -> None:
        await mqttc.subscribe(topic_subscribe)
        async for message in mqttc.messages:
            if isinstance(message.payload, bytes):
                message.payload = message.payload.decode("utf-8")
            match message.payload:
                case self._sesame_config.lock_command:
                    await sesame.lock(self._sesame_config.history_name)
                case self._sesame_config.unlock_command:
                    await sesame.unlock(self._sesame_config.history_name)
                case self._sesame_config.toggle_command:
                    await sesame.toggle(self._sesame_config.history_name)
                case _:
                    logger.warning("Unknown command: %s", message.payload)

    async def start(self) -> None:
        """Start the ssm2mqtt bridge.

        This method initializes the MQTT client and Sesame client, sets up
        the callback for mechanical status changes, and starts listening for
        commands from the MQTT broker. It also handles graceful shutdown on
        termination signals.
        """
        logger.info("Starting ssm2mqtt.")
        logger.info(
            "MQTT info → host: %s, port: %s, user: %s",
            self._mqtt_config.mqtt_host,
            self._mqtt_config.mqtt_port,
            self._mqtt_config.mqtt_user,
        )
        logger.info(
            "Topic info → publish: %s, subscribe: %s",
            self._mqtt_config.topic_publish,
            self._mqtt_config.topic_subscribe,
        )
        logger.info(
            "Sesame info → address: %s, history name: %s",
            self._sesame_config.sesame_address,
            self._sesame_config.history_name,
        )
        logger.info(
            "Command info → lock: %s, unlock: %s, toggle: %s",
            self._sesame_config.lock_command,
            self._sesame_config.unlock_command,
            self._sesame_config.toggle_command,
        )
        async with AsyncExitStack() as stack:
            logger.debug("Connecting to mqtt broker.")
            mqttc = await stack.enter_async_context(
                aiomqtt.Client(
                    self._mqtt_config.mqtt_host,
                    self._mqtt_config.mqtt_port,
                    username=self._mqtt_config.mqtt_user,
                    password=self._mqtt_config.mqtt_password,
                )
            )
            logger.debug("Conneceted to mqtt broker.")

            logger.debug("Connecting to sesame.")
            sesame = await stack.enter_async_context(
                sesame5_client(
                    self._sesame_config.sesame_address,
                    self._sesame_config.sesame_secret_key,
                )
            )
            logger.debug("Connected to sesame.")

            async def on_mechstatus_changed(status: Sesame5MechStatus) -> None:
                await self._mechstatus_publisher(
                    mqttc, self._mqtt_config.topic_publish, status
                )

            sesame.enable_mechstatus_callback(on_mechstatus_changed)

            async with asyncio.TaskGroup() as tg:
                task = tg.create_task(
                    self._mqtt_command_handler(
                        mqttc, sesame, self._mqtt_config.topic_subscribe
                    )
                )
                stop_event = asyncio.Event()

                def stop() -> None:
                    logger.info("Stopping ssm2mqtt.")
                    if not task.done():
                        task.cancel()
                    stop_event.set()

                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        asyncio.get_running_loop().add_signal_handler(sig, stop)
                    except NotImplementedError:
                        logger.warning(
                            "Signal handlers for %s are not supported on this platform. "
                            "ssm2mqtt may not shutdown gracefully.",
                            sig,
                        )
                logger.info("ssm2mqtt started successfully.")
                await stop_event.wait()
        logger.info("ssm2mqtt stopped gracefully.")


async def main():
    """Main entry point for the ssm2mqtt application.

    This function loads the configuration from a JSON file, initializes the
    MQTT and Sesame configurations, and starts the Ssm2MqttBridge.
    """
    logger.debug("Loading configuration.")
    config = load_json("config.json")
    logger.debug("Configuration loaded.")
    mqtt_config = MqttConfig(
        mqtt_host=config["mqtt"]["host"],
        mqtt_port=config["mqtt"]["port"],
        topic_publish=config["mqtt"]["topic_publish"],
        topic_subscribe=config["mqtt"]["topic_subscribe"],
        mqtt_user=config["mqtt"]["user"] or None,
        mqtt_password=config["mqtt"]["password"] or None,
    )
    sesame_config = SesameConfig(
        sesame_address=config["sesame"]["mac_address"],
        sesame_secret_key=config["sesame"]["secret_key"],
        history_name=config["sesame"]["history_name"],
        lock_command=config["sesame"]["lock_command"],
        unlock_command=config["sesame"]["unlock_command"],
        toggle_command=config["sesame"]["toggle_command"],
    )
    bridge = Ssm2MqttBridge(mqtt_config, sesame_config)
    await bridge.start()


if __name__ == "__main__":
    asyncio.run(main())
