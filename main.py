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
import uuid
from typing import Any, Callable, Coroutine, Literal, NamedTuple

import aiomqtt
import gomalock
from gomalock.exc import SesameConnectionError

logger = logging.getLogger(__name__)


class _BridgeConfig(NamedTuple):
    host: str
    port: int
    user: str | None
    password: bytes | None
    base_topic: str
    log_level: Literal[10, 20, 30, 40, 50]
    history_name: str
    sesame_reconnection_limit: int


class _TargetDevice(NamedTuple):
    address: str
    secret_key: str


class _StatusPayload(NamedTuple):
    device_uuid: uuid.UUID
    mech_status: gomalock.Sesame5MechStatus


class _ControlPayload(NamedTuple):
    device_uuid: uuid.UUID
    command: bytes


def _load_config() -> tuple[_BridgeConfig, tuple[_TargetDevice, ...]]:
    with open("config.json", "r", encoding="utf8") as f:
        user_config: dict = json.load(f)
    mqtt_config: dict = user_config["mqtt"]
    bridge_config = _BridgeConfig(
        mqtt_config["host"],
        mqtt_config["port"],
        mqtt_config.get("user") or None,
        mqtt_config.get("password", "").encode("utf-8") or None,
        mqtt_config.get("base_topic", "ssm2mqtt"),
        getattr(logging, user_config.get("log_level", "INFO").upper()),
        user_config.get("history_name", "ssm2mqtt"),
        user_config.get("sesame_reconnection_limit", 10),
    )
    target_devices = tuple(
        _TargetDevice(address, secret_key)
        for address, secret_key in user_config["devices"].items()
    )
    return bridge_config, target_devices


def _configure_log_level(log_level: Literal[10, 20, 30, 40, 50]) -> None:
    logging.basicConfig(level=log_level)
    logging.getLogger("bleak").setLevel(level=logging.WARNING)
    logging.getLogger("gomalock.scanner").setLevel(level=logging.WARNING)


