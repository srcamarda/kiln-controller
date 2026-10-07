"""
tools/check_thermocouple.py — Lê o MAX31855 direto em loop (teste de bancada no Pi).
Uso: python3 tools/check_thermocouple.py   (Ctrl+C para sair)
"""
import time
import board
import busio
import digitalio
import adafruit_max31855

print("Iniciando monitoramento duplo...")

spi = busio.SPI(clock=board.D21, MISO=board.D19)
cs = digitalio.DigitalInOut(board.D20)
sensor = adafruit_max31855.MAX31855(spi, cs)

while True:
    try:
        temp_ext = sensor.temperature
        temp_int = sensor.reference_temperature
        
        print(f"Ponta Termopar: {temp_ext:5.1f}°C  |  Placa (Interna): {temp_int:5.1f}°C")
    except Exception as e:
        print(f"Falha na leitura: {e}")
        
    time.sleep(0.5)