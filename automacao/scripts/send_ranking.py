#!/usr/bin/env python3
"""
~/movimente-automacao/scripts/send_ranking.py
Pipeline de Automacao Movimente-se — V6.1

Fluxo: WAHA (coleta por periodo) -> TXT formato WhatsApp -> POST /api/upload
       -> polling do job -> GET /api/ranking/latest -> envio no grupo.

Contrato real da API (conferido no codigo em app/blueprints/api.py):
  - POST /api/upload  ->  multipart, campo do arquivo = "arquivo"  (NAO "file")
                          campo "periodo" = "AAAA-MM"             (NAO "todos_periodos")
                          a cidade vem do proprio token; nao se envia "cidade".
  - GET  /api/upload/<job_id>   -> {stage, message, pct}; stage final: done|error|cancelled
  - GET  /api/ranking/latest    -> {texto_whatsapp, ranking, patrocinadores[]}
  - GET  /api/strava-sessao     -> {configurado, saude:{ok, message}}
  - GET  /api/parabens          -> metas batidas pendentes de aviso
Autenticacao em todas: header X-Automation-Token.
"""
import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# O UNICO lugar que decide QUAL sessao usar (docs/DICIONARIO.md secao 1).
# Mora ao lado deste arquivo; sem o sys.path o import quebra quando o script e
# chamado por caminho absoluto pelo agendador do Windows.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import prontidao
import wahasessao
# A COPIA E NOSSA QUANDO PRECISA SER (12/09/2026): `copia_sessao.forcar` gera o
# zip do perfil vivo e grava no Postgres, sem depender do job do WAHA.
import copia_sessao

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('America/Sao_Paulo')
except Exception:                                    # pragma: no cover
    TZ = None

# ===== CAMINHOS =====
BASE_DIR   = Path(__file__).resolve().parent.parent
ENV_PATH   = BASE_DIR / '.env'
LOG_DIR    = BASE_DIR / 'logs'
STATE_FILE = BASE_DIR / 'scripts' / '.send_ranking_state.json'
LOCK_FILE  = BASE_DIR / 'scripts' / '.send_ranking.lock'
LOG_DIR.mkdir(exist_ok=True)


# ===== .env =====
def _load_env():
    if not ENV_PATH.exists():
        return
    with open(ENV_PATH, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            if line.startswith('export '):
                line = line[7:]
            key, _, val = line.partition('=')
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()


def _flag(name, default=False):
    raw = (os.getenv(name) or '').strip().lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'sim', 'on')


# ===== CONFIG =====
WAHA_BASE     = os.getenv('WAHA_BASE', 'http://localhost:3000').rstrip('/')
WAHA_API_KEY  = os.getenv('WAHA_API_KEY', '')
# O nome do .env NAO e mais como a sessao e encontrada — vale so para criar
# uma nova e para desempatar. Assim que a instancia responde, WAHA_SESSION passa
# a ser o nome da sessao que esta REALMENTE de pe (ver _fixar_sessao).
WAHA_SESSION_ENV = os.getenv('WAHA_SESSION', 'default')
WAHA_SESSION  = WAHA_SESSION_ENV
GROUP_ID      = os.getenv('WHATSAPP_GROUP_ID', '')
# Para onde vao os alertas de falha. Sem isto, o erro NAO vai para o grupo
# (a comunidade nao precisa ver stack trace) — fica so no log.
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '').strip()

# ===== FASE DE TESTES =====
# Enquanto DESTINO_ENSAIO tiver numero, NADA vai para o grupo: tudo cai no
# WhatsApp do operador. Para devolver a automacao ao grupo basta apagar (ou
# comentar) DESTINO_ENSAIO no .env — nenhuma outra linha muda.
#
# O par se cobre: se o numero de ensaio nao conseguir entregar (sessao dele
# caida, que e o normal nesta fase), a mensagem sai pelo parceiro. Os dois se
# revezam, e nenhuma mensagem se perde por causa de uma sessao instavel.
DESTINO_ENSAIO   = os.getenv('DESTINO_ENSAIO', '').strip()
PARCEIRO_ENSAIO  = os.getenv('PARCEIRO_ENSAIO', '').strip()

# Gancho do n8n. Vazio = ninguem e avisado (so o log). Preenchido = todo erro
# do caminho vira um POST com {evento, detalhe, cidade, origem, quando}.
N8N_WEBHOOK_URL  = os.getenv('N8N_WEBHOOK_URL', '').strip()

# Quao velha pode ser a mensagem mais nova que a coleta trouxe, antes de a
# gente desconfiar que o WhatsApp Web ainda nao terminou de sincronizar.
SYNC_TOLERANCIA_MIN = int(os.getenv('SYNC_TOLERANCIA_MIN', '90'))
SYNC_TENTATIVAS     = int(os.getenv('SYNC_TENTATIVAS', '12'))
SYNC_ESPERA_SEG     = int(os.getenv('SYNC_ESPERA_SEG', '20'))
# Quantas leituras IGUAIS seguidas provam que a sincronizacao acabou. Duas era
# pouco: logo apos o container subir, o WhatsApp Web ainda nem comecou a
# sincronizar e duas leituras seguidas dao o mesmo conjunto incompleto.
SYNC_ESTAVEL_N      = int(os.getenv('SYNC_ESTAVEL_N', '3'))
# Tempo minimo observando antes de aceitar qualquer estabilidade. Protege do
# caso em que a sincronizacao comeca tarde: sem isso, o silencio inicial seria
# lido como "terminou".
SYNC_AQUECIMENTO_SEG = int(os.getenv('SYNC_AQUECIMENTO_SEG', '150'))
# Tempo minimo desde que o CONTAINER do WAHA subiu. E o relogio que importa: a
# sincronizacao do WhatsApp Web comeca quando o container sobe, nao quando a
# coleta comeca. Sem isto, a rodada das 22h poderia subir a stack e coletar 30s
# depois, achando que 3 leituras iguais provam alguma coisa.
SYNC_CONTAINER_MIN_SEG = int(os.getenv('SYNC_CONTAINER_MIN_SEG', '300'))
# A partir de quanto tempo de pe o container conta como MADURO: sincronizacao
# encerrada faz tempo, nao ha o que esperar. Bem acima do minimo acima de
# proposito — o minimo e "ja teve chance de sincronizar", este e "ja terminou".
# A folga entre os dois protege exatamente o caso que o dono levantou: conexao e
# reconexao do WAHA levam tempo, e quem acabou de subir NAO entra por aqui.
SYNC_MADURO_SEG = int(os.getenv('SYNC_MADURO_SEG', '1800'))
# Janela de confirmacao do container maduro. Medido em 06/08 01:40: o WAHA
# estava de pe ha 189 min e a leitura veio IGUAL desde a tentativa 1 — as 9
# leituras seguintes (164s) nao mudaram uma virgula. Duas leituras iguais
# separadas por uma espera provam o mesmo em ~20s.
SYNC_ESTAVEL_MADURO_N = int(os.getenv('SYNC_ESTAVEL_MADURO_N', '2'))
SYNC_AQUECIMENTO_MADURO_SEG = int(os.getenv('SYNC_AQUECIMENTO_MADURO_SEG', '15'))
WAHA_CONTAINER = os.getenv('WAHA_CONTAINER', 'auto-waha-principal-pls')
# Quanto tempo esperar por TUDO PRONTO (prontidao.esperar) antes de desistir.
# Nao e espera fixa: a rodada segue no instante em que a ultima etapa responde
# OK. Este numero e so o prazo maximo.
PRONTIDAO_LIMITE_SEG = int(os.getenv('PRONTIDAO_LIMITE_SEG', '180'))
PRONTIDAO_INTERVALO_SEG = int(os.getenv('PRONTIDAO_INTERVALO_SEG', '10'))
# O laudo tem prazo proprio, e curto de proposito: a maquina esta esperando para
# desligar e o usuario, para dormir. Aqui nao se espera sincronizar — so se
# confere se a peca esta de pe.
LAUDO_LIMITE_SEG = int(os.getenv('LAUDO_LIMITE_SEG', '90'))
# FOLGA ANTES DE PUBLICAR. Pedida pelo dono em 06/08: mesmo com tudo conferido,
# uma pausa curta entre "a leitura esta boa" e "vai para o grupo". Ela cobre a
# ultima janela em que a leitura ainda podia mudar, e custa pouco perto de
# publicar ranking sem um link que estava chegando.
FOLGA_ANTES_ENVIO_SEG = int(os.getenv('FOLGA_ANTES_ENVIO_SEG', '20'))
# Horarios em que a maquina acorda para rodar. Servem so para o aviso dizer
# QUANDO o ranking nao vai sair — "na proxima rodada" nao ajuda ninguem a
# decidir se da para resolver antes.
RODADAS_DO_DIA = [h.strip() for h in
                  os.getenv('RODADAS_DO_DIA', '05:30,15:30').split(',') if h.strip()]


def perguntar_ao_site_o_destino():
    """QUEM MANDA NO MODO TESTE E O SITE, nao este .env (P-27).

    O admin liga e desliga por um botao no painel; a pipeline pergunta antes de
    publicar. O `.env` continua valendo como ponto de partida — e como rede de
    seguranca quando o site nao responde.

    FALHA FECHADA, DE PROPOSITO: sem resposta do site, MANTEM o ensaio. O pior
    desfecho de uma falha de comunicacao tem de ser "nao publicou no grupo",
    nunca "publicou sem querer" — nao existe despublicar (16-C) e o grupo tem
    80 pessoas (16-J).

    Nunca levanta excecao. So mexe nos globais quando a resposta e clara.
    """
    global DESTINO_ENSAIO
    try:
        resp = _session().get(f'{API_URL}/api/config/envio',
                              headers=_api_headers(), timeout=20)
        if resp.status_code != 200:
            log.warning('   O site nao respondeu sobre o modo teste (HTTP %s) '
                        '— mantendo o que o .env diz (%s).',
                        resp.status_code,
                        'ENSAIO' if DESTINO_ENSAIO else 'grupo')
            return
        dados = resp.json() or {}
    except Exception as exc:
        log.warning('   Nao consegui perguntar ao site sobre o modo teste (%s) '
                    '— mantendo o que o .env diz (%s).',
                    exc, 'ENSAIO' if DESTINO_ENSAIO else 'grupo')
        return

    ligado = dados.get('modo_teste')
    destino = str(dados.get('destino_teste') or '').strip()

    # `null` = o site NUNCA foi configurado. Isso nao e "desligado": e "nao
    # sei", e `nao sei` mantem o .env. Sair do ensaio precisa de um clique
    # deliberado no painel, nunca da ausencia de um registro no cofre.
    if ligado is None:
        log.info('   O site ainda nao tem modo teste configurado — seguindo '
                 'pelo .env (%s).', 'ENSAIO' if DESTINO_ENSAIO else 'grupo')
        return

    if ligado and destino:
        # SEMPRE registrado (19/09/2026, item 69): antes so aparecia quando o
        # destino do site diferia do .env — com o mesmo numero a linha sumia, e
        # a rodada em modo teste parecia "o site nao respondeu".
        log.warning('   O site diz: MODO TESTE LIGADO — as mensagens vao para %s '
                    'nesta rodada%s.', destino,
                    '' if destino == DESTINO_ENSAIO else
                    ' (o .env dizia %s)' % (DESTINO_ENSAIO or 'grupo'))
        DESTINO_ENSAIO = destino
    elif ligado and not destino:
        # Ligado sem numero e configuracao pela metade: nao da para desviar
        # para lugar nenhum, e publicar no grupo seria o contrario do pedido.
        log.error('   O site diz MODO TESTE ligado, mas SEM numero de destino. '
                  'Mantendo o ensaio pelo .env e nao publicando no grupo.')
        DESTINO_ENSAIO = DESTINO_ENSAIO or ADMIN_CHAT_ID or ''
    else:
        log.warning('   O site diz: modo teste DESLIGADO — as mensagens vao '
                    'para o GRUPO nesta rodada.')
        DESTINO_ENSAIO = ''


def em_ensaio():
    """A automacao esta desviada do grupo?"""
    return bool(DESTINO_ENSAIO)


def destino_publicacao():
    """Para onde vai o que seria do grupo.

    Em ensaio: o numero do operador. Fora de ensaio: o grupo, e a ordem de
    prioridade configurada no site volta a valer.
    """
    if DESTINO_ENSAIO:
        return _jid_contato(DESTINO_ENSAIO) or DESTINO_ENSAIO
    return GROUP_ID


def _resolver_admin_chat():
    """Corrige o ADMIN_CHAT_ID do .env para o id que o WhatsApp usa hoje.

    O .env guarda '55DD9XXXXXXXX@c.us', que era o formato de sempre. O
    WhatsApp passou a identificar contatos por LID e o '@c.us' desses
    contatos devolve HTTP 500 — ou seja, alerta de falha e codigo de
    pareamento simplesmente nao chegavam, sem ninguem saber.

    Em vez de exigir que alguem edite o .env com um numero que ninguem sabe
    de cor, a gente pergunta ao WAHA na hora. Se a consulta nao der, fica o
    valor do .env: o comportamento nao piora.
    """
    global ADMIN_CHAT_ID
    if not ADMIN_CHAT_ID or ADMIN_CHAT_ID.endswith('@g.us'):
        return
    resolvido = _jid_contato(ADMIN_CHAT_ID.split('@')[0])
    if resolvido and resolvido != ADMIN_CHAT_ID:
        log.debug('ADMIN_CHAT_ID %s -> %s', ADMIN_CHAT_ID, resolvido)
        ADMIN_CHAT_ID = resolvido
API_URL       = os.getenv('MOVIMENTE_API_URL', 'https://movementes.engekon.com').rstrip('/')
API_TOKEN     = os.getenv('MOVIMENTE_API_TOKEN', '')

TIMEOUT        = 45
MAX_RETRIES    = 3
LIMITE_PAG     = 100
MAX_PAGINAS    = 50

# Ultima leitura do grupo falhou de um jeito que NAO adianta repetir?
# Preenchido por `coletar_mensagens`, lido por `coletar_com_sincronizacao`.
# Existe por causa da noite de 06/08: a coleta levou HTTP 422 e o laco repetiu
# o mesmo 422 ONZE vezes, gastando 4 minutos para chegar ao mesmo lugar. 422 e
# resposta de validacao — o WAHA ja disse que o pedido esta errado, e pedido
# errado nao vira certo por insistencia (regra 16-E).
_COLETA_ERRO_DEFINITIVO = None

# Idade (em minutos) da mensagem mais nova quando a coleta foi aceita SO pela
# estabilidade, com a idade acima da tolerancia. None = leitura em dia.
#
# POR QUE ISTO EXISTE (11/08/2026, regra 16-G)
# Leitura estavel NAO e leitura atual. Sessao congelada devolve leituras
# identicas para sempre, entao "N leituras iguais" nao distingue "o grupo esta
# parado" de "esta sessao nao recebe nada ha horas". Em 11/08 as 10:30 a coleta
# trouxe a mensagem mais nova de 706 minutos antes — o grupo tinha tido links a
# manha inteira —, o log declarou "O grupo esta parado" e a rodada terminou
# dizendo PIPELINE CONCLUIDA. O dono processou os links na mao as 10:58.
#
# NAO ABORTA A RODADA de proposito: leitura congelada nao traz km novos, entao
# a regra 6 (nao republica sem novidade) ja segura o ranking sozinha. O que
# faltava era CONTAR para alguem. Abortar castigaria todo dia de grupo parado
# de verdade, que e um estado normal do mundo.
_COLETA_SUSPEITA_MIN = None

# Timestamp da mensagem mais nova que o WAHA ENTREGOU nesta coleta, incluindo
# as que o filtro descarta (midia sem texto, por exemplo). Ver o comentario em
# `etapa_coletar` — comparar a lista de conversas com a mais nova APROVEITAVEL
# acusa falta de sincronizacao a cada foto postada no grupo.
_TS_BRUTO_MAX = 0

# DE QUE INSTANTE ESTA COLETA COMECOU A LER (epoch).
#
# 27/08/2026 — POR QUE ISTO EXISTE. A segunda opiniao do suplente, gravada as
# 01:04 de hoje, chamava `comparar_com_suplente(since, ...)` de dentro de
# `coletar_com_sincronizacao`. Mas `since` e' variavel LOCAL de `etapa_coletar`:
# a rodada das 05:30 morreu com `NameError: name 'since' is not defined` e nao
# publicou nada — nem o ranking, nem a logo. Pior: o trecho so e' alcancado
# quando a leitura esta PARADA, ou seja, exatamente na rodada em que a segunda
# opiniao faria falta; com leitura fresca a funcao retorna antes e ninguem ve.
# `py_compile`/`ast.parse` NAO pegam isso — so rodar pega. (`ERROS.md` §4.31.)
_COLETA_DESDE = 0

# Esta rodada e ensaio (--dry-run / --teste)? Ligado no arranque, lido por
# `notificar_admin` para marcar o alerta com "** IGNORE ESSE TESTE **".
# Ver a regra 16-J-A: teste vai marcado, e nunca para o grupo.
_EM_ENSAIO = False


def _erro_sem_conserto(codigo):
    """Este HTTP melhora se eu repetir? (P-09)

    NAO repetir: 4xx em geral — o servidor entendeu e recusou. Repetir so
    empurra o alerta para mais tarde, que e o contrario do que se quer.
    REPETIR: 5xx e 429. 500 e o sintoma classico do WEBJS ainda aquecendo
    (`WORKING` mas nao pronto), que passa sozinho; 429 e ritmo, tambem passa.
    """
    try:
        codigo = int(codigo)
    except (TypeError, ValueError):
        return False
    if codigo == 429:
        return False
    return 400 <= codigo < 500
SOBREPOSICAO   = 7200        # 2h de overlap sobre o checkpoint
POLL_MAX_SEC   = int(os.getenv('POLL_MAX_SEC', '1800'))
POLL_INTERVAL  = 5
JANELA_PADRAO_HORAS = int(os.getenv('JANELA_PADRAO_HORAS', '48'))
FORCE_DIAS     = int(os.getenv('FORCE_COLLECT_DIAS', '45'))
WAHA_ESPERA    = int(os.getenv('WAHA_ESPERA_SEGUNDOS', '150'))
MIN_INTERVALO_HORAS = float(os.getenv('MIN_INTERVALO_RANKING_HORAS', '2'))
SO_SE_MUDOU    = _flag('SO_ENVIAR_SE_MUDOU', True)
# Estado guardado no DS215j para que WSL/Windows e Xpenology nao publiquem
# duas vezes a mesma coisa. Desligue so para depurar isolado.
ESTADO_COMPARTILHADO = _flag('ESTADO_COMPARTILHADO', True)
ORIGEM         = os.getenv('AMBIENTE_NOME', '') or f'wsl:{os.uname().nodename}'
CHUNK          = 3500

# ===== LOGGING =====
_stamp    = datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
_log_file = LOG_DIR / f'send_ranking_{_stamp}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%d/%m/%Y %H:%M:%S',
    handlers=[logging.FileHandler(_log_file, encoding='utf-8'),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('send_ranking')
# LOG CANONICO (17/09/2026, item D1): cada linha deste script cai tambem
# no livro semanal comum a Windows, WSL e site. Ver scripts/logcanon.py.
try:
    import logcanon
    logcanon.ligar('PIPELINE', 'send_ranking')
except Exception:
    pass


# ===== HTTP =====
def _session():
    sess = requests.Session()
    retry = Retry(total=MAX_RETRIES, backoff_factor=2,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=['GET', 'POST'])
    sess.mount('http://', HTTPAdapter(max_retries=retry))
    sess.mount('https://', HTTPAdapter(max_retries=retry))
    return sess


def _api_headers():
    return {'X-Automation-Token': API_TOKEN}


def _waha_headers(json_body=False):
    headers = {'X-Api-Key': WAHA_API_KEY}
    if json_body:
        headers['Content-Type'] = 'application/json'
    return headers


# ===== TRAVA DE EXECUCAO =====
class Trava:
    """Impede duas execucoes ao mesmo tempo (agendamento + clique no .bat)."""

    def __init__(self):
        self.handle = None

    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(LOCK_FILE, 'w')
        try:
            import fcntl
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError('Ja existe uma execucao em andamento.')
        self.handle.write(f'{os.getpid()} {datetime.now(TZ).isoformat()}\n')
        self.handle.flush()
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.handle.close()
        return False


# ===== ESTADO =====
_ESTADO_VAZIO = {
    'last_ts': 0,           # timestamp da ultima mensagem ja processada
    'last_id': '',
    'ranking_hash': '',     # impressao dos km da ultima publicacao
    'ranking_enviado_em': 0,  # epoch do ultimo ranking publicado
    # OS ULTIMOS MARCOS, do mais novo para o mais antigo (P-76, 15/08/2026).
    #
    # POR QUE ISTO PRECISOU EXISTIR
    # O marco avanca para a mensagem mais nova que a leitura ENTREGOU. Se o
    # WAHA estava fora do ar quando alguem postou, aquela mensagem nao chega —
    # e a mensagem SEGUINTE, que chega, empurra o marco por cima do buraco.
    # Dali em diante a janela incremental (marco - 2h) nunca mais alcanca o que
    # ficou atras: o link some para sempre, sem erro nenhum no caminho.
    #
    # Medido em 15/08: a maquina passou a noite desligada, links foram postados
    # as 06:47, a rodada das 10:30 leu so ate 14/08 22:53 e a das 15:30 salvou
    # o marco em 15/08 14:05 — pulando a manha inteira.
    'marcos': [],
    # Quando a ultima varredura ampla rodou (epoch), para nao repetir na mesma
    # janela e para o log dizer desde quando estamos cobertos.
    'varredura_em': 0,
    # Freio do religar: religar pede ao WAHA para APAGAR a copia da sessao
    # (regra 15-A). Falhou uma vez, nao se tenta de novo a cada rodada.
    'restart_falhou_em': 0,
}


def _estado_local():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding='utf-8') as handle:
                dados = json.load(handle)
            estado = dict(_ESTADO_VAZIO)
            estado.update(dados if isinstance(dados, dict) else {})
            return estado
        except (json.JSONDecodeError, OSError) as exc:
            log.warning('Estado local corrompido, resetando: %s', exc)
    return dict(_ESTADO_VAZIO)


def _estado_remoto():
    """Estado guardado no DS215j — a memoria que Windows e Xpenology dividem."""
    if not ESTADO_COMPARTILHADO:
        return None
    try:
        resp = _session().get(f'{API_URL}/api/automacao/estado',
                              headers=_api_headers(), timeout=20)
        if resp.status_code == 200:
            return resp.json()
        log.warning('   Estado compartilhado indisponivel (HTTP %s) — usando o local',
                    resp.status_code)
    except Exception as exc:
        log.warning('   Estado compartilhado inacessivel (%s) — usando o local',
                    type(exc).__name__)
    return None


def load_state():
    """Funde o estado local com o compartilhado.

    A fusao sempre pega o valor MAIS ADIANTADO de cada campo. Errar para o
    lado de "ja processei / ja enviei" e seguro: no pior caso a publicacao
    espera o proximo horario. O contrario mandaria ranking repetido no grupo.
    """
    local = _estado_local()
    remoto = _estado_remoto()
    if not remoto:
        return local

    fundido = dict(local)
    fundido['last_ts'] = max(local.get('last_ts') or 0, remoto.get('last_ts') or 0)
    if (remoto.get('last_ts') or 0) >= (local.get('last_ts') or 0):
        fundido['last_id'] = remoto.get('last_id') or local.get('last_id', '')

    env_local  = local.get('ranking_enviado_em') or 0
    env_remoto = remoto.get('ranking_enviado_em') or 0
    fundido['ranking_enviado_em'] = max(env_local, env_remoto)
    if env_remoto >= env_local:
        fundido['ranking_hash'] = remoto.get('ranking_hash') or local.get('ranking_hash', '')

    if env_remoto > env_local:
        log.info('   Estado compartilhado mais recente (publicado por "%s" em %s)',
                 remoto.get('atualizado_por') or '?',
                 _ts_str(env_remoto) if env_remoto else '-')
    return fundido


def save_state(**campos):
    """Grava os campos informados no arquivo local e no DS215j."""
    estado = _estado_local()
    estado.update(campos)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as handle:
        json.dump(estado, handle)

    # SO VIAJA O QUE O OUTRO AMBIENTE PRECISA SABER (16/08/2026).
    #
    # `save_state` grava tudo no arquivo local, inclusive campos que so
    # interessam a esta maquina (`restart_falhou_em`, por exemplo). Eles eram
    # mandados ao site do mesmo jeito; o site respondia **HTTP 400 "nada a
    # gravar"** — corretamente, porque nenhum campo compartilhado tinha vindo —
    # e o log gritava "o outro ambiente pode repetir a publicacao", que era
    # MENTIRA: nada sobre publicacao estava sendo gravado. Foi o que apareceu na
    # rodada das 22:52 desta noite. Alarme falso ensina a ignorar alarme.
    CAMPOS_COMPARTILHADOS = ('last_ts', 'last_id', 'ranking_hash',
                             'ranking_enviado_em', 'heartbeat')
    partilhaveis = {k: v for k, v in campos.items() if k in CAMPOS_COMPARTILHADOS}
    if ESTADO_COMPARTILHADO and partilhaveis:
        corpo = dict(partilhaveis)
        corpo['origem'] = ORIGEM
        try:
            resp = _session().post(f'{API_URL}/api/automacao/estado',
                                   headers=_api_headers(), json=corpo, timeout=20)
            if resp.status_code != 200:
                log.warning('   Nao gravei o estado compartilhado (HTTP %s) — '
                            'o outro ambiente pode repetir a publicacao', resp.status_code)
        except Exception as exc:
            log.warning('   Nao gravei o estado compartilhado (%s) — '
                        'o outro ambiente pode repetir a publicacao', type(exc).__name__)
    return estado


