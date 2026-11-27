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
ips_blocked = 0  # simulated FortiGate IPS drops since the PLC target was set
ips_log = collections.deque(maxlen=5)  # kept apart so polling traffic doesn't push them out


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
    return {**target, "default": DEFAULT_TARGET, "ips_blocked": ips_blocked}


@app.post("/api/plc")
async def set_plc(body: Target):
    """Point the HMI at another PLC: drop the old connection and try the new one."""
    global client, ips_blocked
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
        ips_log.clear()
        ips_blocked = 0
    return {**target, "connected": bool(connected)}


SIGNATURE = "Modbus.TCP.Invalid.Packet.Length"


# A deliberately malformed Modbus/TCP frame: the MBAP "length" field under-counts
# the bytes that actually follow, leaving trailing bytes past the declared end of
# the message. A conforming stack never emits this. The frame is shaped to trip the
# operator's custom FortiGate IPS signature (service MODBUS):
#   pattern |00 00| ; context body ; distance 2 ; within 2 ;
#   byte_jump 2,0,relative ; data_at 0,relative
# i.e. protocol-id 00 00 at body offset 2, then jump by the declared length field
# and require that data still remains there. Single probe, not a flood.
DECLARED_LENGTH = 6            # MBAP length field value
MALFORMED_FRAME = bytes([
    0x00, 0x01,              # transaction id
    0x00, 0x00,              # protocol id -> the |00 00| the signature matches at offset 2
    (DECLARED_LENGTH >> 8) & 0xFF, DECLARED_LENGTH & 0xFF,  # length: claims 6 bytes follow
    0x01,                    # unit id        \
    0x03,                    # function code   | the 6 bytes the length field accounts for
    0x00, 0x00,              # start address   |
    0x00, 0x01,              # quantity       /
    0xDE, 0xAD,              # trailing bytes PAST the declared length -> data_at matches here
])
# bytes after the length field: 8 actual vs 6 declared, so 2 bytes sit beyond the
# point the length field claims the message ends.
TRAILING_BYTES = len(MALFORMED_FRAME) - 6 - DECLARED_LENGTH
ATTACK_TIMEOUT = 4.0


@app.post("/api/attack")
async def attack():
    """Send ONE real malformed Modbus/TCP frame to the current PLC target and report
    what came back, to test whether an in-path IPS (e.g. FortiGate's
    Modbus.TCP.Invalid.Packet.Length) actually drops it.

    This genuinely opens a socket to target host:port and writes the frame. It is a
    single probe against the operator-configured target, meant for a lab the operator
    owns. The outcome is inferred from how the far side behaves:
      refused   - TCP refused: nothing listening, or the firewall rejects the port.
      reset     - connection reset right after our frame: consistent with an IPS drop.
      closed    - peer sent FIN with no data after our frame: also consistent with a drop.
      answered  - the PLC replied: the frame was NOT blocked in transit.
      no-reply  - connected, no reset and no reply before timeout: not blocked by the
                  firewall; a Modbus server waiting for the 253 bytes we promised looks
                  like this, so the packet most likely reached the PLC.
      timeout   - could not even connect within the timeout: likely silently dropped.
    """
    global seq, ips_blocked
    host, port = target["host"], target["port"]
    start = time.monotonic()
    outcome, detail, resp_hex = "error", "", ""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=ATTACK_TIMEOUT)
    except asyncio.TimeoutError:
        outcome, detail = "timeout", f"no TCP connection to {host}:{port} within {ATTACK_TIMEOUT:.0f}s (dropped?)"
    except ConnectionRefusedError:
        outcome, detail = "refused", f"{host}:{port} refused the TCP connection"
    except OSError as exc:
        outcome, detail = "error", f"cannot reach {host}:{port}: {exc}"
    else:
        try:
            writer.write(MALFORMED_FRAME)
            await writer.drain()
            remaining = ATTACK_TIMEOUT - (time.monotonic() - start)
            data = await asyncio.wait_for(reader.read(260), timeout=max(0.5, remaining))
            if data:
                resp_hex = data.hex(" ")
                outcome, detail = "answered", f"PLC replied with {len(data)} bytes (frame not blocked in transit)"
            else:
                outcome, detail = "closed", "peer closed the connection after our frame with no reply (consistent with an IPS drop)"
        except asyncio.TimeoutError:
            outcome, detail = "no-reply", "connected and sent, no reset and no reply before timeout (frame most likely reached the PLC)"
        except ConnectionResetError:
            outcome, detail = "reset", "connection reset right after our frame (consistent with an IPS drop)"
        except OSError as exc:
            outcome, detail = "error", f"socket error after send: {exc}"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    blocked = outcome in ("reset", "closed", "timeout")
    elapsed_ms = int((time.monotonic() - start) * 1000)
    seq += 1
    if blocked:
        ips_blocked += 1
    ips_log.appendleft({
        "seq": seq, "time": time.strftime("%H:%M:%S"), "fc": 3, "name": SIGNATURE,
        "address": 0, "count": 1, "values": [], "blocked": blocked, "live": True,
        "detail": f"{outcome.upper()} · {detail}",
    })
    return {
        "signature": SIGNATURE, "target": f"{host}:{port}", "sent_bytes": len(MALFORMED_FRAME),
        "frame_hex": MALFORMED_FRAME.hex(" "), "declared_len": DECLARED_LENGTH, "actual_after_len": len(MALFORMED_FRAME) - 6, "trailing_bytes": TRAILING_BYTES,
        "outcome": outcome, "detail": detail, "blocked": blocked, "response_hex": resp_hex,
        "elapsed_ms": elapsed_ms, "blocked_count": ips_blocked,
    }


@app.get("/api/traffic")
async def get_traffic():
    return list(ips_log) + list(traffic)


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
