"""
rtc.py — Leitura do DS3231 via I2C.
Fallback para horário do sistema se o módulo não estiver presente.
"""

from datetime import datetime
import os

try:
    import board
    import adafruit_ds3231
    _HW_AVAILABLE = True
except (ImportError, NotImplementedError):
    _HW_AVAILABLE = False


class HardwareRTC:
    def __init__(self):
        i2c = board.I2C()
        self._rtc = adafruit_ds3231.DS3231(i2c)

    def now(self) -> datetime:
        t = self._rtc.datetime
        return datetime(t.tm_year, t.tm_mon, t.tm_mday,
                        t.tm_hour, t.tm_min, t.tm_sec)

    def set_time(self, dt: datetime):
        import time
        self._rtc.datetime = time.struct_time(
            (dt.year, dt.month, dt.day,
             dt.hour, dt.minute, dt.second,
             dt.weekday(), -1, -1)
        )


class SystemRTC:
    def now(self) -> datetime:
        return datetime.now()

    def set_time(self, dt: datetime):
        pass  # sistema sem permissão para ajustar RTC externo


def create_rtc():
    if os.environ.get("SIM_RTC") == "1":
        print("\r[rtc] Simulação forçada (SIM_RTC=1).")
        return SystemRTC()
    
    if _HW_AVAILABLE:
        try:
            rtc = HardwareRTC()
            _ = rtc.now()
            print("\r[rtc] DS3231 detectado.")
            return rtc
        except Exception as e:
            print(f"\r[rtc] DS3231 falhou ({e}) — usando horário do sistema.")
    else:
        print("\r[rtc] Bibliotecas não disponíveis — usando horário do sistema.")
    return SystemRTC()