"""
sensor.py — Leitura do termopar MAX31855 via SPI1.
Em modo simulado, modela aquecimento/resfriamento com inércia simples.
"""

import time

# Tenta importar bibliotecas reais do hardware
try:
    import board
    import busio
    import digitalio
    import adafruit_max31855
    _HW_AVAILABLE = True
except (ImportError, NotImplementedError):
    _HW_AVAILABLE = False


# ──────────────────────────────────────────────
# Simulador
# ──────────────────────────────────────────────

class SimulatedSensor:
    """
    Modelo térmico simples do forno.
    Temperatura ambiente inicial: 25°C.
    """

    GAIN_A_ONLY  = 0.6  # °C/s
    GAIN_A_AND_B = 1.2   # °C/s
    LOSS         = 0.8  # °C/s resfriamento passivo
    AMBIENT      = 25.0

    def __init__(self):
        self._temp        = self.AMBIENT
        self._last_tick   = time.monotonic()
        self._bank_a      = False
        self._bank_b      = False

    def update(self, bank_a: bool, bank_b: bool):
        """Avança a simulação. Deve ser chamado a cada ciclo do loop principal."""
        now   = time.monotonic()
        dt    = now - self._last_tick
        self._last_tick = now
        self._bank_a = bank_a
        self._bank_b = bank_b

        if bank_a and bank_b:
            self._temp += self.GAIN_A_AND_B * dt
        elif bank_a:
            self._temp += self.GAIN_A_ONLY * dt
        else:
            # Resfriamento — não cai abaixo da temperatura ambiente
            self._temp = max(self.AMBIENT, self._temp - self.LOSS * dt)

    @property
    def temperature(self) -> float:
        return round(self._temp, 1)

    @property
    def fault(self) -> bool:
        return False


# ──────────────────────────────────────────────
# Hardware real
# ──────────────────────────────────────────────

class HardwareSensor:
    """
    Lê o MAX31855 via SPI1 (spidev1.0).
    DO no GPIO26, CS no GPIO20, SCK no GPIO21.
    """

    def __init__(self):
        # Define o Clock (SCK) e o MISO (DO) usando os pinos GPIO escolhidos
        spi = busio.SPI(clock=board.D21, MISO=board.D26)
        
        # Configura o pino CS como uma saída digital
        cs = digitalio.DigitalInOut(board.D20)
        
        self._dev = adafruit_max31855.MAX31855(spi, cs)

    def update(self, bank_a: bool, bank_b: bool):
        """Sem efeito no hardware — estado dos bancos não afeta a leitura."""
        pass

    @property
    def temperature(self) -> float:
        return self._dev.temperature

    @property
    def fault(self) -> bool:
        return self._dev.temperature is None


# ──────────────────────────────────────────────
# Factory — usa hardware se disponível
# ──────────────────────────────────────────────

def create_sensor():
    if _HW_AVAILABLE:
        try:
            sensor = HardwareSensor()
            # Leitura de teste para confirmar comunicação
            _ = sensor.temperature
            print("\r[sensor] MAX31855 detectado — usando hardware real.")
            return sensor
        except Exception as e:
            print(f"\r[sensor] Hardware falhou ({e}) — usando simulador.")
    else:
        print("\r[sensor] Bibliotecas não disponíveis — usando simulador.")
    return SimulatedSensor()
