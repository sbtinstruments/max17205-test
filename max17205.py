import ctypes
from datetime import datetime
from pathlib import Path
from typing import Optional
import anyio
import asyncssh
#import plotext as plt
import matplotlib.pyplot as plt
import json

_HOST = "zs2149027"
_DB_FILE = Path("db.json")

rSense = 10 # mOhm

# This code assumes that you set:
#
#  * Set PackCfg (0x0BD) to 0x1C62
#  * Set DesignCap (0x018) to 3.35 Ah (value 0x1BBC)
#
# Use these commands to do so in Linux:
#
#     i2cset -f 0 0x36 0x0BD 0x1c62 w
#     i2cset -f 0 0x36 0x018 0x1BBC w
#
# Furthermore, you may want to reduce `FullSOCThr` during your tests
# to widen the cell balancing window.
#
#     i2cset -f 0 0x36 0x013 0x2000 w 
#

class TimeSeriesDatabase:
    def __init__(self) -> None:
        self._data: Optional[dict[str, list[float]]]

    @property
    def data(self) -> dict[str, list[float]]:
        assert self._data is not None
        return self._data

    def append(self, VFSOC: float, FullSOCThr: float, AvgCurrent: float, IChgTerm: float, AvgCell1: float, AvgCell2: float, RepSOC) -> None:
        #t = plt.datetime.datetime_to_string(datetime.now())
        self._data["t"].append(datetime.now().isoformat())
        self._data["VFSOC"].append(VFSOC)
        self._data["FullSOCThr"].append(FullSOCThr)
        self._data["AvgCurrent"].append(AvgCurrent)
        self._data["IChgTerm"].append(IChgTerm)
        self._data["AvgCell1"].append(AvgCell1)
        self._data["AvgCell2"].append(AvgCell2)
        self._data["RepSOC"].append(RepSOC)

    def __enter__(self):
        try:
            with _DB_FILE.open("rb") as io:
                self._data = json.load(io)
        except (OSError, json.JSONDecodeError):
            self._data = {
                "t": [],
                "VFSOC": [],
                "FullSOCThr": [],
                "AvgCurrent": [],
                "IChgTerm": [],
                "AvgCell1": [],
                "AvgCell2": [],
                "RepSOC": [],
            }       
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        with _DB_FILE.open("w") as io:
            json.dump(self._data, io)


async def main() -> None:
    db = TimeSeriesDatabase()

    with db:
        async with asyncssh.connect(_HOST) as conn:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_stream_vals, conn, db)
                await anyio.sleep(1)
                tg.start_soon(_update_plot, db)


async def _update_plot(db: TimeSeriesDatabase) -> None:
    ax1 = plt.subplot(2,1,1)
    ax2 = ax1.twinx()
    ax3 = plt.subplot(2,1,2, sharex=ax1)
    ax4 = ax3.twinx()
    plt.title("Streaming data")

    while True:
        t = [datetime.fromisoformat(x) for x in db.data["t"]]

        ax1.cla()
        ax1.plot(t, [x / 256 for x in db.data["VFSOC"]], label="VFSOC", color="limegreen")
        ax1.plot(t, [x / 256 for x in db.data["FullSOCThr"]], label="FullSOCThr", color="darkgreen")
        ax1.plot(t, [x / 256 for x in db.data["RepSOC"]], label="RepSOC", color="black")
        ax1.set_ylim(0, 100)
        ax1.set_ylabel("State of charge [%]")
        ax1.legend(loc='lower left')
        ax1.grid(axis="y", color='0.95')

        ax2.cla()
        ax2.plot(t, [_to_amps(x) for x in db.data["AvgCurrent"]], label="AvgCurrent", color="red")
        ax2.plot(t, [4 * _to_amps(x) for x in db.data["IChgTerm"]], label="4xIChgTerm", color="darkred")
        ax2.set_ylim(-1, 1)
        ax2.set_ylabel("Current [A]")
        ax2.legend(loc='lower right')

        cell1_voltage = [x * 625 / 8 / 1e6 for x in db.data["AvgCell1"]]
        cell2_voltage = [x * 625 / 8 / 1e6 for x in db.data["AvgCell2"]]
        ax3.cla()
        ax3.plot(t, cell1_voltage, label="AvgCell1", color="blue")
        ax3.plot(t, cell2_voltage, label="AvgCell2", color="darkblue")
        ax3.set_ylim(0, 5)
        ax3.set_ylabel("Voltage [V]")
        ax3.legend(loc='lower left')
        ax3.grid(axis="y", color='0.95')

        assert len(cell1_voltage) == len(cell2_voltage)
        cell_voltage_abs_diff = [
           1e3 * abs(cell1_voltage[i] - cell2_voltage[i]) for i in range(len(cell1_voltage))
        ]
        ax4.cla()
        ax4.plot(t, cell_voltage_abs_diff, label="Difference", color="black")
        ax4.set_ylim(0, 200)
        ax4.set_ylabel("Voltage [mV]")
        ax4.legend(loc='lower right')

        #plt.show()
        plt.draw()
        plt.pause(0.001)
        await anyio.sleep(0.125)

def _to_amps(curr: int) -> float:
    rsense = 10  # mOhm
    return curr * 15625 / (rsense * 10) / 1e6

async def _stream_vals(conn: asyncssh.SSHClientConnection, db: TimeSeriesDatabase) -> None:
    while True:
        await _append_vals(conn, db)
        await anyio.sleep(0.1)


async def _append_vals(conn: asyncssh.SSHClientConnection, db: TimeSeriesDatabase) -> None:
    db.append(
        await _get_reg(conn, 0xFF),
        await _get_reg(conn, 0x13),
        await _get_reg(conn, 0x0B, signed=True),
        await _get_reg(conn, 0x1E, signed=True),
        await _get_reg(conn, 0xD4),
        await _get_reg(conn, 0xD3),
        await _get_reg(conn, 0x06),
    )


async def _get_reg(conn: asyncssh.SSHClientConnection, reg_address: int, *, signed: bool = False) -> int:
    cmd = f"i2cget -f -y 0 0x36 {hex(reg_address)} w"
    completed_process = await conn.run(cmd, check=True)
    hex_string: str = completed_process.stdout.strip()
    result = int(hex_string, 16)
    if signed:
        return ctypes.c_int16(result).value
    return result


anyio.run(main)