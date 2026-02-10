"""MQTT to Sesame 5 bridge for lock control and status reporting.

This module connects to a set of Sesame smart locks over Bluetooth
and integrates them with an MQTT broker.
"""

import asyncio
import contextlib
import functools
import json
import logging
import signal
import sys
import uuid
from typing import NamedTuple

import aiomqtt
from gomalock.sesame5 import Sesame5, Sesame5MechStatus

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO)
logging.getLogger("bleak").setLevel(level=logging.WARNING)
logging.getLogger("gomalock").setLevel(level=logging.WARNING)
logger = logging.getLogger(__name__)


class _MqttConfig(NamedTuple):
    base_topic: str
    host: str
    port: int
    user: str | None
    password: str | None


class _TargetDevice(NamedTuple):
    address: str
    secret_key: str


class _StatusPayload(NamedTuple):
    device_uuid: uuid.UUID
    mech_status: Sesame5MechStatus


class _ControlPayload(NamedTuple):
    device_uuid: uuid.UUID
    command: bytes


def _load_config() -> tuple[str, _MqttConfig, list[_TargetDevice]]:
    with open("config.json", "r", encoding="utf8") as f:
        user_config = json.load(f)
        history_name: str = user_config["history_name"]
        mqtt = user_config["mqtt"]
        mqtt_config = _MqttConfig(
            mqtt["base_topic"],
            mqtt["host"],
            mqtt["port"],
            mqtt.get("user"),
            mqtt.get("password"),
        )
        target_devices = [
            _TargetDevice(address, secret_key)
            for address, secret_key in user_config["devices"].items()
        ]
    return history_name, mqtt_config, target_devices


def _setup_signal_handlers(stop_event: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning(
                "Signal handlers for %s are not supported on this platform", sig
            )


async def _configure_mqttc(
    stack: contextlib.AsyncExitStack, mqtt_config: _MqttConfig
) -> aiomqtt.Client:
    mqttc = await stack.enter_async_context(
        aiomqtt.Client(
            mqtt_config.host,
            mqtt_config.port,
            username=mqtt_config.user,
            password=mqtt_config.password,
        )
    )
    await mqttc.subscribe(f"{mqtt_config.base_topic}/+/set", 1)
    logger.info(
        "Connected to MQTT broker [host: %s, port: %d, base_topic: %s]",
        mqtt_config.host,
        mqtt_config.port,
        mqtt_config.base_topic,
    )
    return mqttc


async def _configure_sesame(
    stack: contextlib.AsyncExitStack,
    status_queue: asyncio.Queue[_StatusPayload],
    target_devices: list[_TargetDevice],
) -> dict[uuid.UUID, Sesame5]:
    connected_devices = {}
    for address, secret_key in target_devices:
        sesame = await stack.enter_async_context(Sesame5(address, secret_key))
        sesame.register_mech_status_callback(
            functools.partial(_produce_status, status_queue)
        )
        device_uuid = sesame.sesame_advertisement_data.device_uuid
        connected_devices[device_uuid] = sesame
        logger.info("Connected to Sesame device [UUID: %s]", device_uuid)
    return connected_devices


def _produce_status(
    queue: asyncio.Queue[_StatusPayload],
    sesame: Sesame5,
    status: Sesame5MechStatus,
) -> None:
    payload = _StatusPayload(sesame.sesame_advertisement_data.device_uuid, status)
    try:
        queue.put_nowait(payload)
    except asyncio.QueueShutDown:
        logger.warning("Shutting down, status discarded")


async def _consume_status(
    queue: asyncio.Queue[_StatusPayload], mqttc: aiomqtt.Client, base_topic: str
) -> None:
    while True:
        try:
            status = await queue.get()
        except asyncio.QueueShutDown:
            break
        try:
            payload = json.dumps(
                {
                    "position": status.mech_status.position,
                    "lockCurrentState": (
                        "LOCKED" if status.mech_status.is_in_lock_range else "UNLOCKED"
                    ),
                    "batteryVoltage": status.mech_status.battery_voltage,
                    "batteryLevel": status.mech_status.battery_percentage,
                    "chargingState": "NOT_CHARGEABLE",
                    "statusLowBattery": status.mech_status.is_battery_critical,
                }
            )
            await mqttc.publish(f"{base_topic}/{status.device_uuid}/get", payload, 1)
            logger.debug("Published status to MQTT [UUID: %s]", status.device_uuid)
        finally:
            queue.task_done()


async def _produce_control(
    queue: asyncio.Queue[_ControlPayload], mqttc: aiomqtt.Client
) -> None:
    async for message in mqttc.messages:
        try:
            device_uuid = uuid.UUID(message.topic.value.split("/")[1])
        except (IndexError, ValueError):
            logger.warning("Invalid topic format [topic: %s]", message.topic.value)
            continue
        try:
            await queue.put(_ControlPayload(device_uuid, message.payload))
        except asyncio.QueueShutDown:
            logger.warning("Shutting down, command discarded")


async def _consume_control(
    queue: asyncio.Queue[_ControlPayload],
    connected_devices: dict[uuid.UUID, Sesame5],
    history_name: str,
) -> None:
    while True:
        try:
            control = await queue.get()
        except asyncio.QueueShutDown:
            break
        try:
            sesame = connected_devices.get(control.device_uuid)
            if sesame is None:
                logger.warning(
                    "Invalid Sesame specified [UUID: %s]", control.device_uuid
                )
                continue
            try:
                command_str = control.command.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "Invalid encoded payload [UUID: %s]", control.device_uuid
                )
                continue
            match command_str:
                case "LOCKED":
                    await sesame.lock(history_name)
                    logger.debug(
                        "Send lock command to Sesame [UUID: %s]",
                        control.device_uuid,
                    )
                case "UNLOCKED":
                    await sesame.unlock(history_name)
                    logger.debug(
                        "Send unlock command to Sesame [UUID: %s]",
                        control.device_uuid,
                    )
                case _:
                    logger.warning(
                        "Invalid command for Sesame [UUID: %s, command: %s]",
                        control.device_uuid,
                        command_str,
                    )
        finally:
            queue.task_done()


async def main() -> None:
    """Main entry point for ssm2mqtt application."""
    stop_event = asyncio.Event()
    status_queue: asyncio.Queue[_StatusPayload] = asyncio.Queue()
    control_queue: asyncio.Queue[_ControlPayload] = asyncio.Queue()

    _setup_signal_handlers(stop_event)
    history_name, mqtt_config, target_devices = _load_config()

    async with contextlib.AsyncExitStack() as stack:
        mqttc = await _configure_mqttc(stack, mqtt_config)
        connected_devices = await _configure_sesame(stack, status_queue, target_devices)
        tg = await stack.enter_async_context(asyncio.TaskGroup())

        tg.create_task(_consume_status(status_queue, mqttc, mqtt_config.base_topic))
        tg.create_task(_consume_control(control_queue, connected_devices, history_name))
        produce_control_task = tg.create_task(_produce_control(control_queue, mqttc))
        logger.info("ssm2mqtt is running")

        await stop_event.wait()
        logger.info("Shutting down ssm2mqtt")
        produce_control_task.cancel()
        status_queue.shutdown()
        control_queue.shutdown()
        await asyncio.wait_for(status_queue.join(), timeout=10)
        await asyncio.wait_for(control_queue.join(), timeout=10)
    logger.info("smm2mqtt has been shut down")


if __name__ == "__main__":
    asyncio.run(main())
