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

Comandos (executados pelo loop principal do controlador, não pela thread do Telegram; a resposta confirma o que aconteceu — ex.: "nenhuma queima em andamento"): `/status` e `/stop` (pede a senha; a mensagem com a senha é apagada do chat se o bot tiver permissão; 3 senhas erradas bloqueiam o `/stop` por 15 min). Só aceita comandos de usuários em `KILN_TELEGRAM_ADMIN_IDS` (vazio = os chat ids, para uso em chat privado); a senha do `/stop` só vale vinda de quem pediu. Comandos enviados com o controlador desligado ou reiniciando são descartados ao iniciar, e qualquer mensagem que chegue com mais de 60 s de atraso (rede caiu e voltou) é ignorada com aviso no chat — envie de novo se ainda quiser. O atraso e o prazo da senha são medidos pela hora do Telegram, não pelo relógio do Pi. Se o loop principal estiver travado e não executar o `/stop` em 10 s, a própria thread do Telegram desliga e **trava** os relés (o ciclo de controle não consegue religá-los) e apaga o estado da queima, que não é retomada nem se o watchdog reiniciar o programa; se o loop voltar, ele conclui a parada, libera a trava e avisa no Telegram.

Entrega: sem internet ou com o Telegram fora, as mensagens são reenviadas com espera crescente — alertas críticos (erros, queima retomada/não retomada, conclusão) por até 6 h, as demais por até 10 min. Falhas aparecem em `journalctl -u kiln-controller`.

### Mensagens num grupo

1. Adicione o bot do forno ao grupo e **promova-o a administrador** (sem isso ele não recebe a senha digitada após o `/stop` — por padrão bots em grupo só veem comandos — e não consegue apagar a mensagem com a senha). Promover pode converter o grupo em supergrupo e **mudar o id**: pegue o id depois deste passo.
2. Descubra o id do grupo: com o serviço rodando, mande `/status` no grupo e veja o log (`journalctl -u kiln-controller -n 20`): aparece `Mensagem de chat não autorizado ignorada: id=-100…`.
3. No `.env`: `KILN_TELEGRAM_CHAT_IDS=<id do grupo>` e `KILN_TELEGRAM_ADMIN_IDS=<seu id de usuário>` (o id que antes estava em `KILN_TELEGRAM_CHAT_IDS`). Reinicie: `sudo systemctl restart kiln-controller`.

Se o grupo mudar de id depois, o log avisa o id novo ao tentar enviar.

## Aviso de queda (healthchecks.io)

Durante cada queima o Pi manda um sinal de vida por minuto para o healthchecks.io (com temperatura, setpoint e segmento, visíveis no histórico do check). Se os sinais param — queda de energia, travamento, falta de internet — o healthchecks.io avisa pelo Telegram em ~10 min. Em ERRO o Pi também sinaliza falha na hora. Ao fim da queima o check é pausado, para não alertar com o forno parado.

1. No healthchecks.io, crie um check com **período 5 min e tolerância 5 min** e conecte a integração do Telegram (@HealthchecksBot).
2. No `.env`: `KILN_HC_PING_URL=https://hc-ping.com/<uuid>` (a URL do check, formato com uuid) e `KILN_HC_API_KEY=<chave read-write>` (Settings do projeto → API Access).
3. Antes da primeira queima, deixe o check **pausado** (botão Pause) — ele é reativado automaticamente quando a queima começa.

## Limites de segurança (software)

Qualquer um destes desliga os relés e trava em ERRO (tela + alerta no Telegram); os valores ficam no topo do `controller.py`:

- **Sobretemperatura**: acima de 1300 °C em qualquer modo (inclusive parado), ou 50 °C acima do alvo do modo ativo.
- **Aquece desligado**: subida de 30 °C com os dois bancos desligados — indica contator colado.
- **Temp. não sobe**: com aquecimento ligado e mais de 10 °C abaixo do setpoint, a temperatura não sobe 10 °C em 40 min (resistência queimada, termopar fora do lugar).
- **Tempo excedido**: segmento acima de 2× a duração teórica + 1 h; pré-aquecimento acima de 24 h.

Depois de um ERRO a vigilância continua: se a temperatura passar de 1300 °C ou subir mais de 30 °C com os relés desligados (contator colado), sai um novo alerta no Telegram (e falha no healthchecks.io), repetido a cada 10 min enquanto continuar subindo.

Falha do termopar **com o forno parado** não trava em ERRO: o controlador avisa que está sem vigilância e, quando a leitura volta, avisa de novo e retoma as verificações. Durante uma queima, falha do termopar por mais de 15 s continua abortando (ERRO).

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
- **Relés desligados ao sair**: o próprio programa, ao encerrar (Ctrl+C, `systemctl stop`, erro), força GPIO13/16 em HIGH com `pinctrl` depois que o gpiozero libera os pinos — rodando à mão também. Sob o systemd, `deploy/relays-off.sh` faz o mesmo no ExecStopPost, cobrindo também o processo morto pelo watchdog.
- **Relés desligados no boot**: `gpio=13,16=op,dh` no `config.txt` faz o firmware do Pi colocar os pinos em HIGH (relés desligados no HL-52S) antes do Linux subir.
- **Tela e relógio são acessórios**: sem `/dev/fb1`, sem fontes ou com erro de escrita, o controle segue sem tela (nova tentativa de abrir a cada 30 s). Se a leitura do DS3231 falhar, usa a última hora boa avançada pelo relógio interno (ou a hora do sistema). Erros aparecem uma vez no log, não derrubam a queima. Para não pesar no loop do Pi 2B, a conversão para o formato da tela (RGB565) é feita dentro do Pillow, em C, e um quadro igual ao anterior não é reescrito (exceto a cada 30 s).

