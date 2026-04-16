import RPi.GPIO as GPIO
import time

PIN_CLK = 12
PIN_DT  = 6

GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(PIN_DT,  GPIO.IN, pull_up_down=GPIO.PUD_UP)

def read_state():
    clk = GPIO.input(PIN_CLK)
    dt  = GPIO.input(PIN_DT)
    return (clk << 1) | dt

last = read_state()

print("Iniciando leitura... (CTRL+C pra sair)")
print("Formato: tempo(ms) | CLK DT | estado")

start = time.monotonic()

try:
    while True:
        state = read_state()
        if state != last:
            t = (time.monotonic() - start) * 1000
            clk = (state >> 1) & 1
            dt  = state & 1
            print(f"{t:8.1f} ms |  {clk}   {dt}  | {state:02b}")
            last = state

        time.sleep(0.0005)  # 0.5ms
except KeyboardInterrupt:
    GPIO.cleanup()