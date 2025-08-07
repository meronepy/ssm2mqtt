"""MQTT-to-Sesame 5 bridge for lock control and status reporting.

This module connects to a set of Sesame smart locks over Bluetooth using the gomalock
library, and integrates them with an MQTT broker. It listens for lock/unlock commands
via MQTT, forwards them to the appropriate devices, and publishes status updates back
to MQTT topics.
"""

import asyncio
import contextlib
import functools
import json
import logging
import signal
import uuid
from typing import NamedTuple

import aiomqtt
from gomalock import sesame5

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("bleak").setLevel(level=logging.WARNING)
logging.getLogger("gomalock").setLevel(level=logging.WARNING)
logger = logging.getLogger(__name__)


class TerminateTaskGroup(Exception):
    """Exception raised to terminate a task group."""


class MqttConfig(NamedTuple):
    """Stores the configuration related to MQTT.

    Attributes:
        base_topic: Topic prefix used for publishing and subscribing.
        host: IP address of the MQTT broker.
        port: Port of the MQTT broker.
        user: MQTT username if using authentication.
        password: MQTT password if using authentication.
    """

    base_topic: str
    host: str
    port: int
    user: str | None
    password: str | None


class TargetDevice(NamedTuple):
    """Stores the information of the target device.

    Attributes:
        mac_address: MAC address of Sesame device.
        secret_key: Secret key of Sesame device.
    """

    mac_address: str
    secret_key: str


class QueueCommand(NamedTuple):
    """Data structure to use for queues.

    Attributes:
        device_uuid: UUID specifying the desired device.
        payload: Operation instructions or Sesame status.
    """

    device_uuid: uuid.UUID
    payload: str


async def force_terminate_task_group():
    """Used to force termination of a task group."""
    raise TerminateTaskGroup()


def load_config() -> tuple[str, MqttConfig, list[TargetDevice]]:
    """Loads configuration from `config.json`.

    Returns:
        A tuple containing the name used for history logging,
        MQTT connection settings, List of configured target devices.
    """
    with open("config.json", "r", encoding="utf8") as f:
        user_config = json.load(f)
        history_name: str = user_config["history_name"]
        mqtt_config = MqttConfig(
            user_config["mqtt"]["base_topic"],
            user_config["mqtt"]["host"],
            user_config["mqtt"]["port"],
            user_config["mqtt"]["user"] or None,
            user_config["mqtt"]["password"] or None,
        )
        target_devices = [
            TargetDevice(address, key)
            for address, key in user_config["devices"].items()
        ]
    return history_name, mqtt_config, target_devices


