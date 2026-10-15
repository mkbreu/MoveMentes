#!/usr/bin/env bash
# ~/movimente-automacao/scripts/executar.sh
#
# Ponto de entrada unico da automacao. Cuida do arranque a frio:
# o WSL derruba a distro quando ninguem esta usando, e junto vao os
# containers. Toda execucao portanto precisa: subir a stack, esperar o
# WAHA reconectar (leva ~60s) e so entao rodar a pipeline.
#
# Uso:
#   ./executar.sh                  pipeline completa (publica no grupo)
#   ./executar.sh --dry-run        coleta e monta o TXT, nao publica
#   ./executar.sh --diagnose       confere cada peca
#   ./executar.sh --monitor        so checa saude (silencio se tudo ok)
#   ./executar.sh --sem-envio      processa mas nao publica
# Qualquer outro argumento e repassado ao send_ranking.py.

set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR" || exit 1

PY="$BASE_DIR/.venv/bin/python"
ESPERA_MAX="${WAHA_ESPERA_SEGUNDOS:-150}"

log() { printf '%s | %s\n' "$(date '+%d/%m/%Y %H:%M:%S')" "$*"; }

# --- 0. JANELA DE PUBLICACAO (15/08/2026) ---
#
# A publicacao passou a ser 05:30 e 15:30. So que existem DOIS agendadores
# apontando para este script — o timer de dentro do WSL e a tarefa do Windows —
# e o do WSL ainda esta nos horarios velhos (10:30 e 22:00). Foi ele que
# publicou as 10:30 de 15/08 sem ninguem pedir.
#
# Enquanto os timers nao forem corrigidos (precisa de senha de administrador do
# Linux), esta guarda garante o combinado: rodada COMPLETA so dentro da janela.
# Ela nao atrapalha mais nada — `--so-fila`, `--monitor`, `--laudo` e `--alertar`
# passam direto, porque nenhum deles publica ranking.
#
# COMO FURAR DE PROPOSITO: `MOVIMENTE_FORCAR=1`. E o que o botao "Publicar
# agora" do painel e o `Movimente-se_Automacao.bat` usam — quando alguem pede,
# a hora nao manda.
#
# 20/09/2026 (P-139) — QUEM DIZ A HORA E O SITE, NAO ESTE ARQUIVO.
# Ate hoje os horarios estavam escritos aqui, e trocar o horario na tela
# "Quem publica e quando" nao mudava nada por aqui: o Wake-on-LAN do site
# acompanhava a tela e este script continuava no horario velho. Agora a
# pergunta vai para `/api/agenda/agora`, que le a agenda da cidade.
# Sem resposta do site, cai na janela de sempre — falha fechada.
_janela_do_site() {
  local url token resposta
  url="$(grep -E '^MOVIMENTE_API_URL=' .env | cut -d= -f2- | tr -d '\"'\''')"
  token="$(grep -E '^MOVIMENTE_API_TOKEN=' .env | cut -d= -f2- | tr -d '\"'\''')"
  [ -n "$url" ] && [ -n "$token" ] || return 2
  resposta="$(curl -s --max-time 15 -H "X-Automation-Token: $token" \
                 "$url/api/agenda/agora" 2>/dev/null)" || return 2
  [ -n "$resposta" ] || return 2
  case "$resposta" in
    *'"dentro_da_janela":true'*)  return 0 ;;
    *'"dentro_da_janela":false'*) return 1 ;;
    *) return 2 ;;
  esac
}

_janela_fixa() {
  local _hm
  _hm=$(date '+%H%M')
  { [ "$_hm" -ge 0510 ] && [ "$_hm" -le 0615 ]; } || \
  { [ "$_hm" -ge 1510 ] && [ "$_hm" -le 1615 ]; }
}