def _configure_signal_handlers(stop_event: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning(
                "Signal handlers for %s are not supported on this platform", sig
            )


async def _configure_mqttc(
    stack: contextlib.AsyncExitStack, bridge_config: _BridgeConfig
) -> aiomqtt.Client:
    mqttc = await stack.enter_async_context(
        aiomqtt.Client(
            hostname=bridge_config.host,
            port=bridge_config.port,
            username=bridge_config.user,
            password=bridge_config.password,
            reconnect=True,
        )
    )
    await mqttc.subscribe(
        f"{bridge_config.base_topic}/+/set", max_qos=aiomqtt.QoS.AT_LEAST_ONCE
    )
    logger.info(
        "Connected to MQTT broker [host=%s, port=%d, base_topic=%s]",
        bridge_config.host,
        bridge_config.port,
        bridge_config.base_topic,
    )
    return mqttc


async def _configure_sesame(
    stack: contextlib.AsyncExitStack,
    status_queue: asyncio.Queue[_StatusPayload],
    target_devices: tuple[_TargetDevice, ...],
    reconnection_limit: int,
) -> dict[uuid.UUID, gomalock.Sesame5]:
    connected_devices = {}
    for address, secret_key in target_devices:
        sesame = await stack.enter_async_context(
            gomalock.Sesame5(
                address,
                secret_key,
                functools.partial(_produce_status, status_queue),
                reconnection_limit,
            )
        )
        device_uuid = sesame.sesame_advertisement_data.device_uuid
        connected_devices[device_uuid] = sesame
        logger.info("Connected to Sesame device [UUID=%s]", device_uuid)
    return connected_devices


async def _perform_sesame_command_with_retry[**P, T](
    retry: bool,
    func: Callable[P, Coroutine[Any, Any, T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    if not retry:
        return await func(*args, **kwargs)
    while True:
        try:
            return await func(*args, **kwargs)
        except (asyncio.TimeoutError, SesameConnectionError):
            # No sleep interval needed; gomalock automatically handles reconnection.
            logger.exception("Command failed, retrying...")


def _produce_status(
    queue: asyncio.Queue[_StatusPayload],
    sesame: gomalock.Sesame5,
    status: gomalock.Sesame5MechStatus,
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
            return
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
            ).encode("utf-8")
            is_duplicate = False
            packet_id = next(mqttc.packet_ids)
            while True:
                try:
                    await mqttc.publish(
                        f"{base_topic}/{status.device_uuid}/get",
                        payload,
                        qos=aiomqtt.QoS.AT_LEAST_ONCE,
                        packet_id=packet_id,
                        duplicate=is_duplicate,
                        retain=True,
                    )
                    break
                except aiomqtt.ConnectError:
                    is_duplicate = True
                    await mqttc.connected()
            logger.debug("Published status to MQTT [UUID=%s]", status.device_uuid)
        finally:
            queue.task_done()


async def _produce_control(
    queue: asyncio.Queue[_ControlPayload], mqttc: aiomqtt.Client
) -> None:
    async for message in mqttc.messages():
        # Handle messages up to QoS 1; QoS 2 messages are treated as QoS 1 by aiomqtt.
        if not isinstance(message, aiomqtt.PublishPacket):
            continue
        if message.qos == aiomqtt.QoS.AT_LEAST_ONCE:
            # aiomqtt guarantees packet_id is an int when QoS is 1 or 2.
            if not isinstance(message.packet_id, int):
                continue
            await mqttc.puback(message.packet_id)
        try:
            device_uuid = uuid.UUID(message.topic.split("/")[1])
        except (IndexError, ValueError):
            logger.warning("Invalid topic format [topic=%s]", message.topic)
            continue
        try:
            await queue.put(_ControlPayload(device_uuid, message.payload))
        except asyncio.QueueShutDown:
            logger.warning("Shutting down, command discarded")


async def _consume_control(
    queue: asyncio.Queue[_ControlPayload],
    connected_devices: dict[uuid.UUID, gomalock.Sesame5],
    history_name: str,
    retry: bool,
) -> None:
    while True:
        try:
            control = await queue.get()
        except asyncio.QueueShutDown:
            return
        try:
            sesame = connected_devices.get(control.device_uuid)
            if sesame is None:
                logger.warning(
                    "Invalid Sesame specified [UUID=%s]", control.device_uuid
                )
                continue
            try:
                command_str = control.command.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Invalid encoded payload [UUID=%s]", control.device_uuid)
                continue
            match command_str.upper():
                case "LOCKED":
                    await _perform_sesame_command_with_retry(
                        retry, sesame.lock, history_name
                    )
                    logger.debug(
                        "Lock command succeeded [UUID=%s]", control.device_uuid
                    )
                case "UNLOCKED":
                    await _perform_sesame_command_with_retry(
                        retry, sesame.unlock, history_name
                    )
                    logger.debug(
                        "Unlock command succeeded [UUID=%s]", control.device_uuid
                    )
                case _:
                    logger.warning(
                        "Invalid command for Sesame [UUID=%s, command=%s]",
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

    bridge_config, target_devices = _load_config()
    _configure_log_level(bridge_config.log_level)
    _configure_signal_handlers(stop_event)

    async with contextlib.AsyncExitStack() as stack:
        mqttc = await _configure_mqttc(stack, bridge_config)
        connected_devices = await _configure_sesame(
            stack, status_queue, target_devices, bridge_config.sesame_reconnection_limit
        )
        tg = await stack.enter_async_context(asyncio.TaskGroup())

        tg.create_task(
            _consume_status(
                status_queue,
                mqttc,
                bridge_config.base_topic,
            )
        )
        tg.create_task(
            _consume_control(
                control_queue,
                connected_devices,
                bridge_config.history_name,
                bridge_config.sesame_reconnection_limit > 0,
            )
        )
        produce_control_task = tg.create_task(_produce_control(control_queue, mqttc))
        logger.info("ssm2mqtt is running")

        await stop_event.wait()
        logger.info("Shutting down ssm2mqtt")
        produce_control_task.cancel()
        status_queue.shutdown()
        control_queue.shutdown()
        await asyncio.wait_for(status_queue.join(), timeout=10)
        await asyncio.wait_for(control_queue.join(), timeout=10)
    logger.info("ssm2mqtt has been shut down")


if __name__ == "__main__":
    asyncio.run(main())
