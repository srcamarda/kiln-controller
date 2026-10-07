Controladora feita para substituir a placa original (manual) de um forno cerâmico de alta temperatura.

Hardware necessário:
- 1x Raspberry Pi (Modelo 2B no meu projeto)
- 2x Relé de alta amperagem
- 1x Módulo MAX31855
- 1x Termopar tipo K
- 1x Módulo DS3231 (RTC)
- 1x Display LCD RPi
- 1x Encoder Rotativo com clique

## Simulação e segurança

Sem hardware detectado o programa **não inicia o controle** (fail-closed): se o MAX31855 não responder ou retornar leitura inválida, a tela mostra "Termopar ausente", os relés ficam desligados, é enviado um alerta no Telegram e a detecção é repetida a cada 5 s.

Os simuladores só são usados quando pedidos explicitamente:

- `SIM_TEMP=1` — termopar simulado
- `SIM_RELAYS=1` — relés simulados
- `SIM_ENCODER=1` — encoder simulado
- `SIM_RTC=1` — horário do sistema em vez do DS3231

Termopar simulado com relés reais é recusado, a menos que `KILN_ALLOW_SIM_WITH_REAL_RELAYS=1` (teste de bancada, com os contatores desligados do forno).

Desenvolvimento no PC: `SIM_TEMP=1 SIM_RELAYS=1 SIM_ENCODER=1 SIM_RTC=1 python3 main.py`

## Configuração do Telegram

Token, chats autorizados e senha do `/stop` **não ficam no código**. Copie `.env.example` para `.env` (mesma pasta do `main.py`), preencha e proteja o arquivo:

```
cp .env.example .env
nano .env
chmod 600 .env
```

Variáveis já definidas no ambiente (ex.: `Environment=`/`EnvironmentFile=` no systemd) têm precedência sobre o `.env`. Sem token/chat o Telegram fica desativado; sem `KILN_STOP_PASSWORD` o `/stop` remoto fica desativado.

Comandos: `/status` e `/stop` (pede a senha; a mensagem com a senha é apagada do chat se o bot tiver permissão; 3 senhas erradas bloqueiam o `/stop` por 15 min). Em grupos, só aceita comandos de pessoas cujo id também está em `KILN_TELEGRAM_CHAT_IDS`.

Entrega: sem internet ou com o Telegram fora, as mensagens são reenviadas com espera crescente — alertas críticos (erros, queima retomada/não retomada, conclusão) por até 6 h, as demais por até 10 min. Falhas aparecem em `journalctl -u kiln-controller`.

## Limites de segurança (software)

Qualquer um destes desliga os relés e trava em ERRO (tela + alerta no Telegram); os valores ficam no topo do `controller.py`:

- **Sobretemperatura**: acima de 1300 °C em qualquer modo (inclusive parado), ou 50 °C acima do alvo do modo ativo.
- **Aquece desligado**: subida de 30 °C com os dois bancos desligados — indica contator colado.
- **Temp. não sobe**: com aquecimento ligado e mais de 10 °C abaixo do setpoint, a temperatura não sobe 10 °C em 40 min (resistência queimada, termopar fora do lugar).
- **Tempo excedido**: segmento acima de 2× a duração teórica + 1 h; pré-aquecimento acima de 24 h.

Esses limites não substituem um limitador de sobretemperatura **em hardware**, em série com os contatores: se o Raspberry Pi travar ou um contator colar, o software não consegue desligar o forno.

## Execução como serviço (systemd)

No Pi, na pasta do projeto (depois de criar o `.env`):

```
sudo sh deploy/install.sh
```

O script instala `kiln-controller.service` (sobe no boot, sem depender de rede), ativa o watchdog de hardware do Pi e reinicia o serviço. Log: `journalctl -u kiln-controller -f`.

Camadas de proteção:

- **Watchdog do serviço** (`WatchdogSec=30`): se o loop principal travar, o systemd mata e reinicia o processo.
- **Watchdog de hardware** (`RuntimeWatchdogSec=14`): se o Pi/kernel travar, o Pi reinicia sozinho.
- **Relés desligados ao sair**: `deploy/relays-off.sh` roda sempre que o processo termina (inclusive morto pelo watchdog) e força GPIO13/16 em HIGH.
- **Relés desligados no boot**: `gpio=13,16=op,dh` no `config.txt` faz o firmware do Pi colocar os pinos em HIGH (relés desligados no HL-52S) antes do Linux subir.

## Retomada da queima após reinício

Durante uma queima o estado é gravado (a cada 60 s e a cada troca de segmento) em `/var/lib/kiln-controller/firing.json` (ou `./state/` rodando à mão). Após queda de energia, travamento ou reinício, o controlador retoma automaticamente e avisa no Telegram:

- a rampa recomeça da temperatura atual; se o forno esfriou abaixo de segmentos já concluídos, volta para o segmento correspondente;
- no patamar, continua com o tempo restante (se esfriou mais de 30 °C, volta a rampar o último segmento e faz o patamar completo).

**Não retoma** (trava em ERRO "Queima interrompida" e avisa) quando:

- o termopar não é detectado (o controlador fica bloqueado até resolver);
- ficou mais de 4 h fora do ar;
- houve 3 retomadas na última hora (falha recorrente);
- a temperatura subiu mais de 30 °C com o controlador fora do ar (contator colado?).

Queima concluída, cancelada (encoder ou `/stop`) ou em ERRO apaga o estado. `systemctl stop`/reboot **não** apaga: a queima é retomada no próximo start. Para abandonar uma queima sem o encoder: `sudo rm /var/lib/kiln-controller/firing.json` com o serviço parado.

## Curvas

As curvas ficam em `curves.json` (ou no caminho de `KILN_CURVES_FILE`). Limites: taxa 10–300 °C/h, temperatura 100–1270 °C (crescente entre segmentos), até 12 segmentos, patamar 1–240 min.

- Gravação atômica: uma queda de energia durante o salvamento não corrompe o arquivo; a versão anterior fica em `curves.json.bak`.
- Na inicialização, curvas inválidas (ex.: editadas à mão com erro) são ignoradas com aviso no console e no Telegram, em vez de impedir o programa de subir. Se o arquivo estiver corrompido, usa o `.bak`.
- Erros ao salvar pelo encoder aparecem na tela.