## Retomada da queima após reinício

Durante uma queima o estado é gravado (a cada 60 s e a cada troca de segmento) em `/var/lib/kiln-controller/firing.json` (ou `./state/` rodando à mão). Após queda de energia, travamento ou reinício, o controlador retoma automaticamente e avisa no Telegram:

- a rampa recomeça da temperatura atual; se o forno esfriou abaixo de segmentos já concluídos, volta para o segmento correspondente;
- no patamar, continua com o tempo restante (se esfriou mais de 30 °C, volta a rampar o último segmento e faz o patamar completo).

**Não retoma** (trava em ERRO "Queima interrompida" e avisa) quando:

- o termopar não é detectado (o controlador fica bloqueado até resolver);
- ficou mais de 4 h fora do ar;
- houve 3 retomadas na última hora (falha recorrente);
- a temperatura subiu mais de 30 °C com o controlador fora do ar (contator colado?).

O tempo fora do ar vem do DS3231. Se a hora não for confiável — oscilador do DS3231 parado (bateria fraca: a data volta para 2000), sem leitura do RTC, ou hora atual mais de 5 min antes do último registro — o tempo fica **desconhecido** e a decisão passa a ser pela temperatura: retoma só se o forno esfriou no máximo 60 °C desde o último registro (indício de queda curta); caso contrário não retoma. Na retomada com hora desconhecida, o aviso no Telegram pede para conferir a bateria e rodar `sync_rtc.py`. O mesmo aviso aparece no log ao iniciar. Os registros das queimas são limpos pela data de modificação do arquivo, então uma queima nova com nome de 2000 não é apagada.

Queima concluída, cancelada (encoder ou `/stop`) ou em ERRO apaga o estado. `systemctl stop`/reboot **não** apaga: a queima é retomada no próximo start. Para abandonar uma queima sem o encoder: `sudo rm /var/lib/kiln-controller/firing.json` com o serviço parado.

## Curvas

As curvas ficam em `curves.json` (ou no caminho de `KILN_CURVES_FILE`). Limites: taxa 10–300 °C/h, temperatura 100–1270 °C (crescente entre segmentos), até 12 segmentos, patamar 1–240 min.

- Gravação atômica: uma queda de energia durante o salvamento não corrompe o arquivo; a versão anterior fica em `curves.json.bak`.
- Na inicialização, curvas inválidas (ex.: editadas à mão com erro) são ignoradas com aviso no console e no Telegram, em vez de impedir o programa de subir. Se o arquivo estiver corrompido, usa o `.bak`.
- Erros ao salvar pelo encoder aparecem na tela.
- Forno já quente ao iniciar (ex.: logo após um pré-aquecimento): os segmentos cujo alvo já foi atingido são pulados (aviso no registro da queima e no Telegram). Se o forno estiver acima do alvo final, a curva não inicia ("FORNO QUENTE"). No pré-aquecimento com o forno acima do alvo, a margem de sobretemperatura vale sobre a temperatura de partida até ele esfriar até o alvo.

## Registro das queimas

Cada queima (ou pré-aquecimento) gera dois arquivos em `/var/log/kiln-controller/` (ou `./logs/` rodando à mão):

- `AAAAMMDD-HHMMSS_<curva>.csv` — uma linha a cada 5 s: temperatura filtrada e bruta, setpoint, segmento, relés A/B, corte do B por subida rápida, taxa de subida, falhas de leitura; mais linhas de evento (troca de segmento, patamar, retomada, erro, fim). Depois do fim, continua 1 linha/min durante o resfriamento até 100 °C.
- `AAAAMMDD-HHMMSS_<curva>.json` — curva completa, parâmetros de controle e segurança vigentes, retomadas e um resumo (temp. máxima, comutações dos relés, taxa real de cada segmento, desvio no patamar).

Uma queima retomada após reinício continua no mesmo arquivo. São mantidas as 100 queimas mais recentes.

Para analisar no PC:

```
scp pi@<ip-do-pi>:/var/log/kiln-controller/AAAAMMDD-HHMMSS_* .
python tools/plot_firing.py AAAAMMDD-HHMMSS_<curva>.csv            # resumo + gráfico
python tools/plot_firing.py AAAAMMDD-HHMMSS_<curva>.csv --png g.png
```

## Testes e ferramentas

Testes automáticos em `tests/` (rodam no PC, sem hardware), a partir da raiz do projeto:

```
python3 -m unittest                          # todos
python3 -m unittest tests.test_curves -v     # um arquivo
```

Ferramentas em `tools/`:

- `plot_firing.py` — resumo e gráfico de uma queima registrada (PC).
- `simulate_curves.py` — simula todas as curvas e imprime a evolução (PC).
- `simulate_sensor.py` — demonstra o termopar simulado (PC).
- `check_thermocouple.py` — lê o MAX31855 em loop (Pi, bancada).
- `check_encoder.py` — mostra os pulsos do encoder (Pi, bancada).
- `show_screens.py` — percorre as telas do display (Pi; `python3 tools/show_screens.py [tela]`).
