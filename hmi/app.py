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

# A well-formed FC03 read-holding-registers request (length field matches the 6 bytes
# that follow, no trailing). It does NOT trip the signature, so an in-path IPS lets it
# through. We send it first to prove the PLC is reachable and answering; only then can a
# silent no-reply to the malformed frame be read as "dropped in transit" rather than
# "PLC busy / unreachable".
BASELINE_FRAME = bytes([
    0x00, 0x02, 0x00, 0x00, 0x00, 0x06, 0x01, 0x03, 0x00, 0x00, 0x00, 0x01,
])
ATTACK_TIMEOUT = 4.0


async def _probe(host, port, frame, read_timeout):
    """Open a fresh TCP connection, send one frame and read one reply.

    Returns (kind, detail, response_bytes). kind is one of:
      refused / connect-timeout / error - never reached the point of a reply.
      reset / closed                     - peer reset or closed after our frame.
      answered                           - got bytes back.
      no-reply                           - connected and sent, but no reply before timeout.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=ATTACK_TIMEOUT)
    except asyncio.TimeoutError:
        return "connect-timeout", f"no TCP connection to {host}:{port} within {ATTACK_TIMEOUT:.0f}s", b""
    except ConnectionRefusedError:
        return "refused", f"{host}:{port} refused the TCP connection", b""
    except OSError as exc:
        return "error", f"cannot reach {host}:{port}: {exc}", b""
    try:
        writer.write(frame)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(260), timeout=read_timeout)
        if data:
            return "answered", f"replied with {len(data)} bytes", data
        return "closed", "peer closed the connection with no reply", b""
    except asyncio.TimeoutError:
        return "no-reply", "connected and sent, but no reply before timeout", b""
    except ConnectionResetError:
        return "reset", "connection reset right after the frame", b""
    except OSError as exc:
        return "error", f"socket error after send: {exc}", b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@app.post("/api/attack")
async def attack():
    """Test whether an in-path IPS (e.g. FortiGate's Modbus.TCP.Invalid.Packet.Length)
    drops a malformed Modbus/TCP frame sent to the current PLC target.

    Two real probes against the operator-configured target, meant for a lab the operator
    owns. First a WELL-FORMED request, to prove the PLC is reachable and answering and that
    benign Modbus passes the firewall; then the MALFORMED frame. Comparing the two removes
    the ambiguity that bit the earlier version: the malformed frame declares a length of 6,
    which is itself a complete valid request, so a reachable PLC answers it -- therefore a
    *silent* no-reply to the malformed frame (while the baseline was answered) means it was
    dropped in transit, i.e. BLOCKED, not "reached the PLC".

      blocked (reset / closed / dropped) - firewall reset, closed, or silently dropped the
                                           malformed frame while the baseline got through.
      passed                             - the PLC answered the malformed frame too: NOT blocked.
      unreachable                        - the baseline itself got no answer: can't test
                                           (PLC down, wrong target, or benign Modbus blocked).
    """
    global seq, ips_blocked
    host, port = target["host"], target["port"]
    start = time.monotonic()

    base_kind, base_detail, _ = await _probe(host, port, BASELINE_FRAME, read_timeout=2.0)
    base_ok = base_kind == "answered"

    if not base_ok:
        outcome, blocked, detail = "unreachable", False, (
            f"baseline valid request to {host}:{port} was not answered ({base_kind}: {base_detail}); "
            "cannot tell a block from an unreachable PLC")
        mal_kind, mal_detail, resp = "skipped", "not sent (baseline failed)", b""
    else:
        mal_kind, mal_detail, resp = await _probe(host, port, MALFORMED_FRAME, read_timeout=2.5)
        if mal_kind == "answered":
            outcome, blocked, detail = "passed", False, (
                "baseline was answered AND the malformed frame was answered too -- not blocked in transit")
        elif mal_kind in ("reset", "closed"):
            outcome, blocked, detail = mal_kind, True, (
                f"baseline was answered, but the malformed frame -> {mal_kind}: {mal_detail} "
                "(consistent with an IPS drop)")
        elif mal_kind == "no-reply":
            outcome, blocked, detail = "dropped", True, (
                "baseline valid request was answered, but the malformed frame got no reply -- "
                "silently dropped in transit (a reachable PLC would have answered it)")
        else:
            outcome, blocked, detail = mal_kind, False, f"malformed probe: {mal_detail}"

    resp_hex = resp.hex(" ")
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
        "frame_hex": MALFORMED_FRAME.hex(" "), "declared_len": DECLARED_LENGTH,
        "actual_after_len": len(MALFORMED_FRAME) - 6, "trailing_bytes": TRAILING_BYTES,
        "baseline_ok": base_ok, "baseline_outcome": base_kind, "malformed_outcome": mal_kind,
        "outcome": outcome, "detail": detail, "blocked": blocked, "response_hex": resp_hex,
        "elapsed_ms": elapsed_ms, "blocked_count": ips_blocked,
    }


# --- Second IPS signature: an exception-response rate match on traffic FROM the PLC ---
#
# The operator's other FortiGate signature is a --flow from_server / --src_port 502 rule:
# it watches the *responses* the PLC sends back, not the client's requests, and fires on a
# rate of 3 in 60s per source IP. The response bytes it keys on:
#   pattern |00 00| at body offset 2  -> the MBAP protocol-id of a Modbus reply
#   byte_test 1,>,127,7               -> byte 7 (the function code) has its high bit set,
#                                        i.e. it is an EXCEPTION response (FC | 0x80)
#   pattern |01| at body offset 8     -> exception code 0x01 = Illegal Function
#   rate 3,60 ; track SRC_IP          -> 3 such replies from one source within 60s
#
# To provoke matching server-side traffic we send a small burst of WELL-FORMED requests that
# each carry an unsupported function code (0x64). The frames are valid in length (so they do
# NOT trip the invalid-length signature), but the PLC can't service the function, so it
# answers each with an "Illegal Function" exception -> exactly the reply the signature keys
# on. Three good exception replies inside the window satisfy the rate condition.
EXC_SIGNATURE = "Modbus.TCP.Illegal.Function.Exception.Rate"
EXC_RATE_COUNT = 3            # the signature's rate: N replies ...
EXC_RATE_WINDOW = 60         # ... within this many seconds
EXC_BURST = 4                # requests we send (one past the threshold, to cross it)
BAD_FUNCTION = 0x64          # an unsupported Modbus function code


def _illegal_function_frame(tid):
    """A well-formed Modbus/TCP request carrying an unsupported function code."""
    return bytes([
        (tid >> 8) & 0xFF, tid & 0xFF,   # transaction id
        0x00, 0x00,                      # protocol id
        0x00, 0x02,                      # length: 2 bytes follow (unit + function) -> well-formed
        0x01,                            # unit id
        BAD_FUNCTION,                    # unsupported function code
    ])


def _matches_exception_signature(resp):
    """True if resp is the exception reply the from_server signature keys on:
    protocol-id 00 00 at offset 2, function byte (offset 7) high-bit set, exc code 0x01 at offset 8."""
    return (len(resp) >= 9 and resp[2] == 0x00 and resp[3] == 0x00
            and resp[7] > 127 and resp[8] == 0x01)


@app.post("/api/exception-scan")
async def exception_scan():
    """Provoke the PLC into emitting a burst of 'Illegal Function' exception responses to
    test the operator's --flow from_server / --src_port 502 rate signature (3 in 60s).

    Unlike the single malformed-frame probe, this signature matches the PLC's *responses*,
    so the test has to make the PLC actually send matching replies. We send a short burst of
    well-formed requests with an unsupported function code; each should come back as an
    Illegal Function exception. Then we judge:

      not-blocked : at least EXC_RATE_COUNT matching exception replies came back -> the rate
                    condition is satisfied and the firewall did NOT drop the responses
                    (either it only logs, or the signature isn't deployed on this path).
      blocked     : the first replies came back but later ones were dropped/reset after the
                    threshold -> consistent with the IPS dropping the PLC's responses once
                    the rate fired.
      unreachable : no exception replies at all -> can't test (PLC down, wrong target, or
                    even benign Modbus blocked).
    """
    global seq, ips_blocked
    host, port = target["host"], target["port"]
    start = time.monotonic()

    results = []          # per-request probe kind
    matched = 0           # replies matching the signature
    dropped_after = 0     # non-answers that came AFTER we'd already seen a reply
    sample_hex = ""
    for i in range(EXC_BURST):
        kind, detail, resp = await _probe(host, port, _illegal_function_frame(0x30 + i),
                                           read_timeout=2.0)
        is_match = kind == "answered" and _matches_exception_signature(resp)
        if is_match:
            matched += 1
            if not sample_hex:
                sample_hex = resp.hex(" ")
        elif kind in ("no-reply", "reset", "closed") and matched > 0:
            dropped_after += 1
        results.append(kind)

    any_reply = matched > 0
    met_rate = matched >= EXC_RATE_COUNT

    if not any_reply:
        outcome, blocked, detail = "unreachable", False, (
            f"no Illegal Function exception replies from {host}:{port} "
            f"({', '.join(results)}); can't test the from_server signature")
    elif met_rate and dropped_after == 0:
        outcome, blocked, detail = "passed", False, (
            f"{matched} Illegal Function exception replies in {EXC_BURST} requests -- the "
            f"rate condition ({EXC_RATE_COUNT} in {EXC_RATE_WINDOW}s) is satisfied and the "
            "replies were NOT dropped in transit (firewall logs only, or signature not on this path)")
    elif dropped_after > 0:
        outcome, blocked, detail = "dropped", True, (
            f"{matched} exception replies came back, then {dropped_after} later one(s) were "
            f"dropped/reset -- consistent with the IPS cutting off the PLC's responses once the "
            f"{EXC_RATE_COUNT}-in-{EXC_RATE_WINDOW}s rate fired")
    else:
        outcome, blocked, detail = "partial", False, (
            f"only {matched} exception reply(ies) came back -- below the rate threshold of "
            f"{EXC_RATE_COUNT}, so the signature would not have fired")

    elapsed_ms = int((time.monotonic() - start) * 1000)
    seq += 1
    if blocked:
        ips_blocked += 1
    ips_log.appendleft({
        "seq": seq, "time": time.strftime("%H:%M:%S"), "fc": BAD_FUNCTION, "name": EXC_SIGNATURE,
        "address": 0, "count": EXC_BURST, "values": [], "blocked": blocked, "live": True,
        "detail": f"{outcome.upper()} · {detail}",
    })
    return {
        "signature": EXC_SIGNATURE, "target": f"{host}:{port}", "sent": EXC_BURST,
        "bad_function": BAD_FUNCTION, "rate_count": EXC_RATE_COUNT, "rate_window": EXC_RATE_WINDOW,
        "matched": matched, "dropped_after": dropped_after, "met_rate": met_rate,
        "outcome": outcome, "detail": detail, "blocked": blocked, "sample_hex": sample_hex,
        "results": results, "elapsed_ms": elapsed_ms, "blocked_count": ips_blocked,
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
