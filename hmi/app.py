"""Web HMI: a Modbus TCP client of the PLC, served to the browser over HTTP.

Every Modbus request it makes is recorded so the page can show the raw
function codes, addresses and values going over the wire.
"""
import asyncio
import collections
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymodbus.client import AsyncModbusTcpClient

PLC_HOST = os.getenv("PLC_ADDR", "127.0.0.1")
PLC_PORT = int(os.getenv("PLC_PORT", "5021"))
PLC_UNIT = int(os.getenv("PLC_UNIT", "1"))
STATIC = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="AirCon HMI")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

client = None  # created on first use, it needs the running event loop
lock = asyncio.Lock()
traffic = collections.deque(maxlen=50)
seq = 0


def signed(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


async def modbus(fc: int, name: str, address: int, count=None, value=None):
    """Run one Modbus request against the PLC and log it."""
    global client, seq
    calls = {
        1: lambda: client.read_coils(address, count=count, slave=PLC_UNIT),
        2: lambda: client.read_discrete_inputs(address, count=count, slave=PLC_UNIT),
        3: lambda: client.read_holding_registers(address, count=count, slave=PLC_UNIT),
        4: lambda: client.read_input_registers(address, count=count, slave=PLC_UNIT),
        5: lambda: client.write_coil(address, value, slave=PLC_UNIT),
        6: lambda: client.write_register(address, value, slave=PLC_UNIT),
    }
    async with lock:
        if client is None:
            client = AsyncModbusTcpClient(PLC_HOST, port=PLC_PORT, timeout=1)
        if not client.connected:
            await client.connect()
        rr = await calls[fc]()
    if rr.isError():
        raise HTTPException(502, f"Modbus error on FC{fc:02d}: {rr}")
    if fc in (1, 2):
        result = [int(b) for b in rr.bits[:count]]
    elif fc in (3, 4):
        result = rr.registers
    else:
        result = [int(value)]
    seq += 1
    traffic.appendleft({
        "seq": seq, "time": time.strftime("%H:%M:%S"), "fc": fc, "name": name,
        "address": address, "count": count or 1, "values": result,
    })
    return result


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/state")
async def state():
    try:
        power, = await modbus(1, "Read Coils", 0, 1)
        compressor, alarm, fault = await modbus(2, "Read Discrete Inputs", 0, 3)
        setpoint, mode, fan, alarm_limit = await modbus(3, "Read Holding Registers", 0, 4)
        temp, hum, cooling, scans = await modbus(4, "Read Input Registers", 0, 4)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"PLC unreachable at {PLC_HOST}:{PLC_PORT}: {exc}")
    return {
        "power": bool(power), "compressor": bool(compressor), "alarm": bool(alarm),
        "sensor_fault": bool(fault), "setpoint": signed(setpoint) / 10, "mode": mode,
        "fan": fan, "alarm_limit": signed(alarm_limit) / 10, "temperature": signed(temp) / 10,
        "humidity": hum / 10, "cooling": cooling, "scans": scans,
    }


@app.get("/api/traffic")
async def get_traffic():
    return list(traffic)


class Write(BaseModel):
    tag: str
    value: float


# tag -> (function code, address, scale) for everything the operator may change.
WRITABLE = {
    "power": (5, 0, None),
    "setpoint": (6, 0, 10),
    "mode": (6, 1, 1),
    "fan": (6, 2, 1),
    "alarm_limit": (6, 3, 10),
}
LIMITS = {"setpoint": (16, 30), "mode": (0, 1), "fan": (1, 3), "alarm_limit": (20, 45)}


@app.post("/api/write")
async def write(body: Write):
    if body.tag not in WRITABLE:
        raise HTTPException(400, f"unknown tag {body.tag}")
    fc, address, scale = WRITABLE[body.tag]
    if body.tag in LIMITS:
        lo, hi = LIMITS[body.tag]
        if not lo <= body.value <= hi:
            raise HTTPException(400, f"{body.tag} must be between {lo} and {hi}")
    if fc == 5:
        await modbus(5, "Write Single Coil", address, value=bool(body.value))
    else:
        raw = int(round(body.value * scale)) & 0xFFFF
        await modbus(6, "Write Single Register", address, value=raw)
    return {"ok": True}
