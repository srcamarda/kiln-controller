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

# Leitura em execução
SAMPLE_INTERVAL_S    = 0.5    # uma leitura SPI a cada 0,5 s, não importa quem consulta
MEDIAN_WINDOW        = 10     # amostras no filtro de mediana (≈ 5 s)
MAX_PHYSICAL_JUMP    = 5.0    # °C — desvio da mediana considerado leitura espúria
JUMP_RESET_COUNT     = 6      # rejeições seguidas (≈ 3 s) → aceita o novo patamar
RAW_WINDOW           = 24     # últimas amostras válidas, aceitas ou não (≈ 12 s)
NOISE_MIN_REJECTS    = 6      # rejeições na janela a partir das quais a leitura é "ruidosa"
NOISE_MAX_SPREAD     = 60.0   # °C — amplitude máx. do ruído para usar a mediana dele
FAULT_TOLERANCE_S    = 15.0   # s sem leitura válida até declarar falha (3 ciclos de controle)


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

    @property
    def fault_reason(self) -> str:
        return ""

    @property
    def last_raw(self):
        return self.temperature

    @property
    def fail_count(self) -> int:
        return 0

    def poll(self):
        pass


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

        # Estado da leitura temporizada (ver _sample)
        self._history      = []      # amostras válidas aceitas (mediana)
        self._last_sample  = None    # monotonic da última tentativa de leitura
        self._last_good    = None    # monotonic da última leitura válida
        self._fail_count   = 0       # leituras inválidas seguidas
        self._fail_reason  = ""
        self._reject_count = 0       # saltos rejeitados seguidos e consistentes
        self._reject_last  = None
        self._raw          = []      # últimas RAW_WINDOW amostras válidas (aceitas ou não)
        self._raw_rej      = []      # ...e se cada uma foi rejeitada pelo filtro
        self._noisy        = False   # já avisou que a leitura está ruidosa
        self._clean        = 0       # amostras aceitas direto (sem ruído) seguidas
        self.last_raw      = None    # última leitura bruta válida (None se a última falhou)

    def update(self, bank_a: bool, bank_b: bool):
        pass

    def probe(self, samples: int = PROBE_SAMPLES, interval: float = PROBE_INTERVAL_S):
        """
        Valida a comunicação com o MAX31855 lendo diretamente o chip.
        Levanta SensorInitError se qualquer amostra for inválida.
        Retorna a última temperatura lida e semeia o filtro com ela.
        """
        temp = None
        for i in range(samples):
            temp, err = self._read_once(check_ref=True)
            if err:
                raise SensorInitError(err)
            if i < samples - 1:
                time.sleep(interval)
        now = time.monotonic()
        self._history     = [temp]
        self._last_good   = now
        self._last_sample = now
        return temp

    # ── Leitura ───────────────────────────────

    def _read_once(self, check_ref: bool = False):
        """
        Uma leitura do chip. Retorna (temp, None) se válida ou (None, motivo).
        A lib Adafruit levanta RuntimeError em termopar aberto/curto.
        A junção fria só é lida na validação inicial ou se temp == 0,0.
        """
        try:
            temp = self._dev.temperature
        except Exception as e:
            return None, f"falha de leitura do MAX31855: {e}"
        if temp is None:
            return None, "MAX31855 retornou leitura vazia"
        if check_ref or temp == 0.0:
            # Só neste caso lê a junção fria: palavra SPI toda zero = MISO sem sinal
            try:
                ref = self._dev.reference_temperature
            except Exception as e:
                return None, f"falha de leitura do MAX31855: {e}"
            if ref == 0.0:
                return None, "leitura nula (módulo desconectado?)"
            if ref is None or not (COLD_JUNCTION_MIN_C <= ref <= COLD_JUNCTION_MAX_C):
                return None, f"junção fria implausível: {ref}"
        if not (THERMO_MIN_C <= temp <= THERMO_MAX_C):
            return None, f"temperatura implausível: {temp:.1f} °C"
        return temp, None

    def poll(self):
        """Faz no máximo uma leitura a cada SAMPLE_INTERVAL_S."""
        now = time.monotonic()
        if self._last_sample is not None and now - self._last_sample < SAMPLE_INTERVAL_S:
            return
        self._last_sample = now
        self._sample(now)

    def _sample(self, now):
        temp, err = self._read_once()
        self.last_raw = temp

        if err:
            if self._fail_count == 0:
                print(f"\r[sensor] Leitura inválida: {err}")
            self._fail_count += 1
            self._fail_reason = err
            return

        if self._fail_count:
            print(f"\r[sensor] Leitura normalizada após {self._fail_count} falha(s).")
        self._fail_count = 0

        # Filtro anti-salto: rejeita desvios grandes da mediana. Uma amostra
        # só conta como "leitura boa" (_last_good) quando entra no histórico —
        # rejeições seguidas por FAULT_TOLERANCE_S viram falha do termopar, em
        # vez de congelar a temperatura sem acusar nada (achado N1).
        if len(self._history) >= 3:
            median   = statistics.median(self._history)
            rejected = abs(temp - median) > MAX_PHYSICAL_JUMP
            # Janela das últimas amostras válidas, aceitas ou não (achado N3):
            # com ruído largo, aceitar só as amostras perto da mediana prende
            # o histórico numa das bordas do ruído (erro de até ±20 °C, falso
            # "aquece desligado"). No modo ruidoso o valor usado é a mediana
            # da janela inteira, sem viés.
            self._raw.append(temp)
            self._raw_rej.append(rejected)
            del self._raw[:-RAW_WINDOW], self._raw_rej[:-RAW_WINDOW]

            if rejected:
                consistent = (self._reject_last is not None
                              and abs(temp - self._reject_last) <= MAX_PHYSICAL_JUMP)
                self._reject_count = self._reject_count + 1 if consistent else 1
                self._reject_last  = temp
                self._clean = 0
                # Rejeições consistentes antes das quais a leitura estava limpa
                prior_rej = sum(self._raw_rej[:-self._reject_count])
                if (self._reject_count >= JUMP_RESET_COUNT
                        and prior_rej < NOISE_MIN_REJECTS):
                    # Valor novo e estável (ex.: reconexão): novo patamar,
                    # semeado com as leituras consistentes (nunca com uma só).
                    # Com ruído largo, seis leituras parecidas em sequência
                    # acontecem por acaso — aí quem acompanha é a mediana da
                    # janela (abaixo), não um patamar novo.
                    print(f"\r[sensor] Novo patamar aceito: {median:.1f} → {temp:.1f} °C")
                    self._history = self._raw[-JUMP_RESET_COUNT:-1]
                    self._raw     = self._raw[-JUMP_RESET_COUNT:]
                    self._raw_rej = [False] * len(self._raw)
                    self._reject_count = 0
                    self._reject_last  = None
                    self._accept(temp, now)
                    return
            else:
                self._reject_count = 0
                self._reject_last  = None

            if sum(self._raw_rej) >= NOISE_MIN_REJECTS:
                spread = max(self._raw) - min(self._raw)
                if spread <= NOISE_MAX_SPREAD:
                    # Ruído limitado (ex.: EMI dos contatores): mediana da janela
                    if not self._noisy:
                        print(f"\r[sensor] Leitura ruidosa (±{spread / 2:.0f} °C) — "
                              f"usando a mediana das leituras.")
                        self._noisy = True
                    self._clean = 0
                    self._accept(statistics.median(self._raw), now)
                    return
                if rejected:
                    # Errático demais: nada é aceito; vira falha após FAULT_TOLERANCE_S
                    self._fail_reason = (f"leitura instável (variação de {spread:.0f} °C "
                                         f"entre leituras seguidas)")
                    return
            elif rejected:
                if not self._fail_reason.startswith("leitura instável"):
                    self._fail_reason = "leitura instável (leituras rejeitadas pelo filtro)"
                return

            if self._noisy:
                self._clean += 1
                if self._clean >= MEDIAN_WINDOW:
                    print("\r[sensor] Leitura estável de novo.")
                    self._noisy = False
        self._accept(temp, now)

    def _accept(self, temp, now):
        self._last_good = now
        self._history.append(temp)
        if len(self._history) > MEDIAN_WINDOW:
            self._history.pop(0)

    # ── Interface usada pelo controle ─────────

    @property
    def temperature(self) -> float:
        """Mediana das últimas leituras válidas. Durante falhas transitórias
        mantém o último valor; use `fault` para saber se ainda é confiável."""
        self.poll()
        if self._history:
            return statistics.median(self._history)
        return 0.0

    @property
    def fault(self) -> bool:
        """True se não houve leitura válida nos últimos FAULT_TOLERANCE_S."""
        self.poll()
        if self._last_good is None or not self._history:
            return True
        return time.monotonic() - self._last_good > FAULT_TOLERANCE_S

    @property
    def fault_reason(self) -> str:
        return self._fail_reason if self.fault else ""

    @property
    def fail_count(self) -> int:
        return self._fail_count


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
