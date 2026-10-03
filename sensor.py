"""
sensor.py — Leitura do termopar MAX31855 via SPI1.
Em modo simulado, modela aquecimento/resfriamento com inércia simples.

Segurança (fail-closed): o simulador só é usado com SIM_TEMP=1 explícito.
Sem essa variável, qualquer falha ao inicializar ou validar o MAX31855
levanta SensorInitError — o programa nunca passa a "medir" um forno
simulado no lugar do real.
"""

import time
import os
import statistics

# Tenta importar bibliotecas reais do hardware
try:
    import board
    import busio
    import digitalio
    import adafruit_max31855
    _HW_AVAILABLE = True
    _HW_IMPORT_ERROR = None
except Exception as e:
    _HW_AVAILABLE = False
    _HW_IMPORT_ERROR = e


class SensorInitError(RuntimeError):
    """Termopar indisponível ou com leitura inválida na inicialização."""


# Faixas de plausibilidade usadas na validação inicial
PROBE_SAMPLES        = 3
PROBE_INTERVAL_S     = 0.2
THERMO_MIN_C         = -20.0    # abaixo disso a leitura não faz sentido p/ o forno
THERMO_MAX_C         = 1400.0   # acima do limite útil do tipo K neste forno
COLD_JUNCTION_MIN_C  = -20.0    # temperatura interna do MAX31855 (ambiente da placa)
COLD_JUNCTION_MAX_C  = 85.0


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
    DO no GPIO19, CS no GPIO20, SCK no GPIO21.
    """

    def __init__(self, dev=None):
        if dev is None:
            spi = busio.SPI(clock=board.D21, MISO=board.D19)
            cs = digitalio.DigitalInOut(board.D20)
            dev = adafruit_max31855.MAX31855(spi, cs)
        self._dev = dev

        # Buffer circular para o filtro de mediana
        self._history = []
        
        # Limite de variação real de temperatura em 0.5s
        self.MAX_PHYSICAL_JUMP = 5.0

    def update(self, bank_a: bool, bank_b: bool):
        pass

    def probe(self, samples: int = PROBE_SAMPLES, interval: float = PROBE_INTERVAL_S):
        """
        Valida a comunicação com o MAX31855 lendo diretamente o chip
        (sem o filtro de `temperature`, que engole exceções).
        Levanta SensorInitError se qualquer amostra for inválida.
        Retorna a última temperatura lida.
        """
        temp = None
        for i in range(samples):
            try:
                temp = self._dev.temperature
                ref  = self._dev.reference_temperature
            except Exception as e:
                # A lib levanta RuntimeError em termopar aberto/curto
                raise SensorInitError(f"falha de leitura do MAX31855: {e}") from e

            if temp is None or ref is None:
                raise SensorInitError("MAX31855 retornou leitura vazia")
            if temp == 0.0 and ref == 0.0:
                # Palavra SPI toda em zero: MISO sem sinal / módulo desconectado
                raise SensorInitError("leitura nula (módulo desconectado?)")
            if not (THERMO_MIN_C <= temp <= THERMO_MAX_C):
                raise SensorInitError(f"temperatura implausível: {temp:.1f} °C")
            if not (COLD_JUNCTION_MIN_C <= ref <= COLD_JUNCTION_MAX_C):
                raise SensorInitError(f"junção fria implausível: {ref:.1f} °C")

            if i < samples - 1:
                time.sleep(interval)
        return temp

    @property
    def temperature(self) -> float:
        try:
            val = self._dev.temperature
            if val is not None:
                # Se já temos um histórico sólido, validamos a física
                if len(self._history) >= 3:
                    current_median = statistics.median(self._history)
                    
                    # Se o salto for fisicamente impossível, ignoramos a leitura falsa
                    if abs(val - current_median) > self.MAX_PHYSICAL_JUMP:
                        val = current_median 
                
                self._history.append(val)
                
                # Mantém 10 amostras
                if len(self._history) > 10:
                    self._history.pop(0)
                
                return statistics.median(self._history)
        except Exception:
            pass
            
        # Se falhar a comunicação, mantém a última temperatura válida
        if self._history:
            return statistics.median(self._history)
        return 0.0

    @property
    def fault(self) -> bool:
        try:
            return self._dev.temperature is None
        except Exception:
            return True


# ──────────────────────────────────────────────
# Factory — usa hardware se disponível
# ──────────────────────────────────────────────

def create_sensor():
    """
    Retorna o sensor a ser usado pelo controle.
    - SIM_TEMP=1  → SimulatedSensor (apenas desenvolvimento/bancada).
    - caso contrário → HardwareSensor validado, ou SensorInitError.
    Nunca cai silenciosamente para o simulador.
    """
    if os.environ.get("SIM_TEMP") == "1":
        print("\r[sensor] ATENÇÃO: simulação forçada (SIM_TEMP=1) — sem leitura real.")
        return SimulatedSensor()

    if not _HW_AVAILABLE:
        raise SensorInitError(
            f"bibliotecas do MAX31855 indisponíveis ({_HW_IMPORT_ERROR}). "
            "Para rodar sem hardware use SIM_TEMP=1."
        )

    try:
        sensor = HardwareSensor()
    except Exception as e:
        raise SensorInitError(f"não foi possível abrir o MAX31855: {e}") from e

    temp = sensor.probe()
    print(f"\r[sensor] MAX31855 detectado — usando hardware ({temp:.1f} °C).")
    return sensor