def setup_signal_handlers(event: asyncio.Event) -> None:
    """Registers signal handlers to set the given event on termination signals.

    Args:
        event: The event to set when SIGINT or SIGTERM is received.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, event.set)
        except NotImplementedError:
            logger.warning(
                "Signal handlers for %s are not supported on this platform.", sig
            )


async def setup_mqtt(
    stack: contextlib.AsyncExitStack, mqtt_config: MqttConfig
) -> aiomqtt.Client:
    """Connects to the MQTT broker and subscribes to command topics.

    Args:
        stack: Async context manager stack for cleanup.
        mqtt_config: MQTT connection settings.

    Returns:
        An active MQTT client instance.
    """
    mqtt = await stack.enter_async_context(
        aiomqtt.Client(
            mqtt_config.host,
            mqtt_config.port,
            username=mqtt_config.user,
            password=mqtt_config.password,
        )
    )
    await mqtt.subscribe(f"{mqtt_config.base_topic}/+/set")
    logger.info(
        "Connected to MQTT broker (host=%s, port=%d, base_topic=%s)",
        mqtt_config.host,
        mqtt_config.port,
        mqtt_config.base_topic,
    )
    return mqtt


async def setup_devices(
    stack: contextlib.AsyncExitStack,
    target_devices: list[TargetDevice],
    status_queue: asyncio.Queue[QueueCommand],
) -> dict[uuid.UUID, sesame5.Sesame5]:
    """Initializes and connects to Sesame devices, registering status callbacks.

    Args:
        stack: Async context manager stack for cleanup.
        target_devices: List of target devices to connect.
        status_queue: Queue to send status updates.

    Returns:
        Mapping of device UUIDs to connected Sesame instances.
    """
    devices = {}
    for address, key in target_devices:
        sesame = await stack.enter_async_context(sesame5.Sesame5(address, key))
        assert sesame.sesame_advertisement_data is not None
        device_uuid = sesame.sesame_advertisement_data.device_uuid
        sesame.set_mech_status_callback(
            functools.partial(produce_status, status_queue, device_uuid)
        )
        devices[device_uuid] = sesame
        logger.info("Connected to Sesame (UUID=%s)", device_uuid)
    return devices


def produce_status(
    status_queue: asyncio.Queue[QueueCommand],
    device_uuid: uuid.UUID,
    mech_status: sesame5.Sesame5MechStatus,
) -> None:
    """Formats device mechanical status and enqueues it for processing.

    Args:
        status_queue: Queue to send formatted status command.
        device_uuid: UUID of the device.
        mech_status: Mechanical status of the device.
    """
    lock_state = "LOCKED" if mech_status.is_in_lock_range else "UNLOCKED"
    payload = json.dumps(
        {
            "position": mech_status.position,
            "lockCurrentState": lock_state,
            "batteryVoltage": mech_status.battery_voltage,
            "batteryLevel": mech_status.battery_percentage,
            "chargingState": "NOT_CHARGEABLE",
            "statusLowBattery": mech_status.is_battery_critical,
        }
    )
    logger.debug("Received status update from Sesame (UUID=%s)", device_uuid)
    try:
        status_queue.put_nowait(QueueCommand(device_uuid, payload))
    except asyncio.QueueShutDown:
        logger.warning("Shutting down, status discarded.")


async def consume_status(
    status_queue: asyncio.Queue[QueueCommand], mqttc: aiomqtt.Client, base_topic: str
) -> None:
    """Continuously consumes status updates from the queue and publishes them to MQTT.

    Args:
        status_queue: Queue containing status messages to publish.
        mqttc: MQTT client used to publish messages.
        base_topic: Topic prefix for MQTT publishing.
    """
    while True:
        try:
            command = await status_queue.get()
        except asyncio.QueueShutDown:
            break
        topic = f"{base_topic}/{command.device_uuid}/get"
        await mqttc.publish(topic, command.payload)
        logger.debug("Published status to MQTT (topic=%s)", topic)
        status_queue.task_done()


async def produce_command(
    command_queue: asyncio.Queue[QueueCommand], mqttc: aiomqtt.Client
) -> None:
    """Listens for incoming MQTT messages and enqueues valid commands.

    Args:
        command_queue: Queue to place parsed commands into.
        mqttc: MQTT client used to receive messages.
    """
    async for message in mqttc.messages:
        try:
            device_uuid = uuid.UUID(message.topic.value.split("/")[1])
        except (IndexError, ValueError):
            logger.warning("Invalid topic format (topic=%s)", message.topic.value)
            continue
        if not isinstance(message.payload, bytes):
            continue
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "Invalid encoded payload (topic=%s, payload=%s)",
                message.topic.value,
                message.payload,
            )
            continue
        logger.debug("Received command from MQTT. (topic=%s)", message.topic.value)
        try:
            await command_queue.put(QueueCommand(device_uuid, payload))
        except asyncio.QueueShutDown:
            logger.warning("Shutting down, command discarded.")


async def consume_command(
    command_queue: asyncio.Queue[QueueCommand],
    devices: dict[uuid.UUID, sesame5.Sesame5],
    history_name: str,
) -> None:
    """Consumes commands from the queue and sends them to the appropriate Sesame device.

    Args:
        command_queue: Queue containing commands to execute.
        devices: Mapping of device UUIDs to Sesame instances.
        history_name: Name used for history when sending commands.
    """
    while True:
        try:
            command = await command_queue.get()
        except asyncio.QueueShutDown:
            break
        try:
            sesame = devices[command.device_uuid]
        except KeyError:
            logger.warning("Invalid Sesame specified (UUID=%s)", command.device_uuid)
            command_queue.task_done()
            continue
        match command.payload:
            case "LOCK":
                await sesame.lock(history_name)
                logger.debug(
                    "Send lock command to Sesame (UUID=%s)", command.device_uuid
                )
            case "UNLOCK":
                await sesame.unlock(history_name)
                logger.debug(
                    "Send unlock command to Sesame (UUID=%s)", command.device_uuid
                )
            case _:
                logger.warning(
                    "Invalid command for Sesame (UUID=%s, command=%s)",
                    command.device_uuid,
                    command.payload,
                )
        command_queue.task_done()


async def runner(stop_event: asyncio.Event):
    """Main async runner that initializes components and starts background tasks.

    Sets up MQTT connection, connects to devices, and launches tasks for handling
    incoming commands and outgoing status updates. Waits for a stop event to initiate
    graceful shutdown.

    Args:
        stop_event: Event used to trigger shutdown of the application.
    """
    status_queue: asyncio.Queue[QueueCommand] = asyncio.Queue()
    command_queue: asyncio.Queue[QueueCommand] = asyncio.Queue()
    setup_signal_handlers(stop_event)
    history_name, mqtt_config, target_devices = load_config()
    async with contextlib.AsyncExitStack() as stack:
        mqttc = await setup_mqtt(stack, mqtt_config)
        devices = await setup_devices(stack, target_devices, status_queue)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    consume_status(status_queue, mqttc, mqtt_config.base_topic)
                )
                tg.create_task(produce_command(command_queue, mqttc))
                tg.create_task(consume_command(command_queue, devices, history_name))
                logger.info("ssm2mqtt started and running.")
                await stop_event.wait()
                logger.debug("Shutdown signal received. Stopping tasks.")
                status_queue.shutdown()
                command_queue.shutdown()
                await asyncio.wait_for(status_queue.join(), 20)
                await asyncio.wait_for(command_queue.join(), 20)
                tg.create_task(force_terminate_task_group())
        except* TerminateTaskGroup:
            pass
    logger.info("ssm2mqtt shutdown complete.")


def main():
    """Entry point for the application.

    Runs the main async runner and handles graceful shutdown on keyboard interrupt.
    """
    stop_event = asyncio.Event()
    try:
        asyncio.run(runner(stop_event))
    except KeyboardInterrupt:
        stop_event.set()


if __name__ == "__main__":
    main()
