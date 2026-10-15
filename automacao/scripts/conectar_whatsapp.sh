#!/usr/bin/env bash
# ~/movimente-automacao/scripts/conectar_whatsapp.sh
#
# Conecta (ou reconecta) o WhatsApp: prepara a sessao, busca um QR Code novo e
# abre a imagem no Windows para voce apontar o celular.
#
# Uso:
#   ./scripts/conectar_whatsapp.sh            # so gera o QR se precisar
#   ./scripts/conectar_whatsapp.sh --recriar  # apaga e refaz a sessao do zero
#
# O QR do WhatsApp vale ~60 segundos e depois se renova algumas vezes. Se
# perder a janela, rode o comando de novo — nao tem limite.

set -uo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR" || exit 1

PY="$BASE_DIR/.venv/bin/python"
API_KEY="$(grep -E '^WAHA_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"'')"
WAHA_URL="$(grep -E '^WAHA_BASE=' .env | cut -d= -f2- | tr -d '"'"'"'')"
WAHA_URL="${WAHA_URL:-http://localhost:3000}"

# O NOME SO SERVE PARA CRIAR. Para encontrar o que ja existe, quem responde e o
# scripts/wahasessao.py, olhando o ESTADO (docs/DICIONARIO.md secao 1).
#
# A falha de 02/08/2026 explica o sintoma mais frustrante do projeto:
# "conectei o WhatsApp com sucesso e mesmo assim o ranking nao sai". Este script
# conectava a sessao "default" (cravada) enquanto a pipeline esperava a do .env
# (principal-pls). Duas sessoes diferentes na MESMA instancia: voce pareava uma
# e a automacao esperava a outra, que seguia em FAILED ate estourar os 150s.
# Nao era a Meta derrubando, nem a rede.
SESSAO_NOVA="$(grep -E '^WAHA_SESSION=' .env | cut -d= -f2- | tr -d '"'"'"'')"
SESSAO_NOVA="${SESSAO_NOVA:-default}"

QR_LINUX="$BASE_DIR/qr_whatsapp.png"
QR_WIN="/mnt/c/Users/USUARIO/Documents/SCRIPTS/qr_whatsapp.png"

log() { printf '%s | %s\n' "$(date '+%H:%M:%S')" "$*"; }
# nome|status|estado|quantas — a fonte unica da verdade sobre a sessao.
linha=''
ler_sessao() { linha="$("$PY" "$BASE_DIR/scripts/wahasessao.py" 2>/dev/null)"; }
campo() { printf '%s' "$linha" | cut -d'|' -f"$1"; }

# --- 1. Stack de pe ---
docker info >/dev/null 2>&1 || { log 'Docker parado. Rode: sudo systemctl start docker'; exit 1; }
docker compose up -d >/dev/null 2>&1
log 'Aguardando a API do WAHA responder...'
for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' -m 8 -H "X-Api-Key: ${API_KEY}" "${WAHA_URL}/api/sessions" 2>/dev/null)" = "200" ] && break
  sleep 5
done

ler_sessao
WAHA_SESSAO="$(campo 1)"; st="$(campo 2)"; estado="$(campo 3)"
if [ -z "$WAHA_SESSAO" ]; then
  # Instancia vazia: agora sim o nome do .env manda, porque vamos CRIAR.
  WAHA_SESSAO="$SESSAO_NOVA"
  log "Nenhuma sessao nesta instancia — vou criar '${WAHA_SESSAO}'."
else
  log "Sessao '${WAHA_SESSAO}' em ${WAHA_URL} — situacao: ${st:-?}/${estado:-?}"
fi

if [ "$st" = 'WORKING' ] && [ "$estado" = 'CONNECTED' ]; then
  log 'A sessao ja esta conectada. Nada a fazer.'
  exit 0
fi

# --- 2. Preparar a sessao ---
# FAXINA ANTES DE PAREAR (pendencia P-02, DICIONARIO secao 7 passo 6).
# Sobra de sessao nasce quando o nome muda: a instancia fica com a antiga
# (morta) e ganha a nova. So sai o que NAO tem copia gravada — apagar sessao com
# copia troca um restart por um QR Code na mao de alguem.
"$PY" "$BASE_DIR/scripts/wahasessao.py" --limpar --manter "$WAHA_SESSAO" 2>/dev/null |
  while IFS='|' read -r f_nome f_acao f_motivo; do
    [ -n "$f_nome" ] && [ "$f_nome" != '-' ] && log "faxina: ${f_nome} — ${f_acao} (${f_motivo})"
  done

# Quantos arquivos de copia esta sessao tem. Vazio = nao deu para perguntar, e
# nesse caso o certo e NAO apagar.
copia="$("$PY" "$BASE_DIR/scripts/wahasessao.py" --copia "$WAHA_SESSAO" 2>/dev/null | cut -d'|' -f1)"

refazer='nao'
if [ "${1:-}" = '--recriar' ]; then
  refazer='sim'
  if [ "${copia:-0}" -gt 0 ] 2>/dev/null; then
    log "AVISO: --recriar apaga uma sessao que TEM copia (${copia} arquivo(s))."
    log '       Ela vai precisar de QR Code novo. Era isso mesmo? (Ctrl+C em 5s para desistir)'
    sleep 5
  fi
