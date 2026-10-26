"""Simulated room temperature/humidity sensor, served over Modbus TCP.

Register map (unit id 1):
  Input register 0  room temperature, degC x10 (signed)
  Input register 1  relative humidity, %RH x10
  Holding register 0  cooling power applied to the room, 0-100 %
                      (written by the PLC; it drives the room model)
"""
import asyncio
import logging
import os
import random

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

HOST = os.getenv("SENSOR_HOST", "0.0.0.0")
PORT = int(os.getenv("SENSOR_PORT", "5020"))
UNIT = int(os.getenv("SENSOR_UNIT", "1"))
AMBIENT = float(os.getenv("AMBIENT_TEMP", "30.0"))

log = logging.getLogger("sensor")

# Function code 4 (input registers) and 3/6/16 (holding registers).
# pymodbus shifts protocol addresses by +1, so blocks start at 1 to make
# protocol address 0 the first value.
store = ModbusSlaveContext(
    ir=ModbusSequentialDataBlock(1, [0] * 10),
    hr=ModbusSequentialDataBlock(1, [0] * 10),
)
context = ModbusServerContext(slaves={UNIT: store}, single=False)


def to_u16(value: float) -> int:
    return int(round(value * 10)) & 0xFFFF


async def simulate_room():
    temp, hum = 28.0, 65.0
    while True:
        cooling = min(max(store.getValues(3, 0, 1)[0], 0), 100)
        # Heat leaks in from outside; the AC removes heat in proportion to its power.
        temp += (AMBIENT - temp) * 0.01 - cooling * 0.0015 + random.uniform(-0.03, 0.03)
        hum += ((70 - cooling * 0.2) - hum) * 0.02 + random.uniform(-0.1, 0.1)
        hum = min(max(hum, 0), 100)
        store.setValues(4, 0, [to_u16(temp), to_u16(hum)])
        log.debug("temp=%.2f hum=%.1f cooling=%d", temp, hum, cooling)
        await asyncio.sleep(1)


async def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.create_task(simulate_room())
    log.info("Sensor listening on Modbus TCP %s:%d (unit %d)", HOST, PORT, UNIT)
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())
