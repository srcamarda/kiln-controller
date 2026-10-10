"""
rtc.py — Leitura do DS3231 via I2C.
Fallback para horário do sistema se o módulo não estiver presente.
"""

from datetime import datetime, timedelta
import os
import time as _time

try:
    import board
    import adafruit_ds3231
    _HW_AVAILABLE = True
except (ImportError, NotImplementedError):
    _HW_AVAILABLE = False


class HardwareRTC:
    def __init__(self, dev=None):
        if dev is None:
            i2c = board.I2C()
            dev = adafruit_ds3231.DS3231(i2c)
        self._rtc = dev
        self._last_good = None     # (datetime lido, time.monotonic() da leitura)
        self._failing   = False

    def now(self) -> datetime:
        """
        Hora do DS3231. Uma leitura I2C falha (ruído perto do forno) não pode
        derrubar o controle: nesse caso devolve a última hora boa avançada pelo
        relógio monotônico (mantém a continuidade, importante para o cálculo
        do tempo fora do ar na retomada); sem nenhuma leitura boa ainda, usa a
        hora do sistema.
        """
        try:
            t = self._rtc.datetime
            dt = datetime(t.tm_year, t.tm_mon, t.tm_mday,
                          t.tm_hour, t.tm_min, t.tm_sec)
        except Exception as e:
            if not self._failing:
                print(f"\r[rtc] Falha ao ler o DS3231 ({e!r}) — usando hora estimada.")
                self._failing = True
            if self._last_good is not None:
                dt0, m0 = self._last_good
                return dt0 + timedelta(seconds=_time.monotonic() - m0)
            return datetime.now()
        if self._failing:
            print("\r[rtc] Leitura do DS3231 normalizada.")
            self._failing = False
        self._last_good = (dt, _time.monotonic())
        return dt

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
            print("\r[rtc] DS3231 detectado - Usando hardware.")
            return rtc
        except Exception as e:
            print(f"\r[rtc] DS3231 falhou ({e}) — usando horário do sistema.")
    else:
        print("\r[rtc] Bibliotecas não disponíveis — usando horário do sistema.")
    return SystemRTC()