elif [ -z "$st" ]; then
  refazer='sim'
elif [ "$st" = 'FAILED' ]; then
  # FAILED NAO VOLTA — E O RESTART SO ARRISCA (16/08/2026, regra do dono).
  #
  # Aqui dizia o contrario: "se tem copia, religa em vez de apagar". Medido
  # hoje as 22:55, com a copia gravada no MESMO dia (18:20), o restart faz isto:
  #
  #     Session has been logged out!  (LOGOUT)
  #     RemoteAuth.logout -> disconnect -> deleteRemoteSession -> delete
  #     KnexTimeoutError: Timeout acquiring a connection
  #
  # Ou seja: com a sessao em FAILED o WhatsApp JA desvinculou o aparelho do lado
  # dele. O restart nao reconecta — ele PEDE PARA APAGAR a copia (regra 15-C) e
  # volta para FAILED em 40 segundos. A copia existe e nao serve.
  #
  # Este script so roda quando UMA PESSOA pediu o pareamento, com o celular na
  # mao — que e exatamente a condicao em que apagar sai barato.
  if [ "${copia:-0}" -gt 0 ] 2>/dev/null; then
    log "Sessao em FAILED com ${copia} arquivo(s) de copia — a copia nao traz sessao caida de volta. Refazendo do zero."
  else
    log 'Sessao em FAILED e sem copia gravada — nao ha o que recuperar.'
  fi
  refazer='sim'
fi

if [ "$refazer" = 'sim' ]; then
  log "Refazendo a sessao '${WAHA_SESSAO}' do zero..."
  curl -s -X DELETE -H "X-Api-Key: ${API_KEY}" "${WAHA_URL}/api/sessions/${WAHA_SESSAO}" >/dev/null 2>&1
  sleep 5
  curl -s -X POST -H "X-Api-Key: ${API_KEY}" -H 'Content-Type: application/json' \
       -d "{\"name\":\"${WAHA_SESSAO}\",\"start\":true}" "${WAHA_URL}/api/sessions" >/dev/null 2>&1
else
  log "Reiniciando a sessao '${WAHA_SESSAO}'..."
  curl -s -X POST -H "X-Api-Key: ${API_KEY}" "${WAHA_URL}/api/sessions/${WAHA_SESSAO}/restart" >/dev/null 2>&1
fi

# --- 3. Esperar o QR aparecer ---
log 'Esperando o QR Code ficar disponivel...'
for _ in $(seq 1 36); do
  ler_sessao
  st="$(campo 2)"; estado="$(campo 3)"
  [ "$st" = 'SCAN_QR_CODE' ] && break
  [ "$st" = 'WORKING' ] && { log 'Conectou sozinha, sem QR.'; exit 0; }
  sleep 5
done

if [ "$st" != 'SCAN_QR_CODE' ]; then
  log "Nao chegou no QR (status: ${st:-sem resposta}). Veja: docker logs ${WAHA_CONTAINER:-auto-waha-principal-pls} --tail 40"
  exit 1
fi

# --- 4. Baixar e abrir ---
curl -s -H "X-Api-Key: ${API_KEY}" "${WAHA_URL}/api/${WAHA_SESSAO}/auth/qr" -o "$QR_LINUX"
if [ ! -s "$QR_LINUX" ]; then
  log 'Falhei ao baixar o QR.'
  exit 1
fi
cp -f "$QR_LINUX" "$QR_WIN" 2>/dev/null && log "QR salvo em C:\\Users\\USUARIO\\Documents\\SCRIPTS\\qr_whatsapp.png"
command -v explorer.exe >/dev/null 2>&1 && explorer.exe 'C:\Users\USUARIO\Documents\SCRIPTS\qr_whatsapp.png' 2>/dev/null

cat <<'FIM'

  ------------------------------------------------------------------
   APONTE O CELULAR AGORA (o QR vale cerca de 1 minuto)

   WhatsApp > Configuracoes > Aparelhos conectados
            > Conectar um aparelho > aponte para a imagem

   Se a imagem nao abriu sozinha, abra:
   C:\Users\USUARIO\Documents\SCRIPTS\qr_whatsapp.png

   Perdeu a janela? Rode este comando de novo.
  ------------------------------------------------------------------

FIM

# --- 5. Confirmar a conexao ---
log 'Aguardando voce escanear (ate 3 minutos)...'
for _ in $(seq 1 36); do
  ler_sessao
  st="$(campo 2)"; estado="$(campo 3)"
  if [ "$st" = 'WORKING' ]; then
    # WORKING ja e sucesso para quem acabou de escanear: o CONNECTED vem logo
    # atras, e quem espera por ele e a pipeline.
    log 'CONECTADO!'
    log "   sessao '$(campo 1)' — ${st}/${estado:-aquecendo}"
    rm -f "$QR_LINUX" "$QR_WIN"
    exit 0
  fi
  [ "$st" = 'FAILED' ] && { log 'A sessao falhou. Rode de novo com --recriar.'; exit 1; }
  sleep 5
done

log 'Tempo esgotado. Rode o comando de novo para gerar outro QR.'
exit 1
