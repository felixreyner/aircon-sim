"""Software PLC for the air conditioner.

Each scan it reads the sensor over Modbus TCP (as a client), runs the AC
control logic, writes the cooling output back to the room model and
publishes its own I/O image on a Modbus TCP server for the HMI or any
external Modbus tool.

Register map (unit id 1):
  Coil 0              AC power on/off (read/write)
  Discrete input 0    compressor running
  Discrete input 1    high temperature alarm
  Discrete input 2    sensor communication fault
  Holding register 0  temperature setpoint, degC x10 (read/write)
  Holding register 1  mode: 0 = cool, 1 = fan only (read/write)
  Holding register 2  fan speed 1-3 (read/write)
  Holding register 3  high temperature alarm limit, degC x10 (read/write)
  Input register 0    room temperature, degC x10 (signed)
  Input register 1    relative humidity, %RH x10
  Input register 2    cooling output, 0-100 %
  Input register 3    PLC scan counter
"""
import asyncio
import logging
import os

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

HOST = os.getenv("PLC_HOST", "0.0.0.0")
PORT = int(os.getenv("PLC_PORT", "5021"))
UNIT = int(os.getenv("PLC_UNIT", "1"))
SENSOR_HOST = os.getenv("SENSOR_ADDR", "127.0.0.1")
SENSOR_PORT = int(os.getenv("SENSOR_PORT", "5020"))
SENSOR_UNIT = int(os.getenv("SENSOR_UNIT", "1"))
SCAN_TIME = float(os.getenv("SCAN_TIME", "0.5"))
HYSTERESIS = 0.5

# pymodbus function-code numbers used to address each table.
COILS, DISCRETE, HOLDING, INPUT = 1, 2, 3, 4

log = logging.getLogger("plc")

# pymodbus shifts protocol addresses by +1, so blocks start at 1 to make
# protocol address 0 the first value.
store = ModbusSlaveContext(
    co=ModbusSequentialDataBlock(1, [1] + [0] * 9),
    di=ModbusSequentialDataBlock(1, [0] * 10),
    hr=ModbusSequentialDataBlock(1, [240, 0, 2, 320] + [0] * 6),
    ir=ModbusSequentialDataBlock(1, [0] * 10),
)
context = ModbusServerContext(slaves={UNIT: store}, single=False)


def signed(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


async def scan_loop():
    sensor = AsyncModbusTcpClient(SENSOR_HOST, port=SENSOR_PORT, timeout=1)
    compressor = False
    scans = 0
    while True:
        scans = (scans + 1) & 0xFFFF
        comm_fault = False
        temp_raw = hum_raw = 0
        try:
            if not sensor.connected:
                await sensor.connect()
            rr = await sensor.read_input_registers(0, count=2, slave=SENSOR_UNIT)
            if rr.isError():
                raise IOError(rr)
            temp_raw, hum_raw = rr.registers
        except Exception as exc:  # keep scanning, flag the fault
            comm_fault = True
            log.warning("sensor read failed: %s", exc)

        power = bool(store.getValues(COILS, 0, 1)[0])
        setpoint_raw, mode, fan, alarm_raw = store.getValues(HOLDING, 0, 4)
        temp = signed(temp_raw) / 10
        setpoint = signed(setpoint_raw) / 10
        fan = min(max(fan, 1), 3)

        if not power or mode != 0 or comm_fault:
            compressor = False
        elif temp > setpoint + HYSTERESIS:
            compressor = True
        elif temp < setpoint - HYSTERESIS:
            compressor = False
        cooling = 40 + 20 * fan if compressor else 0
        alarm = not comm_fault and temp > signed(alarm_raw) / 10

        if not comm_fault:
            try:
                await sensor.write_register(0, cooling, slave=SENSOR_UNIT)
            except Exception as exc:
                log.warning("sensor write failed: %s", exc)

        store.setValues(DISCRETE, 0, [compressor, alarm, comm_fault])
        store.setValues(INPUT, 0, [temp_raw, hum_raw, cooling, scans])
        await asyncio.sleep(SCAN_TIME)


async def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.create_task(scan_loop())
    log.info("PLC listening on Modbus TCP %s:%d (unit %d), sensor at %s:%d",
             HOST, PORT, UNIT, SENSOR_HOST, SENSOR_PORT)
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())
