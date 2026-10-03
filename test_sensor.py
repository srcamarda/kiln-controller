"""
test_sensor.py — Testa o simulador por 10 segundos.
Mostra a temperatura subindo com banco A ligado.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import time
from sensor import create_sensor

# Este script testa o simulador: sem SIM_TEMP=1 o create_sensor() exige hardware.
os.environ.setdefault("SIM_TEMP", "1")

sensor = create_sensor()

print("\nSimulando 10s com Banco A ligado:")
print(f"{'Tempo':>6}  {'Temp':>8}  {'Banco A':>8}  {'Banco B':>8}")
print("-" * 40)

for i in range(20):
    bank_a = True
    bank_b = i >= 12  # Liga banco B depois de ~6s
    sensor.update(bank_a, bank_b)
    print(f"{i*0.5:>5.1f}s  {sensor.temperature:>7.1f}°C  "
          f"{'ON' if bank_a else 'off':>8}  {'ON' if bank_b else 'off':>8}")
    time.sleep(0.5)

print("\nDesligando bancos — resfriamento:")
for i in range(10):
    sensor.update(False, False)
    print(f"{(i+20)*0.5:>5.1f}s  {sensor.temperature:>7.1f}°C  "
          f"{'off':>8}  {'off':>8}")
    time.sleep(0.5)
