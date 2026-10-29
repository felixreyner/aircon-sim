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

# The PLC target starts from the environment and can be changed at runtime from
# the page through /api/plc, so the HMI can be pointed at another PLC.
DEFAULT_TARGET = {
    "host": os.getenv("PLC_ADDR", "127.0.0.1"),
    "port": int(os.getenv("PLC_PORT", "5021")),
    "unit": int(os.getenv("PLC_UNIT", "1")),
}
target = dict(DEFAULT_TARGET)
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
    unit = target["unit"]
    calls = {
        1: lambda: client.read_coils(address, count=count, slave=unit),
        2: lambda: client.read_discrete_inputs(address, count=count, slave=unit),
        3: lambda: client.read_holding_registers(address, count=count, slave=unit),
        4: lambda: client.read_input_registers(address, count=count, slave=unit),
        5: lambda: client.write_coil(address, value, slave=unit),
        6: lambda: client.write_register(address, value, slave=unit),
    }
    async with lock:
        if client is None:
            client = AsyncModbusTcpClient(target["host"], port=target["port"], timeout=1)
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
        raise HTTPException(503, f"PLC unreachable at {target['host']}:{target['port']}: {exc}")
    return {
        "power": bool(power), "compressor": bool(compressor), "alarm": bool(alarm),
        "sensor_fault": bool(fault), "setpoint": signed(setpoint) / 10, "mode": mode,
        "fan": fan, "alarm_limit": signed(alarm_limit) / 10, "temperature": signed(temp) / 10,
        "humidity": hum / 10, "cooling": cooling, "scans": scans,
    }


class Target(BaseModel):
    host: str
    port: int
    unit: int


@app.get("/api/plc")
async def get_plc():
    return {**target, "default": DEFAULT_TARGET}


@app.post("/api/plc")
async def set_plc(body: Target):
    """Point the HMI at another PLC: drop the old connection and try the new one."""
    global client
    host = body.host.strip()
    if not host or any(c.isspace() for c in host) or "/" in host:
        raise HTTPException(400, "host must be a hostname or IP address, without a scheme or path")
    if not 1 <= body.port <= 65535:
        raise HTTPException(400, "port must be between 1 and 65535")
    if not 0 <= body.unit <= 255:
        raise HTTPException(400, "unit ID must be between 0 and 255")
    async with lock:
        if client is not None:
            client.close()
        target.update(host=host, port=body.port, unit=body.unit)
        client = AsyncModbusTcpClient(host, port=body.port, timeout=1)
        connected = await client.connect()
        traffic.clear()
    return {**target, "connected": bool(connected)}


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