if [ $# -eq 0 ] && [ -z "${MOVIMENTE_FORCAR:-}" ]; then
  _janela_do_site
  _resposta_janela=$?
  case "$_resposta_janela" in
    0) log 'O site diz: estamos na janela de publicacao — seguindo.' ;;
    1) log 'O site diz: fora da janela de publicacao (horarios da tela "Quem publica e quando") — nada a fazer.'
       log 'Para publicar agora, use o botao do painel ou MOVIMENTE_FORCAR=1.'
       exit 0 ;;
    *) log 'Nao consegui perguntar a janela ao site — usando a janela de seguranca (05:10-06:15 e 15:10-16:15).'
       if ! _janela_fixa; then
         log 'Fora da janela de seguranca — nada a fazer.'
         exit 0
       fi ;;
  esac
fi

# --- 1. Docker de pe ---
if ! docker info >/dev/null 2>&1; then
  log 'Docker parado — iniciando...'
  sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null
  for _ in $(seq 1 20); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
if ! docker info >/dev/null 2>&1; then
  log 'ERRO: Docker nao subiu. Rode: sudo systemctl start docker'
  exit 1
fi

# --- 2. Containers de pe ---
log 'Subindo a stack (postgres e waha)...'
docker compose up -d 2>&1 | sed 's/^/         /'

# --- 3. Esperar a sessao do WhatsApp ficar pronta ---
# A SESSAO E ENCONTRADA PELO ESTADO, NUNCA PELO NOME.
#
# Ate 03/08/2026 este trecho perguntava por /api/sessions/<nome do .env>. Quando
# o nome divergia — o .env pedia "principal-pls" e a sessao viva se chamava
# "default" — a resposta era 404, o script concluia "sessao nao esta pronta" e
# o ranking simplesmente nao saia. Aconteceu seis vezes na madrugada de 03/08.
#
# Quem decide agora e o scripts/wahasessao.py, o MESMO modulo que o
# send_ranking.py usa: duas pecas lendo do mesmo lugar nao tem como divergir.
API_KEY="$(grep -E '^WAHA_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"'')"
WAHA_URL="$(grep -E '^WAHA_BASE=' .env | cut -d= -f2- | tr -d '"'"'"'')"
WAHA_URL="${WAHA_URL:-http://localhost:3000}"

# nome|status|estado|quantas
linha=''
ler_sessao() { linha="$("$PY" "$BASE_DIR/scripts/wahasessao.py" 2>/dev/null)"; }
campo() { printf '%s' "$linha" | cut -d'|' -f"$1"; }

# Nao basta status=WORKING: o WAHA responde WORKING com o motor WEBJS ainda
# em UNLAUNCHED, e nesse intervalo ler mensagens devolve HTTP 500.
log "Aguardando a sessao do WhatsApp conectar (ate ${ESPERA_MAX}s)..."
fim=$((SECONDS + ESPERA_MAX))
nome=''; status=''; estado=''; quantas=''
while :; do
  ler_sessao
  nome="$(campo 1)"; status="$(campo 2)"; estado="$(campo 3)"; quantas="$(campo 4)"
  [ "$status" = 'WORKING' ] && [ "$estado" = 'CONNECTED' ] && break
  [ "$SECONDS" -ge "$fim" ] && break
  sleep 5
done

if [ -n "$nome" ]; then
  log "Sessao encontrada pelo estado: '${nome}' (${status:-?}/${estado:-?})"
  [ "${quantas:-1}" -gt 1 ] 2>/dev/null && \
    log "AVISO: ${quantas} sessoes nesta instancia — o certo e uma (pendencia P-02)."
fi

if [ "$status" = 'WORKING' ] && [ "$estado" = 'CONNECTED' ]; then
  log 'Sessao WORKING / CONNECTED — seguindo.'
elif [ -z "$nome" ]; then
  # Instancia vazia e instancia muda sao coisas diferentes: sem sessao nenhuma
  # nao ha o que religar, so o que criar (opcao 7 do .bat / conectar_whatsapp.sh).
  log '======================================================================'
  log ' NAO HA SESSAO NENHUMA NESTA INSTANCIA (ou o WAHA nao respondeu).'
  log " Confira ${WAHA_URL}/dashboard, ou rode: ./scripts/conectar_whatsapp.sh"
  log '======================================================================'