def _impressao_ranking(dados):
    """Impressao digital dos km. Muda so quando alguem somou distancia —
    texto de patrocinador ou horario nao entram na conta."""
    import hashlib
    bruto = json.dumps(dados.get('ranking'), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(bruto.encode('utf-8')).hexdigest()[:16]


def _ranking_vazio(dados):
    """Ninguem somou nada ainda neste periodo?

    No dia 1o o contador zera para todo mundo. Publicar uma tabela com zero
    atleta nao informa nada e ainda passa a impressao de que o sistema
    quebrou. Melhor um aviso curto — e as logos dos patrocinadores, que sao o
    compromisso com quem banca o mes.
    """
    ranking = dados.get('ranking') or {}
    total_km = 0.0
    total_atletas = 0
    for secao in ('corrida_caminhada', 'ciclismo'):
        bloco = ranking.get(secao) or {}
        try:
            total_km += float(bloco.get('total_km') or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_atletas += int(bloco.get('total_atletas') or 0)
        except (TypeError, ValueError):
            pass
    return total_km <= 0 and total_atletas <= 0


def avaliar_envio_ranking(dados, args):
    """Decide se o ranking deve mesmo ir para o grupo agora.

    A regra e o KM, nao o relogio: se alguem somou distancia, publica — mesmo
    que o ultimo envio tenha sido ha pouco ("se tem km novo, nao custa nada
    enviar"). Se ninguem somou, republicar so repetiria a mesma tabela.

    Quem impede ranking-em-cima-de-ranking e a impressao digital dos km, que
    fica no DS215j: depois que um ambiente publica, o outro ve a mesma
    impressao e nao repete. E melhor que uma janela de tempo, porque nao
    depende de os dois relogios concordarem.

    A janela de MIN_INTERVALO_HORAS sobrou para um caso so: quando o estado
    compartilhado esta fora do ar e nao da para comparar impressao nenhuma.
    Ai ela evita que uma queda de rede vire mensagem repetida no grupo.

    Devolve (pode_enviar, motivo, impressao).
    """
    estado = load_state()
    impressao = _impressao_ranking(dados)
    ultimo = estado.get('ranking_enviado_em') or 0

    # Vem antes de tudo, inclusive do --forcar-envio: tabela sem nenhum
    # atleta nao vai para o grupo em hipotese nenhuma.
    if _ranking_vazio(dados):
        return False, 'ranking zerado', impressao

    if args.forcar_envio:
        return True, 'envio forcado por parametro', impressao

    conhecido = bool(estado.get('ranking_hash'))
    if conhecido and SO_SE_MUDOU and estado.get('ranking_hash') == impressao:
        return False, 'os km nao mudaram desde a ultima publicacao', impressao

    if conhecido:
        return True, 'os km mudaram desde a ultima publicacao', impressao

    # Sem impressao anterior: ou e a primeira publicacao, ou perdemos o estado.
    # No segundo caso a janela de tempo e a unica protecao que resta.
    if ultimo:
        horas = (time.time() - ultimo) / 3600.0
        if horas < MIN_INTERVALO_HORAS:
            return (False,
                    f'nao consegui comparar os km (estado compartilhado sem '
                    f'impressao) e o ultimo ranking saiu ha {horas:.1f}h — '
                    f'segurando por precaucao ate completar {MIN_INTERVALO_HORAS}h',
                    impressao)

    return True, 'primeira publicacao registrada', impressao


# ===== UTILITARIOS =====
def _jid_to_phone(jid):
    """55DDXXXXXXXX@c.us -> '+55 DD XXXX-XXXX' (formato que o parser reconhece)."""
    match = re.match(r'(\d{2})(\d{2})(\d{4,5})(\d{4})@', jid or '')
    if match:
        return '+%s %s %s-%s' % match.groups()
    return (jid or 'desconhecido').split('@')[0] or 'desconhecido'


def _ts_str(ts):
    return datetime.fromtimestamp(ts, TZ).strftime('%d/%m/%Y %H:%M')


def periodo_atual():
    agora = datetime.now(TZ)
    return f'{agora.year:04d}-{agora.month:02d}'


def _quebrar(texto, limite=CHUNK):
    """Divide respeitando quebras de linha — nao corta o ranking no meio da linha."""
    partes, atual = [], ''
    for linha in texto.split('\n'):
        if len(linha) > limite:                       # linha gigante: corte duro
            if atual:
                partes.append(atual)
                atual = ''
            for i in range(0, len(linha), limite):
                partes.append(linha[i:i + limite])
            continue
        candidato = f'{atual}\n{linha}' if atual else linha
        if len(candidato) > limite:
            partes.append(atual)
            atual = linha
        else:
            atual = candidato
    if atual:
        partes.append(atual)
    return partes or ['']


# Endereco do site DENTRO da rede local, sem passar pela Cloudflare.
#
# POR QUE ISTO EXISTE
# Em 02/08/2026 a rodada morreu na etapa 1 com "API HTTP 530" — o codigo que a
# Cloudflare devolve quando o tunel esta fora do ar. O site estava PERFEITO: no
# mesmo instante ele respondia HTTP 200 em http://NOME-DO-SERVIDOR:5000. Perdeu-se
# uma publicacao por causa de um intermediario, com origem e destino na mesma
# rede e a poucos metros um do outro.
#
# O caminho externo continua sendo o primeiro (e o que funciona de qualquer
# lugar); o local entra so quando ele falha.
# ⚠ VAZIO ATE ALGUEM DESCOBRIR O ENDERECO CERTO.
# Em 02/08 testei http://NOME-DO-SERVIDOR:5000: a raiz responde 200, mas
# /api/health devolve 404 — e outro servico naquela porta, nao o Movimente-se.
# Apontar para o lugar errado seria pior que nao ter fallback: a rodada
# gastaria tempo tentando e o log culparia o endereco errado.
# Para ligar: descubra a porta real do app no DS215j e ponha no .env como
#     MOVIMENTE_API_URL_LOCAL=http://192.168.X.Y:PORTA
API_URL_LOCAL = os.getenv('MOVIMENTE_API_URL_LOCAL', '').rstrip('/')


# ===== ETAPAS =====
def _tentar_api(base):
    """Devolve os dados de /api/health se este endereco responder, senao None."""
    try:
        resp = _session().get(f'{base}/api/health', headers=_api_headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json()
        log.warning('   %s respondeu HTTP %s%s', base, resp.status_code,
                    ' (tunel Cloudflare fora do ar)' if resp.status_code == 530 else '')
    except Exception as exc:
        log.warning('   %s inacessivel: %s', base, exc)
    return None


def etapa_api():
    """1/7 — Disponibilidade da API, pelo caminho externo ou pelo local."""
    global API_URL
    log.info('>>> Etapa 1/7 — Verificando API...')

    dados = _tentar_api(API_URL)
    if dados is None and API_URL_LOCAL and API_URL_LOCAL != API_URL:
        log.warning('   Tentando o endereco local (mesma rede, sem Cloudflare)...')
        dados = _tentar_api(API_URL_LOCAL)
        if dados is not None:
            # Vale para a rodada inteira: nao adianta passar na etapa 1 pelo
            # caminho local e tentar os proximos pedidos pelo que esta caido.
            log.warning('   Seguindo por %s nesta rodada.', API_URL_LOCAL)
            API_URL = API_URL_LOCAL

    if dados is None:
        log.error('   API inacessivel pelos dois caminhos.')
        return False

    log.info('   OK — cidade "%s", banco %s', dados.get('cidade'), dados.get('banco'))

    # PARA ONDE ESTA RODADA PUBLICA — perguntado ao site ANTES de qualquer
    # coisa poder sair (P-27). Aqui e o unico lugar seguro: a etapa 1 acontece
    # antes da coleta, do ranking e das logos.
    perguntar_ao_site_o_destino()
    log.info('   Destino desta rodada: %s',
             ('ENSAIO -> %s' % DESTINO_ENSAIO) if DESTINO_ENSAIO else 'GRUPO')

    _avisar_pareamento_recente(dados.get('pareamento'))
    _sinal_de_vida()
    return True


# O que o site contou sobre o ultimo pareamento desta sessao (12/09/2026).
# Preenchido por `_avisar_pareamento_recente`; lido por
# `conferir_copia_da_sessao`, que precisa saber se o pareamento acabou de
# acontecer antes de dizer que a sessao esta sem copia.
_PAREAMENTO_INFO = None
# Ate quantos minutos depois do pareamento a ausencia de copia e' NORMAL.
_PAREAMENTO_FOLGA_MIN = 5
# Quanto esperar pela copia antes de desistir e avisar (o job grava de 60 em
# 60 s; 3 min cobre o primeiro ciclo com folga).
_ESPERA_COPIA_SEG = 180


def _minutos_desde_o_pareamento():
    """Minutos desde o ultimo pareamento, ou None quando o site nao contou."""
    info = _PAREAMENTO_INFO
    if not isinstance(info, dict):
        return None
    horas = info.get('horas_desde')
    if horas is None:
        return None
    try:
        return int(round(float(horas) * 60))
    except (TypeError, ValueError):
        return None


def _avisar_pareamento_recente(info):
    """Sessao pareada ha pouco tem HISTORICO FURADO — a coleta nao e confiavel.

    P-38 / regra 18. Aparelho recem-vinculado nao recebe as mensagens antigas
    de uma vez: o whatsapp-web.js entrega aos poucos e nunca diz que terminou.
    No caso medido vieram 21 blocos de 39 mensagens com um link faltando.

    POR QUE AVISAR E NAO ABORTAR. Abortar deixaria a cidade sem ranking pelo
    dia inteiro depois de qualquer repareamento — trocaria "pode faltar link"
    por "com certeza nao sai nada". O aviso poe o rastro no log e no alerta, que
    e o que a regra 4-I pede: o admin decide conferindo, sabendo.

    Quem decide se e recente e o SITE (/api/health). Aqui so se obedece — duas
    copias da mesma regra e como o nome de sessao no .env: divergem sozinhas.

    GUARDA A INFORMACAO PARA QUEM VEM DEPOIS (12/09/2026). A conferencia da
    copia precisa saber se o pareamento acabou de acontecer: o job
    "RemoteAuth Backup Sync" leva cerca de um minuto para subir, e ate la
    "nao tem copia" e verdade mas nao e problema. Medido nesta madrugada: o
    dono pareou 00:40:31, o aviso de "sessao sem copia" saiu no WhatsApp dele
    as 00:41:25 e a copia foi gravada as 00:41:41 — 16 segundos depois do susto.
    """
    global _PAREAMENTO_INFO
    _PAREAMENTO_INFO = info if isinstance(info, dict) else None
    if not isinstance(info, dict) or not info.get('recente'):
        return
    horas = info.get('horas_desde')
    quanto = ('ha %s' % _humano_horas(horas)) if horas is not None else 'ha pouco'
    log.warning('   ATENCAO: a sessao "%s" foi pareada %s.',
                info.get('sessao') or '?', quanto)
    log.warning('   Aparelho recem-vinculado recebe o historico do grupo '
                'incompleto, e o WhatsApp nao avisa quando terminou de mandar. '
                'A coleta desta rodada pode nao ter todos os links.')
    log.warning('   O ranking sai assim mesmo — nao publicar seria pior —, mas '
                'confira a conferencia da cidade antes de fechar o mes.')


def _humano_horas(horas):
    """"3 horas", "1 hora e 30 minutos", "40 minutos" — nunca "1.5h".

    Convencao do projeto: tempo para gente vai em portugues, e quem converte e
    o codigo. Nos contamos em base 10 e a hora e base 60.
    """
    try:
        total = int(round(float(horas) * 60))
    except (TypeError, ValueError):
        return 'pouco tempo'
    if total < 60:
        return '%d minuto%s' % (total, '' if total == 1 else 's')
    h, m = divmod(total, 60)
    texto = '%d hora%s' % (h, '' if h == 1 else 's')
    if m:
        texto += ' e %d minuto%s' % (m, '' if m == 1 else 's')
    return texto


def _sinal_de_vida():
    """Diz ao site 'estive aqui', tenha esta rodada o que gravar ou nao.

    E o que permite ao vigia do site (P-35) distinguir duas coisas que de fora
    parecem iguais: a maquina NAO ACORDOU, e a maquina rodou e nao havia
    novidade. Sem isto, uma rodada normal sem novidade — ranking igual, grupo
    parado — deixava o estado do site velho e o vigia acusaria falha onde nao
    houve. Medido em 05/08: a rodada das 22:00 rodou e o site continuou
    marcando 17:00.

    Falhar aqui nao pode derrubar a rodada: e um carimbo, nao uma etapa.
    """
    try:
        resp = _session().post(f'{API_URL}/api/automacao/estado',
                               headers=_api_headers(),
                               json={'heartbeat': True, 'origem': ORIGEM},
                               timeout=20)
        if resp.status_code != 200:
            log.debug('   sinal de vida recusado: HTTP %s', resp.status_code)
    except Exception as exc:
        log.debug('   nao consegui dar sinal de vida: %s', exc)


_SESSAO_MULTIPLA_AVISADA = False


def _fixar_sessao(nome, quantas=None):
    """Passa a rodada inteira a usar a sessao que foi encontrada de pe.

    Trocar o global e o ponto: dai em diante TUDO (coleta, envio, banco da
    copia) fala com a sessao certa, sem precisar mexer em quinze f-strings.
    """
    global WAHA_SESSION, _SESSAO_MULTIPLA_AVISADA
    if not nome:
        return
    if not wahasessao.mesmo_nome(nome, WAHA_SESSION):
        log.warning('   A sessao viva se chama "%s" e o .env pede "%s". '
                    'Seguindo com a que esta de pe: a instancia e o endereco, '
                    'o nome e so rotulo.', nome, WAHA_SESSION_ENV)

    # ADOTA SEMPRE A CAIXA EXATA QUE O WAHA DEVOLVEU — inclusive quando os
    # nomes sao "o mesmo".
    #
    # A ARMADILHA (custou as rodadas de 06 e 07/08): comparar nome ignora
    # maiuscula (regra 16-0, e esta certo — quem digita e gente). Mas o CAMINHO
    # HTTP do WAHA NAO ignora. Com a sessao viva chamada "Principal-PLS" e o
    # .env pedindo "principal-pls", `mesmo_nome` dizia "e a mesma", a
    # atribuicao ficava dentro do `if` e nao acontecia — e a coleta montava
    # /api/principal-pls/... levando
    #     HTTP 422 {"error":"Session \"principal-pls\" does not exist"}
    # a cada rodada, enquanto TODAS as etapas anteriores diziam OK, porque elas
    # perguntam por estado, nao por URL montada na mao.
    #
    # A regra que fica: comparar nome e uma coisa, USAR o nome e outra. Para
    # comparar, ignore a caixa. Para montar URL, use o que o servidor disse,
    # letra por letra.
    WAHA_SESSION = nome
    if quantas and quantas > 1 and not _SESSAO_MULTIPLA_AVISADA:
        _SESSAO_MULTIPLA_AVISADA = True
        # DUAS SESSOES AQUI PASSOU A SER O NORMAL (08/09/2026).
        #
        # Ate a fusao valia "uma instancia = uma sessao", e mais de uma era
        # sinal de sobra a limpar (a antiga P-02). Agora o desenho e UM
        # CONTAINER POR CIDADE, com uma sessao por numero dentro dele
        # (principal e suplente), entao encontrar duas e o esperado -- avisar
        # como problema so ensinaria a ignorar o aviso.
        #
        # O que continua importando e' DIZER QUAL FOI USADA: a escolha e pelo
        # ESTADO (WORKING + CONNECTED), nunca pelo nome, e saber qual entrou
        # e' o que permite conferir a rodada depois.
        log.info('   Esta instancia tem %d sessoes (principal e suplente da '
                 'mesma cidade — e o desenho novo). Usando "%s".',
                 quantas, WAHA_SESSION)


def assumir_sessao_viva():
    """Troca a rodada para OUTRA instancia (papel) que estiver de pe.

    17/08/2026, item A do plano do MVP. So e chamada quando a instancia de
    sempre (a do `.env`) falhou em `etapa_waha`. `_fixar_sessao` sozinho NAO
    BASTA aqui: ela so troca o NOME dentro da MESMA instancia. A suplente
    mora noutra PORTA e com OUTRA CHAVE — chave trocada e HTTP 401 (regra
    16-0-D) — entao troca-se `WAHA_BASE`, `WAHA_API_KEY` e `WAHA_SESSION`
    juntos, ou a troca nao funciona.

    Devolve o papel assumido ('principal'/'suplente') ou '' se ninguem
    respondeu em instancia nenhuma. **Quem assumiu fica so no log, nunca no
    grupo** (decisao do dono, 17/08, §2.3 do plano): o grupo so quer o
    ranking certo, nao qual dos dois numeros publicou.

    A COLETA E OUTRA CONVERSA. Isto serve para PUBLICAR — uma sessao recem
    pareada tem historico furado (regra 18) e a regra 8 (leitura incompleta
    nao publica) continua valendo do mesmo jeito depois da troca.
    """
    global WAHA_BASE, WAHA_API_KEY, WAHA_SESSION
    try:
        prontas = wahasessao.sessoes_prontas()
    except Exception as exc:
        log.warning('   Nao consegui procurar outra sessao de pe: %s', exc)
        return ''
    if not prontas:
        log.error('   NENHUMA sessao de pe em instancia nenhuma — nao ha para onde trocar.')
        return ''

    escolhida = prontas[0]
    if escolhida['base'].rstrip('/') == WAHA_BASE.rstrip('/'):
        # A instancia de sempre respondeu agora, entre uma checagem e outra —
        # nao e bem uma troca de instancia, mas o nome ainda pode ter mudado.
        log.info('   A propria instancia de sempre respondeu agora — sem trocar de instancia.')
        _fixar_sessao(escolhida['nome'], len(prontas))
        return escolhida.get('papel', '')

    log.warning('   A instancia de sempre (%s) nao respondeu — assumindo a sessao '
                '"%s" (%s) em %s para esta rodada.',
                WAHA_BASE, escolhida['nome'], escolhida.get('papel', '?'), escolhida['base'])
    WAHA_BASE = escolhida['base'].rstrip('/')
    WAHA_API_KEY = escolhida['chave']
    WAHA_SESSION = escolhida['nome']
    return escolhida.get('papel', '')


def _status_waha():
    """(status, engine_state, detalhe) da sessao VIVA desta instancia.

    NAO PROCURA PELO NOME. Pergunta /api/sessions e usa a que estiver de pe,
    seja qual for o nome dela. Procurar pelo nome foi a causa raiz do ranking
    nao sair em 02 e 03/08/2026: o .env pedia "principal-pls", a sessao viva se
    chamava "default", e vinham seis HTTP 422 seguidos sem nada publicado.

    Cuidado que continua valendo: logo apos o container subir, o WAHA responde
    status=WORKING com o motor WEBJS ainda em UNLAUNCHED — e nesse intervalo
    toda leitura de mensagens devolve HTTP 500. So da para trabalhar quando o
    engine.state chega em CONNECTED.
    """
    nome, status, estado, quantas = wahasessao.resolver(
        WAHA_BASE, WAHA_API_KEY, WAHA_SESSION_ENV)
    if quantas is None:
        return None, None, 'a instancia nao respondeu'
    if not nome:
        # Instancia de pe e VAZIA e diferente de instancia muda: aqui cabe
        # criar e parear, nao esperar.
        return None, None, 'a instancia nao tem sessao nenhuma'
    _fixar_sessao(nome, quantas)
    return status, estado, {'name': nome, 'status': status,
                            'engine': {'state': estado}, 'sessoes': quantas}


def _causa_da_queda(minutos=90):
    """Le o log do container e diz, em portugues, POR QUE a sessao caiu.

    Devolve linhas prontas para o log; lista vazia quando nao da para saber.
    NUNCA levanta erro: diagnostico que quebra a rodada e pior que diagnostico
    ausente (regra 11).

    O QUE ELE SEPARA, e por que isso importa:
      - navegador morto      -> reiniciar os containers resolve
      - LOGOUT do WhatsApp   -> so pareando de novo, pelo celular
      - Postgres sem conexao -> nem restart nem pareamento resolvem
    Sem essa distincao a resposta era sempre "repareie". Em 09/09 isso teria
    mandado o dono parear a toa: o que faltou naquele minuto foi o pool do
    banco, e a copia da sessao sobreviveu porque a tentativa de apaga-la FALHOU.
    """
    container = os.getenv('WAHA_CONTAINER', 'auto-waha-pls')
    try:
        r = subprocess.run(
            ['docker', 'logs', container, '--since', '%dm' % int(minutos)],
            capture_output=True, text=True, timeout=25, errors='replace')
        bruto = (r.stdout or '') + (r.stderr or '')
    except Exception:
        return []
    if not bruto.strip():
        return []

    # Tira a cor do terminal SEM barra invertida nenhuma no padrao: [[] e uma
    # classe que casa o proprio "[". Ja perdi um patch por causa de escape.
    limpo = re.sub(re.escape(chr(27) + chr(91)) + '[0-9;]*m', '', bruto)

    PISTAS = (
        ('Session has been logged out',
         'O WhatsApp DESVINCULOU o aparelho do lado dele (LOGOUT). Reiniciar '
         'nao adianta: tem de parear de novo, pela tela do WhatsApp no site.'),
        ('The browser has been disconnected',
         'O navegador interno do WAHA morreu. Costuma ser memoria ou pagina '
         'travada; reiniciar os containers com parada limpa resolve.'),
        ('KnexTimeoutError',
         'O banco (Postgres) ficou sem conexao livre. Nem parear nem '
         'reiniciar resolve: e o banco que precisa de folga.'),
        ('auth_failure',
         'O WhatsApp recusou a autenticacao. E pareamento novo.'),
        ('Max qr code retries',
         'Ninguem leu o codigo a tempo. Basta pedir o codigo de novo.'),
    )
    # UMA CAUSA MANDA, O RESTO E CONTEXTO. Em 09/09 as tres apareceram no
    # mesmo log, nesta cadeia: o navegador morreu, o WhatsApp desvinculou e o
    # Postgres estourou depois. Listar as tres lado a lado da conselho
    # contraditorio ("reinicie" e "pareie" ao mesmo tempo). PISTAS esta em
    # ordem de quem manda: o desvinculo vence o navegador, e o erro do banco
    # e sempre CONSEQUENCIA -- fica por ultimo de proposito.
    presentes = [(marca, expl) for marca, expl in PISTAS if marca in limpo]
    achados = []
    if presentes:
        achados.append('CAUSA: %s' % presentes[0][1])
        for _marca, expl in presentes[1:]:
            achados.append('   (tambem no log, provavelmente consequencia: %s)'
                           % expl)

    ultima = None
    for m in re.finditer(
            '([0-9]{2}:[0-9]{2}:[0-9]{2}).*'
            '(logged out|browser has been disconnected|KnexTimeoutError)',
            limpo):
        ultima = m.group(1)
    if ultima:
        achados.append('Ultimo evento desse tipo no log do container: %s '
                       '(horario do container).' % ultima)
    if not achados:
        achados.append('Nao achei no log do container um motivo conhecido '
                       'para a queda -- vale olhar: docker logs %s' % container)
    return achados


MSG_RESCAN = ('A sessao do WhatsApp foi desconectada (o celular desvinculou o aparelho). '
              'E preciso ler o QR Code de novo em ' )


def _reiniciar_sessao():
    """Tenta religar a sessao sem intervencao humana.

    Usa o nome que _status_waha descobriu, nunca o do .env: religar um nome que
    nao existe devolve 422 e some com o unico caminho de recuperacao automatica.
    """
    try:
        resp = _session().post(f'{WAHA_BASE}/api/sessions/{WAHA_SESSION}/restart',
                               headers=_waha_headers(True), timeout=30)
        log.info('   Pedido de restart da sessao: HTTP %s', resp.status_code)
        return resp.status_code in (200, 201)
    except Exception as exc:
        log.warning('   Nao consegui reiniciar a sessao: %s', exc)
        return False


def _principal_de_pe_antes_da_espera():
    """Acha o PRINCIPAL de pe antes de gastar a espera de arranque.

    POR QUE (13/09/2026). A instancia sai do `.env` (`WAHA_BASE`), e quando o
    container do principal nasce noutra porta o endereco do arquivo aponta para
    o vazio. Em 12-13/09 foi o que houve: o principal subiu na 3001 e a etapa 2
    ficou 150 segundos esperando a 3000 responder, para so entao procurar em
    outro lugar — com a sessao viva, vinculada, uma porta ao lado. Dois
    rankings perdidos e dois minutos e meio de espera em toda passada.

    A ORDEM E QUE IMPORTA, NAO O RELOGIO: primeiro o principal, depois o
    suplente (`wahasessao.sessoes_prontas` ja devolve nessa ordem, e as portas
    ja saem organizadas de quem cria o container).

    Por isso aqui SO se troca por outro PRINCIPAL. Se o unico de pe for o
    suplente, nada muda: ele continua sendo o ultimo recurso, assumido pelo
    caminho de sempre (`assumir_sessao_viva`) depois que a espera do principal
    se esgotou. Sem isso, um principal que demora 20 segundos para subir
    perderia a rodada para o suplente — o contrario do desenho.

    Nao levanta excecao e nao espera nada: ou acha na hora, ou sai calada.
    """
    global WAHA_BASE, WAHA_API_KEY, WAHA_SESSION
    try:
        status, _estado, _detalhe = _status_waha()
    except Exception:
        status = None
    # SESSAO MORTA NAO E ARRANQUE (13/09/2026). Medido as 16:41: a instancia do
    # `.env` respondeu — mas com a sessao em FAILED, restaurada de uma copia
    # velha — enquanto o principal de verdade estava pronto na porta ao lado.
    # `None` (nao respondeu), FAILED e STOPPED sao os tres casos em que nao ha
    # nada a esperar ali. STARTING e SCAN_QR_CODE ficam de fora de proposito:
    # esses sim sao arranque, e arranque se espera (regra 16-A).
    if status not in (None, 'FAILED', 'STOPPED'):
        return False            # esta de pe ou subindo: nada a fazer aqui
    try:
        prontas = wahasessao.sessoes_prontas()
    except Exception as exc:
        log.warning('   Nao consegui procurar onde o WAHA esta: %s', exc)
        return False
    for item in prontas:
        if (item.get('papel') or '') != 'principal':
            continue
        base = (item.get('base') or '').rstrip('/')
        if not base or base == WAHA_BASE.rstrip('/'):
            return False
        log.warning('   O principal nao esta em %s — ele responde em %s '
                    '(sessao "%s"). Seguindo por la.',
                    WAHA_BASE, base, item.get('nome'))
        WAHA_BASE = base
        WAHA_API_KEY = item.get('chave') or WAHA_API_KEY
        WAHA_SESSION = item.get('nome') or WAHA_SESSION
        return True
    return False


def etapa_waha(espera=0, tentar_religar=True):
    """2/7 — Sessao WAHA pronta de verdade (WORKING + engine CONNECTED).

    FAILED aqui significa uma coisa so: o WhatsApp derrubou o vinculo do
    aparelho. Restart NAO resolve — e nao e de graca.

    Achado de 09/08 e MEDIDO DE NOVO em 16/08: o restart faz o whatsapp-web.js
    chamar `logout()` -> `deleteRemoteSession()`, isto e, **o WAHA pede para
    APAGAR a copia gravada no Postgres**. Nas duas vezes a copia so sobreviveu
    por sorte — a exclusao morreu num timeout do banco. E nas duas a sessao
    voltou para FAILED em menos de um minuto.

    Por isso, desde 16/08, **este ponto nao religa mais nada**: ele para rapido
    e diz o que resolve (o botao Conectar do site, onde ha gente com o celular).
    `tentar_religar` continua no lugar so para nao quebrar quem chama.
    """
    log.info('>>> Etapa 2/7 — Verificando sessao WAHA...')
    # ONDE, ANTES DE QUANTO TEMPO (13/09/2026) — ver a funcao acima.
    _principal_de_pe_antes_da_espera()
    limite = time.time() + max(espera, 0)
    avisou = False
    religou = bool(load_state().get('restart_falhou_em'))
    if religou and tentar_religar:
        log.warning('   Religar ja foi tentado e nao resolveu — nao vou repetir. '
                    'A sessao precisa ser pareada de novo pelo celular.')
    while True:
        try:
            status, estado, detalhe = _status_waha()
        except Exception as exc:
            status, estado, detalhe = None, type(exc).__name__, str(exc)

        if status == 'WORKING' and estado == 'CONNECTED':
            log.info('   OK — sessao WORKING / engine CONNECTED')
            if load_state().get('restart_falhou_em'):
                save_state(restart_falhou_em=0)   # voltou: o freio se solta
            zerar_freio_whatsapp('Sessao WhatsApp')   # problema resolvido: solta o freio do item C
            # Conectada nao quer dizer gravada. Perguntar agora, que da
            # tempo de agir, e melhor do que descobrir no proximo boot.
            try:
                conferir_copia_da_sessao()
            except Exception as exc:
                log.info('   (conferencia da copia falhou: %s)', exc)
            return True

        if status == 'FAILED':
            # NAO SE RELIGA SESSAO EM `FAILED` — MEDIDO EM 16/08/2026.
            #
            # Ate esta noite aqui se tentava um restart "de graca". O log do
            # WAHA mostrou o preco dele:
            #
            #     Session has been logged out!  (LOGOUT)
            #     RemoteAuth.logout -> deleteRemoteSession -> delete
            #     KnexTimeoutError: Timeout acquiring a connection
            #
            # Em `FAILED` o WhatsApp JA desvinculou o aparelho do lado dele. O
            # restart nao reconecta: ele PEDE PARA APAGAR a copia (regra 15-C) e
            # a sessao volta a `FAILED` em 40 segundos. Nesta mesma noite a
            # rodada das 22:52 gastou 150 segundos assim, para terminar no mesmo
            # lugar — so que mais tarde e com a copia arriscada.
            #
            # Regra do dono (16/08): `FAILED` nao volta. Aqui NAO se apaga nada
            # (nao ha ninguem com o celular na mao — isso e o botao Conectar do
            # site, regra 15-A): o certo e parar rapido e chamar gente.
            log.error('   Sessao em FAILED — o WhatsApp desvinculou o aparelho. '
                      'Religar nao traz de volta (medido em 16/08) e ainda pede '
                      'para apagar a copia: NAO vou tentar.')
            log.error('   O que resolve: abrir a tela do WhatsApp no site e '
                      'clicar em Conectar — o pareamento e refeito por la, com '
                      'o codigo chegando no celular.')
            # E POR QUE CAIU? (09/09/2026, pedido do dono)
            #
            # Ate aqui o log dizia QUE caiu e nunca POR QUE. Em 09/09 a
            # sessao caiu as 11:04 e a causa so apareceu horas depois,
            # lendo o log do container na mao. A diferenca muda o que o
            # dono tem de fazer: navegador morto se resolve com restart,
            # desvinculo pelo celular exige parear de novo, e Postgres
            # sem conexao nao se resolve com nenhum dos dois -- naquele
            # dia o conselho generico teria mandado ele parear a toa.
            for _linha in _causa_da_queda():
                log.error('   %s', _linha)
            save_state(restart_falhou_em=int(time.time()))
            return False

        if time.time() >= limite:
            if status in ('FAILED', 'STOPPED'):
                log.error('   %s%s/dashboard', MSG_RESCAN, WAHA_BASE)
            else:
                log.error('   Sessao indisponivel: status=%s engine=%s (%s)',
                          status, estado,
                          detalhe if isinstance(detalhe, str) else 'sem detalhe')
            return False

        if not avisou:
            log.info('   Aguardando WAHA conectar (ate %ds)... status=%s engine=%s',
                     espera, status or 'sem resposta', estado or '-')
            avisou = True
        time.sleep(5)


# ===================== A COPIA DA SESSAO =====================
# CONECTADO E GRAVADO SAO COISAS DIFERENTES
#
# A sessao "palmeiropolis" passou dias respondendo WORKING / CONNECTED, mandando
# mensagem sem reclamar, e mesmo assim morria em TODO reinicio do container.
# O motivo estava no banco: waha_webjs_palmeiropolis tinha ZERO arquivos. O job
# "RemoteAuth Backup Sync" nunca chegou a rodar para ela — ela nunca foi salva.
#
# Isso e invisivel enquanto a maquina nao reinicia. Quando reinicia, volta para
# o QR Code e a culpa cai no reinicio, que e so o gatilho. A causa e a copia
# que nunca existiu.
#
# Por isso esta conferencia roda junto com a etapa 2: assim que a sessao fica
# pronta, perguntamos tambem se ela esta GRAVADA. Se nao estiver, o alerta sai
# agora — enquanto a sessao ainda esta viva e da para agir — e nao depois de
# perde-la.

def _banco_da_sessao(sessao=None):
    """O banco REAL desta sessao, descoberto no Postgres.

    Nao se calcula esse nome. O session namespace e POR INSTANCIA (a 3000 usa
    "webjs" e a 3001 usa "webjs_suplente") e o .env guarda um valor so —
    calcular acertava a instancia principal por sorte e errava a suplente,
    dizendo "sem copia" numa sessao com 13 MB gravados. Quem descobre e o
    wahasessao, o mesmo modulo que escolhe a sessao.
    """
    banco, _erro = wahasessao.banco_da_sessao(sessao or WAHA_SESSION)
    return banco or '(nenhum banco desta sessao)'


def copia_da_sessao(sessao=None):
    """(arquivos, quando, erro) — o que existe gravado desta sessao.

    arquivos=None quer dizer NAO SEI (docker fora do ar). Nao levantar excecao
    aqui e proposital: conferir a copia nao pode derrubar a rodada.
    """
    return wahasessao.copia_da_sessao(sessao or WAHA_SESSION)


# Nao repete o aviso de "sem copia" antes disto (17/08/2026, item 4.1-f do
# MVP). `notificar_admin` manda WhatsApp cruzado, e esse canal NAO TEM FREIO
# NENHUM (foi exatamente o defeito medido no plano de 17/08, §1.4: 22 avisos
# numa noite so, porque a tarefa que descobre roda de 15 em 15 minutos). Como
# `etapa_waha` chama esta funcao toda vez que a sessao conecta — inclusive no
# `--so-fila` — chamar `notificar_admin` sem guarda aqui reproduziria o mesmo
# defeito para uma etapa nova. O estado local (igual a `restart_falhou_em`)
# resolve isso e se solta sozinho assim que a copia aparecer.
_SEM_COPIA_FREIO_SEG = 6 * 3600  # 6 horas


# A COPIA NAO PODE ENVELHECER (12/09/2026) — ver `wahasessao.idade_da_copia`.
# O job grava de 60 em 60 s; 12 min parada significa que ele NAO esta rodando.
_COPIA_VELHA_SEG = 12 * 60
# Um restart por 2 h, no maximo. Restart e' uma carga nova da pagina do
# WhatsApp Web, e toda carga tem risco (medido: 4 quedas em 11 cargas entre
# 08 e 11/09). Melhor uma carga de propósito agora do que um retrato velho
# restaurado no proximo reinicio, que e' perda certa do vinculo.
_COPIA_RESTART_FREIO_SEG = 2 * 3600


def forcar_copia_se_velha():
    """Sessao de pe com copia parada: reinicia UMA vez para o job voltar.

    POR QUE (medido na madrugada de 12/09/2026). Quando a injecao falha na
    subida, a sessao fica WORKING + CONNECTED mas nunca fica pronta, e o job
    "RemoteAuth Backup Sync" nao comeca. A copia envelhece em silencio — o
    aviso antigo so olhava se a copia EXISTIA, e ela existia (de horas atras).
    No reinicio seguinte o WAHA restaura esse retrato velho e o WhatsApp
    desvincula o aparelho.

    Nao vai na publicacao de proposito: quem chama e a passada de 15 em 15 min
    (`--so-fila`). Reiniciar a sessao no meio de um ranking seria trocar um
    risco por outro pior.

    Regra 15-A continua valendo: em `FAILED` nao se reinicia nada — aqui a
    sessao esta WORKING + CONNECTED, que e' o caso em que o restart e' seguro
    e foi medido funcionando (00:10 de 12/09: job voltou, copia gravada as
    00:11).
    """
    idade, erro = wahasessao.idade_da_copia(WAHA_SESSION)
    if erro or idade is None:
        log.info('   (nao deu para medir a idade da copia: %s)',
                 erro or 'sem resposta')
        return True
    if idade <= _COPIA_VELHA_SEG:
        log.info('   Copia gravada ha %d min — o job de copia esta rodando.',
                 idade // 60)
        if load_state().get('copia_velha_em'):
            save_state(copia_velha_em=0)
        return True

    log.error('   COPIA VELHA: %d min sem gravar, com a sessao de pe. O job '
              '"RemoteAuth Backup Sync" nao esta rodando — do jeito que esta, '
              'o proximo reinicio restaura um retrato velho e o WhatsApp '
              'desvincula o aparelho.', idade // 60)
    # PRIMEIRO GERAR A COPIA, DEPOIS (e so depois) PENSAR EM REINICIAR.
    #
    # Gerar nao custa carga de pagina; reiniciar custa — e carga de pagina foi
    # medida como sorteio de ~1 em 3 para perder o vinculo (`ERROS.md` §4.50).
    # Entao o caminho barato e seguro vem antes: zipar o perfil vivo e gravar no
    # Postgres (`copia_sessao.forcar`, provado as 00:55 de 12/09).
    agora = time.time()
    try:
        ok_forcada, detalhe = copia_sessao.forcar(WAHA_SESSION)
    except Exception as exc:
        ok_forcada, detalhe = False, str(exc)[:150]
    log.info('   Copia forcada: %s', detalhe)
    if ok_forcada:
        idade_nova, erro_nova = wahasessao.idade_da_copia(WAHA_SESSION)
        if erro_nova is None and idade_nova is not None \
                and idade_nova <= _COPIA_VELHA_SEG:
            # A COPIA VOLTOU; A PAGINA, NAO NECESSARIAMENTE (17/09/2026).
            # Job de copia parado e o SINTOMA de pagina que nao carregou
            # ("Failed to inject utils"). Em 17/09 a principal ficou assim das
            # 16:10 as 23:40: a cada 15 min esta funcao forcava a copia e
            # escrevia "RESOLVIDO", e ninguem soube ate o ranking falhar as
            # 21:33. Agora, depois da copia, a leitura do grupo decide. So a
            # leitura prova que a sessao funciona.
            leitura = prontidao._etapa_grupo(WAHA_BASE, WAHA_API_KEY,
                                              WAHA_SESSION, GROUP_ID)
            if leitura['ok'] is False:
                log.error('   Copia refeita (%d s), mas a sessao "%s" NAO LE o '
                          'grupo (%s): a pagina do WhatsApp nao carregou. O '
                          'ranking sai pela outra sessao, se ela ler.',
                          idade_nova, WAHA_SESSION, leitura['detalhe'])
                # P-134 (18/09/2026): PAGINA QUEBRADA SE CONSERTA COM UMA CARGA
                # NOVA. Medido: 06:12 as duas sessoes falharam a injecao e as
                # 06:15 a leitura deu HTTP 500 — a injecao falha IMPEDE ler e
                # enviar. O restart (pagina nova) ja trouxe a sessao de volta em
                # 12/09 00:10. A copia acabou de ser gravada, entao reiniciar
                # agora nao arrisca restaurar retrato velho. Mesmo freio de 2 h.
                if agora - (load_state().get('copia_restart_em') or 0) >= _COPIA_RESTART_FREIO_SEG:
                    save_state(copia_restart_em=int(agora))
                    log.info('   Reiniciando a sessao "%s" UMA vez para a pagina '
                             'carregar de novo...', WAHA_SESSION)
                    if _reiniciar_sessao():
                        time.sleep(120)
                        leitura = prontidao._etapa_grupo(WAHA_BASE, WAHA_API_KEY,
                                                          WAHA_SESSION, GROUP_ID)
                        if leitura['ok'] is not False:
                            log.info('   RESOLVIDO com o restart: a sessao "%s" '
                                     'voltou a ler o grupo.', WAHA_SESSION)
                            save_state(copia_velha_em=0)
                            return True
                        log.error('   O restart NAO trouxe a pagina de volta (%s).',
                                  leitura['detalhe'])
                if time.time() - (load_state().get('pagina_quebrada_avisada_em') or 0) >= 6 * 3600:
                    save_state(pagina_quebrada_avisada_em=int(time.time()))
                    notificar_admin(
                        'Sessao WhatsApp',
                        'A sessao "%s" aparece CONECTADA, mas a pagina do '
                        'WhatsApp dela nao carregou: nao le o grupo nem envia '
                        '(%s).\n\n1. O ranking segue pela outra sessao, se ela '
                        'estiver lendo.\n2. Para trazer esta de volta: reiniciar '
                        'a sessao (ou o container) — e uma nova carga de pagina.'
                        % (WAHA_SESSION, leitura['detalhe']))
                return True
            log.info('   RESOLVIDO sem reiniciar: copia de %d s, e a sessao le '
                     'o grupo.', idade_nova)
            save_state(copia_velha_em=0)
            return True

    if agora - (load_state().get('copia_restart_em') or 0) < _COPIA_RESTART_FREIO_SEG:
        log.warning('   (forcar nao resolveu e ja reiniciei por este motivo ha '
                    'menos de 2 h — nao repito; quem resolve agora e uma pessoa)')
        return False
    save_state(copia_restart_em=int(agora), copia_velha_em=int(agora))
    log.info('   Forcar nao resolveu — reiniciando a sessao UMA vez para o job '
             'de copia voltar...')
    if not _reiniciar_sessao():
        return False
    time.sleep(120)
    idade2, erro2 = wahasessao.idade_da_copia(WAHA_SESSION)
    if erro2 is None and idade2 is not None and idade2 <= _COPIA_VELHA_SEG:
        log.info('   RESOLVIDO: copia gravada ha %d min depois do restart.',
                 idade2 // 60)
        save_state(copia_velha_em=0)
        return True
    quanto = ('tempo desconhecido' if idade2 is None
              else '%d min' % (idade2 // 60))
    log.error('   O restart NAO trouxe o job de volta (copia parada ha %s).',
              quanto)
    notificar_admin(
        'Sessão sem cópia',
        'A sessão "%s" está conectada, mas a cópia de segurança está parada há '
        '%s e nem reiniciar a sessão resolveu. Do jeito que está, ela morre no '
        'próximo reinício da máquina e vai pedir o celular de novo.'
        % (WAHA_SESSION, quanto))
    return False


def vigiar_outras_sessoes():
    """As OUTRAS sessoes prontas da instancia leem o grupo? So detecta e avisa.

    18/09/2026 (P-134, lacuna medida): `forcar_copia_se_velha` so olha a
    principal, e so quando a copia envelhece. Em 18/09 05:11 a suplente falhou
    a injecao e o job de copia comecou mesmo assim — nada a denunciava, e ela
    so seria descoberta no dia em que fosse precisa. Diretriz do dono (18/09):
    detectar, nao mexer. Aqui so se LE (`limit=1`); nunca reinicia nem apaga.
    Um aviso por sessao a cada 6 h.
    """
    try:
        sessoes = wahasessao.listar(WAHA_BASE, WAHA_API_KEY) or []
    except Exception as exc:
        log.info('   (nao consegui listar as sessoes para vigiar: %s)', exc)
        return
    for item in sessoes:
        nome = item.get('name')
        if not nome or wahasessao.mesmo_nome(nome, WAHA_SESSION):
            continue
        status, estado = wahasessao.estado_de(item)
        if status == 'WORKING' and not estado:
            completo = wahasessao.detalhar(WAHA_BASE, WAHA_API_KEY, nome)
            if completo:
                _, estado = wahasessao.estado_de(completo)
        if not wahasessao.pronta(status, estado):
            continue
        leitura = prontidao._etapa_grupo(WAHA_BASE, WAHA_API_KEY, nome, GROUP_ID)
        if leitura['ok'] is not False:
            log.info('   Sessao "%s" le o grupo.', nome)
            continue
        log.error('   A sessao "%s" aparece CONECTADA mas NAO LE o grupo (%s): '
                  'a pagina do WhatsApp dela nao carregou.', nome, leitura['detalhe'])
        chave = 'pagina_quebrada_%s_avisada_em' % re.sub(r'[^a-z0-9]', '_', nome.lower())
        if time.time() - (load_state().get(chave) or 0) >= 6 * 3600:
            save_state(**{chave: int(time.time())})
            notificar_admin(
                'Sessao WhatsApp',
                'A sessao "%s" aparece CONECTADA, mas nao le o grupo (%s).\n\n'
                '1. O vinculo com o celular continua valendo — isto e a pagina '
                'do WhatsApp que nao carregou.\n2. Enquanto a outra sessao ler, '
                'o ranking sai por ela.\n3. Para trazer esta de volta: reiniciar '
                'a sessao (ou o container) — e uma carga nova da pagina.'
                % (nome, leitura['detalhe']))


def conferir_copia_da_sessao():
    """Avisa se a sessao viva nao tem copia gravada. Devolve True quando tem."""
    arquivos, quando, erro = copia_da_sessao()
    if erro:
        log.info('   (nao deu para conferir a copia da sessao: %s)', erro)
        return None
    if arquivos:
        log.info('   Copia da sessao no banco: %d arquivo(s), de %s',
                 arquivos, quando or 'data desconhecida')
        if load_state().get('sem_copia_avisado_em'):
            save_state(sem_copia_avisado_em=0)   # resolvida: o freio se solta
        return True

    # PAREOU AGORA? ESPERE O JOB SUBIR ANTES DE GRITAR (12/09/2026).
    #
    # O job de copia comeca cerca de um minuto depois da sessao ficar pronta.
    # Sem esta folga, TODO pareamento gera um aviso de "sessao sem copia" no
    # WhatsApp do dono — foi o que aconteceu as 00:41:25 desta madrugada, com a
    # copia chegando as 00:41:41. Alarme que se desmente sozinho em 16 segundos
    # ensina o dono a ignorar o proximo, que pode ser verdadeiro.
    _minutos = _minutos_desde_o_pareamento()
    if _minutos is not None and _minutos < _PAREAMENTO_FOLGA_MIN:
        log.info('   Sem copia ainda, mas a sessao foi pareada ha %d minuto(s). '
                 'A COPIA VEM PRIMEIRO: vou esperar ate %d s por ela antes de '
                 'dizer qualquer coisa.', _minutos, _ESPERA_COPIA_SEG)
        _limite = time.time() + _ESPERA_COPIA_SEG
        while time.time() < _limite:
            time.sleep(15)
            _arq, _quando, _erro = copia_da_sessao()
            if _erro:
                continue
            if _arq:
                log.info('   Copia gravada durante a espera (%s). Nada a avisar.',
                         _quando or 'agora')
                if load_state().get('sem_copia_avisado_em'):
                    save_state(sem_copia_avisado_em=0)
                return True
        log.error('   Esperei %d s depois do pareamento e a copia NAO veio — '
                  'agora e' ' problema de verdade.', _ESPERA_COPIA_SEG)

    log.error('   ATENCAO: a sessao "%s" esta conectada mas NAO TEM COPIA '
              'gravada em %s. Do jeito que esta, ela morre no proximo '
              'reinicio e vai pedir QR Code de novo.',
              WAHA_SESSION, _banco_da_sessao())
    log.error('   Confira no painel se o job "RemoteAuth Backup Sync" chegou a '
              'iniciar para esta sessao. Sessao pareada por codigo (request-code) '
              'ja subiu sem esse job — foi assim que a "palmeiropolis" ficou '
              'meses sem copia nenhuma.')
    notificar_n8n(
        'sessao_sem_copia',
        'A sessao "%s" esta WORKING/CONNECTED mas o banco %s tem zero arquivos. '
        'O job RemoteAuth Backup Sync nao esta gravando. A sessao sera perdida '
        'no proximo reinicio do container.' % (WAHA_SESSION, _banco_da_sessao()),
        nivel='alerta')

    # O AVISO NO SITE (17/08/2026) — ate aqui so o n8n era chamado, e ele e
    # no-op sem webhook configurado (P-10): o dono nunca via isto em lugar
    # nenhum. `notificar_admin` registra no site SEMPRE (regra 21-A) e so
    # manda WhatsApp/e-mail dentro da janela do freio acima.
    ultimo_aviso = load_state().get('sem_copia_avisado_em') or 0
    agora = time.time()
    if agora - ultimo_aviso >= _SEM_COPIA_FREIO_SEG:
        notificar_admin(
            'Sessão sem cópia',
            'A sessão "%s" está conectada mas sem cópia de segurança gravada '
            '(banco %s, zero arquivos). Ela morre no próximo reinício da '
            'máquina e vai pedir o celular de novo.'
            % (WAHA_SESSION, _banco_da_sessao()))
        save_state(sem_copia_avisado_em=int(agora))
    else:
        log.info('   (aviso de sem-copia silenciado — ja avisei ha menos de %dh)',
                 _SEM_COPIA_FREIO_SEG // 3600)
    return False


def _inicio_do_mes(agora=None):
    """Epoch do dia 1º deste mês, 00:00."""
    hoje = datetime.fromtimestamp(agora or time.time())
    return int(hoje.replace(day=1, hour=0, minute=0, second=0,
                            microsecond=0).timestamp())


def varredura_de_seguranca(estado, agora=None):
    """Esta rodada precisa reler mais do que o marco manda? (motivo, desde).

    PRIORIDADE MAXIMA: NAO PERDER LINK (regra 4-I).
    A janela incremental (marco - 2h) so cobre atraso pequeno de sincronizacao.
    Ela NAO cobre o buraco que aparece quando o WAHA passa horas fora do ar:
    quem postou nesse intervalo nao chega, a mensagem seguinte empurra o marco
    para frente e o link fica atras da janela para sempre.

    Por isso, de tempos em tempos, a coleta relê largo. Reler de novo o que ja
    esta no banco NAO custa Strava: a deduplicacao acontece no site, antes de
    qualquer raspagem (regra da casa: pedir ao Strava so o necessario).

    Quando releem:
      - a rodada anterior nao publicou ranking -> volta ao 3o marco anterior;
      - dias 10 e 20, na rodada da MANHA -> desde o dia 1o do mes;
      - ultimo dia do mes (28, 29, 30 ou 31), na ultima rodada -> mes inteiro.

    A manha e de proposito nos dias 10 e 20: quem liga a maquina de manha e o
    alarme da BIOS, que nao depende do Wake-on-LAN.
    """
    agora = agora or int(time.time())
    hoje = datetime.fromtimestamp(agora)
    amanha = hoje + timedelta(days=1)
    de_manha = hoje.hour < 12

    if hoje.day in (10, 20) and de_manha:
        return ('dia %d do mes' % hoje.day), _inicio_do_mes(agora)
    if amanha.day == 1 and not de_manha:
        return 'ultimo dia do mes', _inicio_do_mes(agora)

    marcos = [m for m in (estado.get('marcos') or []) if m]
    if not estado.get('publicou_na_ultima') and len(marcos) >= 3:
        # RECUO POR TEMPO, NAO SO POR CONTAGEM DE MARCOS (27/08/2026, pedido do
        # dono). Tres marcos sao tres rodadas — com duas por dia, um dia e
        # meio. Em 26/08 a leitura estava parada ha DEZ HORAS num ponto que os
        # marcos nao alcancavam. Agora volta o que for MAIS ANTIGO entre o
        # terceiro marco e dois dias atras. Reler nao custa (a deduplicacao
        # acontece no site, antes de qualquer raspagem — regra 4-J); perder
        # link custa o mes do atleta.
        dois_dias = int(time.time()) - RECUO_SO_LOGO_SEG
        return ('a rodada anterior nao publicou ranking',
                min(int(marcos[2]), dois_dias))
    return None, 0


    # QUEM POSTOU — le 'participant' ANTES de qualquer outra coisa.
    #
    # Ate 05/08/2026 este trecho procurava 'author' e 'pushName'. O
    # WAHA 2026.7.1 nao manda mais nenhum dos dois em mensagem de
    # grupo (migracao para LID): quem postou vem em 'participant'.
    # Com os dois vazios, o codigo caia no ultimo recurso —
    # msg['from'] — que em grupo e o JID DO GRUPO. Resultado: TODO
    # link do grupo era atribuido a "55DDXXXXXXXXX-XXXXXXXXXX" (o
    # proprio grupo), nenhuma atividade era ligada ao atleta certo, o
    # ranking nunca mudava e a automacao concluia "nada mudou" e
    # mandava so os patrocinadores. Foi o que travou os dias 04 e
    # 05/08.
    from_me = msg.get('fromMe', False)
def _quem_postou(msg, mid=""):
    """Quem mandou esta mensagem, no formato que o TXT espera.

    EXTRAIDA de `etapa_coletar` em 27/08/2026 para o suplente usar A MESMA
    regra. Copiar seria garantir que as duas versoes divergissem — e foi
    exatamente uma divergencia dessas (procurar 'author' quando o WAHA passou
    a mandar 'participant') que travou os dias 04 e 05/08.
    """
    from_me = msg.get('fromMe', False)
    push = msg.get('pushName') or ''
    autor = (msg.get('participant') or msg.get('author') or '')
    if isinstance(autor, dict):          # ja veio como dict em ids
        autor = autor.get('_serialized') or autor.get('id') or ''
    if from_me:
        return 'Engekon'
    if autor:
        return _lid_para_telefone(autor)
    if push:
        return push
    # NUNCA cair para msg['from'] em grupo: e o grupo, nao a pessoa. Sem quem
    # postou, o link vira "desconhecido" e aparece no log — melhor perder um
    # do que atribuir errado.
    log.warning('   Mensagem %s sem quem postou (participant vazio) — link '
                'nao sera atribuido a ninguem.', mid)
    return 'desconhecido'


def _tem_strava(corpo):
    return ('strava.com/activities/' in corpo
            or 'strava.app.link' in corpo
            or 'strava.com/athletes/' in corpo)


def _canonico(msg):
    """Mensagem CRUA do WAHA -> a forma que a pipeline inteira usa.

    POR QUE (27/08/2026): `_unir_do_suplente` juntava a mensagem CRUA do
    suplente na lista do principal, que ja esta normalizada. O ensaio de hoje
    morreu em `etapa_montar_txt` com `KeyError: 'ts'` — e morreria de novo em
    `m['strava']` (linha do resumo) e no `max(m['ts'])` que grava o MARCO da
    proxima rodada. Uma unica porta de entrada resolve as tres.

    Devolve None para o que o proprio `etapa_coletar` ja descarta: sem horario
    ou sem texto (midia, evento de grupo).
    """
    if not isinstance(msg, dict):
        return None
    mid = msg.get('id', '')
    if isinstance(mid, dict):
        mid = mid.get('_serialized') or mid.get('id') or str(mid)
    ts = msg.get('timestamp', 0)
    corpo = (msg.get('body') or '').strip()
    if not ts or not corpo:
        return None
    return {
        'id': mid, 'ts': ts, 'sender': _quem_postou(msg, mid), 'body': corpo,
        'strava': _tem_strava(corpo),
        'timestamp': ts,
        'participant': msg.get('participant') or msg.get('author') or '',
        'fromMe': msg.get('fromMe', False),
    }


def etapa_coletar(force=False):
    """3/7 — Coleta mensagens do grupo via WAHA, paginando por timestamp."""
    log.info('>>> Etapa 3/7 — Coletando mensagens do grupo...')
    estado = load_state()
    ultimo_ts = 0 if force else estado.get('last_ts', 0)

    if force:
        since = int(time.time()) - FORCE_DIAS * 86400
        log.info('   Coleta forcada — ultimos %s dias (desde %s)', FORCE_DIAS, _ts_str(since))
    elif ultimo_ts == 0:
        since = int(time.time()) - JANELA_PADRAO_HORAS * 3600
        log.info('   Sem checkpoint — ultimas %sh (desde %s)', JANELA_PADRAO_HORAS, _ts_str(since))
    else:
        since = ultimo_ts - SOBREPOSICAO
        motivo, desde = varredura_de_seguranca(estado)
        if motivo and desde and desde < since:
            log.warning('   VARREDURA DE SEGURANCA: relendo desde %s (motivo: %s). '
                        'Link repetido e descartado antes do Strava.',
                        _ts_str(desde), motivo)
            since = desde
        else:
            log.info('   Checkpoint %s — coletando desde %s (overlap %ds)',
                     _ts_str(ultimo_ts), _ts_str(since), SOBREPOSICAO)

    # Zera a cada coleta: o que interessa e a falha DESTA leitura, nunca a
    # herdada da tentativa anterior.
    global _COLETA_ERRO_DEFINITIVO, _COLETA_DESDE
    _COLETA_ERRO_DEFINITIVO = None
    # De onde ESTA leitura comecou. Quem compara com o suplente precisa do mesmo
    # ponto de partida, senao compara janelas diferentes e acusa falta que nao ha.
    _COLETA_DESDE = int(since)

    todas, vistos, pagina = [], set(), 0
    while pagina < MAX_PAGINAS:
        try:
            resp = _session().get(
                f'{WAHA_BASE}/api/{WAHA_SESSION}/chats/{GROUP_ID}/messages',
                params={'limit': LIMITE_PAG, 'offset': pagina * LIMITE_PAG,
                        'downloadMedia': 'false', 'filter.timestamp.gte': since},
                headers=_waha_headers(), timeout=30)
            if resp.status_code != 200:
                # O CORPO DA RESPOSTA VAI JUNTO — o numero sozinho nao diz nada.
                #
                # Em 06/08 as duas rodadas da noite morreram aqui com HTTP 422,
                # 11 tentativas cada, e o log guardou so "HTTP 422 na pagina 1".
                # O WAHA responde 422 com um JSON dizendo O QUE recusou, e essa
                # frase foi jogada fora 22 vezes seguidas. Sobrou adivinhar
                # entre limite, offset, filtro de data e id do grupo — com a
                # rodada parada e ninguem sabendo por que.
                #
                # Todas as outras chamadas deste arquivo ja logam resp.text
                # (upload, publicacao, check-exists). Esta era a excecao, e era
                # justamente a que quebrou. Mesmo principio da regra 4-I: erro
                # se registra, nao se engole.
                log.warning('   HTTP %s na pagina %s — o WAHA respondeu: %s',
                            resp.status_code, pagina + 1,
                            (resp.text or '(corpo vazio)')[:400])
                log.warning('   Chamada que falhou: %s/api/%s/chats/%s/messages'
                            ' | limit=%s offset=%s filter.timestamp.gte=%s',
                            WAHA_BASE, WAHA_SESSION, GROUP_ID, LIMITE_PAG,
                            pagina * LIMITE_PAG, since)
                if _erro_sem_conserto(resp.status_code):
                    _COLETA_ERRO_DEFINITIVO = 'HTTP %s — %s' % (
                        resp.status_code, (resp.text or '')[:200] or 'sem detalhe')
                break
            msgs = resp.json()
            if not isinstance(msgs, list) or not msgs:
                log.info('   Fim da paginacao (pagina %s)', pagina + 1)
                break

            novas = 0
            for msg in msgs:
                mid = msg.get('id', '')
                if isinstance(mid, dict):
                    mid = mid.get('_serialized') or mid.get('id') or str(mid)
                ts = msg.get('timestamp', 0)
                corpo = (msg.get('body') or '').strip()

                # A MAIS NOVA QUE O WAHA ENTREGOU, ANTES DE QUALQUER FILTRO.
                # Serve so para comparar com a lista de conversas
                # (`ha_mensagem_mais_nova`) — e a comparacao tem de ser entre
                # iguais. Medido em 12/08: a ultima mensagem do grupo era uma
                # MIDIA (corpo vazio), que este laco descarta com razao. Comparar
                # a lista contra a mais nova APROVEITAVEL acusava falta de
                # sincronizacao a cada foto postada — e num grupo de 80 pessoas
                # isso e o tempo todo. Falso alarme que trava ranking e pior que
                # nao ter conferencia nenhuma.
                global _TS_BRUTO_MAX
                if ts and ts > _TS_BRUTO_MAX:
                    _TS_BRUTO_MAX = ts

                if not ts or not corpo or mid in vistos:
                    continue
                vistos.add(mid)
                novas += 1

                remetente = _quem_postou(msg, mid)

                # OS CAMPOS CRUS FICAM JUNTO (27/08/2026). A comparacao com o
                # suplente (`_e_evento_de_sistema`, `_assinatura`, `_sem_par`)
                # le `timestamp`/`participant`/`fromMe`. Jogar isso fora aqui
                # fazia a comparacao classificar TODA mensagem do principal
                # como evento de sistema: no ensaio de hoje ela disse "o
                # principal viu 0 e o suplente viu 30" com o principal tendo
                # lido 25 de verdade. Guardar o cru custa nada e mantem UMA
                # forma so circulando na pipeline.
                todas.append({
                    'id': mid, 'ts': ts, 'sender': remetente, 'body': corpo,
                    'strava': _tem_strava(corpo),
                    'timestamp': ts,
                    'participant': msg.get('participant') or msg.get('author') or '',
                    'fromMe': msg.get('fromMe', False),
                })
            log.info('   Pagina %s: %s mensagens (%s novas)', pagina + 1, len(msgs), novas)
            if len(msgs) < LIMITE_PAG:
                break
            pagina += 1
        except Exception as exc:
            log.error('   Erro na pagina %s: %s', pagina + 1, exc)
            break

    todas.sort(key=lambda item: item['ts'])
    if todas:
        com_link = [m for m in todas if m['strava']]
        log.info('   %s mensagens (%s com Strava) — de %s a %s',
                 len(todas), len(com_link),
                 _ts_str(todas[0]['ts']), _ts_str(todas[-1]['ts']))

        # O BURACO SE MEDE, NAO SE ADIVINHA (09/09/2026).
        #
        # Pedimos as mensagens A PARTIR de `since`. Se a MAIS ANTIGA que voltou
        # for mais nova que isso, o que existiu no meio nao veio -- e nao vem
        # mais: o checkpoint avanca e ninguem volta la.
        #
        # Foi exatamente assim que os links de Dani Exemplo (5,04 km) e Edu
        # Exemplo (4,89 km) se perderam em 07/09: a sessao foi repareada as
        # 10:28 e a coleta rodou as 10:35, seis minutos depois. Pedimos desde
        # 07/09 13:03 e a mais antiga que veio foi 07/09 19:52 -- quase cinco
        # horas de grupo que nunca foram lidas. O log dizia "22 mensagens" e
        # parecia normal.
        #
        # Nao ha numero fixo a conferir: o motor WEBJS entrega o que o WhatsApp
        # manda, e a documentacao do WAHA nao promete quantidade nenhuma para
        # ele (os limites configuraveis sao do GOWS). Por isso a conta e' entre
        # o que PEDIMOS e o que CHEGOU -- isso vale para qualquer engine.
        _falta = int(todas[0]['ts']) - int(since)
        if _falta > 600:            # 10 min de folga: sincronizacao normal
            log.warning('   ATENCAO: pedi desde %s e a mensagem mais antiga que '
                        'chegou e de %s.', _ts_str(since), _ts_str(todas[0]['ts']))
            log.warning('   Ficaram %s sem leitura. Se alguem postou link nessa '
                        'janela, ele NAO entrou e nao entra depois.',
                        _humano_horas(_falta / 3600.0))
            log.warning('   O que resolve: exporte a conversa do grupo no celular '
                        'e envie o arquivo em Upload de conversa - de la o mes '
                        'inteiro e relido.')
            globals()['_BURACO_SEG'] = _falta
        # Cada link aparece no log com data e autor. Sem isto, "o link nao
        # somou" vira discussao sem prova: agora da para abrir o log do dia e
        # ver se o robo enxergou ou nao.
        for m in com_link:
            log.info('      LINK %s | %s | %s',
                     _ts_str(m['ts']), m['sender'][:24], m['body'][:70])
    else:
        log.info('   Nenhuma mensagem nova no periodo')
    return todas


def _idade_da_coleta(mensagens):
    """Ha quantos minutos foi a mensagem mais nova que conseguimos ler."""
    if not mensagens:
        return None
    return (time.time() - max(m['ts'] for m in mensagens)) / 60.0


def _impressao_da_coleta(mensagens):
    """Assinatura do que foi lido: (quantas, timestamp da mais nova).

    Serve para responder "chegou alguma coisa nova entre uma tentativa e
    outra?". Duas leituras com a mesma assinatura significam que a
    sincronizacao parou de trazer coisa — que e o sinal de que ela terminou.
    """
    if not mensagens:
        return (0, 0)
    return (len(mensagens), max(m['ts'] for m in mensagens))


def idade_do_container():
    """Ha quantos segundos o container do WAHA subiu, ou None se nao der.

    E o relogio certo para julgar sincronizacao: o WhatsApp Web comeca a
    sincronizar quando o container sobe. Devolver None (docker indisponivel,
    WAHA remoto) faz o chamador simplesmente nao aplicar a espera — nunca
    derrubar a rodada por causa da medicao.
    """
    cmd = ['docker', 'inspect', WAHA_CONTAINER, '--format', '{{.State.StartedAt}}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        log.debug('idade_do_container: %s', exc)
        return None
    if r.returncode != 0:
        return None
    bruto = (r.stdout or '').strip()
    if not bruto:
        return None
    # 2026-08-01T19:22:22.358137985Z -> corta os nanossegundos, que o Python
    # nao entende, e trata como UTC.
    texto = bruto.replace('Z', '')
    if '.' in texto:
        inteiro, fracao = texto.split('.', 1)
        texto = inteiro + '.' + fracao[:6]
    try:
        subiu = datetime.strptime(texto, '%Y-%m-%dT%H:%M:%S.%f')
    except ValueError:
        try:
            subiu = datetime.strptime(texto, '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return None
    return (datetime.utcnow() - subiu).total_seconds()


def idade_do_waha():
    """Ha quantos segundos ESTE WAHA esta de pe. None quando nao da para medir.

    Pergunta ao proprio WAHA (GET /api/server/status) antes de perguntar ao
    docker, por tres motivos: quem sincroniza e o PROCESSO, nao o container;
    funciona sem acesso ao docker; e vale para WAHA remoto, que e para onde este
    projeto vai quando sair do WSL. O docker fica como reserva — se o WAHA nao
    respondeu, a idade do container ainda diz alguma coisa.
    """
    idade = prontidao.uptime_do_waha(WAHA_BASE, WAHA_API_KEY)
    if idade is not None:
        return idade
    return idade_do_container()


def etapa_prontidao():
    """2.5/7 — cada peca diz OK antes de a rodada comecar.

    POR QUE ISTO EXISTE, SE A ETAPA 2 JA CONFERIU A SESSAO
    A etapa 2 responde "a sessao esta de pe". Aqui a pergunta e outra e maior:
    da para LER? Publicar e ler nao sao a mesma coisa no WEBJS — o /sendText
    funciona assim que o socket conecta, e a leitura so depois que o banco local
    do WhatsApp Web se completa. Em 01/08 o robo publicou o ranking no grupo e
    nao enxergou o link que estava la; de fora, parecia que ele tinha ignorado.

    A checagem pergunta pelo grupo de verdade (prontidao etapa 6) e confere de
    passagem a copia gravada, a saude do servidor e o uptime. Falhar aqui NAO
    publica nada: apenas avisa e para.
    """
    global WAHA_SESSION
    log.info('>>> Etapa 2.5/7 — Conferindo se tudo esta pronto...')
    rel = prontidao.esperar(
        WAHA_BASE, WAHA_API_KEY,
        preferida=WAHA_SESSION, grupo=GROUP_ID, container=WAHA_CONTAINER,
        limite_seg=PRONTIDAO_LIMITE_SEG, intervalo_seg=PRONTIDAO_INTERVALO_SEG,
        log=log.info)
    if rel['ok']:
        log.info('   Tudo pronto em %s conferencia(s).', rel.get('voltas', 1))
        # A RODADA SEGUE PELA SESSAO QUE LEU (17/09/2026). A conferencia prova a
        # leitura e, se a escolhida nao ler, aprova a outra sessao que le. Sem
        # esta troca, a coleta e a publicacao continuavam na sessao que ja
        # tinha falhado — foi o que deixou o ranking de 17/09 21:33 de fora.
        aprovada = rel.get('sessao')
        if aprovada and not wahasessao.mesmo_nome(aprovada, WAHA_SESSION):
            log.warning('   A sessao "%s" nao leu o grupo; a rodada segue pela '
                        'sessao "%s", que leu — coleta E publicacao.',
                        WAHA_SESSION, aprovada)
            WAHA_SESSION = aprovada
        return True, rel
    log.error('   NAO esta pronto: %s.', ', '.join(rel['impedem']))
    return False, rel


def esperar_o_container_aquecer():
    """Se o WAHA subiu agora, da tempo de a sincronizacao acontecer.

    Devolve ha quantos segundos o container estava de pe QUANDO CHEGAMOS (ou
    None se nao deu para medir). Quem chama usa isso para decidir o tamanho da
    vigilia: nao e a mesma coisa chegar num WAHA que subiu ha 40 segundos e num
    que esta de pe ha 3 horas.

    Nao existe flag de "historico sincronizado" — nem no WAHA (os estados param
    em WORKING, que quer dizer "pronto para usar") nem no whatsapp-web.js, que
    tem issue aberta pedindo esse indicador. Como nao ha a quem perguntar,
    esperamos por relogio.
    """
    idade = idade_do_waha()
    if idade is None:
        log.info('   (nao deu para medir ha quanto tempo o WAHA subiu — '
                 'seguindo sem a espera de aquecimento)')
        return None
    if idade >= SYNC_CONTAINER_MIN_SEG:
        # PISO, nao maturidade. Aqui so dizemos que a sincronizacao ja teve
        # CHANCE de comecar — nao que ela terminou. Quem decide se terminou e o
        # _janela_de_observacao, com um limiar bem maior. Escrever "tempo
        # suficiente para ter sincronizado" aqui fazia o log se contradizer duas
        # linhas depois.
        log.info('   WAHA de pe ha %s — passou do minimo de %s, nao ha '
                 'espera de arranque.', prontidao.duracao(idade),
                 prontidao.duracao(SYNC_CONTAINER_MIN_SEG))
        return idade
    falta = SYNC_CONTAINER_MIN_SEG - idade
    log.warning('   O WAHA subiu ha so %s. O WhatsApp Web comeca a '
                'sincronizar quando o container sobe, entao vou esperar mais '
                '%s antes de confiar no que ler.',
                prontidao.duracao(idade), prontidao.duracao(falta))
    notificar_n8n('aguardando_sincronizacao',
                  'WAHA subiu ha %.0fs; esperando %.0fs para o WhatsApp Web '
                  'sincronizar antes de montar o ranking.' % (idade, falta),
                  nivel='info')
    time.sleep(falta)
    log.info('   Espera cumprida — %ss desde que o WAHA subiu.',
             SYNC_CONTAINER_MIN_SEG)
    return idade


def _janela_de_observacao(idade_na_chegada):
    """(quantas leituras iguais, quantos segundos observando) — o tamanho da
    vigilia, conforme ha quanto tempo o WAHA estava de pe quando chegamos.

    POR QUE DUAS JANELAS E NAO UMA
    A vigilia longa existe por um motivo real: conectar e reconectar o WhatsApp
    Web leva tempo, e logo depois de o container subir a leitura vem incompleta
    sem avisar. Enquanto isso pode estar acontecendo, esperar e barato perto de
    publicar um ranking faltando link.

    So que esse motivo tem prazo. Em 06/08 01:40 o WAHA estava de pe ha 189 min
    e a leitura veio IGUAL desde a primeira tentativa; as 9 leituras seguintes
    custaram 164 segundos e nao mudaram nada. Repetir leitura nao ACELERA
    sincronizacao — o WAHA nao expoe nada que force o carregamento (o
    whatsapp-web.js tem syncHistory internamente, o WAHA nao publica). Insistir
    e vigilia, e vigiar o que ja terminou e so atraso.

    Entao: container recem-subido leva a vigilia inteira; container maduro leva
    confirmacao. Quando nao deu para medir a idade, vale a vigilia inteira — na
    duvida, espera.
    """
    if idade_na_chegada is None:
        log.info('   Sem medida da idade do WAHA — usando a espera inteira '
                 '(%s leituras iguais, %ss observando).',
                 SYNC_ESTAVEL_N, SYNC_AQUECIMENTO_SEG)
        return SYNC_ESTAVEL_N, SYNC_AQUECIMENTO_SEG
    if idade_na_chegada >= SYNC_MADURO_SEG:
        log.info('   WAHA maduro (de pe ha %s): a sincronizacao acabou faz '
                 'tempo. Basta confirmar — %s leituras iguais, %s observando.',
                 prontidao.duracao(idade_na_chegada), SYNC_ESTAVEL_MADURO_N,
                 prontidao.duracao(SYNC_AQUECIMENTO_MADURO_SEG))
        return SYNC_ESTAVEL_MADURO_N, SYNC_AQUECIMENTO_MADURO_SEG
    log.info('   WAHA subiu ha pouco (de pe ha %s): conexao e reconexao ainda '
             'podem estar trazendo mensagem. Espera inteira — %s leituras '
             'iguais, %s observando.', prontidao.duracao(idade_na_chegada),
             SYNC_ESTAVEL_N, prontidao.duracao(SYNC_AQUECIMENTO_SEG))
    return SYNC_ESTAVEL_N, SYNC_AQUECIMENTO_SEG


def coletar_com_sincronizacao(force=False):
    """Coleta e SO devolve quando o que leu parece estar atualizado.

    O PROBLEMA QUE ISTO RESOLVE
    Publicar e ler nao sao a mesma coisa no WEBJS. Publicar e uma acao: assim
    que o socket conecta, o /api/sendText funciona. Ler e consulta ao banco
    local do WhatsApp Web, que so fica completo depois que a sincronizacao
    termina de trazer o periodo em que a maquina esteve desligada.

    Em 01/08 isso apareceu do jeito mais confuso possivel: o robo publicou o
    ranking no grupo (acao) e nao enxergou o link que estava la (consulta).
    Quem olha de fora conclui que ele "ignorou" o link.

    Aqui a gente insiste: se a mensagem mais nova que conseguimos ler for
    velha demais, provavelmente a sincronizacao ainda esta em andamento —
    espera e tenta de novo. Devolve (mensagens, confiavel).

    TRES SINAIS, NAO UM
    Julgar so pela IDADE trava a rodada em grupo de pouco movimento: se ninguem
    posta desde as 13h, as 17h a mensagem mais nova tem 4 horas e a coleta e
    recusada todo dia, para sempre, mesmo estando perfeita.

    Julgar pela ESTABILIDADE sozinha engana do jeito oposto: logo depois de o
    container subir, o WhatsApp Web ainda nem comecou a sincronizar, e duas
    leituras seguidas dao o mesmo conjunto INCOMPLETO. Publicar ai seria um
    ranking faltando link — e ninguem perceberia.

    Por isso sao tres, e as tres precisam bater:
      1. IDADE dentro da tolerancia  -> aceita na hora (banco em dia)
      2. N leituras IGUAIS seguidas  -> parou de chegar coisa
      3. TEMPO minimo observando     -> deu tempo de comecar e terminar

    E qualquer crescimento na leitura ZERA a contagem: se a sincronizacao
    comecar tarde, a espera recomeca do zero.

    O TAMANHO DA ESPERA DEPENDE DE HA QUANTO TEMPO O WAHA ESTA DE PE
    Ver _janela_de_observacao. Container recem-subido leva a vigilia inteira,
    porque e ali que a leitura vem incompleta; container de pe ha horas leva so
    confirmacao, porque ali nao ha mais o que chegar.

    NAO existe no WAHA um indicador de "sincronizacao concluida" nem endpoint
    para FORCAR a sincronizacao. Este desenho e por observacao justamente porque
    nao ha a quem perguntar nem o que apertar.
    """
    # Antes de qualquer leitura: o WAHA teve tempo de sincronizar?
    try:
        idade_na_chegada = esperar_o_container_aquecer()
    except Exception as exc:
        log.info('   (espera de aquecimento falhou, seguindo: %s)', exc)
        idade_na_chegada = None

    estavel_n, aquecimento = _janela_de_observacao(idade_na_chegada)

    mensagens = []
    anterior = None
    iguais = 0                       # leituras identicas seguidas
    comecou = time.time()

    # QUANTAS TENTATIVAS SAO PRECISAS PARA O CRITERIO SER ALCANCAVEL
    # Em 01/08 a rodada das 22h abortou com a leitura perfeita porque o criterio
    # pedia 150s de observacao e as 4 tentativas do .env davam 62s. Nunca mais:
    # o laco calcula o proprio minimo, entao configurar SYNC_TENTATIVAS baixo
    # deixa de ser capaz de criar uma condicao impossivel.
    #
    # O TETO E FOLEGO, A JANELA E CRITERIO DE SAIDA — sao coisas diferentes.
    # O teto sai SEMPRE da vigilia inteira, mesmo quando a janela escolhida foi
    # a curta. Encolher os dois juntos tiraria tentativa exatamente do caso em
    # que insistir vale a pena: a leitura que ainda esta MUDANDO. Com a janela
    # curta a rodada sai na 2a tentativa de qualquer jeito; o teto so importa
    # quando ela nao sai, e ai quanto mais folego, melhor.
    minimo = SYNC_ESTAVEL_N + int(math.ceil(
        float(SYNC_AQUECIMENTO_SEG) / max(SYNC_ESPERA_SEG, 1)))
    tentativas_max = max(SYNC_TENTATIVAS, minimo)
    if tentativas_max > SYNC_TENTATIVAS:
        log.info('   (SYNC_TENTATIVAS=%s nao alcancaria %ss de observacao — '
                 'usando %s tentativas)', SYNC_TENTATIVAS,
                 SYNC_AQUECIMENTO_SEG, tentativas_max)

    for tentativa in range(1, tentativas_max + 1):
        mensagens = etapa_coletar(force=force)

        # UM CONSERTO POR PROBLEMA, NUNCA EM LACO (regra 16-E). Se o WAHA
        # recusou o pedido (4xx), repetir da no mesmo e so atrasa o alerta: em
        # 06/08 foram 11 tentativas identicas com HTTP 422, quatro minutos para
        # terminar exatamente onde a primeira ja tinha terminado. Sair agora
        # deixa o motivo no topo do log, onde alguem enxerga.
        if _COLETA_ERRO_DEFINITIVO:
            log.error('   O WAHA RECUSOU o pedido de leitura: %s',
                      _COLETA_ERRO_DEFINITIVO)
            log.error('   Isso nao melhora repetindo — parando na tentativa '
                      '%s de %s em vez de gastar as outras.',
                      tentativa, tentativas_max)
            return mensagens, False

        idade = _idade_da_coleta(mensagens)
        atual = _impressao_da_coleta(mensagens)
        observando = time.time() - comecou

        if atual == anterior:
            iguais += 1
        else:
            # CRESCEU (ou mudou): a sincronizacao esta trabalhando agora.
            # Zerar aqui e o que protege do falso positivo "comecou tarde":
            # no instante em que chega mensagem, a contagem recomeca do zero.
            if anterior is not None:
                log.info('   A leitura mudou (%s -> %s mensagens) — a '
                         'sincronizacao esta trazendo coisa. Recomecando a '
                         'contagem de estabilidade.', anterior[0], atual[0])
            iguais = 1

        # A LISTA DE CONVERSAS SABE DE ALGO QUE EU NAO LI? (ver
        # `ha_mensagem_mais_nova`). Isto vem ANTES de qualquer criterio de
        # aceite: leitura com pendencia conhecida nao e leitura pronta, por
        # mais recente ou mais estavel que pareca.
        ts_lido = _TS_BRUTO_MAX
        pendente, ts_lista = ha_mensagem_mais_nova(ts_lido)
        if pendente:
            log.warning('   A lista de conversas ja tem mensagem de %s e eu so '
                        'li ate %s — falta chegar. NAO aceito esta leitura.',
                        _ts_str(ts_lista), _ts_str(ts_lido) if ts_lido else '(nada)')
            iguais = 0            # pendencia conhecida zera a estabilidade
            anterior = atual
            if tentativa < tentativas_max:
                time.sleep(min(SYNC_ESPERA_SEG * 3, SYNC_ESPERA_SEG))
                continue
            log.error('   A leitura continuou incompleta ate a ultima '
                      'tentativa. Nada sera publicado.')
            return mensagens, False

        if idade is not None and idade <= SYNC_TOLERANCIA_MIN:
            # Mensagem recente: nao ha o que discutir, o banco esta em dia.
            log.info('   Coleta atual: mensagem mais nova ha %.0f min.', idade)
            return mensagens, True

        if idade is None:
            # Nada no periodo. Pode ser verdade (grupo parado) ou banco vazio
            # por falta de sincronizacao. Nao da para distinguir daqui, e um
            # ranking montado sobre banco vazio e pior que ranking atrasado —
            # entao isto NUNCA vale como estavel.
            log.warning('   Tentativa %s/%s: nenhuma mensagem lida.',
                        tentativa, tentativas_max)
            iguais = 0
        elif iguais >= estavel_n and observando >= aquecimento:
            # A LEITURA PAROU DE MUDAR. Isso e tudo o que sabemos daqui.
            #
            # NAO dizemos mais "o grupo esta parado": o codigo nao tem como
            # saber isso, e afirma-lo escondeu a falha de 11/08 (regra 16-G).
            # Leitura parada tem duas causas indistinguiveis deste lugar —
            # grupo realmente sem movimento, ou sessao que nao recebe mais.
            #
            # 17/08/2026 (plano attach-tidy-globe, A1) — A CONFERENCIA DO
            # SUPLENTE SAIU DAQUI. `ler_com_suplente` comparava o overview do
            # suplente (que inclui EVENTO DE SISTEMA do grupo, sem
            # participant) contra o `_TS_BRUTO_MAX` do principal (que so tem
            # mensagem de verdade) — duas fontes de natureza diferente. Isso
            # deu falso positivo nas rodadas de 14:07 e 15:30 de 17/08 com as
            # duas sessoes saudaveis, e ainda mandava "parear de novo" como
            # conserto — o pior conselho possivel (regra 15-A/15-C: parear
            # apaga a sessao e fura o historico). Decisao do dono: nem toda
            # cidade tem suplente, e essa conferencia nao pode ser regra geral
            # da rodada. A leitura estavel e o que se tem; a segunda opiniao
            # do suplente agora e coisa da conferencia de links (A3), a pedido
            # e no calendario — nunca no caminho de publicar.
            global _COLETA_SUSPEITA_MIN, _COMPARACAO_RESUMO
            if idade > SYNC_TOLERANCIA_MIN:
                _COLETA_SUSPEITA_MIN = idade
                # 27/08/2026 — A SEGUNDA OPINIAO VOLTOU, CONSERTADA (ver o
                # bloco COMPARAR O QUE OS DOIS NUMEROS ENXERGAM). Leitura
                # parada em horario de movimento e exatamente a hora de
                # perguntar ao outro numero.
                faltantes, resumo = comparar_com_suplente(
                    _COLETA_DESDE, mensagens,
                    motivo='leitura parada ha %.0f min' % idade)
                if faltantes is None:
                    _COMPARACAO_RESUMO = {'texto': str(resumo),
                                          'indisponivel': True}
                    log.info('   Leitura parada ha %s. Sem segunda opiniao '
                             '(%s) — publicando com o que ha.',
                             prontidao.duracao(idade * 60), resumo)
                else:
                    _COMPARACAO_RESUMO = resumo
                    _marcar_comparacao()
                    if faltantes:
                        mensagens = _unir_do_suplente(mensagens, resumo)
                        log.warning('   O suplente viu %d mensagem(ns) que esta '
                                    'sessao nao viu — usando as duas leituras '
                                    'juntas. %s', len(faltantes),
                                    resumo['texto'])
                    else:
                        log.info('   Leitura parada ha %s, e o suplente '
                                 'concorda (%s) — o grupo esta mesmo parado.',
                                 prontidao.duracao(idade * 60), resumo['texto'])
            else:
                log.info('   Coleta ESTAVEL: %s leituras iguais em %.0fs de '
                         'observacao (%s mensagens, mais nova ha %.0f min).',
                         iguais, observando, len(mensagens), idade)
            return mensagens, True
        else:
            falta = []
            if iguais < estavel_n:
                falta.append('%s de %s leituras iguais' % (iguais, estavel_n))
            if observando < aquecimento:
                falta.append('%.0fs de %ss observando' % (observando, aquecimento))
            # A IDADE aqui e CONTEXTO, nao acusacao. Em grupo parado a mensagem
            # mais nova tem horas e isso e o estado normal do mundo — o log
            # antigo dizia "ha 410 min (limite 90), ainda nao da para confiar" e
            # quem lia entendia que algo estava errado. O que falta e so a
            # confirmacao de que a leitura parou de mudar.
            log.info('   Tentativa %s/%s: %s mensagens, mais nova ha %.0f min. '
                     'Confirmando a leitura — falta %s.',
                     tentativa, tentativas_max, len(mensagens), idade,
                     '; '.join(falta))

        anterior = atual
        if tentativa < tentativas_max:
            # ESPERA CRESCENTE (P-09). A espera fixa gasta o mesmo tempo nas
            # primeiras tentativas — quando a sincronizacao ainda esta chegando
            # e repetir rapido ajuda — e nas ultimas, quando ja e claro que o
            # problema nao e pressa. Crescendo devagar (1x, 1.5x, 2x... ate 3x)
            # o teto de tentativas rende mais paciencia sem custar mais
            # tentativa, que era o pedido da pendencia.
            espera = min(SYNC_ESPERA_SEG * 3,
                         int(SYNC_ESPERA_SEG * (1 + 0.5 * (tentativa - 1))))
            log.info('   Esperando %s antes de tentar de novo...',
                     prontidao.duracao(espera))
            time.sleep(espera)

    log.error('   A coleta nao ficou confiavel depois de %s tentativas.',
              tentativas_max)
    return mensagens, False


def etapa_montar_txt(mensagens):
    """4/7 — TXT no formato do export oficial: 'dd/mm/aaaa HH:MM - Nome: texto'."""
    log.info('>>> Etapa 4/7 — Montando TXT...')
    if not mensagens:
        log.warning('   Nada a processar')
        return None
    linhas = ['%s - %s: %s' % (_ts_str(m['ts']), m['sender'], m['body']) for m in mensagens]
    txt = '\n'.join(linhas).encode('utf-8')
    log.info('   TXT: %s linhas, %s bytes', len(linhas), len(txt))
    return txt


def etapa_upload(txt_bytes, periodo):
    """5/7 — POST /api/upload. Campo 'arquivo' + periodo AAAA-MM."""
    log.info('>>> Etapa 5/7 — Upload para a API (periodo %s)...', periodo)
    if not txt_bytes:
        return None
    try:
        arquivos = {'arquivo': ('conversa_waha.txt', BytesIO(txt_bytes), 'text/plain')}
        resp = _session().post(f'{API_URL}/api/upload', files=arquivos,
                               data={'periodo': periodo}, headers=_api_headers(), timeout=120)
        if resp.status_code == 200:
            job_id = resp.json().get('job_id')
            log.info('   OK — job %s', job_id)
            return job_id
        log.error('   Upload falhou: HTTP %s — %s', resp.status_code, resp.text[:300])
        return None
    except Exception as exc:
        log.error('   Erro no upload: %s', exc)
        return None


def etapa_poll(job_id):
    """6/7 — Acompanha o job ate done/error/cancelled."""
    log.info('>>> Etapa 6/7 — Processando (limite %ds)...', POLL_MAX_SEC)
    if not job_id:
        return None
    inicio, ultimo = time.time(), None
    while time.time() - inicio < POLL_MAX_SEC:
        try:
            resp = _session().get(f'{API_URL}/api/upload/{job_id}',
                                  headers=_api_headers(), timeout=15)
            if resp.status_code == 200:
                dados = resp.json()
                stage = dados.get('stage', '?')
                pct   = dados.get('pct', 0)
                msg   = (dados.get('message') or '')[:140]
                if (stage, pct) != ultimo:
                    log.info('   [%3d%%] %s — %s', int(pct or 0), stage, msg)
                    ultimo = (stage, pct)
                if stage == 'done':
                    log.info('   Processamento concluido')
                    return dados
                if stage in ('error', 'cancelled'):
                    log.error('   Job %s: %s', stage, msg)
                    return dados
            elif resp.status_code == 404:
                log.warning('   Job %s nao encontrado', job_id)
                return None
        except Exception as exc:
            log.warning('   Erro no polling: %s', exc)
        time.sleep(POLL_INTERVAL)
    log.error('   Timeout apos %ds', POLL_MAX_SEC)
    return None


def etapa_ranking(periodo=None):
    """7/7 — GET /api/ranking/latest. O texto pronto vem em 'texto_whatsapp'."""
    log.info('>>> Etapa 7/7 — Buscando ranking...')
    try:
        params = {'periodo': periodo} if periodo else None
        resp = _session().get(f'{API_URL}/api/ranking/latest', params=params,
                              headers=_api_headers(), timeout=30)
        if resp.status_code == 200:
            dados = resp.json()
            texto = (dados.get('texto_whatsapp') or '').strip()
            partes = [p for p in (dados.get('mensagens') or []) if (p or '').strip()]
            if partes:
                log.info('   OK — ranking em %s mensagens, %s patrocinador(es)',
                         len(partes), len(dados.get('patrocinadores') or []))
                return dados
            if texto:
                log.info('   OK — %s caracteres (formato antigo, sem partes), '
                         '%s patrocinador(es)', len(texto),
                         len(dados.get('patrocinadores') or []))
                return dados
            log.warning('   Ranking veio sem texto')
            return None
        if resp.status_code == 404:
            log.warning('   Nenhum ranking disponivel para o periodo')
            return None
        log.error('   HTTP %s — %s', resp.status_code, resp.text[:250])
        return None
    except Exception as exc:
        log.error('   Erro ao buscar ranking: %s', exc)
        return None


# ===== ENVIO =====
def _so_digitos(valor):
    return re.sub(r'\D', '', str(valor or ''))


# Cache de numero -> chatId. Cada consulta ao WAHA custa ~1s; numa rodada com
# 20 medalhistas isso importa.
#
# E EM ARQUIVO, NAO SO EM MEMORIA
# Em 01/08 a rodada abortou porque o check-exists devolveu 500 na sessao nova.
# O envio funcionava (o @lid estava certo), mas o script nao tinha como
# descobrir o @lid e caiu no '@c.us', que o WhatsApp recusa. Guardando em
# arquivo, um LID descoberto uma vez serve para sempre — inclusive quando o
# check-exists esta fora do ar, que e exatamente quando ele faz falta.
_JID_CACHE = {}
# O mesmo cache lido ao contrario (LID -> telefone), montado sob demanda. E o
# que traduz o 'participant' de volta para o numero de quem postou no grupo.
_LID_INVERSO = {}
_JID_ARQUIVO = Path(__file__).resolve().parent.parent / '.jid_cache.json'


def _carregar_jid_cache():
    """Le o cache do disco uma vez por execucao. Falha aqui nunca e fatal."""
    if _JID_CACHE:
        return
    try:
        if _JID_ARQUIVO.exists():
            dados = json.loads(_JID_ARQUIVO.read_text(encoding='utf-8'))
            if isinstance(dados, dict):
                _JID_CACHE.update({k: v for k, v in dados.items() if v})
                log.debug('cache de LID: %d numero(s) conhecidos', len(_JID_CACHE))
    except Exception as exc:
        log.debug('nao consegui ler o cache de LID: %s', exc)


def _gravar_jid_cache():
    """Grava o que aprendeu. Nunca derruba a rodada por causa disto."""
    try:
        guardar = {k: v for k, v in _JID_CACHE.items() if v and '@lid' in str(v)}
        if guardar:
            _JID_ARQUIVO.write_text(
                json.dumps(guardar, indent=2, ensure_ascii=False), encoding='utf-8')
    except Exception as exc:
        log.debug('nao consegui gravar o cache de LID: %s', exc)


def _lid_para_telefone(lid):
    """'<id>@lid' -> '+55 DD XXXX-XXXX' quando o LID e conhecido.

    POR QUE ISTO EXISTE (05/08/2026)
    O WAHA 2026.7.1 parou de mandar 'author' e 'pushName' nas mensagens de grupo
    (migracao para LID). Quem manda a mensagem vem em 'participant', no formato
    LID. Sem traduzir o LID de volta para o telefone, o autor de TODO link ficava
    sendo o proprio grupo — e nenhuma atividade era atribuida ao atleta certo.

    O cache do disco guarda telefone -> LID; aqui a gente le no sentido inverso.
    LID desconhecido devolve 'lid:<numero>', que e melhor que o ID do grupo: pelo
    menos distingue uma pessoa da outra e aparece na tela de telefones para o
    admin completar.
    """
    bruto = str(lid or '')
    if '@lid' not in bruto:
        return _jid_to_phone(bruto)
    _carregar_jid_cache()
    if not _LID_INVERSO:
        for telefone, guardado in _JID_CACHE.items():
            if guardado and '@lid' in str(guardado):
                _LID_INVERSO[str(guardado).split('@')[0]] = telefone
    numero_lid = bruto.split('@')[0]
    telefone = _LID_INVERSO.get(numero_lid)
    if telefone:
        return _jid_to_phone('%s@c.us' % telefone)
    return 'lid:%s' % numero_lid


def _texto_com_marcacoes(texto, numeros):
    """Troca cada "@telefone" do texto pelo "@id" que o WhatsApp usa. Devolve (texto, mentions).

    POR QUE (01/10/2026, o @ do aviso de medalhas no grupo nao marcava ninguem)
    O WhatsApp so desenha a marcacao quando o "@" do texto e o MESMO id da
    lista `mentions`. Hoje todo contato vira LID ("<id>@lid" — os 26
    do cache sao assim), e o site escreve "@55DD9XXXXXXXX" (o telefone). O
    `_enviar_texto` procurava "87608810049719" no texto, nao achava e mandava
    SEM `mentions`. O parabens funcionava porque ja monta o texto com o id
    (`enviar_parabens`). Aqui se faz o mesmo para a fila: na tela do WhatsApp
    aparece o nome da pessoa, nao o numero.
    """
    marcados = []
    for numero in numeros:
        numero = _so_digitos(numero)
        if not numero:
            continue
        jid = _jid_contato(numero)
        if not jid or jid in marcados:
            continue
        marcados.append(jid)
        usuario = jid.split('@')[0]
        variantes = {numero, numero[2:] if numero.startswith('55') else numero}
        for v in variantes:
            texto = re.sub(r'@%s(?!\d)' % re.escape(v), '@' + usuario, texto)
    return texto, (marcados or None)


def _jid_contato(telefone):
    """Telefone -> chatId que o WhatsApp aceita HOJE.

    POR QUE NAO BASTA MONTAR '5563...@c.us'
    O WhatsApp passou a identificar contatos por LID (algo como
    '<id>@lid'). Mandar para o '@c.us' desses contatos devolve
    HTTP 500 do WAHA — foi exatamente por isso que as mensagens de GRUPO
    chegavam e as de PRIVADO nunca chegavam: o grupo tem id proprio e nao
    passava por aqui.

    Entao a gente pergunta ao WAHA qual e o id de verdade. Se a consulta
    falhar (WAHA fora do ar, versao antiga sem a rota), cai no '@c.us' de
    sempre — melhor tentar do jeito antigo do que nao enviar.
    """
    numero = _so_digitos(telefone)
    if not numero:
        return None
    if not numero.startswith('55'):
        numero = '55' + numero
    if not 12 <= len(numero) <= 13:
        return None

    _carregar_jid_cache()
    if numero in _JID_CACHE:
        return _JID_CACHE[numero]

    reserva = f'{numero}@c.us'
    try:
        resp = _session().get(
            f'{WAHA_BASE}/api/contacts/check-exists',
            headers=_waha_headers(),
            params={'phone': numero, 'session': WAHA_SESSION},
            timeout=25)
        if resp.status_code == 200:
            dados = resp.json() or {}
            if not dados.get('numberExists'):
                log.warning('   %s nao tem WhatsApp — nao vou tentar enviar', numero)
                _JID_CACHE[numero] = None
                return None
            jid = (dados.get('chatId') or '').strip() or reserva
            _JID_CACHE[numero] = jid
            _gravar_jid_cache()
            return jid
        log.warning('   check-exists devolveu HTTP %s para %s', resp.status_code, numero)
    except Exception as exc:
        log.warning('   check-exists falhou para %s: %s', numero, exc)

    # Chegou aqui: o WAHA nao soube dizer o id. NAO gravar o '@c.us' no cache —
    # ele e chute, e um chute gravado vira erro permanente. Devolvemos o
    # '@c.us' so para esta tentativa; na proxima o check-exists pode responder.
    log.warning('   Sem LID para %s — tentando por %s, que o WhatsApp costuma '
                'recusar. Se falhar, o numero precisa entrar no cache '
                '(%s).', numero, reserva, _JID_ARQUIVO.name)
    return reserva


# ===== A LISTA DE CONVERSAS SABE MAIS QUE A LEITURA? (11/08/2026) =====

def ha_mensagem_mais_nova(ts_lido):
    """A lista de conversas conhece mensagem mais nova do que a que eu li?

    A PERGUNTA DO DONO, e por que ela e a certa:
    "se a hora da ultima mensagem e diferente da ultima sincronizada, e porque
    tem coisa nova". Exatamente. Sao DUAS fontes dentro do WAHA, e elas se
    desencontram justamente quando a leitura falha:

      `/chats/<id>/messages`   -> a colecao de mensagens daquela conversa.
                                  E ela que congela quando a sincronizacao para.
      `/chats/overview?ids=`   -> a LISTA de conversas, que carrega so a
                                  ultima mensagem de cada uma.

    O WhatsApp Web carrega a lista de conversas antes do historico completo de
    cada conversa. Entao a lista pode ja saber que chegou mensagem nova
    enquanto a colecao daquela conversa ainda nao a tem. Comparar as duas custa
    **0,045 s** (medido em 12/08 com `?ids=`, devolvendo so o grupo) e nao
    envia nada.

    POR QUE NAO E O "ECO" QUE EU TINHA FEITO ANTES: aquele publicava e conferia
    se a propria publicacao voltava. Funcionava, mas usava o GRUPO como
    instrumento de teste — e o grupo tem 80 pessoas. Regra do dono (12/08):
    **dentro do grupo, so o necessario.** Esta versao nao escreve nada.

    ➜ LIMITE HONESTO, para nao virar falsa seguranca: se o navegador inteiro
      congelar, as duas fontes congelam juntas e esta conferencia nao acusa
      nada. Ela pega a sincronizacao PARCIAL (a mais comum), nao a morte total.
      Quem pega a morte total e uma segunda leitura independente — o suplente
      (regra 16-G). Ver P-65.

    Devolve (True|False|None, timestamp_da_lista). None = nao deu para saber, e
    `nao sei` nunca reprova sozinho.
    """
    try:
        resp = _session().get(
            f'{WAHA_BASE}/api/{WAHA_SESSION}/chats/overview',
            params={'ids': GROUP_ID}, headers=_waha_headers(), timeout=20)
        if resp.status_code != 200:
            log.debug('   (overview: HTTP %s)', resp.status_code)
            return None, None
        for chat in (resp.json() or []):
            if (chat or {}).get('id') != GROUP_ID:
                continue
            ultima = ((chat or {}).get('lastMessage') or {}).get('timestamp')
            if not ultima:
                return None, None
            return int(ultima) > int(ts_lido or 0), int(ultima)
        return None, None
    except Exception as exc:
        log.debug('   (overview falhou: %s)', exc)
        return None, None


def conferir_entrega(chat_id, quantas=4, espera=90):
    """ENVIADO NÃO É ENTREGUE. Devolve (entregues, paradas) do que saiu de nós.

    O `ack` de cada mensagem diz onde ela está:
        0 = ainda aqui   1 = no servidor do WhatsApp   2 = no aparelho   3 = lida

    POR QUE ISTO EXISTE (13/08/2026)
    O dono avisou que as 4 cobranças de medalha não chegaram. O log dizia
    "enviada", a conversa mostrava as 4 com `fromMe: True`, e eu li isso como
    entrega — o mesmo erro da regra 21-I, agora com a minha própria evidência.
    O `ack` mostrou a verdade: as quatro em **1** (paradas no servidor) e a
    mensagem da véspera em **2** (entregue). O código estava certo; o aparelho
    do destino é que não estava recebendo.

    Sem esta conferência o sistema é incapaz de distinguir "mandei" de
    "chegou", e some a diferença entre a automação estar funcionando e o
    ranking não chegar em ninguém.

    ESPERA CURTA DEMAIS ACUSA EM FALSO (13/08). A primeira versao media 12
    segundos e chamou de "nao entregue" mensagens que chegaram depois — o dono
    confirmou o recebimento minutos mais tarde. `ack: 1` recem-enviado e o
    estado NORMAL: o aparelho pode estar sem rede naquele instante e receber
    logo em seguida. Alarme falso ensina a ignorar alarme, entao a janela e
    generosa (90s) e so reclama do que continuou parado depois dela.

    GRUPO NAO SE CONFERE POR `ack` (15/08/2026). Em conversa privada, `ack: 2`
    quer dizer "chegou no aparelho". Em GRUPO, o WhatsApp so promove a mensagem
    para 2 quando ela alcanca TODOS os participantes — com 80 pessoas, boa
    parte com o celular fora de rede, isso demora horas ou nunca acontece.
    Conferir grupo com a regra do privado acusa em falso toda vez.
    ➜ Medido: as 14:05 a mensagem das medalhas chegou ao grupo (o dono viu na
    tela) e as 14:07 saiu um alerta dizendo "1 mensagem parada no servidor,
    nenhuma no aparelho". O envio estava certo; a conferencia e que estava
    perguntando a coisa errada. Para grupo, o teto honesto e `ack >= 1`.

    Nunca levanta excecao: nao saber e `(None, None)`, e nao saber nao alerta.
    """
    if not chat_id:
        return None, None
    # Em grupo, "aceito pelo servidor" e o maximo que da para verificar.
    minimo_entregue = 1 if str(chat_id).endswith('@g.us') else 2
    time.sleep(espera)
    try:
        resp = _session().get(
            f'{WAHA_BASE}/api/{WAHA_SESSION}/chats/{chat_id}/messages',
            params={'limit': quantas, 'downloadMedia': 'false'},
            headers=_waha_headers(), timeout=30)
        if resp.status_code != 200:
            return None, None
        entregues = paradas = 0
        for msg in (resp.json() or []):
            if not (msg or {}).get('fromMe'):
                continue
            ack = (msg or {}).get('ack')
            if ack is None:
                continue
            if int(ack) >= minimo_entregue:
                entregues += 1
            else:
                paradas += 1
        return entregues, paradas
    except Exception as exc:
        log.debug('   (conferencia de entrega falhou: %s)', exc)
        return None, None


def conferir_entrega_por_parte(chat_id, ids, espera=90):
    """(resultado, minimo_entregue) — resultado e [(indice, ack_ou_None), ...].

    17/08/2026, item B do plano do MVP. Igual a `conferir_entrega`, mas por
    ID em vez de agregado: cada parte do ranking tem o seu proprio veredito
    ("parte 1/3 — parou no servidor"), nao um numero unico misturando tudo.

    ``ack=None`` quer dizer "essa mensagem nao apareceu na janela lida" —
    trata como parada, mesma cautela da regra 8 (nao saber nao e "esta tudo
    bem"). Nunca levanta excecao: falha aqui vira "nao sei", nunca derruba
    a rodada.
    """
    if not chat_id or not ids:
        return [], 2
    minimo_entregue = 1 if str(chat_id).endswith('@g.us') else 2   # regra 16-L-A
    time.sleep(espera)
    recentes = {}
    try:
        resp = _session().get(
            f'{WAHA_BASE}/api/{WAHA_SESSION}/chats/{chat_id}/messages',
            params={'limit': max(len(ids) * 3, 10), 'downloadMedia': 'false'},
            headers=_waha_headers(), timeout=30)
        if resp.status_code == 200:
            for msg in (resp.json() or []):
                if not (msg or {}).get('fromMe'):
                    continue
                id_msg = _id_serializado((msg or {}).get('id'))
                if id_msg:
                    recentes[id_msg] = (msg or {}).get('ack')
    except Exception as exc:
        log.debug('   (conferencia por parte falhou: %s)', exc)

    resultado = []
    for indice, id_parte in enumerate(ids, 1):
        ack = recentes.get(id_parte)
        resultado.append((indice, int(ack) if ack is not None else None))
    return resultado, minimo_entregue


# ---------------------------------------------------------------------------
# COMPARAR O QUE OS DOIS NUMEROS ENXERGAM (27/08/2026 — P-65 / P-78-b)
# ---------------------------------------------------------------------------
#
# PARA QUE SERVE, e por que nao e a mesma coisa que a conferencia do Strava:
# esta aqui pega o defeito NA FONTE. Se o suplente viu mensagem que o principal
# nao viu, a coleta esta perdendo — e isso e prova, nao suspeita. Em 26/08 o
# suplente TINHA o link das 06:25 que o principal nunca recebeu, e ninguem
# perguntou a ele.
#
# POR QUE A VERSAO ANTERIOR FOI REMOVIDA EM 17/08 (e o que mudou agora):
# ela comparava o `overview` do suplente — que INCLUI evento de sistema do
# grupo — contra o `_TS_BRUTO_MAX` do principal, que so tem mensagem de
# verdade. Duas fontes de natureza diferente: deu falso positivo com as duas
# sessoes saudaveis e ainda mandava "parear de novo", o pior conselho possivel
# (15-A/15-C). Agora e MENSAGEM COM MENSAGEM, com o mesmo filtro de evento de
# sistema dos dois lados.

# De quantos em quantos dias a comparacao roda por conta propria.
COMPARACAO_DIAS = 10
# Quanto a coleta recua quando a rodada anterior saiu so com a logo. Dois dias:
# em 26/08 a leitura estava parada ha 10 horas e os tres marcos nao alcancavam.
RECUO_SO_LOGO_SEG = 2 * 24 * 3600

_MARCA_COMPARACAO = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '.ultima_comparacao')

_COMPARACAO_RESUMO = None
_ULTIMAS_MENSAGENS = None


def _comparacao_vencida(hoje=None):
    """Ja passaram COMPARACAO_DIAS desde a ultima comparacao?

    O PRAZO TEM MEMORIA, e essa e a licao da P-78-b: disparar por calendario
    ("todo dia 10 e 20") faz a checagem sumir quando a maquina nao liga naquele
    dia, e nada repoe. Aqui quem manda e quantos dias se passaram DE FATO.
    """
    hoje = hoje or datetime.now()
    try:
        with open(_MARCA_COMPARACAO, 'r', encoding='utf-8') as fh:
            quando = datetime.strptime(fh.read().strip()[:10], '%Y-%m-%d')
    except Exception:
        return True
    return (hoje.date() - quando.date()).days >= COMPARACAO_DIAS


def _marcar_comparacao(hoje=None):
    try:
        with open(_MARCA_COMPARACAO, 'w', encoding='utf-8') as fh:
            fh.write((hoje or datetime.now()).strftime('%Y-%m-%d'))
    except Exception as exc:
        log.debug('   (nao consegui gravar a marca da comparacao: %s)', exc)


def _e_evento_de_sistema(msg):
    """Entrou/saiu do grupo, mudou o assunto — nao e mensagem de gente."""
    if not isinstance(msg, dict):
        return True
    if msg.get('fromMe'):
        return False
    return not (msg.get('participant') or msg.get('author'))


def _assinatura(msg):
    """Como reconhecer a MESMA mensagem em duas instancias: QUANDO + O TEXTO.

    O `id` NAO serve: cada instancia gera o seu e nunca batem (medido em 26/08).

    E O AUTOR TAMBEM NAO (medido em 27/08, no teste): a mensagem que NOS
    enviamos chega ao principal como `fromMe` SEM autor, e ao suplente como
    mensagem de outro, COM o autor preenchido. Usar o autor fazia a mesma
    mensagem parecer duas — 9 falsas divergencias num teste de 48 horas, que
    virariam alarme falso. E exatamente o tipo de erro que derrubou a versao de
    17/08; nao vale repetir com outra roupa.

    Quando + texto basta: duas mensagens diferentes no mesmo segundo com o
    mesmo texto nao acontecem na pratica.
    """
    corpo = ' '.join((msg.get('body') or '').split())
    return (int(msg.get('timestamp') or 0), corpo[:160])


# Quanto os relogios das duas instancias podem discordar sobre a MESMA mensagem.
#
# MEDIDO EM 27/08/2026, e por pouco nao virou alarme falso diario: a mesma
# mensagem do ranking aparecia as 07:42:22 numa instancia e as 07:42:23 na
# outra. Cada uma carimba quando RECEBEU, nao quando foi enviada. Comparar por
# igualdade exata de segundo acusaria divergencia todo dia com tudo saudavel —
# o mesmo tipo de erro que derrubou a versao de 17/08, so que com outra roupa.
TOLERANCIA_RELOGIO_SEG = 120


def _sem_par(origem, outra):
    """As mensagens de `origem` que NAO tem correspondente em `outra`.

    Duas mensagens sao a mesma quando o texto e igual e o horario esta dentro
    de TOLERANCIA_RELOGIO_SEG. Cada correspondente e consumido uma vez: duas
    mensagens iguais de verdade (a logo repetida, por exemplo) continuam
    contando como duas.
    """
    disponiveis = {}
    for m in outra:
        corpo = ' '.join((m.get('body') or '').split())[:160]
        disponiveis.setdefault(corpo, []).append(int(m.get('timestamp') or 0))
    for lista in disponiveis.values():
        lista.sort()

    faltantes = []
    for m in origem:
        corpo = ' '.join((m.get('body') or '').split())[:160]
        quando = int(m.get('timestamp') or 0)
        candidatos = disponiveis.get(corpo) or []
        achou = None
        for i, ts in enumerate(candidatos):
            if abs(ts - quando) <= TOLERANCIA_RELOGIO_SEG:
                achou = i
                break
        if achou is None:
            faltantes.append((quando, corpo))
        else:
            candidatos.pop(achou)
    return faltantes


def _ler_mensagens_da_instancia(base, chave, sessao, desde_ts, limite=200):
    """Le o MESMO grupo numa instancia qualquer. So leitura (regra 16-J)."""
    try:
        resp = _session().get(
            '%s/api/%s/chats/%s/messages' % (base, sessao, GROUP_ID),
            params={'limit': limite, 'downloadMedia': 'false',
                    'filter.timestamp.gte': desde_ts},
            headers={'X-Api-Key': chave}, timeout=45)
        if resp.status_code != 200:
            log.info('   (comparacao: %s respondeu HTTP %s — %s)', sessao,
                     resp.status_code, (resp.text or '')[:160])
            return None
        dados = resp.json()
        return dados if isinstance(dados, list) else None
    except Exception as exc:
        log.info('   (comparacao: nao consegui ler %s: %s)', sessao, exc)
        return None


def comparar_com_suplente(desde_ts, mensagens_principal, motivo=''):
    """Os dois numeros estao vendo as mesmas mensagens?

    NAO ESCREVE NADA no grupo (regra 16-J) e DEGRADA COM ELEGANCIA: suplente sem
    sessao devolve `None` — "nao sei" nunca vira "esta tudo bem" (regra 16-A).

    Devolve (faltantes, resumo) ou (None, motivo) quando nao deu para perguntar.
    """
    try:
        env = wahasessao.carregar_env()
        instancias = [i for i in wahasessao.instancias(env)
                      if i.get('papel') == 'suplente']
        if not instancias:
            return None, 'nao ha instancia suplente configurada'
        inst = instancias[0]
        viva = wahasessao.instancia_viva(inst['base'], inst['chave'], env=env)
        if not viva or not viva.get('pronta'):
            # SUPLENTE FORA DO AR NAO E "ESTA TUDO IGUAL" (pedido do dono):
            # significa que a comparacao NAO ACONTECEU nesta rodada.
            return None, 'o suplente nao tem sessao de pe'

        do_suplente = _ler_mensagens_da_instancia(
            viva['base'], inst['chave'], viva['nome'], desde_ts)
        if do_suplente is None:
            return None, 'o suplente nao devolveu a conversa'

        # COMPARAR IGUAL COM IGUAL (27/08/2026). O principal chega aqui ja
        # normalizado por `etapa_coletar`, que descarta o que nao tem corpo:
        # figurinha, imagem, evento de grupo. O suplente chega CRU. Comparar os
        # dois assim contava toda figurinha como "mensagem que so o suplente
        # viu" — e `_conferir_teima` transforma isso em alerta no WhatsApp do
        # dono. No ensaio de hoje foram 4 figurinhas/imagens para 1 mensagem de
        # verdade. Passar o suplente pela MESMA porta (`_canonico`) resolve.
        sup = [c for c in (_canonico(m) for m in do_suplente)
               if c is not None and not _e_evento_de_sistema(c)]
        pri = [m for m in (mensagens_principal or [])
               if not _e_evento_de_sistema(m)]
        so_no_suplente = _sem_par(sup, pri)
        so_no_principal = _sem_par(pri, sup)

        resumo = ('o principal viu %d e o suplente viu %d mensagem(ns) no mesmo '
                  'periodo' % (len(pri), len(sup)))
        if so_no_suplente or so_no_principal:
            log.warning('   COMPARACAO: %s — %d so no suplente, %d so no '
                        'principal%s', resumo, len(so_no_suplente),
                        len(so_no_principal), (' (%s)' % motivo) if motivo else '')
        else:
            log.info('   Comparacao com o suplente: %s, e batem. %s',
                     resumo, motivo or '')
        return (list(so_no_suplente),
                {'texto': resumo, 'so_suplente': len(so_no_suplente),
                 'so_principal': len(so_no_principal),
                 'total_principal': len(pri), 'total_suplente': len(sup),
                 'brutas': sup})
    except Exception as exc:
        log.info('   (comparacao com o suplente falhou: %s)', exc)
        return None, str(exc)


def _unir_do_suplente(mensagens, resumo):
    """Junta o que so o suplente viu com o que esta sessao viu.

    A UNIAO, NUNCA A MENOR (pedido do dono, 26/08/2026): se um numero enxergou
    mais, o ranking fica com o mais. Perder link e o pior desfecho do sistema, e
    reler link repetido nao custa nada — a deduplicacao acontece no site, antes
    de qualquer raspagem (regra 4-J).
    """
    if not resumo or not resumo.get('brutas'):
        return mensagens
    ja_tenho = [m for m in (mensagens or []) if not _e_evento_de_sistema(m)]
    do_outro = [m for m in resumo['brutas'] if not _e_evento_de_sistema(m)]
    # Mesmo casamento com tolerancia da comparacao: sem ele, a mesma mensagem
    # com 1 segundo de diferenca entraria duas vezes no TXT.
    faltando = {(q, c) for q, c in _sem_par(do_outro, ja_tenho)}
    juntas = list(mensagens or [])
    for m in do_outro:
        chave = (int(m.get('timestamp') or 0),
                 ' '.join((m.get('body') or '').split())[:160])
        if chave not in faltando:
            continue
        # A CONVERSAO E OBRIGATORIA: o que sai daqui vai para `etapa_montar_txt`
        # (le `ts`/`sender`/`body`), para o resumo (le `strava`) e para o
        # `max(m['ts'])` que grava o MARCO. Mensagem crua quebrava os tres.
        canonica = _canonico(m)
        if canonica is not None:
            juntas.append(canonica)
    juntas.sort(key=lambda x: int(x.get('ts') or x.get('timestamp') or 0))
    return juntas


def _conferir_teima():
    """Ranking barrado por "km nao mudaram": sera que a leitura e que falhou?

    Roda DEPOIS de decidir a publicacao, entao nao atrasa nada. Se as duas
    sessoes divergirem, avisa dizendo o que foi MEDIDO — nunca "parece que".
    """
    estado = load_state()
    desde = max(int(estado.get('last_ts') or 0) - RECUO_SO_LOGO_SEG, 0)
    faltantes, resumo = comparar_com_suplente(
        desde, _ULTIMAS_MENSAGENS or [], motivo='ranking saiu so com a logo')
    _marcar_comparacao()
    if faltantes is None:
        log.info('   Sem segunda opiniao para tirar a teima (%s).', resumo)
        return
    if not faltantes:
        log.info('   As duas sessoes veem a mesma coisa (%s) — o grupo esta '
                 'mesmo sem link novo.', resumo['texto'])
        return
    log.warning('   DIVERGENCIA ENTRE OS DOIS NUMEROS: %s. %d mensagem(ns) so '
                'no suplente.', resumo['texto'], len(faltantes))
    partes = [
        'O ranking saiu so com a logo dos patrocinadores, e ao comparar os dois '
        'numeros de WhatsApp eles NAO viram a mesma coisa: %s. '
        '%d mensagem(ns) apareceram so no suplente.' % (resumo['texto'],
                                                        len(faltantes)),
        '',
        'O QUE ISSO QUER DIZER: uma das sessoes parou de receber mensagem nova. '
        'A causa mais provavel e que o WhatsApp Web sincroniza em lote ao subir '
        'e a leitura nao registra o que chegou enquanto ela esteve fora.',
        '',
        'O QUE FAZER, nesta ordem:',
        '1) Confira no grupo se no horario do ranking so chegou a logo.',
        '2) Se sim, desconecte e conecte o WhatsApp de novo pela tela de Sessao '
        'do WhatsApp.',
        '3) Os links nao se perdem: a conferencia do Strava, no site, recupera o '
        'que faltar.',
    ]
    notificar_admin('Coleta divergente', chr(10).join(partes))


def ler_com_suplente(ts_lido):
    """O SUPLENTE ve mensagem que eu nao vejo? (P-65)

    POR QUE ISTO E O UNICO TESTE QUE PEGA TUDO
    A conferencia da lista de conversas (`ha_mensagem_mais_nova`) pega
    sincronizacao PARCIAL — a coleta congelada enquanto a lista ja sabe do
    novo. Ela NAO pega o caso de 11/08: navegador inteiro morto, em que as
    duas fontes congelam juntas e concordam entre si. Duas fontes do mesmo
    Chromium nunca desempatam a morte desse Chromium.

    O suplente e outra CONTA, em outra INSTANCIA, com outro Chromium, lendo o
    MESMO grupo. Dois navegadores nao congelam no mesmo instante. Se ele ve
    mensagem mais nova que a minha, a minha leitura esta atrasada — e isso e
    prova, nao suspeita.

    NAO ESCREVE NADA (regra 16-J): so le a lista de conversas do suplente,
    que custa ~0,045 s. O grupo nao fica sabendo que esta conferencia existe.

    REQUISITO OPERACIONAL: o WhatsApp do suplente precisa ser MEMBRO do grupo.
    Se nao for, ele le uma conversa que nao tem, devolve vazio, e a resposta
    correta e `None` (nao sei) — nunca "esta tudo bem".

    DEGRADA COM ELEGANCIA, de proposito: suplente sem sessao, instancia fora do
    ar, grupo inacessivel -> `None`. `nao sei` nunca reprova sozinho (regra
    16-A), porque a cidade nao pode ficar sem ranking so porque a segunda
    opiniao nao estava disponivel.

    Devolve (True|False|None, timestamp_do_suplente):
      True  -> o suplente ve coisa mais nova: MINHA leitura esta atrasada.
      False -> o suplente nao ve nada mais novo: a minha leitura confere.
      None  -> nao deu para perguntar.
    """
    try:
        env = wahasessao.carregar_env()
        instancias = [i for i in wahasessao.instancias(env)
                      if i.get('papel') == 'suplente']
        if not instancias:
            log.info('   (segunda leitura: nao ha instancia suplente no .env)')
            return None, None

        inst = instancias[0]
        viva = wahasessao.instancia_viva(inst['base'], inst['chave'], env=env)
        if not viva or not viva.get('pronta'):
            log.info('   (segunda leitura: o suplente nao tem sessao de pe — '
                     'sem segunda opiniao nesta rodada)')
            return None, None

        resp = _session().get(
            '%s/api/%s/chats/overview' % (viva['base'], viva['nome']),
            params={'ids': GROUP_ID},
            headers={'X-Api-Key': inst['chave']}, timeout=20)
        if resp.status_code != 200:
            log.info('   (segunda leitura: o suplente respondeu HTTP %s)',
                     resp.status_code)
            return None, None

        for chat in (resp.json() or []):
            if (chat or {}).get('id') != GROUP_ID:
                continue
            ultima = ((chat or {}).get('lastMessage') or {}).get('timestamp')
            if not ultima:
                return None, None
            return int(ultima) > int(ts_lido or 0), int(ultima)

        # O suplente respondeu, mas nao conhece este grupo: quase sempre
        # significa que o numero dele NAO E MEMBRO. Isso e "nao sei", e o
        # log precisa dizer o motivo, senao alguem vai passar horas
        # procurando defeito onde falta um convite.
        log.warning('   (segunda leitura: o suplente nao enxerga este grupo — '
                    'o numero dele foi adicionado ao grupo?)')
        return None, None
    except Exception as exc:
        log.info('   (segunda leitura falhou: %s)', exc)
        return None, None


def _sessao_envio():
    """Sessao para FALAR COM O WAHA. Nao repete em cima de HTTP 500.

    POR QUE EXISTE (14/08/2026 — a fila travada de 13 para 14/08)
    O `_session()` normal repete quando vem 500 e, esgotadas as tentativas,
    LEVANTA `ResponseError` em vez de devolver a resposta. Todo o codigo daqui
    faz `if resp.status_code not in (200, 201): mandar so o texto` — e esse
    caminho NUNCA rodava, porque a excecao pulava por cima dele.

    Foi o que transformou "a foto nao foi" em "a mensagem inteira sumiu": a
    cobranca de medalha de um atleta tentou 3 vezes na madrugada, morreu, e o
    aviso — que e o que importa — nunca chegou a ser tentado como texto.

    500 do WAHA nao e engasgo de rede: e o whatsapp-web.js recusando o que
    mandamos. Repetir identico 3 vezes so gasta 13 segundos e esconde o codigo.
    Queda de conexao continua repetindo, que ai repetir resolve mesmo.
    """
    sess = requests.Session()
    retry = Retry(total=2, connect=2, read=0, status=0, backoff_factor=1)
    sess.mount('http://', HTTPAdapter(max_retries=retry))
    sess.mount('https://', HTTPAdapter(max_retries=retry))
    return sess


def _enviar_imagem(chat_id, url, legenda, rotulo=''):
    """Manda imagem com legenda. Devolve True/False e NUNCA levanta excecao.

    Devolver False em vez de estourar e o que garante que quem chamou consiga
    cair no texto puro. Perder a arte e aceitavel; perder o aviso, nao.
    """
    try:
        resp = _sessao_envio().post(
            f'{WAHA_BASE}/api/sendImage', headers=_waha_headers(True),
            json={'session': WAHA_SESSION, 'chatId': chat_id,
                  'caption': legenda,
                  'file': {'url': url, 'mimetype': 'image/png',
                           'filename': 'anexo.png'}},
            timeout=90)
        if resp.status_code in (200, 201):
            return True
        log.warning('   %simagem recusada (HTTP %s): %s',
                    (rotulo + ': ') if rotulo else '', resp.status_code,
                    (resp.text or '')[:160])
    except Exception as exc:
        log.warning('   %simagem falhou (%s)',
                    (rotulo + ': ') if rotulo else '', str(exc)[:160])
    return False


def _id_serializado(bruto):
    """Normaliza o id que o WAHA devolve (string, ou dict com _serialized) para comparar depois."""
    if isinstance(bruto, str):
        return bruto
    if isinstance(bruto, dict):
        return bruto.get('_serialized') or bruto.get('id') or ''
    return ''


def _enviar_texto(chat_id, texto, mentions=None, coletar_ids=None):
    """coletar_ids, se vier uma lista, recebe (id, texto_do_pedaco) de cada
    pedaco enviado com sucesso — usado por `etapa_enviar` para depois
    conferir a entrega parte por parte (item B do plano de 17/08)."""
    partes = _quebrar(texto)
    for indice, parte in enumerate(partes, 1):
        corpo = {'session': WAHA_SESSION, 'chatId': chat_id, 'text': parte}
        # A marcacao so vale no pedaco que realmente contem o "@numero".
        if mentions:
            marcados = [jid for jid in mentions if _so_digitos(jid).split('@')[0] in parte
                        or f'@{_so_digitos(jid)}' in parte]
            if marcados:
                corpo['mentions'] = marcados
        try:
            resp = _sessao_envio().post(
                f'{WAHA_BASE}/api/sendText', headers=_waha_headers(True),
                json=corpo, timeout=30)
        except Exception as exc:
            log.error('   Parte %d/%d nao saiu: %s', indice, len(partes),
                      str(exc)[:200])
            return False
        if resp.status_code not in (200, 201):
            log.error('   Parte %d/%d falhou: HTTP %s — %s',
                      indice, len(partes), resp.status_code, resp.text[:200])
            return False
        log.info('   Parte %d/%d enviada', indice, len(partes))
        if coletar_ids is not None:
            try:
                id_msg = _id_serializado((resp.json() or {}).get('id'))
            except Exception:
                id_msg = ''
            if id_msg:
                coletar_ids.append((id_msg, parte))
            else:
                log.debug('   (o WAHA nao devolveu id desta parte — sem como conferir a entrega dela)')
        if indice < len(partes):
            time.sleep(2)
    return True


def etapa_enviar(dados, destino=None, coletar_ids=None):
    """Publica o ranking.

    Prefere as 3 mensagens prontas que a API devolve em "mensagens" (corrida,
    ciclismo, fechamento). O WhatsApp corta mensagem longa com "Ler mais" e
    quem esta no fim da lista nao aparece; dividido, cada modalidade chega
    inteira. Se a API for antiga e nao mandar as partes, cai no texto unico.

    ``coletar_ids``, se vier uma lista, junta (id, texto) de cada mensagem
    realmente enviada — quem chama usa isso depois para conferir a entrega
    (regra 16-L: enviado nao e entregue) e reenviar so o que ficou parado.
    """
    chat = destino or destino_publicacao()
    partes = [p for p in (dados.get('mensagens') or []) if (p or '').strip()]
    if not partes:
        texto = (dados.get('texto_whatsapp') or '').strip()
        if not texto:
            log.warning('   Nada a enviar')
            return False
        log.info('>>> Enviando ranking (mensagem unica, %s caracteres)...', len(texto))
        return _enviar_texto(chat, texto, coletar_ids=coletar_ids)

    log.info('>>> Enviando ranking em %s mensagens...', len(partes))
    for indice, parte in enumerate(partes, 1):
        log.info('   Mensagem %s/%s (%s caracteres)', indice, len(partes), len(parte))
        if not _enviar_texto(chat, parte, coletar_ids=coletar_ids):
            log.error('   Falhou na mensagem %s/%s', indice, len(partes))
            return False
        if indice < len(partes):
            time.sleep(4)
    return True


def enviar_patrocinadores(patrocinadores, destino=None):
    """Publica a logo e a legenda de cada patrocinador — SEMPRE como IMAGEM.

    NAO TROQUE ISTO POR PREVIA DE LINK. Ja foi feito e ja foi desfeito:

    Em 06/08 o dono reclamou que a mesma arte chegava de novo a cada
    publicacao e enchia a galeria dele. A resposta foi mandar a arte como
    PREVIA DE LINK nas repeticoes (cartao com miniatura, sem download). Em
    09/08 ele reverteu: na conversa dele a previa chegou como **link pelado**,
    sem miniatura nenhuma. Faz sentido — a previa depende de o WhatsApp sair
    buscando a URL e conseguir montar o cartao, e isso nao esta sob nosso
    controle. Patrocinador paga para APARECER; arte que pode nao aparecer nao
    e uma opcao, e a galeria cheia e o preco combinado.

    Regra que fica: a logo do patrocinador na hora do ranking e ajuste
    consolidado. Se a repeticao voltar a incomodar, o caminho e mexer na
    FREQUENCIA (quantas vezes o ranking sai por mes), nunca no FORMATO.
    """
    chat = destino or destino_publicacao()
    if not patrocinadores:
        log.info('   Sem patrocinadores no periodo')
        return True

    log.info('>>> Enviando %s patrocinador(es) — imagem...', len(patrocinadores))

    ok = True
    for item in patrocinadores:
        legenda = item.get('legenda_completa') or item.get('nome') or ''
        logo    = item.get('logo_url')
        try:
            if logo and _enviar_imagem(chat, logo, legenda, item.get('nome') or ''):
                log.info('   Logo enviada: %s', item.get('nome'))
                time.sleep(3)
                continue
            if logo:
                log.warning('   Logo de %s nao foi — enviando so o texto',
                            item.get('nome'))
            ok = _enviar_texto(chat, legenda) and ok
            time.sleep(3)
        except Exception as exc:
            log.error('   Erro no patrocinador %s: %s', item.get('nome'), exc)
            ok = False

    return ok


def enviar_parabens(periodo):
    """Dispara os parabens de meta batida ainda pendentes (opcional, --parabens)."""
    log.info('>>> Verificando parabens pendentes...')
    try:
        resp = _session().get(f'{API_URL}/api/parabens', params={'periodo': periodo},
                              headers=_api_headers(), timeout=30)
        if resp.status_code != 200:
            log.warning('   /api/parabens HTTP %s', resp.status_code)
            return True
        pendentes = resp.json().get('parabens') or []
    except Exception as exc:
        log.error('   Erro ao buscar parabens: %s', exc)
        return True

    if not pendentes:
        log.info('   Nenhum parabens pendente')
        return True

    log.info('   %s parabens a enviar', len(pendentes))
    for item in pendentes:
        texto = item.get('mensagem') or ''
        mentions = None
        jid = _jid_contato(item.get('telefone'))
        if jid:
            # O WhatsApp so desenha a marcacao se o texto tiver o "@numero"
            # literal; o campo mentions apenas diz a quem ele se refere. Sem
            # os dois juntos a pessoa nao recebe notificacao.
            numero = jid.split('@')[0]
            texto = f'@{numero} {texto}'
            mentions = [jid]
            log.info('   Marcando %s (%s)', item.get('nome'), numero)
        else:
            log.info('   %s sem telefone no cadastro — parabens sem marcacao',
                     item.get('nome'))

        if not _enviar_texto(destino_publicacao(), texto, mentions=mentions):
            log.error('   Falha ao enviar parabens de %s', item.get('nome'))
            continue
        try:
            _session().post(f'{API_URL}/api/parabens/marcar', params={'periodo': periodo},
                            headers=_api_headers(),
                            json={'chave_atleta': item.get('chave_atleta'),
                                  'modalidade': item.get('modalidade')}, timeout=20)
            log.info('   Parabens de %s enviado e marcado', item.get('nome'))
        except Exception as exc:
            log.error('   Enviado mas nao marcado (%s): %s', item.get('nome'), exc)
        time.sleep(3)
    return True


def enviar_avisos(com_ranking=False, destino_teste=None):
    """Dispara os avisos programados que estao vencidos.

    A aplicacao web decide QUEM recebe e JA MONTA o texto de cada pessoa; aqui
    so entregamos e confirmamos. Cada confirmacao evita repetir no mesmo dia.
    """
    log.info('>>> Verificando avisos programados...')
    try:
        resp = _session().get(f'{API_URL}/api/avisos/pendentes',
                              params={'com_ranking': '1' if com_ranking else '0'},
                              headers=_api_headers(), timeout=60)
        if resp.status_code == 404:
            log.info('   API sem modulo de avisos — pulando')
            return True
        if resp.status_code != 200:
            log.warning('   /api/avisos/pendentes HTTP %s', resp.status_code)
            return True
        avisos = resp.json().get('avisos') or []
    except Exception as exc:
        log.error('   Erro ao buscar avisos: %s', exc)
        return True

    if not avisos:
        log.info('   Nenhum aviso vencido')
        return True

    for aviso in avisos:
        alvos = aviso.get('destinatarios') or []
        log.info('   Aviso %s "%s" — %s destinatario(s)',
                 aviso.get('codigo'), aviso.get('titulo'), len(alvos))
        imagem = aviso.get('imagem_url')
        for pessoa in alvos:
            # 26/08/2026 (P-105) — o site manda 'chat_id' pronto, mas monta
            # ele na mao (f'{telefone}@c.us'), o formato que o WhatsApp
            # recusa para contato com LID (ver _jid_contato acima, regra
            # 22-C). Resolve pelo mesmo caminho que ranking/parabens usam,
            # em vez de confiar no '@c.us' que a API devolve.
            chat = destino_teste or _jid_contato(pessoa.get('telefone')) or pessoa.get('chat_id')
            texto = pessoa.get('mensagem') or ''
            try:
                ok = _enviar_imagem(chat, imagem, texto) if imagem else False
                if not ok:
                    ok = _enviar_texto(chat, texto)
            except Exception as exc:
                log.error('     Falha com %s: %s', pessoa.get('nome'), exc)
                ok = False

            if destino_teste:
                log.info('     [TESTE] %s -> nao marcado', pessoa.get('nome'))
            else:
                try:
                    _session().post(
                        f'{API_URL}/api/avisos/marcar', headers=_api_headers(),
                        json={'aviso_id': aviso.get('id'),
                              'telefone': pessoa.get('telefone'),
                              'chave_atleta': pessoa.get('chave_atleta'),
                              'status': 'ok' if ok else 'erro'}, timeout=20)
                except Exception as exc:
                    log.warning('     Enviado mas nao marcado (%s): %s',
                                pessoa.get('nome'), exc)
            log.info('     %s %s', 'OK  ' if ok else 'FALHOU', pessoa.get('nome'))
            # Intervalo entre mensagens no privado: disparo em rajada e o que
            # mais chama atencao do WhatsApp e leva a bloqueio.
            time.sleep(6)
    return True


def avisar_anticheat():
    """Manda para o admin as flags novas do anticheat.

    Nao e alerta de erro: e informacao para decidir sem abrir o sistema. Os km
    continuam contando no ranking — so saem se o admin rejeitar na tela de
    revisoes. Aqui ele ve quem esta acumulando flag e decide se vale conversar.
    """
    if not ADMIN_CHAT_ID:
        return True
    try:
        resp = _session().get(f'{API_URL}/api/anticheat/resumo',
                              params={'novos': '1'}, headers=_api_headers(), timeout=40)
        if resp.status_code != 200:
            return True
        dados = resp.json()
        flags = dados.get('flags') or []
    except Exception as exc:
        log.warning('   Nao consegui buscar o anticheat: %s', exc)
        return True

    if not flags:
        log.info('   Anticheat: nenhuma flag nova')
        return True

    log.info('>>> Anticheat: %s flag(s) nova(s) — avisando o admin', len(flags))
    linhas = ['\U0001f50e *Anticheat — atividades para conferir*',
              datetime.now(TZ).strftime('%d/%m/%Y %H:%M'), '']
    for f in flags[:12]:
        vel = f.get('velocidade_media')
        vel_txt = f'{vel:.1f} km/h' if isinstance(vel, (int, float)) else '?'
        linhas.append(f"*{f.get('atleta')}*")
        linhas.append(f"  {f.get('tipo')} ({f.get('nivel')}) — {vel_txt}")
        if f.get('data_atividade'):
            linhas.append(f"  atividade de {str(f['data_atividade'])[:10]}")
        if f.get('atividade_url'):
            linhas.append(f"  {f['atividade_url']}")
        linhas.append(f"  flags deste atleta: {f.get('flags_abertas_do_atleta')} abertas "
                      f"de {f.get('flags_total_do_atleta')} no total")
        linhas.append('  os km DESTA atividade continuam contando no ranking')
        linhas.append('')
    if len(flags) > 12:
        linhas.append(f'... e mais {len(flags) - 12}.')
        linhas.append('')
    linhas.append('Nada foi tirado do ranking. Para rejeitar ou conversar com a')
    linhas.append('pessoa, abra as revisoes:')
    linhas.append(dados.get('painel') or f'{API_URL}/admin/reviews')

    if not _enviar_texto(ADMIN_CHAT_ID, '\n'.join(linhas)):
        return False
    try:
        _session().post(f'{API_URL}/api/anticheat/marcar', headers=_api_headers(),
                        json={'flag_ids': [f['flag_id'] for f in flags]}, timeout=20)
    except Exception as exc:
        log.warning('   Avisado mas nao marcado: %s', exc)
    return True


def notificar_n8n(evento, detalhe, nivel='erro'):
    """Ponto de acoplamento do n8n. Sem URL configurada, nao faz nada.

    De proposito nao levanta excecao e nao devolve erro: avisar que algo deu
    errado nao pode ser mais uma coisa que da errado.
    """
    if not N8N_WEBHOOK_URL:
        log.debug('n8n sem webhook configurado — evento "%s" so no log', evento)
        return False
    corpo = {
        'evento': evento,
        'nivel': nivel,
        'detalhe': str(detalhe)[:2000],
        'origem': ORIGEM,
        'sessao': WAHA_SESSION,
        'em_ensaio': em_ensaio(),
        'quando': datetime.now(TZ).isoformat(),
    }
    try:
        resp = _session().post(N8N_WEBHOOK_URL, json=corpo, timeout=20)
        ok = resp.status_code < 400
        log.info('   n8n avisado (%s): HTTP %s', evento, resp.status_code)
        return ok
    except Exception as exc:
        log.warning('   Nao consegui avisar o n8n sobre "%s": %s', evento, exc)
        return False


def _enviar_com_parceiro(numero_principal, texto):
    """Entrega no numero pedido; se nao der, entrega pelo parceiro.

    Existe por causa da fase de testes: as duas sessoes caem com frequencia e
    uma mensagem que nao chega em ninguem e pior do que uma mensagem que chega
    no outro telefone do mesmo dono.
    """
    tentativas = [n for n in (numero_principal, PARCEIRO_ENSAIO) if n]
    ultimo_erro = None
    for indice, numero in enumerate(tentativas):
        alvo = _jid_contato(numero) or numero
        if not alvo:
            continue
        try:
            if _enviar_texto(alvo, texto):
                if indice:
                    log.warning('   Entregue pelo parceiro (%s) — o principal nao deu.',
                                numero)
                return True
        except Exception as exc:
            ultimo_erro = exc
            log.warning('   Falhou por %s: %s', numero, exc)
    if ultimo_erro:
        log.error('   Nenhum numero do par conseguiu entregar: %s', ultimo_erro)
    return False


def _ultimas_linhas_do_log(quantas=30):
    """As ultimas linhas do log DESTA rodada, para irem junto no alerta.

    Sem elas o aviso diz que falhou e nao diz por que, e alguem tem que abrir o
    WSL para descobrir — que e exatamente o atrito que fez ninguem perceber a
    falha de 02/08 ate o dia seguinte.
    """
    try:
        with open(_log_file, encoding='utf-8', errors='replace') as fh:
            return [linha.rstrip('\n') for linha in fh.readlines()[-quantas:]]
    except Exception as exc:
        log.debug('nao consegui reler o log: %s', exc)
        return []


def notificar_por_email(etapa, erro):
    """Pede ao SITE que mande o e-mail de falha. Devolve True se ele saiu.

    O DELL NAO MANDA E-MAIL — ele pede. As credenciais do Microsoft Graph moram
    no cofre do Synology, a maquina que fica ligada o tempo todo, e nao devem
    existir aqui. O efeito colateral e o melhor da ideia: quando a falha e do
    lado do Dell (WAHA morto, sessao caida, WSL derrubado), quem avisa e a OUTRA
    maquina. Alerta que depende da peca quebrada nao e alerta.
    """
    try:
        resp = _session().post(
            f'{API_URL}/api/alerta', headers=_api_headers(),
            json={'etapa': str(etapa)[:120], 'erro': str(erro)[:2000],
                  'log': _ultimas_linhas_do_log(), 'origem': ORIGEM},
            timeout=60)
    except Exception as exc:
        log.error('   Nao consegui pedir o e-mail de alerta ao site: %s', exc)
        return False
    if resp.status_code != 200:
        log.error('   O site recusou o pedido de alerta: HTTP %s %s',
                  resp.status_code, resp.text[:200])
        return False
    dados = {}
    try:
        dados = resp.json() or {}
    except Exception:
        pass
    if dados.get('repetido'):
        log.info('   E-mail de alerta silenciado (a mesma etapa ja avisou ha pouco).')
        return True
    if dados.get('ok'):
        log.info('   E-mail de alerta enviado para: %s',
                 ', '.join(dados.get('destinatarios') or []) or '(?)')
        return True
    log.error('   O site nao conseguiu mandar o e-mail: %s',
              dados.get('detalhe') or 'sem detalhe')
    return False


def _mesmo_numero(a, b):
    """Dois jeitos de escrever o mesmo telefone sao a mesma pessoa?

    ISTO NAO E FRESCURA. O WAHA devolve o numero da sessao SEM o nono digito
    (55DDXXXXXXXX) e o cadastro guarda COM ele (55DD9XXXXXXXX). Comparando cru,
    o sistema conclui que sao gente diferente — e ai manda o alerta para o
    proprio numero que esta enviando, o que devolve HTTP 500 (regra 20) e o
    aviso se perde. Comparamos pais+DDD e os 8 ultimos digitos.
    """
    a, b = _so_digitos(a), _so_digitos(b)
    if not a or not b or len(a) < 10 or len(b) < 10:
        return False
    return (a[:4], a[-8:]) == (b[:4], b[-8:])


def _destinos_do_alerta():
    """Telefones que devem receber o alerta, na ordem de quem responde primeiro.

    A fonte e o SITE (quem publica, por ordem_envio), porque e la que o cadastro
    vive. Sem o site, cai no que o .env conhece — nao e a mesma coisa, mas e
    melhor que ninguem.
    """
    numeros = []

    def _juntar(valor):
        tel = _so_digitos(str(valor or '').split('@')[0])
        if tel and not any(_mesmo_numero(tel, n) for n in numeros):
            numeros.append(tel)

    try:
        resp = _session().get(f'{API_URL}/api/agenda/agora',
                              headers=_api_headers(), timeout=20)
        if resp.status_code == 200:
            remetentes = (resp.json() or {}).get('remetentes') or {}
            for item in (remetentes.get('ranking') or []):
                _juntar(item.get('telefone'))
    except Exception as exc:
        log.debug('   nao consegui perguntar os destinos ao site: %s', exc)

    _juntar(ADMIN_CHAT_ID)
    _juntar(PARCEIRO_ENSAIO)
    return numeros


def _jid_na_instancia(sessao, telefone):
    """chatId do numero, perguntando a UMA instancia especifica."""
    numero = _so_digitos(telefone)
    if not numero:
        return None
    if not numero.startswith('55'):
        numero = '55' + numero
    try:
        resp = _session().get(
            f"{sessao['base']}/api/contacts/check-exists",
            headers={'X-Api-Key': sessao['chave']},
            params={'phone': numero, 'session': sessao['nome']}, timeout=25)
        if resp.status_code == 200:
            dados = resp.json() or {}
            if not dados.get('numberExists'):
                return None
            return (dados.get('chatId') or '').strip() or f'{numero}@c.us'
    except Exception as exc:
        log.debug('   check-exists em %s falhou: %s', sessao['base'], exc)
    return f'{numero}@c.us'


def _enviar_por(sessao, chat_id, texto):
    """Manda por uma instancia especifica (nao pela global desta rodada)."""
    try:
        resp = _session().post(
            f"{sessao['base']}/api/sendText",
            headers={'X-Api-Key': sessao['chave'], 'Content-Type': 'application/json'},
            json={'session': sessao['nome'], 'chatId': chat_id, 'text': texto},
            timeout=30)
        return resp.status_code in (200, 201)
    except Exception as exc:
        log.debug('   envio por %s falhou: %s', sessao['base'], exc)
        return False


def alertar_whatsapp_cruzado(texto):
    """Avisa pelo WhatsApp que ESTIVER de pe, no telefone de quem esta fora.

    A IDEIA, do dono em 06/08: cada cidade tem um principal e um suplente, cada
    um com o proprio WhatsApp em instancia propria. Quando um cai, o outro quase
    sempre esta de pe — entao quem avisa e o que esta vivo, no telefone do dono
    da sessao quebrada. Chega na hora e na tela que a pessoa ja esta olhando.

    | Quem caiu | Quem avisa            | Para quem            |
    | suplente  | sessao do principal   | telefone do suplente |
    | principal | sessao do suplente    | telefone do principal|
    | os dois   | ninguem               | e-mail (P-04)        |

    Nunca manda para o numero da propria sessao que envia (regra 20): mandar
    para si mesmo devolve HTTP 500 e o alerta se perde justamente na hora em
    que ele mais importa.

    Devolve (entregues, detalhe).
    """
    try:
        prontas = wahasessao.sessoes_prontas()
    except Exception as exc:
        return [], 'nao consegui olhar as instancias: %s' % exc
    if not prontas:
        return [], 'nenhuma sessao de pe — a cidade esta muda no WhatsApp'

    destinos = _destinos_do_alerta()
    if not destinos:
        return [], 'nenhum telefone cadastrado para avisar'

    entregues = []
    for sessao in prontas:
        for numero in destinos:
            if any(_mesmo_numero(numero, ok) for ok in entregues):
                continue
            if _mesmo_numero(numero, sessao.get('numero')):
                # E o proprio numero desta sessao: mandar daqui para ele
                # mesmo da 500. Se outra sessao estiver de pe, ela cobre.
                continue
            chat = _jid_na_instancia(sessao, numero)
            if not chat:
                continue
            if _enviar_por(sessao, chat, texto):
                entregues.append(numero)
                log.info('   Alerta entregue a %s pela sessao %s (%s)',
                         numero, sessao['nome'], sessao.get('papel', '?'))
    if entregues:
        return entregues, 'entregue por WhatsApp'
    return [], 'nenhuma sessao de pe conseguiu entregar aos destinos'


def _acao_da_etapa(etapa):
    """{botao, url, explica} — o LINK que resolve esta etapa.

    POR QUE LINK E NAO CAMINHO. O alerta chega no WhatsApp, no celular, quase
    sempre fora de hora. Escrever "abra o Movimente-se > Painel > Sessao do
    WhatsApp" obriga a pessoa a sair do WhatsApp, abrir o navegador, digitar o
    endereco, achar o menu e lembrar o caminho — e a essa altura ela ja
    desistiu. Um link e um toque.

    Regra que vale para TODA mensagem que o sistema manda pedindo acao: se da
    para mandar o link, manda o link.

    Quem sabe qual tela resolve o que e o SITE (app/alertas.py). Aqui so
    perguntamos, para nao existir uma segunda lista de telas neste arquivo — duas
    listas em maquinas diferentes divergem, e isso ja custou caro neste projeto.
    """
    reserva = {'botao': 'Abrir o Movimente-se',
               'url': f'{API_URL}/admin', 'explica': ''}
    try:
        resp = _session().get(f'{API_URL}/api/alerta/acao',
                              headers=_api_headers(),
                              params={'etapa': etapa}, timeout=15)
        if resp.status_code == 200:
            dados = resp.json() or {}
            if dados.get('url'):
                return {'botao': dados.get('botao') or reserva['botao'],
                        'url': dados['url'],
                        'explica': dados.get('explica') or ''}
    except Exception as exc:
        log.debug('   nao consegui perguntar a acao ao site: %s', exc)
    return reserva


def _registrar_no_site(etapa, erro, canal, detalhe, so_registrar):
    """Deixa a falha gravada no site. Com so_registrar, NAO manda e-mail.

    Registrar e avisar sao coisas diferentes: mesmo quando o WhatsApp resolveu,
    a falha tem que ficar no historico — senao ninguem descobre depois que ela
    vinha se repetindo.
    """
    try:
        resp = _session().post(
            f'{API_URL}/api/alerta', headers=_api_headers(),
            json={'etapa': str(etapa)[:120], 'erro': str(erro)[:2000],
                  'log': _ultimas_linhas_do_log(), 'origem': ORIGEM,
                  'canal': canal, 'detalhe': detalhe,
                  'so_registrar': bool(so_registrar)},
            timeout=60)
    except Exception as exc:
        log.error('   Nao consegui falar com o site para o alerta: %s', exc)
        return None
    if resp.status_code != 200:
        log.error('   O site recusou o alerta: HTTP %s %s',
                  resp.status_code, resp.text[:200])
        return None
    try:
        return resp.json() or {}
    except Exception:
        return {}


MAX_WHATSAPP_POR_DIA = 2   # item C do plano de 17/08 — da 3a falha em diante, so registro


def _freio_whatsapp_avaliar(etapa):
    """Quantos avisos por WhatsApp esta etapa ja mandou HOJE. Zera na virada do dia."""
    hoje = datetime.now(TZ).strftime('%Y-%m-%d')
    registro = (load_state().get('freio_whatsapp') or {}).get(etapa) or {}
    if registro.get('dia') != hoje:
        return 0
    return int(registro.get('contagem') or 0)


def _freio_whatsapp_registrar(etapa):
    """Soma 1 ao contador de hoje desta etapa."""
    hoje = datetime.now(TZ).strftime('%Y-%m-%d')
    freios = dict(load_state().get('freio_whatsapp') or {})
    anterior = freios.get(etapa) or {}
    contagem = (int(anterior.get('contagem') or 0) + 1) if anterior.get('dia') == hoje else 1
    freios[etapa] = {'dia': hoje, 'contagem': contagem}
    save_state(freio_whatsapp=freios)
    return contagem


def zerar_freio_whatsapp(etapa):
    """O problema desta etapa se resolveu: solta o freio para a proxima vez que ela falhar.

    Quem chama e o proprio ponto que descobre a recuperacao (ex.: `etapa_waha`
    quando a sessao volta a WORKING/CONNECTED) — nao ha como `notificar_admin`
    saber sozinho que uma etapa sarou, ja que so e chamada em falha.
    """
    freios = dict(load_state().get('freio_whatsapp') or {})
    if etapa in freios:
        freios.pop(etapa, None)
        save_state(freio_whatsapp=freios)


def notificar_admin(etapa, erro):
    """Alerta de falha. Vai para o admin — nunca para o grupo da comunidade.

    A ORDEM, decidida pelo dono em 06/08:

      1. WhatsApp CRUZADO — quem esta de pe avisa quem caiu. Chega na hora.
      2. n8n, se algum dia tiver webhook (hoje e no-op, pendencia P-10).
      3. E-MAIL — so quando o WhatsApp nao entregou a ninguem, porque isso
         significa que as DUAS sessoes da cidade estao com problema. E
         justamente ai que o e-mail e insubstituivel: ele nao depende de
         nenhuma peca que a rodada usa.

    Ate 05/08 o unico canal era o WhatsApp da propria rodada, e isso tem um
    defeito de raiz: a pipeline falha justamente quando aquele WhatsApp esta
    fora, e o aviso morre junto com a falha. Foi assim que o ranking de 02/08
    nao saiu e ninguem soube ate o dia seguinte.

    A FALHA E REGISTRADA NO SITE SEMPRE, tenha o aviso saido por onde tiver.

    ENSAIO NAO ACORDA NINGUEM SEM AVISAR QUE E ENSAIO (regra 16-J-A).
    Em 12/08 as 07:04 um `--dry-run` meu disparou um alerta de verdade no
    WhatsApp do dono — um falso positivo de uma versao que eu estava testando.
    Ele perguntou "isso foi teste?", e essa pergunta e o defeito: `--dry-run`
    promete nao publicar, mas o alerta saia igual, sem nada que o distinguisse
    de uma falha real. Agora ensaio vai MARCADO. Nao se cala o alerta em ensaio
    de proposito: alerta que some no teste e alerta que ninguem testou.
    """
    acao = _acao_da_etapa(etapa)
    # A MARCA VEM PRIMEIRO, antes de qualquer coisa que pareca urgente: quem le
    # no celular ve as duas primeiras palavras na previa da notificacao, e e ali
    # que ele decide se levanta da cadeira.
    marca = '** IGNORE ESSE TESTE **\n' if _EM_ENSAIO else ''
    texto = (f'{marca}'
             '⚠️ *Movimente-se — falha na automacao*\n'
             f'{datetime.now(TZ).strftime("%d/%m/%Y %H:%M")}\n'
             f'Etapa: {etapa}\n'
             f'Erro: {erro}\n\n'
             f"*{acao['botao']}:*\n"
             f"{acao['url']}")
    if acao.get('explica'):
        texto += '\n\n' + acao['explica']
    canais = []

    # 1. WhatsApp cruzado — ATE 2 POR DIA, POR ETAPA (17/08/2026, item C).
    #
    # `alertar_whatsapp_cruzado` nao tem freio nenhum por conta propria, e
    # esta funcao roda de 15 em 15 min (`--so-fila`). Foi assim que 16/08
    # gerou 22 avisos numa noite so (§1.4 do plano de 17/08) enquanto o
    # e-mail, que TEM freio de 60 min, mandou so 6. O freio mora aqui porque e
    # aqui que TODO aviso de falha da automacao passa.
    contagem_hoje = _freio_whatsapp_avaliar(etapa)
    if contagem_hoje < MAX_WHATSAPP_POR_DIA:
        texto_whatsapp = texto
        if contagem_hoje == MAX_WHATSAPP_POR_DIA - 1:
            texto_whatsapp += ('\n\n_(nao vou repetir isso hoje; se continuar, '
                               'o aviso vai por e-mail)_')
        entregues, detalhe_wpp = alertar_whatsapp_cruzado(texto_whatsapp)
        if entregues:
            canais.append('WhatsApp (%s)' % ', '.join(entregues))
            _freio_whatsapp_registrar(etapa)
        else:
            log.warning('   WhatsApp nao avisou ninguem: %s', detalhe_wpp)
    else:
        entregues, detalhe_wpp = [], (
            'freio de %dx/dia atingido para "%s" — so registrando; o e-mail '
            '(com o freio de 60 min dele) segue como canal de insistencia'
            % (MAX_WHATSAPP_POR_DIA, etapa))
        log.info('   WhatsApp silenciado: %s', detalhe_wpp)

    # 2. n8n.
    try:
        if notificar_n8n('falha_pipeline', '%s: %s' % (etapa, erro), nivel='erro'):
            canais.append('n8n')
    except Exception as exc:
        log.debug('n8n nao avisado: %s', exc)

    # 3. E-mail — so se o WhatsApp nao alcancou ninguem. Quando alcancou, o
    #    site so registra (sem e-mail), para o historico nao ficar com buraco.
    resposta = _registrar_no_site(
        etapa, erro,
        canal=('whatsapp' if entregues else 'email'),
        detalhe=(detalhe_wpp if entregues else
                 'WhatsApp nao entregou: %s' % detalhe_wpp),
        so_registrar=bool(entregues))
    if resposta and resposta.get('ok'):
        canais.append('e-mail')
    elif resposta and resposta.get('repetido'):
        log.info('   E-mail silenciado (a mesma etapa ja avisou ha pouco).')
        canais.append('e-mail (silenciado)')
    elif not entregues:
        log.error('   O e-mail tambem nao saiu: %s',
                  (resposta or {}).get('detalhe') or 'o site nao respondeu')

    if canais:
        log.info('   Admin notificado por: %s', ', '.join(canais))
        return True
    # Ultima linha de defesa: se nem isto aparecer no log, nada avisou ninguem.
    log.error('=' * 55)
    log.error(' ALERTA NAO ENTREGUE POR NENHUM CANAL — %s: %s', etapa, erro)
    log.error(' Ninguem foi avisado. Confira o site e o WAHA na mao.')
    log.error('=' * 55)
    return False


def modo_monitor():
    """Checa saude e so fala quando ha algo a fazer. Sem problema = sem mensagem."""
    log.info('=== MONITOR DE SAUDE ===')
    problemas = []

    try:
        resp = _session().get(f'{API_URL}/api/health', headers=_api_headers(), timeout=15)
        if resp.status_code != 200:
            problemas.append(f'API respondeu HTTP {resp.status_code}. Verifique o DS215j.')
        else:
            log.info('API: ok')
    except Exception as exc:
        problemas.append(f'API inacessivel ({type(exc).__name__}). Verifique o DS215j.')

    try:
        status, estado, _ = _status_waha()
        if status in ('FAILED', 'STOPPED'):
            problemas.append(f'{MSG_RESCAN}{WAHA_BASE}/dashboard')
        elif status != 'WORKING':
            problemas.append(f'Sessao WhatsApp em "{status}". Se nao normalizar sozinha, '
                             f'refaca o scan do QR em {WAHA_BASE}/dashboard')
        elif estado != 'CONNECTED':
            problemas.append(f'Sessao WhatsApp ainda nao conectou (engine "{estado}"). '
                             'Se persistir, refaca o scan do QR.')
        else:
            log.info('WAHA: WORKING / CONNECTED')
    except Exception as exc:
        problemas.append(f'WAHA offline ({type(exc).__name__}). Suba os containers.')

    try:
        resp = _session().get(f'{API_URL}/api/strava-sessao', headers=_api_headers(), timeout=20)
        if resp.status_code == 200:
            dados = resp.json()
            saude = dados.get('saude') or {}
            if not dados.get('configurado'):
                problemas.append('Cookie do Strava ausente. Recole em Painel > Sessao Strava.')
            elif saude.get('ok') is False:
                problemas.append(f'Cookie do Strava recusado: {saude.get("message")}. '
                                 'Recole em Painel > Sessao Strava.')
            else:
                log.info('Strava: cookie valido')
        else:
            problemas.append(f'/api/strava-sessao HTTP {resp.status_code}')
    except Exception as exc:
        problemas.append(f'Falha ao checar cookie do Strava: {exc}')

    if not problemas:
        log.info('Tudo saudavel — nenhuma notificacao enviada (silencio absoluto).')
        return True

    log.warning('%s problema(s) encontrado(s)', len(problemas))
    for item in problemas:
        log.warning('   - %s', item)
    corpo = ('\U0001f6a8 *Movimente-se — atencao*\n'
             + datetime.now(TZ).strftime('%d/%m/%Y %H:%M') + '\n\n'
             + '\n'.join(f'• {p}' for p in problemas))
    notificar_n8n('monitor', ' | '.join(problemas))
    if ADMIN_CHAT_ID:
        try:
            # Vai pelo par: se a sessao do principal caiu, o alerta sobre a
            # queda nao pode depender justamente dela.
            _enviar_com_parceiro(ADMIN_CHAT_ID.split('@')[0], corpo)
        except Exception as exc:
            log.error('Nao consegui avisar o admin: %s', exc)
    else:
        log.error('ADMIN_CHAT_ID nao definido — alerta ficou apenas no log.')
    return False


def modo_diagnostico():
    """Confere cada peca sem mexer em nada."""
    log.info('=' * 55)
    log.info('MODO DIAGNOSTICO')
    log.info('=' * 55)

    faltando = [k for k in ('WAHA_API_KEY', 'MOVIMENTE_API_URL', 'MOVIMENTE_API_TOKEN',
                            'WHATSAPP_GROUP_ID') if not os.getenv(k)]
    if faltando:
        log.warning('.env incompleto: %s', ', '.join(faltando))
    else:
        log.info('.env: ok')
    if not ADMIN_CHAT_ID:
        log.warning('ADMIN_CHAT_ID vazio — alertas de falha ficarao so no log.')

    api_ok  = etapa_api()
    waha_ok = etapa_waha(espera=WAHA_ESPERA)

    log.info('--- amostra da coleta (24h) ---')
    try:
        resp = _session().get(
            f'{WAHA_BASE}/api/{WAHA_SESSION}/chats/{GROUP_ID}/messages',
            params={'limit': 5, 'downloadMedia': 'false',
                    'filter.timestamp.gte': int(time.time()) - 86400},
            headers=_waha_headers(), timeout=20)
        if resp.status_code == 200:
            msgs = resp.json()
            log.info('   WAHA devolveu %s mensagem(ns)', len(msgs) if isinstance(msgs, list) else 0)
            if isinstance(msgs, list) and msgs:
                primeira = msgs[0]
                log.info('   Exemplo: %s | %s | %s', _ts_str(primeira.get('timestamp', 0)),
                         primeira.get('pushName') or primeira.get('author') or primeira.get('from'),
                         (primeira.get('body') or '')[:70])
        else:
            log.warning('   HTTP %s', resp.status_code)
    except Exception as exc:
        log.warning('   %s', exc)

    log.info('--- ranking do periodo %s ---', periodo_atual())
    dados = etapa_ranking(periodo_atual())
    rank_ok = bool(dados)
    if dados:
        log.info('   Previa: %s', (dados.get('texto_whatsapp') or '')[:160].replace('\n', ' | '))

    log.info('--- saude geral ---')
    modo_monitor()

    log.info('=' * 55)
    return all([api_ok, waha_ok, rank_ok, not faltando])


def avisar_ranking_zerado(periodo, destino):
    """Avisa UMA vez por mes que o contador ainda esta zerado.

    O aviso substitui a tabela vazia. Sai uma vez so: repetir "ninguem
    correu ainda" duas vezes por dia ate alguem postar seria barulho, e
    barulho e o jeito mais rapido de o grupo parar de ler o que a gente
    manda.
    """
    estado = load_state()
    if estado.get('aviso_zerado_periodo') == periodo:
        log.info('   Aviso de ranking zerado ja saiu neste periodo - nao repito.')
        return
    try:
        mes = int(periodo.split('-')[1])
    except (IndexError, ValueError):
        mes = 0
    nomes = ('', 'Janeiro', 'Fevereiro', 'Marco', 'Abril', 'Maio',
             'Junho', 'Julho', 'Agosto', 'Setembro', 'Outubro',
             'Novembro', 'Dezembro')
    nome_mes = nomes[mes] if 1 <= mes <= 12 else periodo
    quebra = chr(10)
    texto = (
        '\U0001f4c5 *' + nome_mes + ' comecou* - o contador esta zerado.'
        + quebra + quebra +
        'Ainda nao chegou nenhum link de atividade deste mes. Assim que o '
        'primeiro aparecer aqui no grupo, o ranking volta a sair nos '
        'horarios de sempre.'
        + quebra + quebra +
        'E so mandar o link da sua atividade do Strava aqui. '
        '\U0001f3c3\U0001f6b4'
    )
    if _enviar_texto(destino, texto):
        save_state(aviso_zerado_periodo=periodo)
        log.info('   Aviso de ranking zerado publicado.')
    else:
        log.warning('   Nao consegui publicar o aviso de ranking zerado.')


# ===== PIPELINE =====
def _proxima_rodada(agora=None):
    """O proximo horario de rodada, como texto. '' quando nao ha lista."""
    if not RODADAS_DO_DIA:
        return ''
    agora = agora or datetime.now(TZ)
    minutos_agora = agora.hour * 60 + agora.minute
    melhores = []
    for h in RODADAS_DO_DIA:
        try:
            hh, mm = [int(x) for x in h.split(':')]
        except ValueError:
            continue
        melhores.append((hh * 60 + mm, h))
    if not melhores:
        return ''
    melhores.sort()
    for minutos, texto in melhores:
        if minutos > minutos_agora:
            return texto
    return melhores[0][1] + ' de amanha'


def etapa_laudo():
    """Antes de desligar: a proxima rodada vai acontecer ou nao?

    POR QUE ISTO EXISTE, E POR QUE NAO E UMA SENTINELA
    O site nao alcanca o WAHA — ele mora aqui no Dell. Entre uma rodada e outra
    a quebra fica invisivel: em 06/08 a sessao caiu as 11h10 e so a rodada da
    noite descobriria. A tentacao e ficar vigiando de N em N minutos, mas
    vigilancia continua para tudo e hardware queimando a toa.

    O momento que importa e ESTE: a maquina esta prestes a desligar e ainda ha
    gente acordada. Se a sessao esta quebrada, e agora que se avisa — "do jeito
    que esta, nao vai ter ranking as 22:00" — enquanto ainda da tempo de alguem
    resolver. Depois de desligada, ninguem mais conserta nada ate a proxima vez.

    Devolve True quando esta tudo pronto. Nao publica nada e nao muda estado.
    """
    log.info('=' * 55)
    log.info('LAUDO ANTES DE DESLIGAR')
    log.info('=' * 55)
    rel = prontidao.esperar(
        WAHA_BASE, WAHA_API_KEY, preferida=WAHA_SESSION, grupo=GROUP_ID,
        container=WAHA_CONTAINER, limite_seg=LAUDO_LIMITE_SEG,
        intervalo_seg=PRONTIDAO_INTERVALO_SEG, log=log.info)
    if rel['ok']:
        log.info('   Tudo pronto — a proxima rodada (%s) tem tudo de que precisa.',
                 _proxima_rodada() or 'a proxima')
        return True

    quando = _proxima_rodada()
    tentativas = ''
    for c in rel.get('consertos') or []:
        tentativas += ('\n- Ja tentei %s: %s'
                       % (c['acao'], 'funcionou' if c['ok']
                          else 'nao resolveu (%s)' % c['detalhe']))
    if not tentativas:
        tentativas = '\n- Nao havia conserto automatico para este caso.'

    texto = ('Do jeito que esta, *nao vai ter ranking%s*.\n\n'
             'O que esta travando: %s\n%s\n\n%s'
             % (' as %s' % quando if quando else ' na proxima rodada',
                ', '.join(rel['impedem']), tentativas,
                prontidao.resumo(rel)))
    log.error(texto)
    notificar_admin('Prontidao', texto)
    return False


# ===== FECHAMENTO DO MES QUE ATRASOU (P-146, 01/10/2026) =====
#
# Em 30/09 o Dell estava desligado no Wi-Fi e nao acordou para a rodada das
# 22:30 do ultimo dia. Quando ligou, ja era 01/10: a rodada saiu como OUTUBRO
# (zerado), os links de 30/09 foram ao periodo errado e o ranking final de
# setembro nunca foi ao grupo — o dono colou a mao. Regra do dono (01/10):
# se o fechamento nao sair na hora, ele sai no DIA 1, a partir da rodada das
# 05:30 (nunca de madrugada); a rodada das 15:30 e a segunda tentativa. Do dia 2
# em diante NAO tenta mais: o dono manda pela copia manual, avisado por e-mail.
FECHAMENTO_ATE_DIA = 1
FECHAMENTO_A_PARTIR_HORA = 5


def _fim_do_periodo(periodo):
    """'2026-09' -> 01/10/2026 00:00: o instante em que aquele mes fecha."""
    ano, mes = (int(x) for x in periodo.split('-'))
    ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return datetime(ano, mes, 1, tzinfo=TZ)


def _na_janela_do_fechamento(periodo, quando):
    """Entre 22:00 do ultimo dia e a meia-noite — a hora da rodada das 22:30."""
    fim = _fim_do_periodo(periodo).timestamp()
    return fim - 2 * 3600 <= quando < fim


def fechamento_pendente(agora=None, estado=None):
    """No dia 1, a partir das 05:00, devolve o mes anterior ('AAAA-MM'); senao None.

    DESENHO DO DONO (01/10/2026, 22h): o ranking final sai na rodada das 22:30
    do ultimo dia (o Synology liga o Dell). No dia 1, TODA rodada (05:30, 15:30
    e qualquer pedido extra, mesmo depois das 15:30) confere o mes anterior de
    novo: le o grupo, lanca os links (inclusive os postados depois da
    meia-noite) e, se os km do mes anterior MUDARAM em relacao ao ultimo
    ranking dele que foi ao grupo, republica o final antes do mes corrente.
    Quem decide se republica e `etapa_fechamento`, comparando o retrato.
    `estado` fica na assinatura por compatibilidade; nao e mais usado.
    """
    agora = agora or datetime.now(TZ)
    if agora.day > FECHAMENTO_ATE_DIA or agora.hour < FECHAMENTO_A_PARTIR_HORA:
        return None
    ano, mes = (agora.year - 1, 12) if agora.month == 1 else (agora.year, agora.month - 1)
    return f'{ano:04d}-{mes:02d}'


def _guardar_retrato(periodo, impressao):
    """Guarda o retrato dos km do ultimo ranking de `periodo` que foi ao grupo."""
    retratos = dict(load_state().get('hash_por_periodo') or {})
    retratos[periodo] = impressao
    # So os ultimos meses interessam (o dia 1 olha o mes anterior).
    retratos = dict(sorted(retratos.items())[-6:])
    save_state(hash_por_periodo=retratos)


_MESES_FECHAMENTO = ('', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                     'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro')


def _nome_periodo(periodo):
    ano, mes = periodo.split('-')
    return '%s/%s' % (_MESES_FECHAMENTO[int(mes)], ano)


def _cabecalho_fechamento(periodo_fechar):
    """Primeira linha do ranking final que sai no dia seguinte (pedido do dono)."""
    agora = datetime.now(TZ)
    corrente = '%04d-%02d' % (agora.year, agora.month)
    return ('\U0001f4cc *RANKING FINAL DE %s — MÊS ENCERRADO*\n'
            '⚠️ _Não se refere ao mês corrente (%s)._\n\n'
            % (_nome_periodo(periodo_fechar).upper(), _nome_periodo(corrente)))


def avisar_fechamento_falhou(periodo_fechar, motivo):
    """E-mail/alerta quando o ranking final do dia 1 nao saiu, dizendo o proximo passo."""
    agora = datetime.now(TZ)
    if agora.day == 1 and agora.hour < 15:
        depois = 'Vou tentar de novo na rodada das 15:30 de hoje.'
    else:
        depois = ('Não haverá nova tentativa automática: envie o ranking final de %s '
                  'pela cópia manual (Ranking > mês de %s > Copiar).'
                  % (_nome_periodo(periodo_fechar), _nome_periodo(periodo_fechar)))
    notificar_admin('Fechamento',
                    'O ranking FINAL de %s não saiu no grupo (%s). %s'
                    % (_nome_periodo(periodo_fechar), motivo, depois))


def etapa_fechamento(periodo_fechar, mensagens, args):
    """Publica, no dia 1, o ranking final do mes que fechou sem a rodada do ultimo dia.

    Ordem (pedido do dono, 01/10/2026 18h): a leitura do grupo desta rodada vai
    inteira — inclusive link do mes anterior postado DEPOIS da meia-noite.
    1) Primeiro para o MES CORRENTE: as atividades novas entram no mes delas.
    2) Depois, os mesmos links para o MES QUE FECHOU: as atividades dele entram
       (as do mes corrente ja estao no banco e caem na triagem como "ja no
       banco", sem virar alarme de "outro mes").
    3) Ranking final com cabecalho "nao se refere ao mes corrente".
    Nao mexe no checkpoint (quem avanca e a rodada normal, logo depois).
    True = resolvido (publicou, ou os km nao mudaram); False = fica pendente e
    o dono recebe o aviso com o proximo passo.
    """
    log.info('=' * 55)
    log.info('FECHAMENTO ATRASADO — ranking final de %s (a rodada do ultimo dia '
             'nao aconteceu)', periodo_fechar)
    log.info('=' * 55)
    if mensagens and not args.dry_run:
        txt = etapa_montar_txt(mensagens)
        for periodo_job in (periodo_atual(), periodo_fechar):
            job_id = etapa_upload(txt, periodo_job) if txt else None
            resultado = etapa_poll(job_id) if job_id else None
            if not resultado or resultado.get('stage') != 'done':
                avisar_fechamento_falhou(periodo_fechar,
                                         'não consegui lançar os links de %s' % periodo_job)
                return False

    dados = etapa_ranking(periodo_fechar)
    if not dados:
        avisar_fechamento_falhou(periodo_fechar, 'o site não devolveu o ranking')
        return False
    if args.dry_run or args.sem_envio:
        log.info('   (%s) ranking final de %s NAO publicado.',
                 'dry-run' if args.dry_run else '--sem-envio', periodo_fechar)
        return False

    _resolver_admin_chat()
    destino = ADMIN_CHAT_ID if args.teste else destino_publicacao()
    impressao = _impressao_ranking(dados)
    ultimo = (load_state().get('hash_por_periodo') or {}).get(periodo_fechar)
    if _ranking_vazio(dados):
        pode, motivo = False, 'ranking do mes anterior zerado'
    elif ultimo == impressao:
        pode, motivo = False, 'o grupo ja tem o ranking final certo (km nao mudaram)'
    else:
        pode, motivo = True, ('km do mes anterior mudaram desde o ultimo ranking dele'
                              if ultimo else 'sem registro do ranking final no grupo')
    if args.teste:
        pode, motivo = True, 'modo teste'
    if pode:
        cab = _cabecalho_fechamento(periodo_fechar)
        dados = dict(dados)
        partes = [p for p in (dados.get('mensagens') or []) if (p or '').strip()]
        if partes:
            dados['mensagens'] = [cab + partes[0]] + partes[1:]
        else:
            dados['texto_whatsapp'] = cab + (dados.get('texto_whatsapp') or '')
        if FOLGA_ANTES_ENVIO_SEG > 0:
            time.sleep(FOLGA_ANTES_ENVIO_SEG)
        if not etapa_enviar(dados, destino=destino):
            avisar_fechamento_falhou(periodo_fechar, 'o WhatsApp não aceitou a mensagem')
            return False
        log.info('   Ranking final de %s publicado (%s).', periodo_fechar, motivo)
        if not args.teste:
            _guardar_retrato(periodo_fechar, impressao)
    else:
        log.info('   Ranking final de %s NAO publicado — %s.', periodo_fechar, motivo)
    # Quem bateu a meta no ultimo dia ganha o parabens do mes certo (idempotente).
    if not args.sem_parabens and not args.teste:
        enviar_parabens(periodo_fechar)
    return True


def pipeline(args):
    inicio  = time.time()
    periodo = args.periodo or periodo_atual()
    log.info('=' * 55)
    log.info('PIPELINE MOVIMENTE-SE — periodo %s%s', periodo, ' [DRY RUN]' if args.dry_run else '')
    log.info('=' * 55)

    if not etapa_api():
        if not args.dry_run:
            notificar_admin('API Health', 'API do Movimente-se nao respondeu.')
        return False

    if not etapa_waha(espera=WAHA_ESPERA):
        # ANTES DE DESISTIR, TENTA A OUTRA SESSAO (17/08/2026, item A). O
        # ranking sai pelo suplente se for o unico que estiver de pe —
        # decisao do dono, para nao perder a rodada por causa de um numero
        # so. Coleta continua exigindo leitura confiavel (regra 8) do mesmo
        # jeito depois da troca.
        papel_assumido = assumir_sessao_viva()
        if papel_assumido and etapa_waha(espera=0):
            log.warning('   RODADA SEGUE PELA SESSAO %s (%s) — a de sempre nao respondeu.',
                        WAHA_SESSION, papel_assumido)
        else:
            if not args.dry_run:
                # Antes o alerta so saia se o proprio WAHA estivesse de pe para
                # manda-lo — ou seja, o aviso morria junto com a falha. Desde a
                # P-04 o e-mail sai pelo site, que nao depende do WAHA nem do Dell.
                notificar_admin('Sessao WhatsApp',
                                f'{MSG_RESCAN}{WAHA_BASE}/dashboard')
            return False

    pronto, rel_prontidao = etapa_prontidao()
    if not pronto:
        # Nao publica nada. Uma peca fora do lugar aqui significa leitura
        # incompleta la na frente — e ranking sobre leitura incompleta sai
        # errado sem ninguem perceber, que e o pior desfecho possivel.
        log.error('=' * 55)
        log.error('RODADA INTERROMPIDA — a automacao nao esta pronta.')
        log.error('Nada foi publicado.')
        log.error('=' * 55)
        if not args.dry_run:
            # O alerta diz o que ja foi TENTADO. Sem isso quem recebe repete o
            # que a maquina acabou de fazer — reinicia a sessao de novo, espera
            # de novo — e perde tempo justamente quando ele e curto.
            tentativas = ''
            for c in rel_prontidao.get('consertos') or []:
                tentativas += ('\n- Ja tentei %s: %s'
                               % (c['acao'], 'funcionou' if c['ok']
                                  else 'nao resolveu (%s)' % c['detalhe']))
            if not tentativas:
                tentativas = '\n- Nao havia conserto automatico para este caso.'
            notificar_admin('Prontidao',
                            'A automacao nao ficou pronta em %s (%s). Nada foi '
                            'publicado.\n\n%s\n%s'
                            % (prontidao.duracao(PRONTIDAO_LIMITE_SEG),
                               ', '.join(rel_prontidao['impedem']),
                               prontidao.resumo(rel_prontidao), tentativas))
        return False

    mensagens, coleta_confiavel = coletar_com_sincronizacao(force=args.force_collect)

    # 17/08/2026 (plano attach-tidy-globe, A2) — COLETA RUIM NAO ABORTA A
    # RODADA INTEIRA. Ate aqui, `return False` matava tudo — inclusive o
    # patrocinador, que pela regra 11-B deveria sair de qualquer jeito. O dono
    # notou isso primeiro: "se fosse so falta de link, a logo teria ido".
    # Agora: sem leitura confiavel, os links desta leitura NAO entram (fica
    # como se nao houvesse mensagem nova — o proximo marco nao avanca, regra
    # 4-J cobre o buraco), e o ranking publicado e o que ja existe (regra 6
    # barra sozinho se nada mudou) — mas patrocinador e avisos seguem.
    coleta_bloqueada = not coleta_confiavel and not args.force_collect
    if coleta_bloqueada:
        log.error('=' * 55)
        log.error('LEITURA DO GRUPO NAO FICOU CONFIAVEL — ranking desta '
                   'rodada fica de fora. Patrocinador e avisos seguem.')
        log.error('=' * 55)
        if not args.dry_run:
            notificar_admin('Coleta',
                            'Nao consegui ler o grupo com seguranca (o WhatsApp Web '
                            'parece nao ter terminado de sincronizar). O RANKING nao '
                            'foi publicado nesta rodada — nenhum km foi perdido, so '
                            'adiado para a proxima leitura. Patrocinador e avisos do '
                            'ciclo seguem normalmente.')
            notificar_n8n('coleta_incompleta',
                          'A leitura do grupo nao ficou confiavel; ranking adiado, '
                          'patrocinador/avisos seguiram.')
        mensagens = []

    # LEITURA PAROU DE MUDAR, MAS ESTA VELHA (regra 16-G). Segue a rodada — km
    # nenhum se perde e a regra 6 segura o ranking sozinha se nao houver
    # novidade —, mas ALGUEM PRECISA SABER. Sem este aviso, 11/08 se repete:
    # so as logos chegaram, o log disse PIPELINE CONCLUIDA e o dono descobriu
    # sozinho, processando os links na mao.
    if _COLETA_SUSPEITA_MIN and not args.force_collect:
        log.warning('   A mensagem mais nova tem %s. Vou publicar e conferir a '
                    'PROVA DE VIDA da leitura no fim da rodada — e ela que diz '
                    'se o grupo esta parado ou se eu e que nao estou recebendo.',
                    prontidao.duracao(_COLETA_SUSPEITA_MIN * 60))

    # P-146: o mes anterior ficou sem a rodada do ultimo dia? Sai agora, antes
    # do mes corrente — e so com leitura confiavel (regra 8). Periodo explicito
    # (--periodo) e quem chamou sabendo o que queria: nao entra aqui.
    if not args.periodo:
        try:
            pendente = fechamento_pendente()
            if pendente and coleta_bloqueada:
                avisar_fechamento_falhou(pendente, 'a leitura do grupo não ficou confiável')
            elif pendente:
                etapa_fechamento(pendente, mensagens, args)
        except Exception as exc:
            log.error('   Fechamento atrasado falhou: %s — a rodada segue.', exc)
            try:
                avisar_fechamento_falhou(fechamento_pendente() or '?', 'erro: %s' % exc)
            except Exception:
                pass

    if mensagens:
        txt = etapa_montar_txt(mensagens)
        if args.dry_run:
            log.info('DRY RUN — upload e envio nao serao feitos.')
            log.info('Previa do TXT:\n%s', txt.decode('utf-8')[:600] if txt else '(vazio)')
            return True

        job_id = etapa_upload(txt, periodo)
        if not job_id:
            notificar_admin('Upload', 'A API recusou o TXT da conversa.')
            return False

        resultado = etapa_poll(job_id)
        if not resultado or resultado.get('stage') != 'done':
            estagio = (resultado or {}).get('stage', 'sem resposta')
            notificar_admin('Processamento', f'Job terminou em "{estagio}".')
            return False

        # Checkpoint so avanca depois do processamento confirmado.
        novo_marco = max(m['ts'] for m in mensagens)
        # GUARDA OS ULTIMOS 5 MARCOS. E o que permite voltar tres marcos quando
        # a rodada anterior nao publicou — sem isto nao ha para onde voltar, e
        # um buraco de sincronizacao vira link perdido para sempre (P-76).
        #
        # `estado` nao existia neste escopo (NameError, 15/08): pipeline()
        # nunca chamava load_state(), so etapa_coletar() chamava, e la dentro.
        # A pipeline quebrava DEPOIS de processar e ANTES de publicar.
        estado = load_state()
        anteriores = [m for m in (estado.get('marcos') or []) if m]
        marcos = [novo_marco] + [m for m in anteriores if m != novo_marco][:4]
        save_state(last_ts=novo_marco, last_id=mensagens[-1]['id'], marcos=marcos)
        log.info('   Checkpoint salvo ate %s', _ts_str(novo_marco))
    else:
        log.info('Sem mensagens novas — seguindo para publicar o ranking atual.')
        if args.dry_run:
            log.info('DRY RUN — encerrando.')
            return True

    dados = etapa_ranking(periodo)
    if not dados:
        log.info('Ranking indisponivel — atividades ficaram salvas, nada foi publicado.')
        return True

    if args.sem_envio:
        log.info('--sem-envio: ranking obtido mas nao publicado.')
        log.info('Previa:\n%s', (dados.get('texto_whatsapp') or '')[:800])
        return True

    # --teste manda tudo para o numero de alertas em vez do grupo. Serve para
    # conferir formato sem incomodar a comunidade.
    _resolver_admin_chat()
    destino = ADMIN_CHAT_ID if args.teste else destino_publicacao()
    if em_ensaio() and not args.teste:
        log.warning('MODO ENSAIO — nada vai para o grupo. Tudo para %s '
                    '(parceiro: %s). Para voltar ao grupo, apague DESTINO_ENSAIO '
                    'do .env.', DESTINO_ENSAIO, PARCEIRO_ENSAIO or 'nenhum')
    if args.teste:
        if not ADMIN_CHAT_ID:
            log.error('--teste exige ADMIN_CHAT_ID no .env.')
            return False
        log.info('MODO TESTE — tudo vai para %s, nada para o grupo.', ADMIN_CHAT_ID)

    # A FOLGA COMBINADA. Tudo ja foi conferido; esta pausa e o ultimo respiro
    # entre "a leitura esta boa" e "isto vai para o grupo inteiro". Publicar e
    # irreversivel: nao da para despublicar no WhatsApp.
    if FOLGA_ANTES_ENVIO_SEG > 0 and not args.dry_run:
        log.info('   Folga de %s antes de publicar...',
                 prontidao.duracao(FOLGA_ANTES_ENVIO_SEG))
        time.sleep(FOLGA_ANTES_ENVIO_SEG)

    # RANKING FINAL DO ULTIMO DIA SAI SEMPRE (regra do dono, 01/10/2026): das
    # 22:00 a meia-noite do ultimo dia, o ranking vai ao grupo mesmo sem km
    # novo desde as 15h30 — e o ranking final do mes. Zerado continua barrado.
    if not args.forcar_envio and not args.periodo \
            and _na_janela_do_fechamento(periodo, time.time()):
        log.info('   Ranking FINAL do ultimo dia: sai mesmo sem km novo.')
        args.forcar_envio = True
    # ENVIO FORCADO PROGRAMADO PELO DONO (01/10/2026): o arquivo
    # scripts/.forcar_envio_ate guarda uma data/hora ISO; ate ela, o ranking sai
    # mesmo sem km novo (zerado continua barrado). Uso unico: apaga ao publicar.
    _marca_forcar = Path(__file__).resolve().parent / '.forcar_envio_ate'
    if not args.forcar_envio and _marca_forcar.exists():
        try:
            if datetime.now() < datetime.fromisoformat(_marca_forcar.read_text().strip()):
                log.info('   Envio FORCADO programado pelo dono: sai mesmo sem km novo.')
                args.forcar_envio = True
        except Exception as exc:
            log.warning('   Marca de envio forcado ilegivel (%s) — ignorada.', exc)
    pode, motivo, impressao = avaliar_envio_ranking(dados, args)
    if coleta_bloqueada:
        # Nao deixa a falta de "impressao anterior" (primeira publicacao,
        # ou estado perdido) publicar em cima de leitura que a gente ja
        # sabe que nao ficou confiavel — regra 8 tem de valer mesmo nesse
        # caso raro que `avaliar_envio_ranking` nao enxerga (A2).
        pode, motivo = False, 'leitura do grupo nao ficou confiavel nesta rodada'
    if args.teste:
        pode, motivo = True, 'modo teste'
    ids_ranking = []   # [(id, texto_da_parte), ...] — item B, confere a entrega mais abaixo
    if pode:
        if not etapa_enviar(dados, destino=destino, coletar_ids=ids_ranking):
            notificar_admin('Envio', 'Nao consegui publicar o ranking no grupo.')
            return False
        # Em teste o estado NAO avanca: senao o envio real seguinte seria
        # barrado por "os km nao mudaram".
        if not args.teste:
            save_state(ranking_hash=impressao, ranking_enviado_em=time.time(),
                       publicou_na_ultima=True)
            try:
                _marca_forcar.unlink()    # uso unico
            except FileNotFoundError:
                pass
            _guardar_retrato(periodo, impressao)    # P-146: o dia 1 compara com isto
        if motivo:
            log.info('   (%s)', motivo)
    else:
        # Nao e erro: e a regra funcionando. O grupo ainda recebe o que faz
        # sentido repetir (patrocinador/avisos), so o ranking fica de fora.
        log.info('>>> Ranking NAO publicado — %s.', motivo)
        # SO A LOGO E O SINAL MAIS FORTE DE QUE A LEITURA FALHOU (pedido do
        # dono, 26/08/2026): e aqui que a segunda opiniao vale mais, entao a
        # comparacao roda AGORA, fora do prazo dos 10 dias. Nao muda esta
        # rodada — serve para TIRAR A TEIMA e deixar registrado.
        if motivo and 'km nao mudaram' in motivo and not args.teste:
            try:
                _conferir_teima()
            except Exception as _e:
                log.info('   (nao consegui conferir com o suplente: %s)', _e)
        # MARCA A RODADA COMO "SO LOGO" (P-76). A proxima coleta volta tres
        # marcos por causa disto: quando chega so a logo, a primeira suspeita e
        # que a leitura nao trouxe o que devia — e reler e barato, perder link
        # nao. Nao gera aviso nenhum: fica no log e no proximo relatorio.
        if not args.teste:
            save_state(publicou_na_ultima=False)
        if motivo == 'ranking zerado' and not args.teste:
            avisar_ranking_zerado(periodo, destino)

    # Patrocinador e parabens saem mesmo quando o ranking foi barrado: o
    # ranking se repete, a logo do patrocinador do mes e o parabens de quem
    # bateu a meta nao.
    if not args.sem_patrocinadores:
        enviar_patrocinadores(dados.get('patrocinadores') or [], destino=destino)

    # CONFERE A ENTREGA DO RANKING, PARTE POR PARTE (17/08/2026, item B).
    #
    # So depois da logo, que vai por ultimo (regra 16-K) — checar antes
    # correria o risco de confundir a espera da logo com a mensagem ter
    # ficado parada. `enviado != entregue` (regra 16-L): o log dizia "Parte
    # 1/1 enviada" nas tres rodadas do ranking cortado de 17/08, e a parte 1
    # nunca tinha saido do servidor do WhatsApp.
    if pode and ids_ranking and not args.dry_run and not args.teste:
        ids_apenas = [i for i, _ in ids_ranking]
        resultado, minimo = conferir_entrega_por_parte(destino, ids_apenas)
        paradas = []
        for indice, ack in resultado:
            if ack is not None and ack >= minimo:
                log.info('   parte %d/%d — entregue (ack %s)', indice, len(ids_ranking), ack)
            else:
                log.warning('   parte %d/%d — parou no servidor (ack %s)',
                            indice, len(ids_ranking), ack)
                paradas.append((indice, ids_ranking[indice - 1][1]))

        if paradas:
            # REENVIA UMA VEZ SO, SEM LACO (regra 16-E) — so a(s) parte(s) que
            # ficaram, nunca o ranking inteiro. Id reenviado fica marcado por
            # estar fora de `ids_ranking`: nao ha segunda tentativa, nem que o
            # reenvio tambem pare.
            reenvio_ids = []
            for indice, texto_parte in paradas:
                log.warning('   Parte %d/%d parada — reenviando (unica tentativa)...',
                            indice, len(ids_ranking))
                _enviar_texto(destino, texto_parte, coletar_ids=reenvio_ids)
            if reenvio_ids:
                resultado2, minimo2 = conferir_entrega_por_parte(
                    destino, [i for i, _ in reenvio_ids])
                ainda_paradas = sum(1 for _, ack in resultado2
                                    if ack is None or ack < minimo2)
                if ainda_paradas:
                    notificar_admin(
                        'Envio',
                        'Reenviei %d parte(s) do ranking que ficaram paradas no servidor '
                        'do WhatsApp e tentei de novo — tambem nao chegou. Isto e sessao '
                        'doente, nao mensagem perdida.' % ainda_paradas)
                else:
                    log.info('   Reenvio confirmado: as %d parte(s) chegaram desta vez.',
                             len(reenvio_ids))
            else:
                notificar_admin(
                    'Envio',
                    'Uma parte do ranking ficou parada no servidor do WhatsApp e o '
                    'reenvio nem saiu.')

    if not args.sem_parabens and not args.teste:
        enviar_parabens(periodo)
    if not args.sem_avisos:
        enviar_avisos(com_ranking=pode,
                      destino_teste=(ADMIN_CHAT_ID if args.teste else None))
    if not args.teste:
        avisar_anticheat()
    # A fila e o que o admin despachou pelas telas do site enquanto esta
    # maquina estava desligada. Sai por ultimo, depois de tudo que e do grupo.
    if not args.sem_fila:
        # Nem a fila nem a vinculacao podem derrubar a rodada inteira: um
        # numero sem WhatsApp ou o WAHA recusando UMA mensagem nao e motivo
        # para o ranking do dia falhar.
        try:
            etapa_fila(destino_teste=(ADMIN_CHAT_ID if args.teste else None))
        except Exception as exc:
            log.error('   Fila de mensagens falhou: %s', exc)
        if not args.teste:
            try:
                etapa_vinculacoes()
            except Exception as exc:
                log.error('   Fila de vinculacao falhou: %s', exc)

    log.info('=' * 55)
    log.info('PIPELINE CONCLUIDA em %.1fs', time.time() - inicio)
    log.info('=' * 55)
    return True



# ===================== FILA DE MENSAGENS =====================
# POR QUE ISTO EXISTE
# O site fica ligado o tempo todo; esta maquina passa o dia desligada. Quando
# o admin aperta "enviar agora" numa tela, a mensagem NAO pode falhar na cara
# dele nem sumir: ela entra numa fila no site e sai aqui, assim que a maquina
# sobe. E o que faz o botao "enviar" dizer a verdade.
#
# Cada item volta marcado: enviada, ou falhou (e nesse caso volta para a fila
# ate o teto de tentativas, para uma queda de rede nao engolir a mensagem).

def _destino_da_fila(item):
    """Para onde vai: o grupo, ou o privado de um numero.

    O ENSAIO PRECISA VALER PARA O PRIVADO TAMBEM
    Ate 02/08 so o destino "grupo" era desviado; mensagem privada ia direto
    para o telefone do atleta, mesmo com DESTINO_ENSAIO preenchido. Na
    madrugada de 02/08, 10 pessoas receberam aviso de medalha as 01:23 durante
    a fase de ensaio, porque a janela da medalha abre no dia 2 e a fila drenou.

    "Nada vai para o grupo" nunca quis dizer "pode ir para as pessoas". Em
    ensaio, NADA sai para terceiros — nem grupo, nem privado.
    """
    destino = str(item.get('destino') or '').strip()
    if not destino:
        return None
    if destino.lower() in ('grupo', 'group'):
        return destino_publicacao() or None

    if em_ensaio():
        log.warning('   MODO ENSAIO: %s seria para %s — desviando para %s.',
                    item.get('tipo') or 'mensagem', destino, DESTINO_ENSAIO)
        return _jid_contato(DESTINO_ENSAIO) or DESTINO_ENSAIO

    return _jid_contato(destino)


def _quantas_na_fila():
    """Quantas mensagens estao esperando. 0 quando nao der para saber.

    Serve para o alerta dizer o TAMANHO do estrago: "a sessao caiu" preocupa;
    "a sessao caiu e tem 3 avisos de medalha parados" faz agir.
    """
    try:
        resp = _session().get(f'{API_URL}/api/fila/pendentes',
                              headers=_api_headers(), timeout=20)
        if resp.status_code != 200:
            return 0
        return len(resp.json().get('mensagens') or [])
    except Exception:
        return 0


def etapa_fila(destino_teste=None):
    """Entrega o que o site deixou na fila. Devolve True mesmo se vazia."""
    log.info('>>> Verificando a fila de mensagens do site...')
    try:
        resp = _session().get(f'{API_URL}/api/fila/pendentes',
                              headers=_api_headers(), timeout=45)
        if resp.status_code == 404:
            log.info('   API sem fila de mensagens — pulando')
            return True
        if resp.status_code != 200:
            log.warning('   /api/fila/pendentes HTTP %s', resp.status_code)
            return True
        itens = resp.json().get('mensagens') or []
    except Exception as exc:
        log.error('   Erro ao buscar a fila: %s', exc)
        return True

    if not itens:
        log.info('   Fila vazia')
        return True

    log.info('   %s mensagem(ns) na fila', len(itens))
    enviados_para = {}   # {chat: quantas} — para conferir o ack no fim
    for item in itens:
        chat = destino_teste or _destino_da_fila(item)
        rotulo = f"{item.get('tipo')} -> {item.get('destino')}"
        if not chat:
            log.warning('   %s: sem destino valido — marcando como falha', rotulo)
            _marcar_fila(item['id'], 'falhou', 'destino invalido')
            continue

        texto = item.get('texto') or ''
        # PODE SER UMA PESSOA OU MUITAS (15/08/2026). O aviso de medalhas no
        # grupo marca todos os medalhistas de uma vez, e o site manda os
        # numeros separados por virgula. Quem nao existir no WhatsApp fica de
        # fora da marcacao — o nome continua no texto.
        mentions = None
        if item.get('mencionar'):
            texto, mentions = _texto_com_marcacoes(texto, str(item['mencionar']).split(','))

        # A IMAGEM NUNCA DERRUBA O TEXTO (14/08). Se a foto nao for, o aviso
        # sai mesmo assim — sem ele a pessoa nao sabe que tem medalha para
        # buscar. `_enviar_imagem` devolve False em vez de estourar, entao esta
        # queda para o texto acontece de verdade.
        ok = False
        try:
            if item.get('imagem_url'):
                ok = _enviar_imagem(chat, item['imagem_url'], texto, rotulo)
            if not ok:
                if item.get('imagem_url'):
                    log.warning('   %s: seguindo so com o texto', rotulo)
                ok = _enviar_texto(chat, texto, mentions=mentions)
        except Exception as exc:
            log.error('   %s: %s', rotulo, exc)
            ok = False

        if destino_teste:
            log.info('   [TESTE] %s -> nao marcado', rotulo)
            continue
        _marcar_fila(item['id'], 'enviada' if ok else 'falhou',
                     None if ok else 'falha no envio pelo WAHA')
        log.info('   %s %s', 'OK  ' if ok else 'FALHOU', rotulo)
        if ok:
            enviados_para.setdefault(chat, 0)
            enviados_para[chat] += 1
        time.sleep(6)   # rajada no privado e o que mais chama atencao do WhatsApp

    # ENVIADO NAO E ENTREGUE (13/08). Depois de drenar a fila, conferir o `ack`
    # de cada destino: mensagem parada em 1 nunca chegou no aparelho, e o admin
    # precisa saber disso HOJE — nao quando alguem reclamar que nao recebeu.
    for chat, quantas in (enviados_para or {}).items():
        entregues, paradas = conferir_entrega(chat, quantas=max(quantas, 2))
        if paradas and not entregues:
            log.error('   ENTREGA TRAVADA em %s: %s mensagem(ns) no servidor do '
                      'WhatsApp, nenhuma no aparelho.', chat, paradas)
            notificar_admin(
                'Mensagens enviadas mas NAO entregues',
                'O que aconteceu: %s mensagem(ns) sairam daqui e ficaram '
                'paradas no servidor do WhatsApp — o aparelho de destino nao '
                'recebeu. O envio funcionou; a entrega, nao.\n\n'
                'O QUE FAZER, nesta ordem:\n'
                '1) Confira se o celular que deveria receber esta ligado, com '
                'internet e com o WhatsApp aberto. Na maioria das vezes e so '
                'isso, e as mensagens entram sozinhas quando ele voltar.\n'
                '2) Se o aparelho esta normal e mesmo assim nao chegou, o '
                'numero pode ter trocado de aparelho ou bloqueado este '
                'contato.\n'
                '3) Nada precisa ser reenviado por enquanto: o WhatsApp '
                'entrega sozinho assim que o aparelho voltar.' % paradas)
        elif paradas and entregues:
            log.warning('   Entrega parcial em %s: %s no aparelho, %s ainda no '
                        'servidor.', chat, entregues, paradas)
        elif entregues:
            log.info('   Entrega confirmada em %s: %s no aparelho.',
                     chat, entregues)
    return True


def _marcar_fila(mensagem_id, situacao, detalhe=None):
    try:
        _session().post(f'{API_URL}/api/fila/marcar', headers=_api_headers(),
                        json={'id': mensagem_id, 'situacao': situacao,
                              'detalhe': detalhe}, timeout=20)
    except Exception as exc:
        log.warning('   Enviado mas nao marcado (%s): %s', mensagem_id, exc)


# ===================== VINCULACAO DE WHATSAPP =====================
# Quando o admin de uma cidade pede para vincular o numero dele e esta maquina
# esta desligada, o pedido fica numa fila no site. Aqui a gente atende: cria a
# sessao daquela cidade, pega o codigo de pareamento e manda pelo WhatsApp do
# responsavel. E isto que permite ao admin de outra cidade se virar sozinho.

def etapa_vinculacoes():
    log.info('>>> Verificando pedidos de vinculacao de WhatsApp...')
    try:
        resp = _session().get(f'{API_URL}/api/waha/pendentes',
                              headers=_api_headers(), timeout=30)
        if resp.status_code == 404:
            log.info('   API sem fila de vinculacao — pulando')
            return True
        if resp.status_code != 200:
            log.warning('   /api/waha/pendentes HTTP %s', resp.status_code)
            return True
        pedidos = resp.json().get('pendentes') or []
    except Exception as exc:
        log.error('   Erro ao buscar vinculacoes: %s', exc)
        return True

    if not pedidos:
        log.info('   Nenhum pedido de vinculacao')
        return True

    for pedido in pedidos:
        sessao = pedido.get('sessao') or 'default'
        telefone = _so_digitos(pedido.get('telefone') or '')
        responsavel = _jid_contato(pedido.get('numero_responsavel') or '')
        log.info('   Pedido %s: cidade %s / sessao %s / numero %s',
                 pedido.get('id'), pedido.get('cidade'), sessao, telefone)
        _marcar_vinculacao(pedido['id'], 'em_andamento')

        try:
            if pedido.get('recriar'):
                _session().delete(f'{WAHA_BASE}/api/sessions/{sessao}',
                                  headers=_waha_headers(), timeout=30)
                time.sleep(3)
            # UMA SESSAO POR INSTANCIA (DICIONARIO secao 7, pendencia P-02).
            # Sobra de sessao nasce justamente aqui: a instancia fica com a
            # antiga e ganha a nova, e foi assim que a 3000 acabou com "default"
            # e "principal-pls" juntas em 02/08. A faxina so tira o que NAO tem
            # copia gravada — apagar sessao com copia troca um restart por um QR
            # Code na mao de alguem.
            for nome_sobra, acao, motivo in wahasessao.limpar_instancia(
                    WAHA_BASE, WAHA_API_KEY, manter=sessao):
                log.info('   faxina: %s -> %s (%s)', nome_sobra or '-', acao, motivo)
            # E OS RESTOS DELA — BANCO, VOLUME E COPIA (13/09/2026, regra 15).
            #
            # `limpar_instancia` tira a sessao sobrando de DENTRO da instancia,
            # mas o banco de credencial, o volume e a copia em arquivo ficavam
            # para sempre. `faxina_sessao.py` existe desde 16/08 para recolher
            # isso e NUNCA foi chamado por ninguem — e a divida que o proprio
            # REGRAS.md descreve: "rotina escrita e nunca chamada e divida, nao
            # recurso". Medido em 13/09: dois bancos e dois volumes orfaos
            # parados na maquina.
            #
            # O pareamento e a hora natural (regra 15), e a faxina traz as
            # travas dela: so' recolhe o que NAO existe mais no WAHA, nunca o
            # que aparece no compose, no .env ou num container de pe, e nao
            # apaga copia de sessao viva. Falhou? a rodada segue: recolher lixo
            # nunca pode atrapalhar publicar.
            try:
                _faxina = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name('faxina_sessao.py')),
                     '--apagar'],
                    capture_output=True, text=True, timeout=180)
                for _linha in (_faxina.stdout or '').splitlines():
                    _limpa = _linha.strip()
                    if _limpa.startswith(('apagado:', 'volume ', 'copia ')):
                        log.info('   faxina de restos: %s', _limpa)
            except Exception as _exc:
                log.info('   (a faxina de restos nao rodou: %s)', _exc)
            # Cria a sessao se ainda nao existir (409 = ja existe, tudo bem).
            _session().post(f'{WAHA_BASE}/api/sessions', headers=_waha_headers(True),
                            json={'name': sessao, 'start': True}, timeout=40)
            # O WEBJS sobe um Chromium: 60-90s a frio. Sem esta espera o
            # pedido de codigo volta 500.
            time.sleep(75)
            r = _session().post(f'{WAHA_BASE}/api/{sessao}/auth/request-code',
                                headers=_waha_headers(True),
                                json={'phoneNumber': telefone}, timeout=60)
            codigo = (r.json() or {}).get('code') if r.status_code in (200, 201) else None
        except Exception as exc:
            log.error('   Falha ao criar sessao/codigo: %s', exc)
            codigo = None

        if not codigo:
            _marcar_vinculacao(pedido['id'], 'falhou', 'nao consegui o codigo de pareamento')
            log.warning('   Pedido %s: sem codigo', pedido.get('id'))
            continue

        aviso = (f"Codigo de pareamento do WhatsApp para *{pedido.get('cidade')}*:\n\n"
                 f"*{codigo}*\n\n"
                 "No celular: WhatsApp > Aparelhos conectados > Conectar um aparelho "
                 "> Conectar com numero de telefone. Vale cerca de 1 minuto.\n\n"
                 "_mensagem automatica do sistema_")
        alvo = _jid_contato(telefone) or responsavel
        entregue = _enviar_texto(alvo, aviso) if alvo else False
        if responsavel and responsavel != alvo:
            _enviar_texto(responsavel, aviso)
        _marcar_vinculacao(pedido['id'], 'atendido' if entregue else 'falhou',
                           codigo=codigo,
                           detalhe=None if entregue else 'codigo gerado mas nao entregue')
        log.info('   Pedido %s: codigo %s %s', pedido.get('id'), codigo,
                 'entregue' if entregue else 'NAO entregue')
    return True


def _marcar_vinculacao(pedido_id, situacao, codigo=None, detalhe=None):
    try:
        _session().post(f'{API_URL}/api/waha/marcar', headers=_api_headers(),
                        json={'id': pedido_id, 'situacao': situacao,
                              'codigo': codigo, 'detalhe': detalhe}, timeout=20)
    except Exception as exc:
        log.warning('   Nao consegui marcar a vinculacao %s: %s', pedido_id, exc)

def main():
    parser = argparse.ArgumentParser(description='Automacao Movimente-se V6.1')
    parser.add_argument('--dry-run', action='store_true',
                        help='Coleta e monta o TXT, mas nao envia nada')
    parser.add_argument('--diagnose', action='store_true', help='Confere cada peca e sai')
    parser.add_argument('--monitor', action='store_true',
                        help='So checa saude; silencio se estiver tudo bem')
    parser.add_argument('--force-collect', action='store_true',
                        help=f'Ignora o checkpoint e recolhe os ultimos {FORCE_DIAS} dias')
    parser.add_argument('--periodo', help='Periodo AAAA-MM (padrao: mes corrente)')
    parser.add_argument('--sem-envio', action='store_true',
                        help='Processa e busca o ranking, mas nao publica no grupo')
    # Logo do patrocinador com legenda e parabens de meta batida fazem parte da
    # publicacao normal — nao sao extras. Ficam ligados por padrao; os
    # parametros abaixo servem so para desligar em teste.
    parser.add_argument('--sem-patrocinadores', action='store_true',
                        help='NAO publicar as logos/legendas dos patrocinadores')
    parser.add_argument('--sem-parabens', action='store_true',
                        help='NAO disparar os parabens de meta batida')
    parser.add_argument('--so-fila', action='store_true',
                        help='So entrega a fila de mensagens do site e sai')
    parser.add_argument('--sem-fila', action='store_true',
                        help='Pula a fila de mensagens do site')
    parser.add_argument('--sem-avisos', action='store_true',
                        help='NAO disparar os avisos programados desta rodada')
    parser.add_argument('--teste', action='store_true',
                        help='Manda tudo para o seu numero (ADMIN_CHAT_ID) em vez do '
                             'grupo. Nao mexe no estado nem dispara parabens.')
    parser.add_argument('--alertar', metavar='TEXTO',
                        help='So avisa o responsavel e sai. Usa a cadeia '
                             'completa: WhatsApp cruzado, depois e-mail. E o '
                             'que a parada limpa do Windows chama quando o '
                             'ranking nao saiu.')
    parser.add_argument('--etapa', default='Parada da maquina',
                        help='Etapa a registrar junto com --alertar')
    parser.add_argument('--laudo', action='store_true',
                        help='Antes de desligar: confere se a proxima rodada '
                             'vai acontecer e avisa se nao vai. Sai 0 quando '
                             'esta tudo pronto, 1 quando nao esta.')
    parser.add_argument('--forcar-envio', action='store_true',
                        help=f'Ignora a trava de {MIN_INTERVALO_HORAS}h e a checagem '
                             'de "km nao mudaram" e publica assim mesmo')
    args = parser.parse_args()

    # Marca a rodada como ensaio ANTES de qualquer coisa poder alertar
    # (regra 16-J-A). Tem de ser aqui e nao la embaixo: a etapa 1 ja e capaz
    # de chamar `notificar_admin`.
    global _EM_ENSAIO
    _EM_ENSAIO = bool(getattr(args, 'dry_run', False) or getattr(args, 'teste', False))

    if args.periodo and not re.fullmatch(r'\d{4}-\d{2}', args.periodo):
        parser.error('--periodo deve estar no formato AAAA-MM')

    if args.alertar:
        # Caminho curto e sem trava: nao coleta, nao publica, nao mexe em
        # estado. So avisa. Precisa ser barato porque quem chama esta com a
        # maquina prestes a hibernar.
        log.info('>>> Alerta pedido de fora: %s', args.etapa)
        sys.exit(0 if notificar_admin(args.etapa, args.alertar) else 1)

    if args.laudo:
        # Sem trava e sem estado: nao coleta, nao publica. Quem chama esta com a
        # maquina prestes a desligar, entao tem de ser barato e terminar.
        sys.exit(0 if etapa_laudo() else 1)

    if args.diagnose:
        sys.exit(0 if modo_diagnostico() else 1)
    if args.monitor:
        sys.exit(0 if modo_monitor() else 1)
    if args.so_fila:
        # Caminho curto: nao coleta, nao publica ranking. So entrega o que o
        # admin despachou pelas telas. Serve para a maquina subir, esvaziar a
        # fila e desligar.
        if not etapa_api():
            sys.exit(1)
        if not etapa_waha(espera=WAHA_ESPERA):
            # ANTES DE DESISTIR, TENTA A OUTRA SESSAO (17/08/2026, item A) —
            # mesma logica da pipeline completa: a fila sai pelo suplente se
            # for o unico de pe, em vez de ficar parada ate a sessao de
            # sempre voltar.
            papel_assumido = assumir_sessao_viva()
            if not (papel_assumido and etapa_waha(espera=0)):
                # AVISAR AQUI TAMBEM (14/08/2026). Este ramo saia com `exit(1)` e
                # NINGUEM ficava sabendo: so a rodada completa avisava. Como esta
                # tarefa roda de 15 em 15 minutos, era ela quem descobria a sessao
                # morta primeiro — e calava.
                #
                # Foi assim em 14/08: a sessao caiu as 11:22, esta tarefa viu as
                # 11:35, saiu em silencio, e o dono ficou olhando tres mensagens de
                # medalha paradas sem nenhum aviso. O e-mail sai pelo SITE, que nao
                # depende do WAHA nem desta maquina (P-04), e o proprio site anexa o
                # botao que leva a tela de reconectar (regra 21-E) — por isso o
                # texto daqui nao carrega link nenhum.
                quantas = _quantas_na_fila()
                recado = (
                    'O WhatsApp da automacao esta desconectado e precisa ser '
                    'pareado de novo — reiniciar sozinho nao resolve mais.')
                if quantas:
                    recado += (' Tem %d mensagem(ns) esperando na fila; nenhuma '
                               'delas se perde, todas saem assim que o WhatsApp '
                               'voltar.' % quantas)
                notificar_admin('Sessao WhatsApp', recado)
                sys.exit(1)
            log.warning('   FILA SEGUE PELA SESSAO %s (%s) — a de sempre nao respondeu.',
                        WAHA_SESSION, papel_assumido)
        # A COPIA NAO PODE ENVELHECER (12/09/2026). Esta passada roda de 15 em
        # 15 minutos e e o unico lugar barato que ve a sessao de pe fora da
        # publicacao — por isso a vigilancia da copia mora aqui, e nao na
        # pipeline completa (reiniciar a sessao no meio de um ranking seria
        # trocar um risco por outro pior). Detalhe em `forcar_copia_se_velha`.
        try:
            forcar_copia_se_velha()
        except Exception as exc:
            log.info('   (vigilancia da copia falhou: %s)', exc)
        try:
            vigiar_outras_sessoes()
        except Exception as exc:
            log.info('   (vigilancia das outras sessoes falhou: %s)', exc)
        _resolver_admin_chat()
        # Com --teste tudo vai para o numero do admin, nao para o grupo:
        # serve para conferir o encanamento sem incomodar ninguem.
        etapa_fila(destino_teste=(ADMIN_CHAT_ID if args.teste else None))
        if not args.teste:
            etapa_vinculacoes()
        sys.exit(0)

    try:
        with Trava():
            sys.exit(0 if pipeline(args) else 1)
    except RuntimeError as exc:
        log.error('%s', exc)
        sys.exit(3)


if __name__ == '__main__':
    main()
