"""
sync_rtc.py — Sincroniza o horário do RTC com o atual.

"""

from datetime import datetime
from rtc import create_rtc, HardwareRTC

def sync():
    my_rtc = create_rtc()
    
    if isinstance(my_rtc, HardwareRTC):
        # Pega a hora atual do sistema (Raspberry Pi)
        now = datetime.now()
        
        # Grava no módulo DS3231
        my_rtc.set_time(now)
        
        # Lê de volta para confirmar
        stored_time = my_rtc.now()
        print(f"\n[Sucesso] RTC sincronizado para: {stored_time.strftime('%d/%m/%Y %H:%M:%S')}")
    else:
        print("\n[Erro] Módulo DS3231 não detectado. Verifique as conexões I2C.")

if __name__ == "__main__":
    sync()