elif [ "$status" = 'FAILED' ] || [ "$status" = 'STOPPED' ]; then
  # NAO RELIGAR AQUI. Achado de 09/08 (madrugada), lendo o log do WAHA:
  # quando o WhatsApp derruba o vinculo, o restart faz o whatsapp-web.js
  # chamar `logout()` -> `deleteRemoteSession()`, ou seja, **o WAHA tenta
  # APAGAR a copia gravada no Postgres**. Nesta madrugada so nao apagou por
  # sorte (a exclusao morreu num timeout do banco). Regra 15-A: nunca apagar
  # sessao que tem copia — na duvida, tambem nao.
  #
  # Antes, uma rodada com a sessao caida pedia DOIS restarts: um aqui e outro
  # dentro da pipeline. Dois pedidos de exclusao da mesma copia, por rodada,
  # duas vezes por dia. Quem religa agora e so a pipeline (regra 16-E: um
  # conserto por problema, nunca em laco) — la o desfecho e registrado e o
  # alerta sai.
  log "Sessao '${nome}' em ${status} — quem tenta religar (uma vez so) e a pipeline."
else
  log "AVISO: sessao '${nome}' status='${status:-sem resposta}' engine='${estado:--}'. A pipeline tentara e alertara se falhar."
fi

# --- 3.4 Contar ao site o que esta maquina tem ---
# Endereco, porta, chave e nome de sessao de cada instancia. Roda ANTES do
# provisionamento de proposito: e com esta informacao que o site deixa de
# inventar nome e porta ao atender um "Conectar" (P-64).
#
# Em 14/08/2026 o site estava com a chave da instancia ERRADA: o WAHA devolvia
# HTTP 401, o site concluia "automacao quebrada" e mandava e-mail de 15 em 15
# minutos sobre uma sessao WORKING+CONNECTED que publicava normalmente.
# Nunca derruba a rodada.
# --- 3.4 Faxina da cidade pedida pelo site (18/09/2026, plano A10 + item 5) ---
# Roda ANTES do relato das instancias, para o site ja receber o estado
# depois da faxina. Sem pedido, e uma pergunta HTTP e nada mais.
# Nunca derruba a rodada.
if [ -f "$BASE_DIR/scripts/faxina_cidade.py" ]; then
  "$PY" "$BASE_DIR/scripts/faxina_cidade.py" --do-site 2>&1 | sed 's/^/         /' || true
fi

# --- 3.45 Versoes (19/09/2026, plano item 67): relata 1x por dia e executa a
# troca do WAHA que o master supremo pediu. Nunca derruba a rodada.
if [ -f "$BASE_DIR/scripts/versoes.py" ]; then
  "$PY" "$BASE_DIR/scripts/versoes.py" --do-site 2>&1 | sed 's/^/         /' || true
fi

if [ -f "$BASE_DIR/scripts/registrar_instancias.py" ]; then
  log 'Contando ao site quais instancias existem aqui...'
  "$PY" "$BASE_DIR/scripts/registrar_instancias.py" 2>&1 | sed 's/^/         /' || true
fi

# --- 3.5 Provisionamento pedido pelo site ---
# Um admin apertou "Conectar" na tela do Movimente-se: aqui a instancia dele e
# criada (porta, container, bancos, sessao) e o codigo de pareamento e obtido.
# Roda ANTES da pipeline de proposito — se alguem esta esperando um codigo na
# tela, isso e mais urgente que o ranking, e leva menos tempo.
#
# Nunca derruba a rodada: falhar em provisionar nao pode impedir o ranking de
# quem ja esta conectado.
if [ -f "$BASE_DIR/scripts/provisionar.py" ]; then
  log 'Verificando pedidos de conexao de WhatsApp feitos pelo site...'
  "$PY" "$BASE_DIR/scripts/provisionar.py" 2>&1 | sed 's/^/         /'
fi

# --- 4. Pipeline ---
log "Executando: send_ranking.py $*"
"$PY" "$BASE_DIR/scripts/send_ranking.py" "$@"
codigo=$?

case "$codigo" in
  0) log 'Concluido com sucesso.' ;;
  3) log 'Ja havia uma execucao em andamento — nada foi feito.' ;;
  *) log "Terminou com erro (codigo $codigo). Veja o log mais recente em logs/." ;;
esac
exit "$codigo"
