# Modbus Air Con Demo

A fully simulated air conditioning system that shows how Modbus TCP works:

```
 Browser ──HTTP──▶ Web HMI ──Modbus TCP──▶ Software PLC ──Modbus TCP──▶ Simulated sensor
  :8080            (client)                (server :502 / client)       (server :5020, room model)
```

* **Simulated sensor** (`sensor/`) is a Modbus TCP server. It runs a tiny room model:
  heat leaks in from a 30 °C outside, and the AC removes heat according to the
  cooling power the PLC writes to it.
* **Software PLC** (`plc/`) scans every 0.5 s. It is a Modbus *client* of the sensor
  (reads temperature and humidity, writes the cooling output) and a Modbus *server*
  for the HMI and any external Modbus tool.
* **Web HMI** (`hmi/`) is a Modbus client of the PLC. It shows live values, a trend,
  operator controls, and a log of every Modbus request it sends (function code,
  address, count, values).

## Run it

```bash
docker compose up -d --build
```

Then open `http://<host-ip>:8080`.

| Service | Container port | Published on host | Protocol |
|---|---|---|---|
| PLC | 5021 | `0.0.0.0:502` | Modbus TCP |
| Sensor | 5020 | `0.0.0.0:5020` | Modbus TCP |
| HMI | 8080 | `0.0.0.0:8080` | HTTP |

Every port is published on all interfaces, so other machines can connect with a Modbus
client such as Modbus Poll, QModMaster, mbpoll or pymodbus. Change the host ports with
environment variables, for example `PLC_PUBLIC_PORT=1502 docker compose up -d`.

To reach them from other machines, allow the ports through the host firewall
(e.g. `sudo ufw allow 502/tcp`, `sudo ufw allow 5020/tcp`, `sudo ufw allow 8080/tcp`).
Modbus has no authentication or encryption, so anyone who can reach port 502 can
switch the AC and change the setpoint. Keep it on a trusted LAN and do not forward
these ports to the internet.

Without Docker (Python 3.10+):

```bash
pip install -r requirements.txt
python sensor/sensor.py &
python plc/plc.py &
uvicorn app:app --app-dir hmi --host 0.0.0.0 --port 8080
```

Run that way, the PLC listens on port 5021 (port 502 needs root on Linux).

## Register maps

All addresses are 0-based protocol addresses, unit (slave) id 1. Many tools show the
same registers with a 1-based prefix, e.g. holding register 0 = `40001`,
input register 0 = `30001`, coil 0 = `00001`, discrete input 0 = `10001`.
Temperatures are scaled by 10 (`245` = 24.5 °C) and signed 16-bit.

### PLC (host port 502)

| Table | Addr | Name | Access | Notes |
|---|---|---|---|---|
| Coil | 0 | Power | R/W | 1 = on |
| Discrete input | 0 | Compressor running | R | |
| Discrete input | 1 | High temperature alarm | R | room temp > alarm limit |
| Discrete input | 2 | Sensor comm fault | R | PLC can't reach the sensor |
| Holding register | 0 | Setpoint | R/W | °C x10, default 240 |
| Holding register | 1 | Mode | R/W | 0 = cool, 1 = fan only |
| Holding register | 2 | Fan speed | R/W | 1 to 3 |
| Holding register | 3 | Alarm limit | R/W | °C x10, default 320 |
| Input register | 0 | Room temperature | R | °C x10 |
| Input register | 1 | Humidity | R | %RH x10 |
| Input register | 2 | Cooling output | R | 0 to 100 % |
| Input register | 3 | Scan counter | R | increments every PLC scan |

Control logic: when power is on and mode is cool, the compressor starts above
setpoint + 0.5 °C and stops below setpoint − 0.5 °C. Cooling output is
40 + 20 × fan speed % while the compressor runs.

### Sensor (host port 5020)

| Table | Addr | Name | Access | Notes |
|---|---|---|---|---|
| Input register | 0 | Room temperature | R | °C x10 |
| Input register | 1 | Humidity | R | %RH x10 |
| Holding register | 0 | Cooling power applied | R/W | written by the PLC, drives the room model |

## Demo walkthrough

1. Open the HMI. Point out the four reads repeating every second in the traffic log:
   FC01 Read Coils, FC02 Read Discrete Inputs, FC03 Read Holding Registers and
   FC04 Read Input Registers, one per Modbus data table.
2. Lower the setpoint to 22 °C. A **FC06 Write Single Register** to address 0 with
   value `220` appears. The compressor LED turns on and the trend shows the room cooling.
3. Switch power off. A **FC05 Write Single Coil** appears, the compressor stops and the
   room slowly warms up. Switch it back on.
4. Lower the alarm limit below the room temperature to trigger the high temperature
   alarm (discrete input 1).
5. From another machine, read the PLC directly, bypassing the HMI:
   ```bash
   mbpoll -m tcp -a 1 -r 1 -c 4 -t 3 -1 <host-ip>   # input registers 30001-30004
   mbpoll -m tcp -a 1 -r 1 -t 4 <host-ip> 230        # write setpoint 23.0 °C
   ```
   or in Python:
   ```python
   from pymodbus.client import ModbusTcpClient
   c = ModbusTcpClient("<host-ip>", port=502)
   c.connect()
   print(c.read_input_registers(0, count=4, slave=1).registers)
   c.write_register(0, 230, slave=1)
   ```
   The HMI picks up the new setpoint on its next poll, which shows that the PLC, not
   the HMI, owns the data.
6. Stop the sensor with `docker compose stop sensor`. The PLC raises the sensor comm
   fault and stops the compressor. `docker compose start sensor` recovers it.
