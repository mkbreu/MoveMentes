# app/blueprints/processing.py
# -*- coding: utf-8 -*-
"""
Processamento de TXT do WhatsApp: extrai links Strava, scrapa dados
via __NEXT_DATA__ JSON, salva atividades e chama anti-cheat.
"""
import calendar
import html as _html
import json
import hashlib
import re
import threading
import time
import logging
from datetime import datetime, date, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode
from flask import Blueprint
import requests
from bs4 import BeautifulSoup
from sqlalchemy import text
from app import db
from app.models import Activity, ProcessedFile, Config
from app.ratepacer import SharedPacer
from app import stravasinal

logger = logging.getLogger(__name__)

# ============================================================
# FORCA IPv4 NA RESOLUCAO DE DNS (20/08/2026)
#
# O `_HARD_REQUEST_TIMEOUT` abaixo existe desde 19/07/2026 por causa de
# `getaddrinfo()` nao respeitar o `timeout` do `requests` — mas medido ao
# vivo em 20/08 (importacao de atleta, perfil de 62 meses) ele CONTINUOU
# preso por bem mais que os 75s do wrapper, duas vezes na mesma rodada,
# sempre no meio de uma sequencia longa de requisicoes seguidas. A causa
# mais provavel para "join(75) nao bastou" e a mais comum desse sintoma em
# redes domesticas: o resolvedor tenta IPv6 primeiro, nao ha rota de
# verdade para IPv6, e a espera pelo timeout do IPv6 (que pode passar de
# 75s dependendo do SO) acontece ANTES do socket em si existir — o
# `_hard_get` mede o tempo da chamada inteira, entao herda essa demora.
#
# `allowed_gai_family` e o gancho documentado do urllib3 (usado por
# `requests`) para isso: forca so IPv4 na resolucao, sem monkeypatch em
# `socket` global (que afetaria coisas fora do escopo de HTTP). Nao
# resolve a causa raiz se for outra coisa — fica registrado como P-80 em
# `PENDENCIAS.md` ate confirmar com um profiler ao vivo — mas e a correcao
# de maior probabilidade, baixo risco (o Strava atende IPv4 normalmente) e
# zero custo se a causa for outra (nao piora nada).
try:
    import socket as _socket
    import urllib3.util.connection as _urllib3_conn

    def _forcar_ipv4(*args, **kwargs):
        return _socket.AF_INET

    _urllib3_conn.allowed_gai_family = _forcar_ipv4
    logger.info('processing: DNS forcado para IPv4 (mitigacao P-80)')
except Exception as _exc:
    logger.warning('processing: nao consegui forcar IPv4 na resolucao de DNS: %s', _exc)
# ============================================================

processing_bp = Blueprint('processing', __name__, url_prefix='/processing')

# ===== CONSTANTES =====
def _esporte_cru(source):
    """O `sportType` como o Strava escreveu, sem passar pelo SPORT_MAP."""
    try:
        bruto = (source.get('activityKind') or {}).get('sportType')
    except AttributeError:
        bruto = None
    bruto = (bruto or '').strip()
    return bruto[:30] or None


SPORT_MAP = {
    'run':               'corrida_caminhada',
    'walk':              'corrida_caminhada',
    'hike':              'corrida_caminhada',
    'virtualrun':        'corrida_caminhada',
    'trailrun':          'corrida_caminhada',
    'ride':              'ciclismo',
    'virtualride':       'ciclismo',
    'ebikeride':         'ciclismo',
    'mountainbikeride':  'ciclismo',
    'gravelride':        'ciclismo',
}

WA_LINE_RE    = re.compile(
    r'^\[?(\d{1,2}/\d{1,2}/\d{4})[, ]+\d{1,2}:\d{2}(?::\d{2})?(?:\]?\s*[-–]\s*|\]\s*)(.+?):\s*(.*)'
)
WA_PHONE_RE   = re.compile(r'^\+55[\s\-\(]?\d{2}[\s\-\)]?\s?\d{4,5}[\s\-]?\d{4}$')
# Aceita sufixos de caminho (/flyover, /embed/<hash>, /overview...) e query
# para nao perder o share_sig; a normalizacao depois reduz tudo a /activities/<id>.
STRAVA_DIRECT_RE = re.compile(
    r'https?://(?:www\.)?strava\.com/activities/\d+(?:/[\w-]+)*(?:\?[^\s<>"\]]+)?',
    re.IGNORECASE
)
STRAVA_SHORT_RE  = re.compile(r'https?://strava\.app\.link/\S+', re.IGNORECASE)
STRAVA_PROFILE_RE = re.compile(
    r'https?://(?:www\.)?strava\.com/athletes/(\d+)(?:[/?#].*)?$',
    re.IGNORECASE,
)
# Links do PROPRIO site (07/09/2026): o dono cola o link da pagina publica
# do atleta ou da tela de ajuste manual, em vez do link do Strava. Aceita
# qualquer host de proposito (teste, IP interno, producao) -- o padrao do
# caminho ja e especifico o suficiente.
SISTEMA_ATLETA_RE = re.compile(r'https?://[^/\s]+/ath/(\d+)(?:[/?#].*)?$', re.IGNORECASE)
SISTEMA_AJUSTE_RE = re.compile(
    r'https?://[^/\s]+/admin/atletas/(\d+)/ajuste-manual(?:[/?#].*)?$', re.IGNORECASE)


def resolver_link_do_sistema(url):
    """Se `url` for um link do proprio site (`/ath/<id>` ou
    `/admin/atletas/<id>/ajuste-manual`), devolve o link de perfil Strava
    equivalente. Senao, devolve None."""
    m = SISTEMA_ATLETA_RE.match(url) or SISTEMA_AJUSTE_RE.match(url)
    if not m:
        return None
    return f'https://www.strava.com/athletes/{m.group(1)}'

SCRAPE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
}
_LINK_CACHE_READY = False

# ===== IMPORTACAO POR PERFIL (sessao autenticada) =====
# A listagem publica de atividades de um perfil foi aposentada pelo Strava: o
# endpoint /athletes/<id>/interval (o mesmo que a pagina de perfil chama ao
# trocar de mes) responde 401 sem sessao. Guardamos o cookie no cofre
# (app/secretstore.py) e o usamos SOMENTE para listar os IDs do periodo; a
# pagina de cada atividade continua sendo lida sem cookie, publicamente, o que
# mantem a dupla checagem dos dados e nao vincula o scraping a uma conta.
STRAVA_SESSION_COOKIE_NAME = '_strava4_session'
# Marcas do formulario de login (resposta 200 que na verdade e "faca login").
_STRAVA_LOGIN_MARKERS = (
    'session[email]', 'session[password]', 'id="login-form"',
)

# Teto global de duracao do scraping por job: ao atingir, o job finaliza com
# resultados parciais e os links restantes viram pendencias (nunca fica preso).
_JOB_DEADLINE_SECONDS = 20 * 60
# Teto absoluto: mesmo com controle de taxa, nunca deixar um job passar disto.
_JOB_DEADLINE_MAX = 6 * 60 * 60

# ===== CONTROLE DE TAXA (rate limiter) =====
# Perfis por opcao de processamento. "interval" e o minimo de segundos entre
# requisicoes HTTP ao Strava; "batch_pause" e a pausa apos "batch_size"
# requisicoes. Os numeros mantem a coleta em ~10-15 req/min -- a faixa segura
# equivalente ao limite padrao da API do Strava, abaixo do radar de deteccao.
# "cycle_size" e o tamanho do lote de atividades NOVAS por ciclo completo
# (resolver lote -> scrapear lote -> repetir): o scraping comeca cedo e uma
# interrupcao perde no maximo o andamento do ciclo atual, nunca o ja salvo.
# "deadline_max" e o teto de UMA rodada do job. Rodadas sao curtas de
# proposito: ao bater o teto com progresso real, o proprio sistema agenda uma
# continuacao automatica (ver upload.py) que segue de onde parou -- qualquer
# tamanho de backlog termina sozinho, em rodadas limitadas, sem job eterno.
STRAVA_THROTTLE_PROFILES = {
    'mes_atual':          {'interval': 4.0, 'batch_pause': 60.0,   # ~15/min
                           'cycle_size': 400, 'deadline_max': 2 * 3600},
    'mes_atual_anterior': {'interval': 4.6, 'batch_pause': 60.0,   # ~13/min
                           'cycle_size': 500, 'deadline_max': 3 * 3600},
    # 9s DA' EXATAMENTE 100 A CADA 15 MINUTOS (28/08/2026), que e' o teto do
    # Strava: o espacamento e o teto passam a dizer a mesma coisa, em vez de
    # um permitir o dobro do outro (6s dava ~150/15min).
    'periodo_especifico': {'interval': 9.0, 'batch_pause': 100.0,  # ~6,7/min
                           'cycle_size': 300, 'deadline_max': 3 * 3600},
    'ano_especifico':     {'interval': 9.0, 'batch_pause': 100.0,  # ~6,7/min
                           'cycle_size': 400, 'deadline_max': 6 * 3600},
    'todos_periodos':     {'interval': 9.0, 'batch_pause': 100.0,  # ~6,7/min
                           'cycle_size': 500, 'deadline_max': 8 * 3600},
}
# Modo desconhecido anda no ritmo seguro, nunca no rapido. Os perfis do mes
# corrente (o ranking) seguem como estavam: sao poucas dezenas de links e a
# saida deles e' prioridade (regra 4-R).
_STRAVA_THROTTLE_DEFAULT = {'interval': 9.0, 'batch_pause': 60.0}
# Prazo absoluto de uma unica requisicao HTTP, contando resolucao de DNS.
# O timeout do requests NAO cobre getaddrinfo: com DNS doente no NAS a thread
# fica pendurada para sempre (visto em producao em 2026-07-19). Estourou o
# prazo: a requisicao e abandonada e o link vira pendencia retentavel.
_HARD_REQUEST_TIMEOUT = 75.0
# Fatia da espera do _hard_get. A espera nao pode ser um bloco unico e MUDO:
# ver o comentario do proprio _hard_get.
_HARD_JOIN_SLICE = 5.0
# Prazo de UM link inteiro, somando todas as tentativas dele (pagina normal,
# URL alternativa e flyover). Sem este teto, um link que nao responde consome
# 4 requisicoes de 75s -- 5 minutos de fila do Strava gastos num link so, o
# que a regra 4-C proibe na pratica.
_LINK_BUDGET_SEG = 150.0
# Prazo para INTERPRETAR a pagina ja baixada (BeautifulSoup + extracao). Achado
# ao vivo em 20-21/08/2026: a requisicao HTTP em si e protegida pelo
# _HARD_REQUEST_TIMEOUT, mas o parse rodava sem nenhum teto -- 20 travamentos
# de ~720s numa unica importacao (madrugada de 21/08), sempre logo apos
# "Scraping: <url>", sem nenhum log de timeout de rede, confirmando que o
# tempo ia embora DEPOIS da pagina chegar. Mesmo padrao do _hard_get, agora
# tambem para o trecho que so usa CPU (sem rede).
_PARSE_HARD_TIMEOUT = 30.0
_STRAVA_THROTTLE_BATCH_SIZE = 100
_STRAVA_MIN_INTERVAL = 3.0
_STRAVA_MAX_INTERVAL = 30.0
# Backoff progressivo quando o Strava responde 429/403(cloudflare)/503.
_STRAVA_BACKOFF_SCHEDULE = (30.0, 120.0, 300.0)
# Apos N falhas consecutivas de scraping (timeout / sem dados), assume bloqueio.
_STRAVA_BLOCK_FAILURE_STREAK = 5
_STRAVA_BLOCK_PAUSE = 180.0
# Etapas de pendencia que NAO adianta retentar (o link em si e ruim). Todas as
# demais (timeout, rate_limit, shortlink, sem dados) sao retentaveis: uma nova
# rodada de processamento tem chance de resolve-las.
_PENDING_NONRETRYABLE_STAGES = frozenset(
    {'validacao', 'inexistente', 'privada', 'link_morto',
     'indisponivel', 'sem_distancia', 'nao_atividade'}
)
# ETAPA DIZ O QUE ACONTECEU, E SE ADIANTA REPETIR (27/08/2026).
# Antes tudo virava "distancia invalida" — um saco onde cabia bloqueio do
# Strava, atividade de terceiro, atividade sem distancia e defeito nosso de
# leitura. Repetir só adianta em três: bloqueio, falta de sessão e leitura.
_MOTIVO_DA_ETAPA = {
    'bloqueio':      ('O Strava limitou o acesso agora. Repetir mais tarde '
                      'costuma resolver.', True),
    'sem_sessao':    ('O Strava pediu login: a sessão do cookie não valeu. '
                      'Recolar o cookie do Strava resolve.', True),
    'indisponivel':  ('A atividade não está disponível para a conta do cookie '
                      '(o Strava devolveu o painel). Repetir com a mesma conta '
                      'dá no mesmo.', False),
    'inexistente':   ('A atividade não existe mais no Strava.', False),
    'privada':       ('A atividade não é pública (perfil privado ou só para '
                      'seguidores).', False),
    'sem_distancia': ('A atividade não tem distância (musculação, ioga, '
                      'fragmento de GPS). Não é perda.', False),
    'sem_data':      ('Li a distância mas não achei a data na página.', True),
    'sem_dono':      ('Li a atividade mas não achei de quem ela é. Sem dono o '
                      'quilômetro não entra — seria de qualquer um.', True),
    'formato':       ('Nenhum dos leitores entendeu esta página do Strava.', True),
    'timeout':       ('O Strava demorou demais para responder.', True),
    # LINK DO STRAVA QUE NÃO É ATIVIDADE (15/09/2026, regra do dono).
    # Um atleta compartilhou pelo aplicativo o PERFIL dele
    # (strava.app.link → strava.com/athletes/<id>) achando que estava postando
    # a caminhada. Não há distância nesse endereço, então nunca ia resolver —
    # e como "shortlink não resolvido" era retentável, o link voltava em TODA
    # rodada, para sempre, sem ninguém avisar. Ele ficou fora do ranking em
    # silêncio. Vale para qualquer endereço do Strava que não seja atividade:
    # perfil, clube, segmento, rota.
    'nao_atividade': ('O link é do Strava, mas não é de uma atividade — não há '
                      'distância nele. Só o link da atividade conta.', False),
}
# Marcas de pagina de bloqueio no corpo da resposta. Amplas (para 403/503) e
# especificas de challenge (para respostas 200 que na verdade sao CloudFlare).


class ScrapeCancelled(Exception):
    """Cancelamento solicitado pelo usuario durante uma requisicao de scraping."""


class StravaRateLimited(Exception):
    """Backoff esgotado: o Strava manteve o bloqueio. Encerra o job com parciais."""


class ScrapeTimeout(Exception):
    """Timeout de rede ao buscar/resolver um link. Pendencia retentavel."""


class ScrapePermanent(Exception):
    """Link definitivamente indisponivel: atividade apagada, perfil fechado ou
    shortlink morto. Pendencia PERMANENTE -- retentar nunca vai funcionar."""

    def __init__(self, motivo, stage='indisponivel'):
        super().__init__(motivo)
        self.motivo = motivo
        self.stage = stage


class StravaLimiteDeVolume(Exception):
    """O TETO DE REQUISICOES (do dia, da hora ou dos 15 min) acabou.

    NAO e' erro de leitura e NAO e' bloqueio do Strava: e' a NOSSA trava, para
    nao CHEGAR no bloqueio. Quem recebe deve CONGELAR o trabalho e voltar
    quando a janela virar — nunca insistir, nunca marcar como falha.

    Existe porque ate 27/08/2026 havia espacamento e nao havia teto: 1.312
    requisicoes num dia, 522 numa hora, e o Strava respondeu 429 + 403.
    """

    def __init__(self, janela, segundos):
        super().__init__('Teto de requisicoes ao Strava atingido (%s). '
                         'Volta em %d minuto(s).' % (janela, int(segundos // 60)))
        self.janela = janela
        self.segundos = float(segundos)


class StravaAuthRequired(Exception):
    """Cookie de sessao do Strava ausente, invalido ou expirado."""


def _hard_get(url, kwargs, pulso=None, cancel_check=None):
    """requests.get com prazo total, imune a travamento de DNS/SO.

    Roda a requisicao em thread propria e espera no maximo
    _HARD_REQUEST_TIMEOUT. Se estourar, abandona a thread (daemon) e levanta
    ScrapeTimeout -- o job segue para o proximo link em vez de congelar.

    ESPERAR NAO PODE SER FICAR MUDO (P-80, medido em 20/08/2026).
    A espera era um `join(75)` unico, e nada era dito ao job durante ela. Um
    unico link pode fazer QUATRO requisicoes (pagina normal e alternativa,
    cada uma com flyover): 4 x 75s de silencio, mais o espacamento de 6s entre
    elas, dao **318 segundos sem um batimento** -- acima dos 300s em que a
    importacao de atleta declara um job morto. O trabalho nunca esteve
    travado: ele foi declarado morto enquanto esperava, e a "retomada"
    reiniciava tudo. Era essa a metade que faltava para explicar o laco de
    20/08 (ERROS.md 4.16).

    Agora a espera e fatiada e o `pulso` avisa que o job esta vivo. O prazo
    total NAO muda -- 75s continua sendo o teto de uma requisicao.

    CANCELAMENTO (22/08/2026): ate aqui, a espera fatiada so checava tempo e
    dava pulso -- nunca cancelamento. Um clique em "Cancelar" podia ficar sem
    efeito por ate 75s (varias vezes, se o link disparasse ate 4 requisicoes
    encadeadas). Com `cancel_check`, cada volta do laco confere ANTES do
    pulso -- cancelado nao gasta nem o pulso a toa. A thread em si continua
    sem poder ser matada (Python nao mata thread): ela fica abandonada, rodando
    sozinha, exatamente como ja acontecia no caminho de timeout.
    """
    resultado = {}

    def _worker():
        try:
            resultado['resp'] = requests.get(url, **kwargs)
        except Exception as exc:
            resultado['exc'] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    inicio = time.monotonic()
    while True:
        t.join(_HARD_JOIN_SLICE)
        if not t.is_alive():
            break
        if cancel_check is not None and cancel_check():
            logger.info('Requisicao abandonada por cancelamento: %s', url)
            raise ScrapeCancelled()
        esperando = time.monotonic() - inicio
        if esperando >= _HARD_REQUEST_TIMEOUT:
            logger.warning('Requisicao abandonada por prazo (%ss): %s',
                           int(_HARD_REQUEST_TIMEOUT), url)
            raise ScrapeTimeout()
        if pulso is not None:
            try:
                pulso(esperando)
            except Exception:
                pass
    if 'exc' in resultado:
        raise resultado['exc']
    return resultado['resp']


def _hard_run(func, timeout=_PARSE_HARD_TIMEOUT, url='', cancel_check=None):
    """Roda func() (sem argumentos) numa thread separada, com prazo.

    MESMO PADRAO DO _hard_get, agora para trechos que nao fazem requisicao
    de rede nenhuma -- interpretar o HTML ja baixado (BeautifulSoup, extracao
    por regex). Achado ao vivo em 20-21/08/2026 (importacao do perfil
    <id-3>): 20 travamentos de ~720s numa madrugada so, sempre logo apos
    "Scraping: <url>" no log e SEM nenhum aviso de timeout de rede -- ou seja,
    a pagina chegava certinha e o tempo sumia depois, interpretando ela. Uma
    pagina grande (Strava passa de 1MB) num NAS de 512MB/CPU fraca pode
    prender o interprete inteiro (o parser e Python puro, nao libera o GIL).

    Assim como no `_hard_get`, Python nao mata thread: se estourar o prazo, a
    thread trava sozinha, abandonada, e o job segue para o proximo link --
    melhor perder uma pagina do que a rodada inteira.
    """
    resultado = {}

    def _worker():
        try:
            resultado['valor'] = func()
        except Exception as exc:
            resultado['exc'] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    inicio = time.monotonic()
    while True:
        t.join(_HARD_JOIN_SLICE)
        if not t.is_alive():
            break
        if cancel_check is not None and cancel_check():
            logger.info('Interpretacao da pagina abandonada por cancelamento: %s', url)
            raise ScrapeCancelled()
        esperando = time.monotonic() - inicio
        if esperando >= timeout:
            logger.warning('Interpretacao da pagina abandonada por prazo (%ss): %s',
                           int(timeout), url)
            raise ScrapeTimeout()
    if 'exc' in resultado:
        raise resultado['exc']
    return resultado.get('valor')


class StravaThrottle:
    """Controla a taxa de requisicoes ao Strava de forma cancelavel e leve.

    - Garante um intervalo minimo entre requisicoes (pacing).
    - Faz uma pausa periodica a cada lote (batch) de requisicoes.
    - Aplica backoff progressivo em 429/403(cloudflare)/503 e, se o bloqueio
      persistir, levanta StravaRateLimited para encerrar o job com parciais.
    - Mantem o heartbeat do job vivo durante pausas longas (chama o
      progress_callback periodicamente), evitando retomadas falsas do worker.
    """

    def __init__(self, interval=4.6, batch_pause=60.0, batch_size=100,
                 cancel_check=None, progress_callback=None, shared_pacer=None,
                 session_cookie=None, respeitar_teto=True):
        self.interval = max(_STRAVA_MIN_INTERVAL, min(_STRAVA_MAX_INTERVAL, float(interval)))
        self.batch_pause = max(0.0, float(batch_pause))
        self.batch_size = int(batch_size) if batch_size else 0
        self.cancel_check = cancel_check
        self.progress_callback = progress_callback
        # Fila de slots compartilhada entre jobs e workers (ver app/ratepacer.py).
        # Sem ela, dois processamentos simultaneos dobrariam a taxa no Strava.
        self.shared_pacer = shared_pacer
        # O COOKIE VIVE AQUI (27/08/2026). Ver o cabecalho do patch: raspar
        # atividade sem sessao devolve 200 com a casca de login, e isso virava
        # "Distancia invalida". Quem constroi o throttle (`build_strava_throttle`)
        # ja sabe a cidade, entao ja sabe o cookie.
        self.session_cookie = session_cookie or None
        # O RANKING NAO E' BARRADO PELA NOSSA TRAVA (27/08/2026, regra do dono).
        # A saida do ranking e' prioridade: upload de atleta nao pode causar
        # falha nela. Ele CONTA o que gasta — senao o retrato do dia fica falso
        # — mas nao e' bloqueado pelo teto. Quem obedece e' o trabalho longo,
        # que e' importante e nao urgente.
        self.respeitar_teto = bool(respeitar_teto)
        self._last_request = 0.0
        self._requests_in_batch = 0
        self._backoff_index = 0
        self._last_pct = 60
        # Ultima mensagem mostrada na tela. O pulso da espera repete ELA em vez
        # de escrever um texto generico: trocar "(15/23)" por "aguardando"
        # apagaria a unica informacao util da tela (regra do CLAUDE.md: aviso
        # nao substitui status, fica embaixo).
        self._last_msg = None

    def _cancelled(self):
        return bool(self.cancel_check and self.cancel_check())

    def note_progresso(self, message, pct=None):
        """Guarda o que a tela esta mostrando, para o pulso poder repeti-lo."""
        if message:
            self._last_msg = message
        self.note_pct(pct)

    def _pulso(self, esperando_seg):
        """Diz "ainda estou aqui" enquanto uma requisicao demora."""
        base = self._last_msg or 'Buscando dados no Strava'
        self._ping(
            'scraping',
            '%s (o Strava esta demorando a responder: %ds)'
            % (base, int(esperando_seg)))

    def note_pct(self, pct):
        """Mantem o percentual atual para as mensagens de pausa."""
        if pct is not None:
            self._last_pct = pct

    def _ping(self, stage, message):
        if self.progress_callback:
            try:
                self.progress_callback(stage, message, self._last_pct)
            except Exception:
                pass

    def sleep(self, seconds, message=None, stage='scraping'):
        """Dorme de forma cancelavel, atualizando o progresso periodicamente.

        Fatias curtas garantem cancelamento responsivo; o ping a cada ~15s
        mantem o heartbeat do job fresco durante pausas longas (backoff/lote).
        """
        if seconds <= 0:
            return
        if message:
            self._ping(stage, message)
        deadline = time.monotonic() + seconds
        next_ping = time.monotonic() + 15
        while True:
            if self._cancelled():
                raise ScrapeCancelled()
            now = time.monotonic()
            if now >= deadline:
                return
            if message and now >= next_ping:
                self._ping(stage, f'{message} (~{int(deadline - now)}s)')
                next_ping = now + 15
            time.sleep(min(1.0, deadline - now))

    def _wait_turn(self):
        if self.batch_size and self._requests_in_batch >= self.batch_size:
            self._requests_in_batch = 0
            self.sleep(self.batch_pause,
                       'Pausa de seguranca para nao sobrecarregar o Strava')
        # Senha na fila global: quando ha outro processamento rodando, a espera
        # vem daqui e os dois jobs somam no mesmo ritmo seguro. Sozinho, a fila
        # devolve zero e vale o espacamento local de sempre.
        if self.shared_pacer is not None:
            espera = self.shared_pacer.reserve(self.interval)
            if espera > 0:
                self.sleep(
                    espera,
                    ('Outro processamento esta usando o Strava. Aguardando a vez'
                     if espera > 5 else None),
                )
        if self._last_request:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.interval:
                self.sleep(self.interval - elapsed)

    # `_is_rate_limited` REMOVIDA EM 09/09/2026 (varredura de codigo orfao).
    #
    # Ela respondia uma pergunta so -- "isto e' bloqueio?" -- e desde 29/08 quem
    # responde e' `app/stravasinal.py`, que da NOME e REMEDIO a cada resposta:
    # 429 com `Retry-After` e' limite honesto, 429 sem o cabecalho e' deteccao
    # de bot (e ai repetir piora). Conferido antes de apagar: o `stravasinal`
    # cobre inclusive o caso que so' ela pegava, o desafio do CloudFlare que
    # chega com HTTP 200 (stravasinal.py, linhas 27 e 156).
    #
    # As constantes `_STRAVA_BLOCK_MARKERS` e `_STRAVA_CHALLENGE_MARKERS`
    # sairam junto: nasceram com ela e nao tinham outro leitor.

    def get(self, url, **kwargs):
        """GET cancelavel, com pacing e backoff.

        Levanta ScrapeCancelled se o usuario cancelar e StravaRateLimited se o
        backoff se esgotar (Strava manteve o bloqueio).
        """
        # A TRAVA DE BLOQUEIO VEM ANTES ATE DO TETO (29/08/2026).
        # Se o Strava ja nos barrou ha pouco, a coisa mais cara que da para
        # fazer e' bater na porta de novo: cada tentativa em cima de um
        # bloqueio ativo aumenta a chance de virar bloqueio longo. Congela e
        # volta sozinho -- a fila e os checkpoints ficam intactos.
        if self.respeitar_teto:
            try:
                ate = stravasinal.bloqueado_ate()
            except Exception:
                ate = None
            if ate:
                raise StravaLimiteDeVolume(
                    'bloqueio do Strava', max(0, ate - time.time()))

        # O TETO VEM ANTES DE TUDO (27/08/2026). Este e' o portao unico de
        # acesso ao Strava: conferir aqui faz o limite valer para importacao,
        # upload, conferencia e recuperacao sem que nenhuma precise lembrar.
        if self.shared_pacer is not None and self.respeitar_teto:
            janela, falta = self.shared_pacer.estourou()
            if janela:
                raise StravaLimiteDeVolume(janela, falta)
        kwargs.setdefault('headers', SCRAPE_HEADERS)
        kwargs.setdefault('timeout', (5, 15))
        # TODO acesso ao Strava autenticado (27/08/2026, regra do dono). Sem
        # isto, atividade que exige login devolvia 200 com a casca de
        # autenticacao e o link era contado como ilegivel.
        if self.session_cookie:
            kwargs.setdefault('cookies',
                              {STRAVA_SESSION_COOKIE_NAME: self.session_cookie})
        while True:
            self._wait_turn()
            if self._cancelled():
                raise ScrapeCancelled()
            resp = _hard_get(url, kwargs, pulso=self._pulso, cancel_check=self.cancel_check)
            self._last_request = time.monotonic()
            self._requests_in_batch += 1
            # CONTA DEPOIS DE SAIR, nunca antes: requisicao que nem chegou a
            # acontecer nao gasta cota.
            if self.shared_pacer is not None:
                self.shared_pacer.registrar()
            # LER A RESPOSTA ANTES DE INSISTIR (29/08/2026, regra do dono).
            # Antes havia uma pergunta so -- "isto e' bloqueio?" -- e um
            # remedio so: esperar e repetir. Agora cada resposta tem nome e
            # remedio proprio (app/stravasinal.py). O discriminante que
            # mudou tudo: 429 COM `Retry-After` e' limite honesto (espera o
            # que ele mandou); 429 SEM o cabecalho e' deteccao de bot, e ai
            # repetir PIORA.
            try:
                diagnostico, espera_pedida = stravasinal.ler(resp, url)
            except Exception:
                diagnostico, espera_pedida = stravasinal.OK, None

            if diagnostico == stravasinal.LIMITE_HONESTO:
                # O servidor disse quanto esperar. Obedecer nao tem risco.
                self._backoff_index = 0
                self.sleep(min(float(espera_pedida or 60), 900.0),
                           'O Strava pediu uma pausa e disse por quanto '
                           'tempo. Esperando o que ele mandou')
                continue

            if stravasinal.e_bloqueio(diagnostico):
                frase, _parar = stravasinal.explicar(diagnostico)
                try:
                    ate, tentativa, recado = stravasinal.registrar_bloqueio(
                        diagnostico)
                except Exception:
                    ate, tentativa, recado = None, 1, frase
                logger.warning('Strava barrou o acesso (%s, %da vez): %s',
                               diagnostico, tentativa, recado)
                if ate:
                    raise StravaLimiteDeVolume(
                        'bloqueio do Strava', max(0, ate - time.time()))
                raise StravaRateLimited()

            # Qualquer outro diagnostico (privada, inexistente, mes sem
            # conta, instavel, ok) NAO e' bloqueio: a resposta volta e quem
            # chamou decide. Foi a leitura dar certo, entao a trava se solta.
            self._backoff_index = 0
            if diagnostico == stravasinal.OK:
                try:
                    stravasinal.liberar()
                except Exception:
                    pass
            return resp

    def block_pause(self):
        """Pausa apos uma sequencia de falhas (possivel bloqueio ativo)."""
        self.sleep(_STRAVA_BLOCK_PAUSE, 'Aguardando liberacao do Strava')
        self._backoff_index = 0


def build_strava_throttle(mode=None, cidade=None, cancel_check=None,
                          progress_callback=None, session_cookie=None,
                          respeitar_teto=True):
    """Cria um StravaThrottle a partir do perfil da opcao e da config da cidade.

    A config por cidade (strava_delay_segundos) so pode tornar a coleta MAIS
    lenta (mais segura) que o perfil, nunca mais rapida.
    """
    perfil = STRAVA_THROTTLE_PROFILES.get(mode or '', _STRAVA_THROTTLE_DEFAULT)
    interval = perfil['interval']
    try:
        if cidade:
            cfg = Config.query.filter_by(cidade=cidade).first()
            delay_cfg = getattr(cfg, 'strava_delay_segundos', None) if cfg else None
            if delay_cfg and float(delay_cfg) > interval:
                interval = float(delay_cfg)
    except Exception as exc:
        logger.debug('Falha ao ler strava_delay_segundos: %s', exc)
    # Uma fila unica para TODO acesso ao Strava: o limite e do IP do servidor,
    # nao de cada job ou cidade.
    pacer = None
    try:
        pacer = SharedPacer('strava')
    except Exception as exc:
        logger.debug('Sem fila compartilhada de acesso ao Strava: %s', exc)
    # O COOKIE DA CIDADE ENTRA AQUI, uma vez (27/08/2026). Quem receber este
    # throttle nao precisa lembrar de mandar cookie -- e o que garante o
    # "todos usam" sem depender de ninguem lembrar.
    if session_cookie is None and cidade:
        try:
            from app import secretstore
            session_cookie = secretstore.get(secretstore.STRAVA_SESSION,
                                             scope=cidade)
        except Exception as exc:
            logger.warning('Nao consegui ler o cookie do Strava para %s: %s',
                           cidade, exc)
    return StravaThrottle(
        interval=interval,
        batch_pause=perfil['batch_pause'],
        batch_size=_STRAVA_THROTTLE_BATCH_SIZE,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
        shared_pacer=pacer,
        session_cookie=session_cookie,
        respeitar_teto=respeitar_teto,
    )


def _activity_id_from_url(url):
    m = re.search(r'/activities/(\d+)', url or '')
    return m.group(1) if m else None


def classify_strava_link(url):
    """Triagem de um link, sem rede.

    - 'direct'  : ja contem /activities/<id> (pode ter /flyover, /embed, query)
    - 'short'   : strava.app.link, precisa resolver via redirect
    - 'nao_atividade' : e do Strava, mas nao e atividade (perfil, clube,
                        segmento, rota)
    - 'invalid' : nao e link do Strava — nao e da nossa conta conferir

    'nao_atividade' e separado de 'invalid' porque nao e lixo: e alguem
    tentando postar a propria atividade e errando o botao do aplicativo. Esse
    caso vira aviso ao admin, com quem postou e quando (15/09/2026).
    """
    u = _trim_url_token(url).lower()
    if not u:
        return 'invalid'
    if 'strava.app.link' in u:
        return 'short'
    if re.search(r'strava\.com/activities/\d+', u):
        return 'direct'
    if re.search(r'(?:^|//|\.)strava\.com/', u):
        return 'nao_atividade'
    return 'invalid'


def _ensure_link_cache_schema():
    global _LINK_CACHE_READY
    if _LINK_CACHE_READY:
        return
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS strava_link_cache (
                short_url TEXT PRIMARY KEY,
                resolved_url TEXT NOT NULL,
                activity_id TEXT,
                updated_at DATETIME
            )
        """))
    _LINK_CACHE_READY = True


def _get_cached_shortlink(short_url):
    try:
        _ensure_link_cache_schema()
        row = db.session.execute(
            text("SELECT resolved_url, activity_id FROM strava_link_cache WHERE short_url = :short_url"),
            {'short_url': short_url},
        ).fetchone()
        if not row:
            return None
        return {'resolved_url': row[0], 'activity_id': row[1]}
    except Exception as exc:
        logger.debug('Falha ao ler cache de shortlink %s: %s', short_url, exc)
        return None


def _save_cached_shortlink(short_url, resolved_url):
    activity_id = _activity_id_from_url(resolved_url)
    if not short_url or not resolved_url or not activity_id:
        return
    try:
        _ensure_link_cache_schema()
        db.session.execute(
            text("""
                INSERT OR REPLACE INTO strava_link_cache
                (short_url, resolved_url, activity_id, updated_at)
                VALUES (:short_url, :resolved_url, :activity_id, :updated_at)
            """),
            {
                'short_url': short_url,
                'resolved_url': resolved_url,
                'activity_id': activity_id,
                'updated_at': datetime.utcnow(),
            },
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.debug('Falha ao salvar cache de shortlink %s: %s', short_url, exc)


def _make_gps_fingerprint(location_list):
    if not location_list or len(location_list) < 10:
        return None
    n = len(location_list)
    step = max(1, n // 50)
    sample = location_list[::step][:50]
    parts = [str(round(p['lat'], 8)) + ',' + str(round(p['lng'], 8)) for p in sample]
    return __import__('hashlib').sha1('|'.join(parts).encode()).hexdigest()


def _normalize_phone(raw):
    digits = re.sub(r'\D', '', raw)
    if not digits.startswith('55'):
        digits = '55' + digits
    if len(digits) >= 12:
        return '+' + digits
    return None


def _compact_athlete_name(*parts):
    """Primeiro + ultimo nome, ja SEM emoji e sem tipo sanguineo.

    ATE 29/08/2026 esta funcao pegava o ultimo PEDACO BRUTO do nome. Quando
    o atleta punha emoji no fim do perfil do Strava, o emoji era o ultimo
    pedaco e expulsava o sobrenome: "Maykon Abreu A+" foi gravado como
    "Maykon A+". A regra de "isto nao e nome" ja existia -- mas so no
    ranking, so na hora de EXIBIR. Agora mora em `app/nomes.py` e vale
    tambem na hora de GRAVAR, que e onde o estrago era permanente.
    """
    from app.nomes import nome_para_gravar
    return nome_para_gravar(*parts)


def _trim_url_token(url):
    return (url or '').strip().rstrip('.,;)]}>')


def _sanitize_strava_activity_url(raw_url, keep_share_sig=True):
    """
    Normaliza URL de atividade Strava.
    - Mantém apenas /activities/<id>
    - Preserva share_sig (quando existir), removendo parâmetros volumosos (_branch, utm etc.)
    """
    raw_url = _trim_url_token(raw_url)
    m = re.search(r'/activities/(\d+)', raw_url)
    if not m:
        return None
    activity_id = m.group(1)
    base = f'https://www.strava.com/activities/{activity_id}'

    if not keep_share_sig:
        return base

    try:
        parsed = urlparse(raw_url)
        query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        share_sig = query_params.get('share_sig')
        if share_sig:
            return f'{base}?{urlencode({"share_sig": share_sig})}'
    except Exception:
        pass
    return base


def _has_share_sig(url):
    return 'share_sig=' in (url or '')


def _is_valid_strava_id(value):
    sid = str(value or '').strip()
    return bool(sid and sid.isdigit() and sid != '0')




def _apply_metric_scalars(data, dist_m=None, moving_s=None, elev_m=None):
    """
    Preenche métricas padronizadas no dict de saída.
    """
    if dist_m is None:
        dist_m = 0
    if moving_s is None:
        moving_s = 0
    if elev_m is None:
        elev_m = 0

    dist_m = float(dist_m or 0)
    moving_s = int(moving_s or 0)
    elev_m = float(elev_m or 0)

    data['distancia_raw'] = dist_m
    data['distancia'] = round(dist_m / 1000, 3)
    data['elevacao_total'] = elev_m
    data['tempo_minutos'] = moving_s // 60 if moving_s else 0

    if moving_s > 0 and dist_m > 0:
        dist_km = dist_m / 1000
        tempo_h = moving_s / 3600
        data['velocidade_media'] = round(dist_km / tempo_h, 2)
        if data.get('modalidade') == 'corrida_caminhada':
            data['pace_min_km'] = round((moving_s / 60) / dist_km, 2)


def _extract_scalars_from_flyover(flyover):
    """
    Extrai métricas de flyoverData.scalarsToShow.
    """
    dist_m = moving_s = elev_m = None
    for scalar in (flyover.get('scalarsToShow') or []):
        value_type = str(scalar.get('valueType') or '').lower()
        raw_value = scalar.get('rawValue')
        if raw_value is None:
            continue
        if value_type == 'distance':
            dist_m = raw_value
        elif value_type == 'movingtime':
            moving_s = raw_value
        elif value_type == 'elevationgain':
            elev_m = raw_value
    return dist_m, moving_s, elev_m


def extract_links_with_phones(txt_content):
    """
    Percorre o TXT do WhatsApp linha a linha com rastreamento de contexto.
    Retorna lista de tuplas: (phone_or_None, full_url, wa_date_or_None).
    wa_date e um objeto date da mensagem que contem o link (pre-filtragem).
    """
    results = {}
    current_phone = None
    current_date  = None
    sequence = 0

    for line in txt_content.splitlines():
        line = line.strip()
        if not line:
            continue

        m_msg = WA_LINE_RE.match(line)
        if m_msg:
            date_str = m_msg.group(1).strip()
            sender   = m_msg.group(2).strip()
            content  = m_msg.group(3).strip()

            try:
                current_date = datetime.strptime(date_str, '%d/%m/%Y').date()
            except ValueError:
                current_date = None

            if WA_PHONE_RE.match(sender):
                current_phone = _normalize_phone(sender)
            else:
                current_phone = None

            search_text = content
        else:
            search_text = line

        for m in STRAVA_DIRECT_RE.finditer(search_text):
            sequence += 1
            raw_url = m.group(0)
            sanitized = _sanitize_strava_activity_url(raw_url, keep_share_sig=True)
            if not sanitized:
                continue
            act_m = re.search(r'/activities/(\d+)', sanitized)
            if not act_m:
                continue
            act_id = act_m.group(1)
            if act_id not in results:
                results[act_id] = {
                    'phone': current_phone,
                    'url': sanitized,
                    'date': current_date,
                    'sequence': sequence,
                }
            else:
                prev_url = results[act_id]['url']
                if _has_share_sig(sanitized) or not _has_share_sig(prev_url):
                    results[act_id] = {
                        'phone': current_phone,
                        'url': sanitized,
                        'date': current_date,
                        'sequence': sequence,
                    }

        for m in STRAVA_SHORT_RE.finditer(search_text):
            sequence += 1
            short_url = _trim_url_token(m.group(0))
            key = f'short_{short_url}'
            results[key] = {
                'phone': current_phone,
                'url': short_url,
                'date': current_date,
                'sequence': sequence,
            }

    ordered = sorted(results.values(), key=lambda item: item['sequence'], reverse=True)
    return [(item['phone'], item['url'], item['date']) for item in ordered]


def _rotulo_periodos(periodos_alvo):
    """"08/2026" ou "07/2026 e 08/2026" — o mes que o job estava processando.

    Serve para o registro dizer POR QUE a atividade nao entrou, em portugues,
    para quem for ler o aviso depois.
    """
    if not periodos_alvo:
        return 'todos'
    partes = sorted('%02d/%d' % (mes, ano) for ano, mes in periodos_alvo)
    if len(partes) == 1:
        return partes[0]
    return ', '.join(partes[:-1]) + ' e ' + partes[-1]


def _janela_prefiltro_mensagem(periodos_alvo, tolerancia_dias=7):
    """Janela [inicio, fim] de datas de mensagem que podem conter atividades dos periodos.

    A mensagem do WhatsApp nunca antecede a atividade compartilhada, entao o
    limite inferior e exato; a tolerancia no fim cobre links enviados alguns
    dias depois da virada do mes (a data real do Strava continua decidindo).
    """
    inicios = [date(ano, mes, 1) for ano, mes in periodos_alvo]
    fins = [
        date(ano, mes, calendar.monthrange(ano, mes)[1])
        for ano, mes in periodos_alvo
    ]
    return min(inicios), max(fins) + timedelta(days=tolerancia_dias)


def resolve_strava_shortlink(short_url, throttle=None):
    try:
        if throttle is not None:
            resp = throttle.get(short_url, allow_redirects=True)
        else:
            # Pelo _hard_get tambem aqui: o timeout do requests nao cobre a
            # resolucao de nome, que e onde o travamento aparece (P-80).
            resp = _hard_get(short_url, {'allow_redirects': True,
                                         'timeout': (5, 15),
                                         'headers': SCRAPE_HEADERS})
        final_url = resp.url or ''
        # Shortlink morto: o proprio encurtador respondeu "nao existe" sem
        # redirecionar para nenhuma atividade. Retentar nunca resolve.
        if (throttle is not None
                and getattr(resp, 'status_code', 0) in (404, 410)
                and 'strava.com/activities/' not in final_url):
            raise ScrapePermanent(
                'O link encurtado nao existe mais (atividade apagada ou link quebrado).',
                stage='link_morto',
            )
        # 0) O link curto caiu numa pagina do Strava que NAO e atividade —
        #    perfil, clube, segmento, rota (15/09/2026). Vem ANTES da busca no
        #    HTML de proposito: a pagina de um perfil LISTA as atividades da
        #    pessoa, e o passo 2 pegaria a primeira que encontrasse — isto e,
        #    lancaria uma atividade qualquer no lugar da que ela quis postar.
        #    Medido em 15/09 com https://strava.app.link/TmnfZ90Ws6b: HTTP 200
        #    e destino https://www.strava.com/athletes/<id>.
        #    So vira pendencia no fluxo em LOTE (com throttle). Na chamada
        #    avulsa o link de perfil e legitimo — e por ele que se importa um
        #    atleta inteiro (upload.py) —, entao ali devolvemos o proprio
        #    endereco, que antes se perdia como None.
        if (final_url and 'strava.com/activities/' not in final_url
                and re.search(r'(?:^|//|\.)strava\.com/', final_url, re.IGNORECASE)):
            if throttle is not None:
                raise ScrapePermanent(
                    _MOTIVO_DA_ETAPA['nao_atividade'][0],
                    stage='nao_atividade',
                )
            perfil = STRAVA_PROFILE_RE.match(final_url)
            if perfil:
                return 'https://www.strava.com/athletes/%s' % perfil.group(1)
            return final_url
        # 1) Redirect direto para a atividade (caso comum).
        if 'strava.com/activities/' in final_url:
            return _sanitize_strava_activity_url(final_url, keep_share_sig=True)
        # 2) Pagina de preview (sem redirect util): procurar o link canonico da
        #    atividade dentro do HTML retornado. Format-agnostico: acha o
        #    /activities/<id> onde quer que esteja (og:url, __NEXT_DATA__, etc.).
        try:
            html = resp.text or ''
        except Exception:
            html = ''
        m = STRAVA_DIRECT_RE.search(html)
        if m:
            return _sanitize_strava_activity_url(m.group(0), keep_share_sig=True)
        # 3) Fallback: activityId no proprio deep link (short ou ja resolvido).
        for candidate in (final_url, short_url):
            m2 = re.search(r'[?&]activityId=(\d+)', candidate or '')
            if m2:
                return f'https://www.strava.com/activities/{m2.group(1)}'
        return None
    except (ScrapeCancelled, StravaRateLimited, ScrapeTimeout, ScrapePermanent):
        raise
    except requests.exceptions.Timeout:
        # Timeout so vira pendencia retentavel no fluxo em lote (com throttle);
        # chamadas avulsas (link unico) mantem o comportamento antigo (None).
        if throttle is not None:
            raise ScrapeTimeout()
        return None
    except Exception as e:
        logger.error(f"Erro ao resolver shortlink {short_url}: {e}")
        return None


def _extract_from_next_data(html_text, soup):
    """Estrategia 1: extrai dados do JSON __NEXT_DATA__ (Next.js)."""
    next_script = soup.find('script', id='__NEXT_DATA__')
    if not next_script or not next_script.string:
        return None
    try:
        nd  = json.loads(next_script.string)
        pp = nd.get('props', {}).get('pageProps', {})
        act = pp.get('activity', {}) or {}
        flyover = pp.get('flyoverData', {}) or {}
        if not act and not flyover:
            return None

        data = {}
        source = act if act else flyover

        athlete = dict(act.get('athlete') or {})
        flyover_athlete = flyover.get('athlete') or {}
        if not _is_valid_strava_id(athlete.get('id')) and _is_valid_strava_id(flyover_athlete.get('id')):
            athlete['id'] = flyover_athlete.get('id')
        if not athlete.get('firstName') and flyover_athlete.get('firstName'):
            athlete['firstName'] = flyover_athlete.get('firstName')
        if not athlete.get('lastName') and flyover_athlete.get('lastName'):
            athlete['lastName'] = flyover_athlete.get('lastName')
        data['id_usuario_strava'] = str(athlete.get('id', '0'))
        fname = athlete.get('firstName', '')
        lname = athlete.get('lastName', '')
        data['nome_usuario'] = _compact_athlete_name(fname, lname)

        sport = (source.get('activityKind', {}) or {}).get('sportType', '').lower()
        data['modalidade'] = SPORT_MAP.get(sport, 'outro')
        # O esporte cru viaja junto: 'Run' e 'Walk' viram a mesma modalidade, e
        # so ele sabe qual era (28/08/2026).
        data['esporte_strava'] = _esporte_cru(source)

        # 1) Formato antigo/legado: pageProps.activity com scalars/activityStats
        scalars = act.get('scalars') or {}
        stats = act.get('activityStats') or {}
        if scalars or stats:
            _apply_metric_scalars(
                data=data,
                dist_m=scalars.get('distance') or stats.get('distance'),
                moving_s=scalars.get('movingTime') or stats.get('movingTime'),
                elev_m=scalars.get('elevationGain') or stats.get('elevationGain'),
            )

        # 2) Formato atual (2026): pageProps.flyoverData.scalarsToShow
        if (not data.get('distancia') or data.get('distancia', 0) <= 0) and flyover:
            dist_m, moving_s, elev_m = _extract_scalars_from_flyover(flyover)
            _apply_metric_scalars(data=data, dist_m=dist_m, moving_s=moving_s, elev_m=elev_m)

        sl = act.get('startLocal', '')
        if sl:
            data['data_hora'] = datetime.fromisoformat(sl)

        # Fingerprint GPS
        # - legado: activity.streams.location
        # - atual: flyoverData.streams.location
        gps_location = []
        if act.get('streams'):
            gps_location = (act.get('streams', {}) or {}).get('location', []) or []
        elif flyover.get('streams'):
            gps_location = (flyover.get('streams', {}) or {}).get('location', []) or []
        data["gps_fingerprint"] = _make_gps_fingerprint(gps_location)

        return data
    except Exception as e:
        logger.warning(f"Falha ao parsear __NEXT_DATA__: {e}")
        return None


def _num(texto, chave, bloco=None):
    """`chave: 123.4` ou `"chave": 123.4` -- a chave INTEIRA, nunca o fim dela.

    A ANCORA E A CORRECAO DE 29/08/2026 (incidente dos 266 km). Sem ela o
    padrao casava QUALQUER chave TERMINADA em `distance` -- e o bloco de
    equipamento do Strava tem um `"distance"` por tenis, com a quilometragem
    ACUMULADA do calcado. Uma corrida de 16,471 km foi gravada como 266,509
    km (o total do "Adidas NMD R1") e publicada no grupo. Agora a chave tem
    de comecar onde o padrao diz: depois de `{`, de `,` ou do inicio do
    bloco. E a mesma guarda que `_extract_from_regex` ganhou em 27/08 e que
    faltou aqui. Ver ERROS.md.
    """
    alvo = bloco if bloco is not None else texto
    m = re.search(r'(?:^|[{,])\s*["\']?%s["\']?\s*:\s*"?([0-9]+(?:\.[0-9]+)?)'
                  % chave, alvo)
    return float(m.group(1)) if m else None


def _tem_chave(bloco, chave):
    """A chave existe neste bloco? (chave inteira, nao final de outra)"""
    return re.search(r'(?:^|[{,])\s*["\']?%s["\']?\s*:'
                     % chave, bloco) is not None


# O bloco da ATIVIDADE se reconhece por estas chaves. `distance` sozinho NAO
# serve de assinatura -- o bloco de equipamento tambem tem `distance`.
_CHAVES_DA_ATIVIDADE = ('moving_time', 'elev_gain', 'activityAthleteId',
                        'elapsed_time', 'calories')
# Bloco de EQUIPAMENTO (tenis e bicicletas do atleta). Nunca e a atividade.
_CHAVES_DE_EQUIPAMENTO = ('shoes', 'bikes')


def _bloco_da_atividade(html_text):
    """O `pageView.activity().set({...})` que TEM os numeros DA ATIVIDADE.

    SAO TRES BLOCOS, NAO DOIS (medido em 29/08/2026, incidente dos 266 km).
    O comentario antigo dizia dois; o Strava passou a servir tres para quem
    tem equipamento cadastrado:

        bloco 0  adjusting_elevation, map_start_index         (sem numeros)
        bloco 1  bikes, shoes -> um `distance` POR TENIS      (a armadilha)
        bloco 2  distance, elev_gain, moving_time, activityAthleteId

    A versao anterior devolvia "o primeiro bloco que contenha a substring
    `distance`" e por isso caiu no bloco do tenis: gravou os 266,509 km
    acumulados do calcado como se fossem a corrida de 16,471 km. E como o
    bloco do tenis tambem nao tem `activityAthleteId`, a atividade ficou sem
    dono e virou um segundo atleta no ranking.

    A ESCOLHA AGORA E POR ASSINATURA, nao por posicao: o bloco precisa ter
    `distance` E pelo menos uma chave que so existe em atividade. Bloco com
    `shoes`/`bikes` esta fora, doa a quem doer.
    """
    for m in re.finditer(r'pageView\.activity\(\)\.set\(\{(.*?)\}\)\s*;',
                         html_text, re.DOTALL):
        corpo = m.group(1)
        if _num(html_text, 'distance', corpo) is None:
            continue
        if any(_tem_chave(corpo, chave) for chave in _CHAVES_DE_EQUIPAMENTO):
            continue
        if any(_num(html_text, chave, corpo) is not None
               for chave in _CHAVES_DA_ATIVIDADE):
            return corpo
    return ''


def _completar_metricas(data):
    """Velocidade e pace a partir de distancia e tempo, sem repetir conta."""
    dist_m = data.get('distancia_raw') or 0
    moving_s = (data.get('tempo_minutos') or 0) * 60
    if moving_s > 0 and dist_m > 0:
        dist_km = dist_m / 1000.0
        data['velocidade_media'] = round(dist_km / (moving_s / 3600.0), 2)
        if data.get('modalidade') == 'corrida_caminhada':
            data['pace_min_km'] = round((moving_s / 60.0) / dist_km, 2)
    return data


_MESES_PT = {'janeiro': 1, 'fevereiro': 2, 'março': 3, 'marco': 3,
             'abril': 4, 'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8,
             'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12}
_MESES_EN = {'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5,
             'june': 6, 'july': 7, 'august': 8, 'september': 9, 'october': 10,
             'november': 11, 'december': 12}


def _data_pelo_texto(html_text):
    """A data da atividade escrita para gente, na tag `<time>`.

    POR QUE (27/08/2026): em atividade recente o bloco JS traz `distance`,
    `elev_gain`, `moving_time`, `calories`... e **NÃO traz `startDateLocal`**.
    Sem data a cadeia inteira rejeita a leitura e a atividade some — mesmo com
    a distância na mão. A data está na página, em texto, e **no idioma do
    `Accept-Language`** (aqui pt-BR):
        pt: "17:10, terça-feira, 18 de agosto de 2026"
        en: "5:10 PM on Tuesday, August 18, 2026"
    Os dois são lidos: o cabeçalho pode mudar e o Strava cai para o inglês
    sozinho em algumas contas.
    """
    for bruto in re.findall(r'<time[^>]*>([^<]{6,80})</time>', html_text):
        texto = ' '.join(bruto.split())
        baixo = texto.lower()

        m = re.search(r'(\d{1,2}):(\d{2}).*?(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})',
                      baixo)
        if m and m.group(4) in _MESES_PT:
            try:
                return datetime(int(m.group(5)), _MESES_PT[m.group(4)],
                                int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass

        m = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm).*?([a-z]+)\s+(\d{1,2}),\s*(\d{4})',
                      baixo)
        if m and m.group(4) in _MESES_EN:
            hora = int(m.group(1)) % 12
            if m.group(3) == 'pm':
                hora += 12
            try:
                return datetime(int(m.group(6)), _MESES_EN[m.group(4)],
                                int(m.group(5)), hora, int(m.group(2)))
            except ValueError:
                pass
    return None


def _extract_from_classic(html_text, soup=None):
    """A pagina LOGADA (classica). Ver o cabecalho do patch de 27/08/2026."""
    bloco = _bloco_da_atividade(html_text)
    if not bloco:
        return None

    data = {}
    dist_m = _num(html_text, 'distance', bloco)
    if not dist_m or dist_m <= 0:
        return None
    data['distancia_raw'] = dist_m
    data['distancia'] = round(dist_m / 1000.0, 3)

    elev = _num(html_text, 'elev_gain', bloco)
    if elev is not None:
        data['elevacao_total'] = elev
    mov = _num(html_text, 'moving_time', bloco)
    if mov:
        data['tempo_minutos'] = int(mov) // 60

    # A DATA VEM EM EPOCH E SE LE EM UTC (calibrado contra o banco, 27/08).
    ts = _num(html_text, 'startDateLocal', bloco)
    if ts:
        try:
            data['data_hora'] = datetime.utcfromtimestamp(int(ts))
        except (ValueError, OSError):
            pass
    if not data.get('data_hora'):
        # Atividade recente nao traz `startDateLocal` no bloco. Ver
        # `_data_pelo_texto`: sem isto a atividade era lida e jogada fora.
        data['data_hora'] = _data_pelo_texto(html_text)

    # O DONO DA ATIVIDADE, NUNCA A CONTA LOGADA. `currentAthleteId` e' a conta
    # do cookie; quem interessa e' `activityAthleteId` -- e e' disso que a
    # guarda do atleta (regra 4-D) depende para nao contar treino em grupo de
    # outra pessoa como se fosse do atleta importado.
    # A FONTE ESTRUTURADA PRIMEIRO (29/08/2026): id e nome do MESMO objeto.
    dono_id, nome_dono = _atleta_da_atividade(html_text)
    if nome_dono:
        data['nome_usuario'] = nome_dono
    if not dono_id:
        dono = re.search(r'activityAthleteId\s*:\s*(\d+)', bloco)
        dono_id = dono.group(1) if dono else None
    if not dono_id:
        # SEGUNDA VIA PARA O DONO (29/08/2026, medido na <id-da-atividade>).
        # A pagina classica nem sempre traz `activityAthleteId` no bloco.
        # Antes isso caia no `setdefault('id_usuario_strava','0')` e a
        # atividade virava do "atleta zero"; agora sem dono nao se grava,
        # entao recusar aqui seria perder atividade BOA. `_dono_pela_pagina`
        # e conservador de proposito: so responde quando ha exatamente um
        # `/athletes/<id>` diferente da conta do cookie. Na duvida, None --
        # e ai a recusa esta certa.
        dono_id = _dono_pela_pagina(html_text)
    if dono_id:
        data['id_usuario_strava'] = dono_id
        # O regex de link so entra se a fonte estruturada nao deu nome: o
        # PRIMEIRO `athletes/<id>` da pagina e um item de menu, e por isso
        # ele gravava "Treinamento" no lugar da pessoa.
        if not data.get('nome_usuario'):
            nome = re.search(
                r'athletes/%s[^>]*>([^<]{2,60})' % dono_id, html_text)
            if nome:
                data['nome_usuario'] = _compact_athlete_name(
                    nome.group(1).strip())

    # `Strava.Labs.Activities.Pages.RidePageView(123, 'Ride', factory)`
    tipo = re.search(r"Pages\.\w+PageView\(\s*\d+\s*,\s*['\"]([A-Za-z]+)['\"]",
                     html_text)
    if tipo:
        data['modalidade'] = SPORT_MAP.get(tipo.group(1).lower(), 'outro')

    return _completar_metricas(data)




# O embed do Strava, a fonte que nao depende do layout do site. Ver o cabecalho
# do patch de 27/08/2026 e a regra 4-K no CLAUDE.md.
STRAVA_EMBED_URL = 'https://strava-embeds.com/activity/%s'

_MESES_EMBED = {m: i + 1 for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July',
     'August', 'September', 'October', 'November', 'December'])}


def _objeto_js(html_text, marcador):
    """O `{...}` BALANCEADO que vem depois do marcador, ja como dicionario.

    Contar chaves em vez de casar com regex e o que permite ler o objeto
    inteiro sem depender da ordem dos campos -- e sem o risco de parar no
    primeiro `}` que aparecer dentro de uma URL ou de um sub-objeto.
    """
    inicio = html_text.find(marcador)
    if inicio < 0:
        return None
    inicio = html_text.find('{', inicio)
    if inicio < 0:
        return None
    nivel = 0
    dentro = False
    escapado = False
    for pos in range(inicio, min(len(html_text), inicio + 20000)):
        c = html_text[pos]
        if escapado:
            escapado = False
            continue
        if c == '\\':
            escapado = True
            continue
        if c == '"':
            dentro = not dentro
            continue
        if dentro:
            continue
        if c == '{':
            nivel += 1
        elif c == '}':
            nivel -= 1
            if nivel == 0:
                try:
                    return json.loads(html_text[inicio:pos + 1])
                except Exception:
                    return None
    return None


def _atleta_da_atividade(html_text):
    """O DONO da atividade (id e nome), do objeto que o Strava ja monta.

    A PAGINA TRAZ OS DOIS ATLETAS, e confundi-los e o jeito mais facil de
    furar a regra 4-D (medido em 29/08/2026):

        activityAthlete = new Strava.Models.Athlete({...})        <- o DONO
        currentAthlete  = new Strava.Models.CurrentAthlete({...}) <- o cookie

    Ler o `activityAthlete` resolve dono e nome de uma vez, da mesma fonte
    -- que e o principio que faltava: metrica e identidade vem juntas, nunca
    catadas de pedacos diferentes da pagina.

    O nome saia errado ate aqui por outro motivo: o codigo procurava
    `athletes/<id>[^>]*>(...)`, e o PRIMEIRO link com esse id e o do menu
    ("Treinamento", "Meu perfil"). Atividade lida so pela pagina logada ia
    para o banco com o nome do item de menu.
    """
    dados = _objeto_js(html_text, 'Strava.Models.Athlete(')
    if not isinstance(dados, dict):
        return None, None
    sid = dados.get('id')
    if not _is_valid_strava_id(sid):
        return None, None
    nome = _compact_athlete_name(dados.get('first_name'),
                                 dados.get('last_name')) \
        or _compact_athlete_name(dados.get('display_name'))
    return str(sid), (nome if nome != 'Desconhecido' else None)


def _dono_pela_pagina(html_text):
    """O id do DONO da atividade, na pagina que nao tem `activityAthleteId`.

    MEDIDO EM 27/08/2026: o layout novo (Microfrontend) traz dois links
    `/athletes/<id>` -- o da conta logada e o do dono. `athleteId`/`athlete_id`
    no corpo da pagina e' SEMPRE a conta logada (o cookie), nunca o dono:
    confundir os dois faria toda atividade parecer do administrador e furaria
    a guarda da regra 4-D de um jeito silencioso.
    """
    eu = re.search(r'athlete_?[Ii]d["\']?\s*[:=]\s*["\']?(\d+)', html_text)
    eu = eu.group(1) if eu else None
    achados = [a for a in dict.fromkeys(re.findall(r'/athletes/(\d+)', html_text))
               if a != eu]
    return achados[0] if len(achados) == 1 else None


def _extract_from_embed(activity_id, buscar):
    """ULTIMO RECURSO — le a atividade pelo EMBED publico.

    `buscar` e' a funcao de GET ja com espacamento (regra 4-C): este leitor
    NAO abre requisicao por fora da fila.

    Devolve None quando o embed nao responde 200 (404 = atividade nao publica)
    ou quando nao da para achar a distancia. Nunca inventa numero.
    """
    if not activity_id:
        return None
    try:
        # EM INGLES, EXPLICITAMENTE (27/08/2026). O embed responde no idioma do
        # `Accept-Language`, e o resto do projeto pede pt-BR: o leitor lia
        # "Distance" e vinha "Distância", entao ele falhava calado justamente
        # nas atividades que so ele salvaria. Fonte de ultimo recurso nao pode
        # depender do idioma de quem chamou.
        cabecalhos = dict(SCRAPE_HEADERS)
        cabecalhos['Accept-Language'] = 'en-US,en;q=0.9'
        resp = buscar(STRAVA_EMBED_URL % activity_id, headers=cabecalhos)
    except (ScrapeCancelled, StravaRateLimited, ScrapeTimeout):
        raise
    except Exception as exc:
        logger.info('Embed indisponivel para %s: %s', activity_id, exc)
        return None
    codigo = getattr(resp, 'status_code', 0)
    if codigo != 200:
        # CADA CODIGO COM O NOME DELE (09/09/2026). A mensagem dizia sempre
        # "404 = atividade nao publica", inclusive quando o codigo era 403 --
        # e 403 aqui nao fala da atividade, fala de NOS: o strava-embeds.com
        # recusa o servico inteiro (mesmo corpo de 24.745 bytes para
        # atividades diferentes, sem nenhuma marca de CloudFlare). Ler "404 =
        # nao publica" diante de um 403 mandou procurar defeito no link de
        # dois atletas cujas atividades estavam publicas o tempo todo.
        motivo = {404: 'atividade não pública ou apagada',
                  403: 'o serviço de embed recusou (não é sobre esta atividade)',
                  429: 'limite de acesso'}.get(codigo, 'resposta inesperada')
        logger.info('Embed de %s respondeu HTTP %s — %s',
                    activity_id, codigo, motivo)
        return None

    try:
        sopa = BeautifulSoup(resp.content, 'html.parser')
        for tag in sopa(['script', 'style']):
            tag.decompose()
        partes = [p.strip() for p in sopa.stripped_strings]
    except Exception as exc:
        logger.info('Nao consegui interpretar o embed de %s: %s', activity_id, exc)
        return None
    texto = ' | '.join(partes)

    # OS DOIS IDIOMAS, mesmo pedindo ingles: o Strava pode ignorar o
    # cabecalho, e leitor que so funciona quando tudo colabora nao e' rede de
    # seguranca. Em pt o separador decimal e' virgula ("1,2 km").
    m = (re.search(r'Dist[aâ]nc[ia]{1,2} \| ([0-9]+(?:[.,][0-9]+)?) (km|mi)', texto)
         or re.search(r'Distance \| ([0-9]+(?:[.,][0-9]+)?) (km|mi)', texto))
    if not m:
        return None
    valor = float(m.group(1).replace(',', '.'))
    if m.group(2) == 'mi':
        valor = valor * 1.609344
    if valor <= 0:
        return None

    data = {'distancia': round(valor, 3), 'distancia_raw': valor * 1000.0}

    m = re.search(r'\b(Walk|Run|Ride|Hike|TrailRun|VirtualRun|VirtualRide|'
                  r'EBikeRide|GravelRide|MountainBikeRide)\b', texto)
    if m:
        data['modalidade'] = SPORT_MAP.get(m.group(1).lower(), 'outro')
    else:
        # Nomes em portugues, para quando o cabecalho nao pegar.
        m = re.search(r'\b(Caminhada|Corrida|Pedalada|Trilha|Caminhada em trilha)\b',
                      texto)
        if m:
            data['modalidade'] = {
                'caminhada': 'corrida_caminhada',
                'corrida': 'corrida_caminhada',
                'trilha': 'corrida_caminhada',
                'caminhada em trilha': 'corrida_caminhada',
                'pedalada': 'ciclismo',
            }.get(m.group(1).lower(), 'outro')

    m = re.search(r'([A-Z][a-z]+) (\d{1,2}), (\d{4})', texto)
    if not m:
        # "5 de abril de 2024" -> reaproveita o mesmo caminho, invertendo a
        # ordem para casar com a assinatura de grupos abaixo.
        pt = re.search(r'(\d{1,2}) de ([a-zç]+) de (\d{4})', texto.lower())
        if pt and pt.group(2) in _MESES_PT:
            try:
                data['data_hora'] = datetime(int(pt.group(3)),
                                             _MESES_PT[pt.group(2)],
                                             int(pt.group(1)))
            except ValueError:
                pass
    if m and m.group(1) in _MESES_EMBED:
        try:
            # SEM HORA, DE PROPOSITO: o embed nao publica a hora. Meia-noite
            # mantem a atividade no MES certo, que e' o que o ranking usa
            # (regra 9). Quem precisar da hora tem de ler a pagina.
            data['data_hora'] = datetime(int(m.group(3)),
                                         _MESES_EMBED[m.group(1)],
                                         int(m.group(2)))
        except ValueError:
            pass

    m = (re.search(r'Elev Gain \| ([0-9]+(?:[.,][0-9]+)?) m', texto)
         or re.search(r'Ganho de elev\.? \| ([0-9]+(?:[.,][0-9]+)?) m', texto))
    if m:
        data['elevacao_total'] = float(m.group(1).replace(',', '.'))
    m = (re.search(r'(?:Time|Tempo) \| (?:(\d+)\s*h\s*)?(\d+)\s*min?\b', texto)
         or re.search(r'(?:Time|Tempo) \| (?:(\d+)h )?(\d+)m', texto))
    if m:
        data['tempo_minutos'] = int(m.group(1) or 0) * 60 + int(m.group(2))

    if partes:
        data['nome_usuario'] = _compact_athlete_name(partes[0])
    return _completar_metricas(data)


def _extract_from_regex(html_text, soup):
    """Estrategia 2: regex no HTML bruto (fallback)."""
    data = {}

    m = re.search(
        r'"flyoverData"\s*:\s*\{.*?"athlete"\s*:\s*\{[^{}]{0,300}?"id"\s*:\s*"(\d+)"',
        html_text,
        re.DOTALL,
    )
    if not m:
        m = re.search(r'"athlete"\s*:\s*\{[^{}]{0,300}?"id"\s*:\s*"(\d+)"', html_text)
    data['id_usuario_strava'] = m.group(1) if m else '0'

    title_meta = soup.find('meta', property='og:title')
    if title_meta:
        parts = (title_meta.get('content') or '').split(' by ')
        data['nome_usuario'] = _compact_athlete_name(parts[-1]) if len(parts) > 1 else 'Desconhecido'
    else:
        data['nome_usuario'] = 'Desconhecido'

    m2 = re.search(r'"sportType"\s*:\s*"([^"]+)"', html_text)
    if m2:
        data['modalidade'] = SPORT_MAP.get(m2.group(1).lower(), 'outro')
        data['esporte_strava'] = m2.group(1).strip()[:30] or None

    m3 = re.search(
        r'"distance"\s*:\s*([\d.]+)\s*,\s*"elevationGain"\s*:\s*([\d.]+)\s*,\s*"movingTime"\s*:\s*(\d+)',
        html_text
    )
    if m3:
        dist_m   = float(m3.group(1))
        elev_m   = float(m3.group(2))
        moving_s = int(m3.group(3))

        data['distancia_raw']  = dist_m
        data['distancia']      = round(dist_m / 1000, 3)
        data['elevacao_total'] = elev_m
        data['tempo_minutos']  = moving_s // 60

        if moving_s > 0 and dist_m > 0:
            dist_km = dist_m / 1000
            data['velocidade_media'] = round(dist_km / (moving_s / 3600), 2)
            if data.get('modalidade') == 'corrida_caminhada':
                data['pace_min_km'] = round((moving_s / 60) / dist_km, 2)

    m4 = re.search(r'"startLocal"\s*:\s*"([^"]+)"', html_text)
    if m4:
        try:
            data['data_hora'] = datetime.fromisoformat(m4.group(1))
        except ValueError:
            pass

    return data


_CAMPOS_DA_ATIVIDADE = (
    'distancia', 'distancia_raw', 'data_hora', 'modalidade', 'elevacao_total',
    'tempo_minutos', 'velocidade_media', 'pace_min_km', 'gps_fingerprint',
    'nome_usuario',
)


def _juntar_leituras(base, novo):
    """Completa `base` com o que `novo` trouxe -- sem nunca sobrescrever.

    AS DUAS PAGINAS SE COMPLETAM (28/08/2026). Para atividade recente o Strava
    devolve, na URL anonima, uma casca com a DATA e sem os numeros; e, com
    cookie, a pagina classica com a DISTANCIA e sem a data. Cada uma sozinha
    era recusada pela cadeia, que exige as duas coisas -- e o link virava
    "nao pude ler" tendo os dois dados em maos.

    Nao e' inventar numero (4-M): sao duas renderizacoes da MESMA atividade, e
    cada campo vem de quem o publica. Conferido contra o banco: a data da
    anonima bateu ao segundo em 6 de 6, e a distancia da logada em 6 de 6.

    Quem chegou primeiro fica: a ordem das tentativas e' que decide a
    preferencia, e a anonima vem primeiro porque e' a unica com o tracado.
    """
    if not novo:
        return base
    for campo in _CAMPOS_DA_ATIVIDADE:
        valor = novo.get(campo)
        if valor in (None, '', 0) or base.get(campo) not in (None, '', 0):
            continue
        base[campo] = valor
    if not _is_valid_strava_id(base.get('id_usuario_strava')) \
            and _is_valid_strava_id(novo.get('id_usuario_strava')):
        base['id_usuario_strava'] = novo.get('id_usuario_strava')
    if (not base.get('nome_usuario') or base.get('nome_usuario') == 'Desconhecido') \
            and novo.get('nome_usuario') and novo.get('nome_usuario') != 'Desconhecido':
        base['nome_usuario'] = novo['nome_usuario']
    return base


def _e_casca_de_login(html):
    """A pagina veio, mas e a tela de autenticacao do Strava, nao a atividade.

    MEDIDO EM 27/08/2026, abrindo tres das 76 falhas pela fila compartilhada:
    HTTP 200, 865 KB, `__NEXT_DATA__` presente e `pageProps` com
    `isLoggedIn`, `sessionData`, `authenticationData` -- e NENHUM objeto de
    atividade. Nao ha redirecionamento para /login, entao a checagem por URL
    nao pega. O que separa e' `isLoggedIn` FALSO: a mesma pagina logada tem o
    campo verdadeiro.
    """
    if not html or '__NEXT_DATA__' not in html:
        return False
    return bool(re.search(r'"isLoggedIn"\s*:\s*false', html))


def parse_strava_activity(activity_url, cancel_check=None, throttle=None,
                          session_cookie=None):
    """
    Scrapa uma atividade Strava e retorna dict com os dados.
    Usa __NEXT_DATA__ JSON primeiro, regex como fallback.
    Retorna None se falhar ou dados insuficientes.
    Levanta ScrapeCancelled se cancel_check() indicar cancelamento.
    Se throttle for fornecido, cada requisicao respeita o controle de taxa.
    """
    def _do_get(u, com_cookie=True, **kw):
        # A PAGINA ANONIMA E' A UNICA QUE TRAZ O TRACADO (28/08/2026). O
        # gps_fingerprint sai de streams.location, que so existe no
        # __NEXT_DATA__ servido SEM cookie. Ao mandar cookie em toda
        # requisicao (27/08) a coleta caiu de 100% para ZERO no mesmo dia.
        # `cookies={}` nao e' ignorado: ocupa a chave e impede o setdefault
        # do throttle de colar a sessao por cima.
        if not com_cookie:
            kw['cookies'] = {}
        if throttle is not None:
            return throttle.get(u, **kw)
        # Sem throttle (caminho de teste/uso avulso): ainda assim pelo
        # _hard_get, senao a requisicao pode ficar presa alem do timeout (P-80).
        # E com cookie tambem: atividade que exige login devolve 200 com a
        # casca de autenticacao, que nao se parece com erro nenhum.
        opcoes = {'headers': SCRAPE_HEADERS, 'timeout': (5, 15)}
        if session_cookie and com_cookie:
            opcoes['cookies'] = {STRAVA_SESSION_COOKIE_NAME: session_cookie}
        opcoes.update(kw or {})
        return _hard_get(u, opcoes, cancel_check=cancel_check)

    try:
        m = re.search(r'/activities/(\d+)', activity_url or '')
        if not m:
            logger.warning(f'URL invalida: {activity_url}')
            return None

        activity_id = m.group(1)
        preferred_url = _sanitize_strava_activity_url(activity_url, keep_share_sig=True)
        fallback_url = _sanitize_strava_activity_url(activity_url, keep_share_sig=False)
        candidate_urls = list(dict.fromkeys([u for u in [preferred_url, fallback_url] if u]))
        # ANONIMA PRIMEIRO, LOGADA DEPOIS (28/08/2026). A anonima traz o
        # tracado e nao gasta a cota da nossa conta; a logada entra quando a
        # anonima e' muro de cadastro. Por URL, nesta ordem.
        tentativas = [(u, com) for u in candidate_urls for com in (False, True)]

        data = {'id_atividade': activity_id}

        last_err = None
        extracted = None
        reuniao = {}
        used_url = None
        atividade_inexistente = False
        exige_login = False
        sem_sessao = False
        indisponivel = False
        # UM LINK NAO PODE CONSUMIR A FILA DO STRAVA INTEIRA. Sao ate quatro
        # requisicoes por link (pagina normal e alternativa, cada uma com
        # flyover); com todas esgotando o prazo isso da 5 minutos gastos num
        # link so. Passando do teto, para de insistir NESTE link e devolve
        # como timeout: ele vira pendencia retentavel e o job segue (4-C).
        inicio_link = time.monotonic()

        def _estourou_o_prazo_do_link():
            return (time.monotonic() - inicio_link) >= _LINK_BUDGET_SEG

        for candidate_url, com_cookie in tentativas:
            if cancel_check and cancel_check():
                raise ScrapeCancelled()
            if _estourou_o_prazo_do_link():
                logger.warning(
                    'Prazo do link esgotado (%ss) antes de tentar %s',
                    int(_LINK_BUDGET_SEG), candidate_url)
                raise ScrapeTimeout()
            try:
                resp = _do_get(candidate_url, com_cookie=com_cookie)
                # Redirecionado para a tela de login: atividade nao publica
                # (perfil privado ou visivel apenas para seguidores).
                if '/login' in (getattr(resp, 'url', '') or '').lower():
                    # Sem cookie, cair no login e' o ESPERADO para atividade
                    # nao publica -- nao e' diagnostico. Quem acusa e' a
                    # tentativa logada, logo a seguir.
                    if com_cookie:
                        exige_login = True
                    continue
                # SEM REDIRECIONAR, TAMBEM (27/08/2026). O caso comum nao e o
                # redirect: o Strava serve a CASCA de autenticacao na propria
                # URL da atividade, com HTTP 200 -- um Next.js cujo pageProps
                # tem isLoggedIn/sessionData e nenhuma atividade. A checagem
                # por '/login' na URL nunca via isso, e a atividade caia em
                # "Distancia invalida". Foram 76 numa noite so.
                if _e_casca_de_login(getattr(resp, 'text', '') or ''):
                    if com_cookie:
                        sem_sessao = True
                    # A CASCA NAO E' VAZIA: ELA TEM A DATA (28/08/2026).
                    # Descartar a pagina inteira aqui era o que fazia o link
                    # virar "nao pude ler" tendo os dois dados em maos -- a
                    # data mora nesta casca e a distancia na pagina logada.
                    # A pagina ja foi baixada; colher nao custa requisicao.
                    # Conferido contra o banco: bateu ao segundo em 6 de 6.
                    try:
                        casca_soup = BeautifulSoup(resp.content, 'html.parser')
                        for _leitor in (_extract_from_next_data,
                                        _extract_from_regex):
                            reuniao = _juntar_leituras(
                                reuniao, _leitor(resp.text, casca_soup) or {})
                    except Exception as exc_casca:
                        logger.info('Nao consegui colher a casca de %s: %s',
                                    candidate_url, exc_casca)
                    continue
                # O PAINEL NAO E' A ATIVIDADE (27/08/2026). Quando a atividade
                # nao esta disponivel para a conta do cookie, o Strava responde
                # **HTTP 200 e entrega /dashboard** — o feed da propria conta.
                # Nenhuma checagem via isso, e o leitor de ultimo recurso varre
                # a pagina inteira atras de numeros: seria km de OUTRA PESSOA
                # gravado como se fosse do atleta. Sai daqui antes de qualquer
                # leitor tocar no corpo.
                destino = (getattr(resp, 'url', '') or '').lower()
                if '/dashboard' in destino or '/athlete/onboarding' in destino:
                    indisponivel = True
                    logger.info('Atividade %s indisponivel para esta conta '
                                '(o Strava devolveu o painel).', activity_id)
                    continue
                resp.raise_for_status()

                # INTERPRETACAO COM PRAZO (P-80, 20-21/08/2026). BeautifulSoup +
                # extracao rodam numa thread com teto (_hard_run) -- sem isto,
                # uma pagina grande travava o interprete inteiro sem nenhum
                # aviso, e o job so "acordava" 12 minutos depois via o
                # detector de heartbeat parado.
                def _interpretar_pagina():
                    # TRES LEITORES, NESTA ORDEM (27/08/2026). O Strava serve
                    # duas paginas diferentes para a mesma atividade -- Next.js
                    # sem cookie, classica com cookie -- e nenhuma das duas e'
                    # garantida. O log diz SEMPRE qual funcionou: quando o
                    # primeiro parar de funcionar, isso aparece no log antes de
                    # virar link perdido.
                    soup = BeautifulSoup(resp.content, 'html.parser')
                    html_text = resp.text
                    parcial = {}
                    for nome, leitor in (('next_data', _extract_from_next_data),
                                         ('pagina_logada', _extract_from_classic),
                                         ('regex', _extract_from_regex)):
                        try:
                            extraido = leitor(html_text, soup)
                        except Exception as exc:
                            logger.info('Leitor %s falhou em %s: %s',
                                        nome, candidate_url, exc)
                            continue
                        parcial = _juntar_leituras(parcial, extraido)
                        if parcial.get('data_hora') and parcial.get('distancia'):
                            if nome != 'next_data':
                                logger.info('Leitor "%s" resolveu %s',
                                            nome, candidate_url)
                            return parcial
                    # ULTIMO RECURSO: O EMBED (27/08/2026). So chega aqui
                    # quando NENHUM leitor de HTML deu conta -- e' o caso do
                    # layout novo do Strava, em que a pagina nao tem numero
                    # nenhum (as metricas vem por XHR). O id do dono nao vem no
                    # embed, entao sai da propria pagina; sem ele a guarda da
                    # regra 4-D nao roda e o dado NAO e' aproveitado.
                    do_embed = _extract_from_embed(activity_id, _do_get)
                    if do_embed and do_embed.get('data_hora'):
                        dono = _dono_pela_pagina(html_text)
                        if dono:
                            do_embed['id_usuario_strava'] = dono
                            logger.info('Leitor "embed" resolveu %s (%.2f km, '
                                        'precisao de 1 casa e sem hora)',
                                        candidate_url, do_embed['distancia'])
                            return do_embed
                        logger.warning('Embed leu %s mas nao achei o dono na '
                                       'pagina — sem dono nao da para aplicar a '
                                       'guarda do atleta (4-D), entao descarto.',
                                       candidate_url)
                    # NUNCA None: ate 27/08 o ultimo leitor da cadeia era o
                    # `_extract_from_regex`, que SEMPRE devolvia dicionario. Com
                    # a cadeia nova qualquer leitor pode devolver None, e o
                    # codigo logo abaixo faz `extracted.get(...)` sem guarda --
                    # dava `'NoneType' object has no attribute 'get'`, que o
                    # except de fora engolia e virava "Distancia invalida".
                    # Visto na conferencia automatica das 11:40 de 27/08.
                    return parcial

                if cancel_check and cancel_check():
                    raise ScrapeCancelled()
                lido = _hard_run(_interpretar_pagina, url=candidate_url,
                                 cancel_check=cancel_check)
                # ACUMULA ENTRE AS TENTATIVAS, nao so dentro de uma pagina: e'
                # aqui que a data da anonima encontra a distancia da logada.
                reuniao = _juntar_leituras(reuniao, lido or {})
                extracted = reuniao

                # O FLYOVER SAIU (28/08/2026). Ele redireciona para
                # /dashboard -- que o robots.txt do Strava PROIBE e que e' o
                # feed da conta logada, nao a atividade. Como enriquecimento
                # nunca entregou nada; como leitor, entregaria km de outra
                # pessoa.
                extracted = extracted or {}
                if extracted and extracted.get('distancia') and extracted.get('data_hora'):
                    used_url = candidate_url
                    break
            except (ScrapeCancelled, StravaRateLimited, ScrapeTimeout):
                raise
            except requests.exceptions.HTTPError as e:
                last_err = e
                status = getattr(e.response, 'status_code', 0)
                if status in (404, 410):
                    atividade_inexistente = True
                elif status in (401, 403):
                    # 403 com marcas de CloudFlare ja teria virado backoff no
                    # throttle: aqui e a recusa "simples" de atividade privada.
                    exige_login = True
                logger.warning(f"HTTP {status} para {candidate_url}")
            except requests.exceptions.RequestException as e:
                last_err = e
                logger.warning(f"Erro de conexao para {candidate_url}: {e}")
            except Exception as e:
                last_err = e
                logger.warning(f"Erro geral scraping {candidate_url}: {e}")

        data.update(extracted or {})

        # Se nao houve dado util e o ultimo erro foi timeout de rede, o link nao
        # falhou de verdade -- ficou pendurado. Marca como pendencia retentavel
        # (apenas no fluxo em lote; chamadas avulsas mantem retorno None).
        if throttle is not None and \
                (not data.get('distancia') or data['distancia'] <= 0) and \
                isinstance(last_err, requests.exceptions.Timeout):
            raise ScrapeTimeout()

        # Sem dado util e com resposta definitiva do Strava: pendencia
        # permanente com o motivo correto (apenas no fluxo em lote).
        if throttle is not None and (not data.get('distancia') or data['distancia'] <= 0):
            if atividade_inexistente:
                raise ScrapePermanent(
                    'A atividade nao existe mais no Strava (foi apagada ou o link esta errado).',
                    stage='inexistente',
                )
            if exige_login:
                raise ScrapePermanent(
                    'A atividade nao e publica (perfil privado ou visivel so para seguidores).',
                    stage='privada',
                )

        # A CASCA DE LOGIN NAO E "DISTANCIA INVALIDA" (27/08/2026).
        # O Strava responde HTTP 200 com uma pagina Next.js de autenticacao
        # (pageProps com isLoggedIn/sessionData e NENHUMA atividade) quando a
        # atividade exige sessao. Sem reconhecer isso, 76 atividades de uma
        # noite viraram "nao puderam ser lidas", com a mensagem mandando
        # repetir a importacao -- que falharia igual, para sempre.
        # E' PENDENCIA RETENTAVEL, nao permanente: a falha e nossa (sessao),
        # nao da atividade. Com o cookie valido a mesma URL le normalmente.
        # CADA DESFECHO COM O NOME DELE (27/08/2026, pedido do dono:
        # "separe o que e bloqueio do Strava do que e erro propriamente dito").
        # So vale quando ha throttle, que e o caminho em lote: no avulso o
        # contrato continua sendo devolver None.
        # A FALTA DE SESSAO SE JULGA ANTES DO RESTO (09/09/2026).
        #
        # Este teste ja existia logo abaixo, e por estar abaixo nunca era
        # alcancado: quando falta cookie o Strava serve a casca de login, nada
        # e' extraido, e o bloco seguinte carimbava o link como
        # `formato`/PERMANENTE -- "nao e uma atividade valida, reenviar nao
        # adianta". Mentira: a atividade existe e e' publica; quem estava sem
        # credencial era o site.
        #
        # Foi exatamente o que aconteceu em 08/09, depois que o cofre ficou
        # ilegivel as 15h46: dois links bons do grupo (Dani Exemplo, 5,04 km, e
        # Edu Exemplo, 4,89 km) voltaram como "2 pendencias permanentes" e
        # NUNCA seriam retentados. Com o cofre consertado, os mesmos dois links
        # foram lidos de primeira.
        #
        # Falta de sessao e' defeito NOSSO e retentavel: assim que o cookie
        # volta, a mesma URL le normalmente. O comentario logo abaixo ja dizia
        # isso desde 27/08 -- so' estava no lugar errado da cascata.
        if (not data.get('distancia') or data['distancia'] <= 0) and sem_sessao:
            if throttle is not None:
                raise ScrapeTimeout()

        if throttle is not None and (not data.get('distancia')
                                     or data['distancia'] <= 0):
            if indisponivel:
                raise ScrapePermanent(_MOTIVO_DA_ETAPA['indisponivel'][0],
                                      stage='indisponivel')
            if not extracted:
                raise ScrapePermanent(_MOTIVO_DA_ETAPA['formato'][0],
                                      stage='formato')

        if (not data.get('distancia') or data['distancia'] <= 0) and sem_sessao:
            if throttle is not None:
                raise ScrapeTimeout()
            logger.warning('Sem sessao do Strava para %s (o Strava devolveu a '
                           'pagina de login, com HTTP 200)',
                           used_url or activity_url)
            return None

        if not data.get('distancia') or data['distancia'] <= 0:
            # CHEGOU AQUI = a pagina foi lida e a atividade nao tem distancia
            # (musculacao, ioga, fragmento de GPS). NAO E' PERDA, e dizer
            # "nao pude ler" sobre isso e' o que fazia a conta nunca fechar.
            logger.info('Atividade %s sem distancia (nao pontua) — %s',
                        activity_id, used_url or activity_url)
            if throttle is not None:
                raise ScrapePermanent(_MOTIVO_DA_ETAPA['sem_distancia'][0],
                                      stage='sem_distancia')
            return None
        if not data.get('data_hora'):
            # LI A DISTANCIA E NAO ACHEI A DATA: e' defeito NOSSO de leitura,
            # nao do Strava, e repetir depois de consertar o leitor resolve.
            # Sem este nome a atividade sumia com a mesma frase de tudo.
            logger.warning('Data nao encontrada para %s (distancia %s lida)',
                           used_url or activity_url, data.get('distancia'))
            if throttle is not None:
                raise ScrapePermanent(_MOTIVO_DA_ETAPA['sem_data'][0],
                                      stage='sem_data')
            return None

        data.setdefault('modalidade',   'outro')
        data.setdefault('nome_usuario', 'Desconhecido')

        # SEM DONO NAO EXISTE QUILOMETRO (29/08/2026, incidente dos 266 km).
        # Ate aqui um `setdefault('id_usuario_strava', '0')` transformava
        # "nao sei de quem e" em "e do atleta zero". Esse '0' atravessava a
        # gravacao inteira, o ranking passava a agrupar por NOME (a chave
        # `name:` em ranking.py) e a atividade virava um SEGUNDO atleta, sem
        # codigo, com km de ninguem. "Cada km deve ter seu respectivo atleta
        # sem duvida" (dono, 27/08) -- e duvida nao se conserta depois: nao
        # se grava.
        if not _is_valid_strava_id(data.get('id_usuario_strava')):
            logger.warning('Dono nao identificado em %s (distancia %s lida) '
                           '— nao gravo sem saber de quem e.',
                           used_url or activity_url, data.get('distancia'))
            if throttle is not None:
                raise ScrapePermanent(_MOTIVO_DA_ETAPA['sem_dono'][0],
                                      stage='sem_dono')
            return None

        data['nome_usuario'] = _compact_athlete_name(data.get('nome_usuario'))

        return data

    except (ScrapeCancelled, StravaRateLimited, ScrapeTimeout, ScrapePermanent):
        raise
    except Exception as e:
        logger.error(f"Erro geral scraping {activity_url}: {e}", exc_info=True)
        return None


def _strava_login_required(resp):
    """True quando a resposta e, na pratica, um pedido de login.

    O Strava responde de tres formas quando o cookie de sessao esta ausente ou
    expirado: 401/403, redirecionamento para /login (o requests ja seguiu) ou
    200 com o HTML do formulario de login. Cobrimos os tres.
    """
    status = getattr(resp, 'status_code', 0)
    if status in (401, 403):
        return True
    url_final = (getattr(resp, 'url', '') or '').lower()
    if '/login' in url_final or '/onboarding' in url_final:
        return True
    try:
        corpo = (resp.text or '')[:6000].lower()
    except Exception:
        corpo = ''
    return any(marca in corpo for marca in _STRAVA_LOGIN_MARKERS)


def _rotacionar_cookie_sessao(resp, cookie_enviado):
    """Persiste o cookie de sessao renovado que o Strava devolveu na resposta.

    O Strava (Rails) emite um _strava4_session novo a cada resposta; regravar o
    valor renovado mantem a sessao viva indefinidamente enquanto for usada
    (persistencia estilo WAHA). Regra de seguranca: SO rotaciona quando a
    resposta e autenticada de verdade -- um cookie emitido junto com a tela de
    login e uma sessao anonima e sobrescreveria o cofre com lixo.
    """
    try:
        if getattr(resp, 'status_code', 0) != 200 or _strava_login_required(resp):
            return
        novo = None
        historico = list(getattr(resp, 'history', []) or []) + [resp]
        for r in historico:
            valor = getattr(r, 'cookies', {}).get(STRAVA_SESSION_COOKIE_NAME)
            if valor:
                novo = valor
        if not novo or novo == cookie_enviado:
            return
        from app import secretstore
        secretstore.rotate_value(secretstore.STRAVA_SESSION, cookie_enviado, novo)
    except Exception as exc:
        logger.debug('Rotacao do cookie de sessao ignorada: %s', type(exc).__name__)


def _strava_session_get(url, session_cookie, throttle=None, referer=None):
    """GET autenticado por cookie de sessao, com pacing quando ha throttle.

    O cookie nunca aparece em log: erros mencionam apenas a URL alvo.
    """
    headers = dict(SCRAPE_HEADERS)
    headers['X-Requested-With'] = 'XMLHttpRequest'
    headers['Accept'] = 'text/javascript, text/html, application/xml, text/xml, */*'
    if referer:
        headers['Referer'] = referer
    cookies = {STRAVA_SESSION_COOKIE_NAME: session_cookie}
    if throttle is not None:
        resp = throttle.get(url, headers=headers, cookies=cookies, timeout=(5, 20))
    else:
        resp = _hard_get(url, {'headers': headers, 'cookies': cookies, 'timeout': (5, 20)})
    _rotacionar_cookie_sessao(resp, session_cookie)
    return resp


class StravaPerfilRestrito(RuntimeError):
    """O perfil existe e tem km, mas NAO mostra as atividades para a nossa conta.

    Medido em 07/09/2026: a resposta traz `#totals` com quilometragem e
    `"preFetchedEntries":[]`, e tem ~3,8 KB contra ~400 KB de um perfil aberto.
    E' privacidade do atleta, nao defeito nosso.

    PERMANENTE de proposito: repetir com o mesmo cookie da o mesmo resultado
    para sempre. So muda se a pessoa abrir o perfil ou aceitar a nossa conta
    como seguidora -- e por isso quem recebe esta excecao AVISA o admin, com
    nome, em vez de tentar de novo.
    """


class StravaMesAnteriorAConta(RuntimeError):
    """O mes pedido e ANTERIOR a criacao da conta do atleta.

    Nao e falha de leitura nem mes sem treino: e o FIM DO HISTORICO dele.
    Existe como excecao propria (29/08/2026) porque o remedio e o oposto
    do de `StravaListaVazia`: aquela pede para tentar de novo depois, esta
    pede para PARAR DE DESCER. Enquanto as duas eram a mesma coisa, uma
    importacao de atleta novo gastava 57 requisicoes perguntando por meses
    que nunca existiram (medido em 28/08/2026, 156 num dia).
    """


class StravaListaVazia(RuntimeError):
    """A pagina veio inteira, mas nao conseguimos extrair atividade nenhuma.

    Existe para NAO deixar erro de leitura passar por "esse mes nao tem nada".
    Foi essa confusao que fez a conferencia dizer "esta tudo certo" para a Bia
    Exemplo enquanto faltavam 136 km: o parser procurava um padrao que o Strava nao
    usa mais, achava zero, e zero era lido como "nada faltando".
    """


# Um HTML menor que isto e resposta curta/erro; maior que isto e pagina de
# verdade, e entao "zero atividades" so pode ser defeito de leitura.
_HTML_MINIMO_PAGINA = 20000






def _totais_do_intervalo(texto):
    """Km totais que o proprio Strava mostra para o mes. None se nao achar.

    E a VERDADE ARITMETICA da conferencia: se este numero nao bate com a soma do
    ranking daquele atleta no mes, falta alguma coisa — mesmo que a lista de
    atividades falhe. Um numero simples, que o atleta confere a olho no perfil.

    Vem do bloco ``jQuery("#totals").html(...)``, algo como
    ``413,5 km 23 h 15 min 3.892 m``. Aceita virgula (pt-BR) e ponto de milhar.
    """
    if not texto:
        return None
    bloco = re.search(r'#totals"\)\.html\("(.*?)"\);', texto, re.S)
    if not bloco:
        return None
    limpo = re.sub(r'<[^>]+>', ' ', bloco.group(1).replace('\\u003c', '<')
                                                  .replace('\\/', '/'))
    achado = re.search(r'([\d.,]+)\s*km', limpo, re.I)
    if not achado:
        return None
    numero = achado.group(1).replace('.', '').replace(',', '.')
    try:
        return float(numero)
    except ValueError:
        return None


_NOME_ATLETA_CACHE = {}


def _quem_e(athlete_id):
    """"Fulano (12345)" para o log. Sem cadastro, so o numero.

    27/08/2026, pedido do dono: log de raspagem que diz so o numero obriga a
    abrir o banco para saber de quem se esta falando -- e num backfill de 64
    meses sao dezenas de linhas iguais, uma atras da outra.
    """
    sid = str(athlete_id or '')
    if not sid:
        return '?'
    if sid in _NOME_ATLETA_CACHE:
        return _NOME_ATLETA_CACHE[sid]
    rotulo = sid
    try:
        from app.models import Atleta
        a = Atleta.query.filter_by(strava_id=sid).first()
        if a and a.nome_atual:
            rotulo = '%s (%s)' % (a.nome_atual, sid)
    except Exception:
        pass
    _NOME_ATLETA_CACHE[sid] = rotulo
    return rotulo


def _buscar_intervalo(athlete_id, ano, mes, session_cookie, throttle=None):
    """HTML cru de UM mes do perfil, pelo endpoint /interval. UMA requisicao.

    Este e o mesmo endpoint que a propria pagina de perfil usa ao navegar pelos
    meses; exige cookie de sessao (o Strava aposentou a listagem publica).
    Levanta StravaAuthRequired quando o cookie esta ausente/expirado.

    Existe separado porque a LISTA e os TOTAIS moram na MESMA resposta: antes
    eram duas requisicoes identicas para ler as duas coisas, e requisicao a
    mais e o caminho curto para o ban (regra 4-C do CLAUDE.md).
    """
    if not session_cookie:
        raise StravaAuthRequired('Cookie de sessao do Strava nao configurado.')
    ano = int(ano)
    mes = int(mes)
    query = urlencode({
        'interval': f'{ano:04d}{mes:02d}',
        'interval_type': 'month',
        'chart_type': 'miles',
        # SEMPRE 0 (28/08/2026). O `year_offset` e' dica de paginacao da
        # TELA, nao endereco do mes -- quem diz o mes e' o `interval`. E ele
        # so vale ate a idade da conta: pedir 5 anos atras num perfil criado
        # ha 3 devolve **HTTP 400**, e era isso que fazia toda importacao de
        # atleta recente perder os meses anteriores a conta dele, para
        # sempre, gastando requisicao em cada nova tentativa (job de
        # 28/08 02h34: 43 de 64 meses, todos os anteriores a 2023 em 400).
        # Provado que nao muda o mes: mesmo `#totals` com o offset calculado
        # e com 0 -- 587,5 km e 1.367,7 km, identicos. (`ERROS.md` §4.34.)
        'year_offset': 0,
    })
    perfil_url = f'https://www.strava.com/athletes/{athlete_id}'
    resp = _strava_session_get(f'{perfil_url}/interval?{query}', session_cookie,
                               throttle=throttle, referer=perfil_url)
    if _strava_login_required(resp):
        raise StravaAuthRequired(
            'O Strava recusou a sessao ao consultar o perfil (cookie expirado ou invalido).'
        )
    situacao = getattr(resp, 'status_code', 0)
    if situacao == 400:
        # HTTP 400 AQUI QUER DIZER "a conta nao existia nesse mes".
        # Nao e erro: e o fim do historico do atleta. Ver a excecao.
        logger.info('Perfil %s: a conta ainda nao existia em %02d/%04d '
                    '(HTTP 400) — fim do historico dele.',
                    _quem_e(athlete_id), mes, ano)
        raise StravaMesAnteriorAConta(
            'A conta deste atleta ainda nao existia em %02d/%04d.'
            % (mes, ano))
    if situacao != 200:
        logger.warning('Perfil %s: resposta %s ao consultar %02d/%04d',
                       _quem_e(athlete_id), situacao, mes, ano)
        raise StravaListaVazia(
            'O Strava respondeu %s ao listar %02d/%04d.'
            % (situacao, mes, ano))
    return resp.text or ''


def _buscar_intervalo_ano(athlete_id, ano, session_cookie, throttle=None):
    """Total de km do ANO inteiro (mesmo endpoint, interval_type=year). UMA requisicao.

    Pre-filtro para "todos os periodos"/"ano inteiro" (21-22/08/2026): o total
    de um ano e a soma dos totais dos 12 meses dele. Se vier exatamente <= 0,
    e PROVA (nao suposicao por silencio) de que nenhum mes daquele ano tem
    atividade -- pode pular a listagem mes a mes inteira daquele ano. Uma
    tentativa anterior tentava adivinhar isso parando depois de N meses vazios
    seguidos; foi rejeitada com razao (atleta pode sumir 2+ anos e voltar) e
    substituida por isto, que cobre o periodo inteiro sem excecao.

    Ao contrario de `_buscar_intervalo`, NUNCA levanta por falha de leitura --
    devolve None (desconhecido). Quem chama trata None como "nao pular": cai
    de volta na varredura mes a mes de sempre. So StravaAuthRequired (cookie
    ruim) e StravaRateLimited (via throttle) propagam, porque esses dois ja
    abortam o job inteiro de qualquer forma.
    """
    if not session_cookie:
        raise StravaAuthRequired('Cookie de sessao do Strava nao configurado.')
    ano = int(ano)
    query = urlencode({
        'interval': f'{ano:04d}',
        'interval_type': 'year',
        'chart_type': 'miles',
        # SEMPRE 0 (28/08/2026). O `year_offset` e' dica de paginacao da
        # TELA, nao endereco do mes -- quem diz o mes e' o `interval`. E ele
        # so vale ate a idade da conta: pedir 5 anos atras num perfil criado
        # ha 3 devolve **HTTP 400**, e era isso que fazia toda importacao de
        # atleta recente perder os meses anteriores a conta dele, para
        # sempre, gastando requisicao em cada nova tentativa (job de
        # 28/08 02h34: 43 de 64 meses, todos os anteriores a 2023 em 400).
        # Provado que nao muda o mes: mesmo `#totals` com o offset calculado
        # e com 0 -- 587,5 km e 1.367,7 km, identicos. (`ERROS.md` §4.34.)
        'year_offset': 0,
    })
    perfil_url = f'https://www.strava.com/athletes/{athlete_id}'
    resp = _strava_session_get(f'{perfil_url}/interval?{query}', session_cookie,
                               throttle=throttle, referer=perfil_url)
    if _strava_login_required(resp):
        raise StravaAuthRequired(
            'O Strava recusou a sessao no pre-filtro anual (cookie expirado ou invalido).'
        )
    if getattr(resp, 'status_code', 0) != 200:
        logger.warning('Perfil %s: resposta %s no pre-filtro do ano %04d',
                       _quem_e(athlete_id), getattr(resp, 'status_code', '?'), ano)
        return None
    return _totais_do_intervalo(resp.text or '')


def _ids_conferidos(texto, ano, mes, km_total, athlete_id=None):
    """IDs do texto, recusando-se a chamar de "mes vazio" o que e falha de leitura.

    QUEM DECIDE E O TOTAL, NAO O TAMANHO DA PAGINA. A mesma resposta que traz a
    lista traz os km do mes:

    - total > 0 e lista vazia  -> ha km la e nao reconhecemos nada: FALHA.
    - total = 0                -> mes realmente sem atividade: lista vazia e a
      verdade, e o atleta que ficou um mes parado nao pode derrubar a leitura
      (era o que aconteceria julgando so pelo tamanho do HTML).
    - total ilegivel           -> caimos no tamanho da pagina: resposta grande
      sem nada reconhecido continua sendo falha.

    Existe porque o zero silencioso ja escondeu 136 km de uma atleta: o parser
    antigo procurava "/activities/", que sumiu da resposta, achava zero, e o
    zero virava "nada faltando" na conferencia.
    """
    # ESTRUTURA, NAO REGEX (28/08/2026). Ate aqui a conferencia procurava
    # `"entity_id":"<10+ digitos>"` no bloco da lista -- e esse padrao casa
    # com id de COMENTARIO, que colide com id de atividade antiga. Numa
    # conferencia isso vira "o atleta esqueceu de postar" para uma atividade
    # que nunca existiu, e cada uma dessas custa uma requisicao ao Strava.
    #
    # E filtra pelo DONO: a conferencia pergunta se ESTE atleta esqueceu de
    # postar. Treino em grupo de terceiro nunca foi link que ele devia ter
    # postado (regra 4-D).
    entradas = _entradas_da_listagem(texto)
    if entradas is not None:
        ids = [e['id_atividade'] for e in entradas
               if not athlete_id
               or str(e['id_usuario_strava']) == str(athlete_id)]
        if ids:
            return ids
    if km_total is not None and km_total <= 0:
        return []
    if km_total is not None:
        raise StravaListaVazia(
            'O Strava diz %.2f km em %02d/%04d, mas nenhuma atividade foi '
            'reconhecida na lista. O formato da pagina provavelmente mudou — '
            'a leitura precisa ser revista.' % (km_total, mes, ano))
    if len(texto) > _HTML_MINIMO_PAGINA:
        raise StravaListaVazia(
            'A pagina de %02d/%04d veio com %d caracteres, sem total legivel e sem '
            'nenhuma atividade reconhecida. O Strava provavelmente mudou o formato '
            '— a leitura precisa ser revista.' % (mes, ano, len(texto)))
    return []



# ---------------------------------------------------------------------------
# A LISTAGEM LIDA COMO ESTRUTURA, NAO COMO TEXTO (28/08/2026)
# ---------------------------------------------------------------------------
# Ate 27/08 os ids saiam de regex na resposta inteira, procurando
# `"entity_id":"<10+ digitos>"`. Esse padrao casa com
# `kudosAndComments...reactions_summary.entity_id`, que e' ID DE COMENTARIO --
# e id de comentario COLIDE com id de atividade antiga. Foi assim que duas pessoas de fora
# viraram atletas de Palmeiropolis: 11 de 11 ids orfaos de
# tres amostras foram explicados por esse caminho, conferidos um a um no JSON.
#
# Aqui nao se procura numero em lugar nenhum: abre-se o componente React e
# leem-se as entradas declaradas. `Activity` e' do dono do perfil;
# `GroupActivity` traz uma lista, cada uma com o SEU dono no `entry-owner`;
# `Challenge` e `Club` sao descartados por nao serem atividade.
#
# E a listagem ja traz os NUMEROS -- distancia, data, esporte, desnivel e
# tempo. Conferido contra o nosso banco: 71 atividades, 71 bateram, nenhuma
# divergiu. Por isso um mes custa UMA requisicao, e nao uma por atividade.

_ESCAPES_JS = {'\\': '\\', '"': '"', "'": "'", '/': '/',
               'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f'}


def _desescapar_uma_passada(texto):
    """Desfaz o escape de string JS numa varredura so, da esquerda para a direita.

    Fazer em duas passadas (trocar a barra e depois a aspa) corrompe o
    conteudo: uma barra invertida literal escapada vira escape da proxima
    aspa. A resposta do Strava e' uma string JS que contem HTML que contem
    JSON -- tres camadas -- e so a varredura unica preserva as tres.
    """
    if not texto:
        return ''
    saida = []
    i = 0
    n = len(texto)
    while i < n:
        c = texto[i]
        if c == '\\' and i + 1 < n:
            p = texto[i + 1]
            if p in _ESCAPES_JS:
                saida.append(_ESCAPES_JS[p])
                i += 2
                continue
            if p == 'u' and i + 5 < n:
                try:
                    saida.append(chr(int(texto[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            saida.append(p)
            i += 2
            continue
        saida.append(c)
        i += 1
    return ''.join(saida)


def _contexto_da_listagem(texto):
    """O `appContext` do componente Microfrontend, ou None se nao houver.

    None NAO quer dizer "mes vazio" -- quer dizer que nao conseguimos ler.
    Quem chama levanta StravaListaVazia (regra 4-G).

    SOBRE AS PASSADAS DE DESESCAPE (07/09/2026). O laco abaixo desescapa
    ate estabilizar. MEDIDO: hoje UMA passada basta, inclusive na resposta
    curta -- cheguei a escrever aqui que ela vinha duplamente escapada, e
    ESTAVA ERRADO: o que eu via eram as barras do `repr()` do Python. O
    laco fica porque o numero de camadas ja mudou antes (string JS que
    contem HTML que contem JSON) e repetir ate parar de mudar custa nada
    -- mas NAO foi isto que causou a P-130. A causa era outra, e esta
    logo abaixo: `preFetchedEntries` vazio em perfil restrito.
    """
    plano = texto or ''
    for _ in range(4):          # teto: string que nao estabiliza esta corrompida
        seguinte = _desescapar_uma_passada(plano)
        if seguinte == plano:
            break
        plano = seguinte
        if re.search(r"data-react-class='Microfrontend'", plano):
            break
    for m in re.finditer(
            r"data-react-class='Microfrontend'\s+data-react-props='([^']*)'", plano):
        try:
            props = json.loads(_html.unescape(m.group(1)))
        except (ValueError, TypeError):
            continue
        contexto = props.get('appContext') if isinstance(props, dict) else None
        if isinstance(contexto, dict) and 'preFetchedEntries' in contexto:
            return contexto
    return None


def _sem_acento(texto):
    """Compara rotulo sem depender de acento nem de caixa.

    O Strava responde no idioma do cabecalho: "Distancia" para nos, "Distance"
    para quem le em ingles. Casar por prefixo sem acento cobre os dois.
    """
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', texto or '')
                   if unicodedata.category(c) != 'Mn').lower()


_ROTULOS_STATS = {
    'distancia': ('distanc',),          # "Distancia" / "Distance"
    'elevacao':  ('elev',),             # "Ganho de elev." / "Elevation"
    'tempo':     ('tempo', 'time', 'duracao'),
}


def _minutos_do_rotulo(texto):
    """"2 h 34 min" -> 154.0. None quando nao houver numero de tempo.

    E O TEMPO EM MOVIMENTO, e e por isso que ele existe aqui (28/08/2026).
    O campo `elapsedTime` da mesma entrada e o tempo DECORRIDO, com paradas:
    numa amostra, 10.082 s de decorrido contra 9.240 s ("2h 34min") de
    movimento. Calcular velocidade com o decorrido so podia dar numero BAIXO
    demais -- e deu: uma corrida saiu a 0,32 km/h, que o anti-cheat leu como
    suspeita. Numero errado e pior que numero ausente (regra 4-M).
    """
    if not texto:
        return None
    total = 0.0
    achou = False
    for numero, unidade in re.findall(r'([0-9]+)\s*(h|min|s)\b', texto):
        achou = True
        valor = float(numero)
        if unidade == 'h':
            total += valor * 60.0
        elif unidade == 'min':
            total += valor
        else:
            total += valor / 60.0
    return total if achou else None


def _stats_da_listagem(stats):
    """[{key: stat_one, value: "40,21<abbr...> km"}, ...] -> numeros.

    Casa pelo ROTULO, nunca pela posicao: a ordem das colunas muda conforme a
    modalidade, e ler `stat_two` como "elevacao" sempre daria certo ate o dia
    em que nao desse.
    """
    if not isinstance(stats, list):
        return {}
    bruto = {}
    for item in stats:
        if isinstance(item, dict) and item.get('key'):
            bruto[item['key']] = item.get('value')
    saida = {}
    for pos in ('one', 'two', 'three', 'four'):
        valor = bruto.get('stat_%s' % pos)
        rotulo = _sem_acento(str(bruto.get('stat_%s_subtitle' % pos) or ''))
        if not valor:
            continue
        texto = re.sub(r'<[^>]*>', ' ', str(valor))
        m = re.search(r'([0-9]+(?:[.,][0-9]+)?)', texto)
        if not m:
            continue
        try:
            numero = float(m.group(1).replace(',', '.'))
        except ValueError:
            continue
        for campo, marcas in _ROTULOS_STATS.items():
            if any(marca in rotulo for marca in marcas):
                if campo == 'distancia':
                    # A listagem escreve km; metros aparecem em atividade curta.
                    saida['distancia'] = (numero if ' km' in texto
                                          else numero / 1000.0)
                elif campo == 'elevacao':
                    saida['elevacao'] = numero
                elif campo == 'tempo':
                    saida['tempo_min'] = _minutos_do_rotulo(texto)
                break
    return saida


_RE_DONO_LISTAGEM = re.compile(
    r'class="entry-owner"[^>]*href="/athletes/(\d+)"[^>]*>([^<]*)<')


def _dono_da_entrada(entrada):
    """(id, nome) do `entry-owner` de uma entrada, ou (None, None).

    O dono viaja dentro de um pedaco de HTML guardado no JSON. Procurar no
    despejo da entrada inteira e' de proposito: o campo muda de lugar entre
    `Activity` e `GroupActivity`, o ancora nao.
    """
    try:
        texto = json.dumps(entrada, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, None
    m = _RE_DONO_LISTAGEM.search(texto.replace('\\"', '"'))
    if not m:
        return None, None
    return m.group(1), (_compact_athlete_name(m.group(2).strip()) or None)


def _data_da_listagem(valor):
    """'2022-08-28T19:27:49Z' -> datetime ingenuo (hora local do atleta).

    O campo do treino em grupo se chama `start_date_local` e vem com o mesmo
    sufixo `Z` do outro -- o `Z` aqui e' enfeite, nao fuso. Conferido contra o
    banco em 71 atividades: o mes bateu em todas.
    """
    if not valor:
        return None
    texto = str(valor).strip().replace('Z', '').strip()
    for formato in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(texto[:19], formato)
        except ValueError:
            continue
    return None


def _atividade_da_listagem(activity_id, esporte, quando, stats, segundos,
                           dono_id, dono_nome):
    """Monta o dicionario no MESMO formato de `parse_strava_activity`.

    Assim a gravacao, a deduplicacao, a guarda do atleta e o anti-cheat
    continuam sendo um caminho so -- a listagem entra por onde a raspagem ja
    entrava, em vez de ganhar um segundo caminho para manter.
    """
    quando = _data_da_listagem(quando)
    numeros = _stats_da_listagem(stats)
    distancia = numeros.get('distancia')
    if not activity_id or not quando or not distancia or distancia <= 0:
        return None
    # SO O TEMPO EM MOVIMENTO, ou nenhum. `segundos` (elapsedTime) fica de
    # fora de proposito: e outra grandeza, e misturar as duas foi o que
    # produziu velocidades impossiveis. Sem tempo, a atividade entra sem
    # velocidade -- e os detectores do anti-cheat, que saem cedo quando o
    # campo esta vazio, simplesmente nao a julgam.
    minutos = numeros.get('tempo_min')
    dados = {
        'id_atividade': str(activity_id),
        'id_usuario_strava': str(dono_id or '0'),
        'nome_usuario': dono_nome or 'Desconhecido',
        'modalidade': SPORT_MAP.get(str(esporte or '').lower(), 'outro'),
        # a listagem ja diz o esporte; guardar o cru custa uma coluna e e' o
        # que separa corrida de caminhada sem chutar (28/08/2026)
        'esporte_strava': (str(esporte).strip()[:30] or None) if esporte else None,
        'distancia': round(float(distancia), 3),
        'distancia_raw': float(distancia) * 1000.0,
        'data_hora': quando,
        'tempo_minutos': round(minutos, 2) if minutos else None,
        'elevacao_total': numeros.get('elevacao'),
        # NAO VEM NA LISTAGEM, e nao se inventa: o tracado so existe na pagina
        # anonima da atividade. Quem precisa dele e' o anti-cheat do mes
        # corrente, que continua abrindo atividade por atividade.
        'gps_fingerprint': None,
    }
    if minutos and minutos > 0:
        dados['velocidade_media'] = round(dados['distancia'] / (minutos / 60.0), 2)
        dados['pace_min_km'] = round(minutos / dados['distancia'], 2)
    return dados


def _entradas_da_listagem(texto):
    """Atividades declaradas na resposta de /interval, no formato de gravacao.

    Devolve None quando o componente nao foi encontrado -- que e' "nao consegui
    ler", nunca "nao tem nada" (regra 4-G).
    """
    contexto = _contexto_da_listagem(texto)
    if contexto is None:
        return None
    dono_perfil = str(contexto.get('athleteProfileId') or '') or None
    saida = []
    for entrada in (contexto.get('preFetchedEntries') or []):
        if not isinstance(entrada, dict):
            continue
        tipo = entrada.get('entity')
        if tipo == 'Activity':
            atividade = entrada.get('activity') or {}
            dono_id, dono_nome = _dono_da_entrada(entrada)
            pronto = _atividade_da_listagem(
                atividade.get('id'), atividade.get('type'),
                atividade.get('startDate'), atividade.get('stats'),
                atividade.get('elapsedTime'),
                dono_id or dono_perfil, dono_nome)
            if pronto:
                saida.append(pronto)
        elif tipo == 'GroupActivity':
            # CADA UMA PARA O SEU DONO (regra do dono, 27/08/2026). Treino em
            # grupo nao e' fraude nem duplicata: quando o Strava falha no
            # telefone de um, os km valem para os dois. Quem nao estiver no
            # cadastro e' descartado por quem chama, nao aqui.
            for sub in ((entrada.get('rowData') or {}).get('activities') or []):
                if not isinstance(sub, dict):
                    continue
                dono_id, dono_nome = _dono_da_entrada(sub)
                pronto = _atividade_da_listagem(
                    sub.get('activity_id'), sub.get('activity_class_name'),
                    sub.get('start_date_local'), sub.get('stats'),
                    sub.get('elapsed_time'), dono_id, dono_nome)
                if pronto:
                    saida.append(pronto)
        # Challenge, Club e o que mais vier: nao sao atividade, nao entram.
    vistos = set()
    unicas = []
    for item in saida:
        if item['id_atividade'] in vistos:
            continue
        vistos.add(item['id_atividade'])
        unicas.append(item)
    return unicas


def fetch_athlete_month_atividades(athlete_id, ano, mes, session_cookie,
                                   throttle=None):
    """As atividades de um mes do perfil, com numeros, em UMA requisicao.

    Devolve (lista, total_km). Levanta StravaListaVazia quando o total diz que
    o mes tem km e a leitura nao devolveu atividade nenhuma (regra 4-G).
    """
    texto = _buscar_intervalo(athlete_id, ano, mes, session_cookie,
                              throttle=throttle)
    total = _totais_do_intervalo(texto)
    entradas = _entradas_da_listagem(texto)
    if entradas is None:
        raise StravaListaVazia(
            'Nao encontrei a lista de atividades de %02d/%d no perfil %s '
            '(o Strava respondeu, mas a pagina nao trouxe o componente da '
            'listagem).' % (mes, ano, athlete_id))
    if not entradas and total and total > 0:
        # NAO E' "NAO CONSEGUI LER" -- E' "ELE NAO ME DEIXA VER" (07/09/2026).
        #
        # Chegar aqui significa que o componente da listagem FOI encontrado (se
        # nao tivesse sido, `entradas` seria None e o ramo acima teria pegado),
        # que ele veio VAZIO, e que mesmo assim o mes tem quilometro no total.
        # Isso e' a assinatura de perfil com atividades restritas: o Strava
        # publica o agregado e esconde a lista. Medido lado a lado com dois
        # perfis abertos, no mesmo mes -- 3,8 KB contra 400 KB.
        #
        # Repetir com o mesmo cookie da o mesmo resultado para sempre; era isso
        # que fazia o job descer dez meses seguidos gastando requisicao em cada
        # (18 num dia, 29/08) e terminar com "Nenhuma atividade encontrada".
        raise StravaPerfilRestrito(
            'O perfil %s soma %.1f km em %02d/%d, mas nao mostra as atividades '
            'para a nossa conta — as atividades dele sao privadas ou so para '
            'seguidores.' % (athlete_id, total, mes, ano))
    return entradas, total

def fetch_athlete_month_activity_ids(athlete_id, ano, mes, session_cookie, throttle=None):
    """IDs das atividades de UM mes do perfil, via endpoint /interval do Strava."""
    texto = _buscar_intervalo(athlete_id, ano, mes, session_cookie, throttle=throttle)
    return _ids_conferidos(texto, ano, mes, _totais_do_intervalo(texto),
                           athlete_id=athlete_id)


def fetch_athlete_month_summary(athlete_id, ano, mes, session_cookie, throttle=None):
    """(ids, km_totais) do mes, numa unica requisicao ao Strava.

    Os dois juntos sao o que torna a conferencia confiavel: a lista diz QUAIS
    atividades faltam, o total diz QUANTO falta. Quando os dois discordam, quem
    manda e o total — ele nao depende de a gente reconhecer cada atividade.

    CUIDADO AO INTERPRETAR O TOTAL: ele soma TODAS as modalidades do Strava,
    inclusive as que o Movimente-se nao conta (natacao, por exemplo). Por isso
    diferenca no total nunca vira acusacao de km faltando por conta propria —
    ela so aponta que ha algo a explicar, e a confirmacao continua sendo
    atividade por atividade em parse_strava_activity (regra 4-D).
    """
    texto = _buscar_intervalo(athlete_id, ano, mes, session_cookie, throttle=throttle)
    km = _totais_do_intervalo(texto)
    return _ids_conferidos(texto, ano, mes, km, athlete_id=athlete_id), km


def fetch_athlete_month_entries(athlete_id, ano, mes, session_cookie,
                                throttle=None):
    """As atividades do mes COM data, numa unica requisicao (20/09/2026, P-136).

    `fetch_athlete_month_summary` devolve so os ids, e a regra do fechamento
    precisa saber QUANDO cada atividade que falta aconteceu (nos dias 10 e 20
    so entram as posteriores ao marco anterior). A data ja vinha na mesma
    resposta -- isto e uma porta para ela, nao uma leitura a mais.
    """
    texto = _buscar_intervalo(athlete_id, ano, mes, session_cookie, throttle=throttle)
    entradas = _entradas_da_listagem(texto) or []
    return [e for e in entradas
            if not athlete_id or str(e['id_usuario_strava']) == str(athlete_id)]


def fetch_athlete_nome(athlete_id, session_cookie, throttle=None):
    """O nome do atleta, lido da pagina de perfil DELE. Ver o patch de 06/09.

    Devolve None sempre que houver a menor duvida sobre de quem e' o nome --
    porque nome errado gravado em cima de nome certo nao volta sozinho, e ja
    passou por 62 atletas uma vez.
    """
    if not session_cookie or not _is_valid_strava_id(athlete_id):
        return None
    perfil_url = 'https://www.strava.com/athletes/%s' % athlete_id
    try:
        if throttle is not None:
            resp = throttle.get(
                perfil_url,
                cookies={STRAVA_SESSION_COOKIE_NAME: session_cookie})
        else:
            resp = _hard_get(perfil_url, {
                'headers': SCRAPE_HEADERS,
                'cookies': {STRAVA_SESSION_COOKIE_NAME: session_cookie},
                'timeout': (5, 20),
            })
    except (ScrapeCancelled, StravaRateLimited, StravaLimiteDeVolume):
        raise
    except Exception as exc:
        logger.info('Nome do atleta %s indisponivel: %r', athlete_id, exc)
        return None

    if getattr(resp, 'status_code', 0) != 200 or _strava_login_required(resp):
        return None

    # TRAVA 1 -- A RESPOSTA E' DO PERFIL QUE EU PEDI?
    # O `<title>` nao carrega o id, entao a amarracao com a regra 4-V se faz
    # aqui: o id foi na requisicao, e a resposta tem de continuar nela. Um
    # redirecionamento (perfil apagado, privado, tela de login) traria o titulo
    # de OUTRA pagina, e o nome iria para o atleta errado sem ninguem ver.
    destino = (getattr(resp, 'url', '') or '')
    if ('/athletes/%s' % athlete_id) not in destino:
        logger.warning('Nome do atleta %s recusado: a resposta veio de %r.',
                       athlete_id, destino[:120])
        return None

    _rotacionar_cookie_sessao(resp, session_cookie)
    html = (resp.text or '')

    achado = re.search(r'<title>([^<|]{2,80})', html)
    if not achado:
        return None
    nome = _compact_athlete_name(achado.group(1).strip())
    if not nome or nome == 'Desconhecido':
        return None

    # TRAVA 2 -- ESTE NOME E' O DA CONTA DO COOKIE?
    # E' a assinatura exata do estrago de 06/09: o leitor pegava o
    # `first_name` do `currentAthlete` e devolvia o dono do cookie para todo
    # mundo. Se o nome lido bate com o da conta logada e o perfil pedido NAO e'
    # o dela, a leitura pegou a pagina errada -- ou o lugar errado da pagina.
    dono_cookie = re.search(r'"display_name"\s*:\s*"([^"]{0,80})"', html)
    id_cookie = re.search(r'Strava\.Models\.CurrentAthlete\(\{"id":(\d+)', html)
    if dono_cookie and id_cookie and str(id_cookie.group(1)) != str(athlete_id):
        if nome == _compact_athlete_name(dono_cookie.group(1)):
            logger.warning(
                'Nome do atleta %s recusado: veio igual ao da conta logada '
                '(%r). E a assinatura do incidente de 06/09 — nao gravo.',
                athlete_id, nome)
            return None
    return nome


def fetch_athlete_avatar_url(athlete_id, session_cookie, throttle=None,
                             com_nome=False):
    """URL publica da foto de perfil do atleta (CDN do Strava).

    Com `com_nome=True` devolve `(foto, nome)` -- o nome sai da MESMA
    resposta, sem custar uma segunda requisicao (29/08/2026).

    A pagina de perfil so mostra o avatar com sessao ativa. A URL retornada e
    da CDN publica (funciona depois sem cookie). None quando nao encontrada.
    """
    vazio = (None, None) if com_nome else None
    if not session_cookie:
        return vazio
    perfil_url = f'https://www.strava.com/athletes/{athlete_id}'
    try:
        # Requisicao "de navegador" (sem os headers XHR do _strava_session_get):
        # a pagina completa do perfil e que carrega a foto.
        if throttle is not None:
            resp = throttle.get(
                perfil_url,
                cookies={STRAVA_SESSION_COOKIE_NAME: session_cookie},
            )
        else:
            resp = _hard_get(perfil_url, {
                'headers': SCRAPE_HEADERS,
                'cookies': {STRAVA_SESSION_COOKIE_NAME: session_cookie},
                'timeout': (5, 20),
            })
    except (ScrapeCancelled, StravaRateLimited):
        raise
    except Exception as exc:
        logger.warning('Avatar do atleta %s indisponivel: %r', athlete_id, exc)
        return vazio
    if getattr(resp, 'status_code', 0) != 200 or _strava_login_required(resp):
        logger.debug('Avatar %s: resposta %s (sem foto)',
                     athlete_id, getattr(resp, 'status_code', '?'))
        return vazio
    _rotacionar_cookie_sessao(resp, session_cookie)
    html = (resp.text or '').replace('\\/', '/')

    def _limpar(url):
        # No HTML as aspas podem vir escapadas (&quot;): corta qualquer resto.
        return re.split(r'&quot|&#34|["\']', url)[0][:500]

    # 1) Caminho classico da CDN de fotos, amarrado ao proprio id do atleta
    #    (evita pegar o avatar de OUTRO atleta que aparece no feed do perfil).
    m = re.search(
        r'https://[a-z0-9.\-]+/pictures/athletes/%s/[^"\'\s\\]+' % re.escape(str(athlete_id)),
        html,
    )
    foto_cdn = _limpar(m.group(0)) if m else None
    if foto_cdn and not com_nome:
        return foto_cdn
    # 2) Campo JSON generico de perfil (formatos novos do Strava).
    m2 = re.search(r'"profile(?:_medium|Medium|ImageUrl|_image_url)?"\s*:\s*"(https://[^"]+)"', html)
    foto = foto_cdn
    if not foto and m2 and 'avatar' not in m2.group(1) and 'badge' not in m2.group(1):
        foto = _limpar(m2.group(1))
    if not com_nome:
        return foto

    # O NOME NAO SE COLHE AQUI. NAO ATE HAVER ANCORA PROVADA (29/08/2026).
    #
    # A versao anterior fazia `re.search(r'"first_name"...', html)` e dizia,
    # neste mesmo comentario, que "a pagina E do atleta pedido, entao nao ha
    # risco de confundir com a conta logada". ESTAVA ERRADO: o Strava monta
    # o objeto da conta do cookie (`currentAthlete`) tambem na pagina de
    # perfil de terceiro, e ele vem ANTES. O regex pegava sempre o dono do
    # cookie -- e o botao "Atualizar nome e foto" escreveu o mesmo nome em
    # 76 dos 101 atletas.
    #
    # E' o MESMO erro da regra 4-D que eu tinha acabado de consertar na
    # pagina da atividade (`_atleta_da_atividade`), horas antes. Comentario
    # que AFIRMA que a guarda nao e' necessaria e' o mesmo veneno de
    # 27/08: faz a proxima leitura parar de procurar.
    #
    # A FOTO CONTINUA, porque ela sempre teve o id do atleta DENTRO da
    # ancora (`/pictures/athletes/<id>/`) -- e por isso nunca errou.
    # Quando houver ancora igualmente amarrada ao id para o nome, ela volta.
    return foto, None


def validate_strava_session_cookie(session_cookie):
    """Testa o cookie contra uma pagina que so existe para quem esta logado.

    Retorna (ok, mensagem, athlete_id_da_sessao). Nao altera nada na conta:
    e uma unica leitura da propria area do usuario.
    """
    if not (session_cookie or '').strip():
        return False, 'Nenhum cookie informado.', None
    try:
        resp = _strava_session_get(
            'https://www.strava.com/athlete/training', session_cookie,
            referer='https://www.strava.com/dashboard',
        )
    except ScrapeTimeout:
        return False, 'Tempo esgotado ao falar com o Strava. Tente novamente.', None
    except Exception as exc:
        return False, f'Falha de rede ao testar o cookie: {type(exc).__name__}', None

    if _strava_login_required(resp):
        return False, 'Cookie recusado pelo Strava (expirado ou invalido). Cole um cookie novo.', None
    if getattr(resp, 'status_code', 0) != 200:
        return False, f'O Strava respondeu {getattr(resp, "status_code", "?")}. Tente novamente.', None

    athlete_id = None
    try:
        encontrado = re.search(r'"athlete_id"\s*:\s*"?(\d+)', resp.text or '')
        if not encontrado:
            encontrado = re.search(r'/athletes/(\d+)', resp.text or '')
        if encontrado:
            athlete_id = encontrado.group(1)
    except Exception:
        athlete_id = None
    if athlete_id:
        return True, f'Cookie valido. Sessao conectada ao atleta {athlete_id}.', athlete_id
    return True, 'Cookie valido: o Strava aceitou a sessao.', None


def _meses_para_varrer(periodos):
    """(ano, mes) a consultar, em ordem cronologica e sem meses futuros."""
    hoje = date.today()
    limite = (hoje.year, hoje.month)
    meses = sorted(
        {(int(ano), int(mes)) for ano, mes in (periodos or ())}
    )
    return [par for par in meses if par <= limite]


def _profile_urls_por_intervalo(athlete_id, periodos, session_cookie,
                                throttle=None, progress_callback=None):
    """Percorre os meses pedidos pelo endpoint /interval e junta os IDs.

    Consulta exatamente os meses de ``periodos`` (1 requisicao cada) -- quem
    decide QUAIS meses entram (piso, mes vigente excluido, pre-filtro por
    ano) e o chamador (upload.py). Ate 21/08/2026 esta funcao tinha um modo
    ``periodos=None`` que caminhava para tras a partir de hoje e parava apos
    N meses vazios seguidos -- removido (codigo morto: nenhum chamador real
    jamais passou None) porque essa mesma heuristica foi tentada e rejeitada
    no fluxo real (atleta pode ficar 2+ anos sem Strava e voltar depois), e
    deixar uma segunda copia dela dormindo aqui era armadilha para o proximo
    chamador que aparecesse.
    """
    def _nota(mensagem, pct):
        if progress_callback:
            try:
                progress_callback(mensagem, pct)
            except Exception:
                pass

    meses = _meses_para_varrer(periodos)
    ids_unicos = []
    conhecidos = set()
    total = len(meses)
    for indice, (ano, mes) in enumerate(meses, start=1):
        pct = 15 + int((indice / max(total, 1)) * 10)
        _nota(f'Lendo o perfil no Strava: {mes:02d}/{ano} ({indice}/{total})...', pct)
        do_mes = fetch_athlete_month_activity_ids(
            athlete_id, ano, mes, session_cookie, throttle=throttle
        )
        novos = 0
        for activity_id in do_mes:
            if activity_id not in conhecidos:
                conhecidos.add(activity_id)
                ids_unicos.append(activity_id)
                novos += 1
        logger.info('Perfil %s: %02d/%04d -> %s atividade(s) (%s novas)',
                    _quem_e(athlete_id), mes, ano, len(do_mes), novos)

    return [f'https://www.strava.com/activities/{aid}' for aid in ids_unicos]


def extract_strava_profile_activity_urls(profile_url, max_pages=100, periodos=None,
                                         session_cookie=None, throttle=None,
                                         progress_callback=None):
    """Lista as atividades de um perfil Strava.

    Com cookie de sessao usa o endpoint /interval mes a mes (unico caminho que
    o Strava ainda serve); sem cookie cai na paginacao publica legada, que hoje
    costuma vir vazia mas segue aqui como plano B e para nao mudar o
    comportamento de quem chama sem os parametros novos.

    O retorno contem somente URLs de atividade; os dados e a data continuam
    sendo validados individualmente por ``parse_strava_activity`` antes de
    qualquer insercao no banco.
    """
    match = STRAVA_PROFILE_RE.match((profile_url or '').strip())
    if not match:
        return None, []

    athlete_id = match.group(1)

    if session_cookie:
        urls = _profile_urls_por_intervalo(
            athlete_id, periodos, session_cookie,
            throttle=throttle, progress_callback=progress_callback,
        )
        logger.info('Perfil %s: %s atividade(s) listadas via sessao autenticada',
                    _quem_e(athlete_id), len(urls))
        return athlete_id, urls

    urls = []
    seen = set()
    for page in range(max_pages):
        offset = page * 200
        endpoint = (
            f'https://www.strava.com/athletes/{athlete_id}/activities'
            f'?offset={offset}&limit=200'
        )
        try:
            # Idem: sem o _hard_get esta chamada podia ficar presa alem do
            # timeout declarado, que e o sintoma da P-80.
            response = _hard_get(endpoint, {'headers': SCRAPE_HEADERS,
                                            'timeout': (5, 15)})
            response.raise_for_status()
        except (requests.RequestException, ScrapeTimeout) as exc:
            logger.warning('Falha ao buscar atividades publicas do atleta %s: %s', athlete_id, exc)
            break

        page_urls = []
        for found in STRAVA_DIRECT_RE.finditer(response.text):
            url = _sanitize_strava_activity_url(found.group(0), keep_share_sig=True)
            if url:
                page_urls.append(url)

        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and str(item.get('id') or '').isdigit():
                    page_urls.append(
                        f"https://www.strava.com/activities/{item['id']}"
                    )

        novos = 0
        for url in page_urls:
            activity_id = _activity_id_from_url(url)
            if activity_id and activity_id not in seen:
                seen.add(activity_id)
                urls.append(url)
                novos += 1
        if not page_urls or novos == 0:
            break
        time.sleep(0.5)

    return athlete_id, urls


# ===== PROCESSAMENTO DO TXT =====

def process_whatsapp_txt(txt_path, cidade, zip_filename=None, arquivo_origem=None,
                          processed_file_id=None,
                          process_current_month=True, admin_id=None,
                          progress_callback=None, cancel_check=None,
                          periodos=None, throttle_profile=None,
                          respeitar_teto=True,
                          atleta_esperado=None,
                          ids_ja_tentados=None, on_link_concluido=None,
                          fora_do_grupo=False):
    """
    Processa TXT exportado do WhatsApp:
    - Extrai links Strava e telefones com rastreamento de contexto
    - Scrapa dados de cada atividade
    - Filtra pela data real da atividade Strava e modalidade valida
    - periodos aceita pares (ano, mes); None mantem o comportamento legado
    - Salva no banco e roda anti-cheat
    - Reporta progresso via progress_callback(stage, message, pct) se fornecido
    - throttle_profile controla a taxa de requisicoes (ver build_strava_throttle)
    - ids_ja_tentados / on_link_concluido: memoria de curto prazo DENTRO de um
      mesmo job, para que uma retomada nao refaca o que ja foi decidido.
    - fora_do_grupo: marca as atividades salvas com a tag Activity.fora_do_grupo
      (nao veio de link postado no grupo, ex.: importacao por perfil Strava).
      NAO tira do ranking -- serve so para permitir depois um total separado
      de "quanto veio de fato de link postado no grupo".

    POR QUE ISSO EXISTE (medido em 20/08/2026)
    A importacao do perfil <id-1> foi retomada 41 vezes num dia. Cada
    retomada voltava ao primeiro link, entao o MESMO link chegou a ser buscado
    29 vezes no Strava — e nada era salvo, porque a fila sempre travava na
    mesma atividade. Link que falha nunca entra no banco, e a triagem so pula
    o que ESTA no banco: sem esta memoria, falha vira repeticao eterna.
    Ela vale so para o job corrente (o arquivo do job expira em 24 h): repetir
    a importacao depois continua tentando de novo, como sempre. O que acaba e
    a repeticao dentro da mesma rodada, que e desperdicio de requisicao do
    Strava (regra 4-C).
    """
    origem = zip_filename or arquivo_origem or 'desconhecido'
    ids_ja_tentados = {str(i) for i in (ids_ja_tentados or []) if i}

    def _concluido(act_id, desfecho):
        """Marca o link como resolvido NESTE job (ver docstring)."""
        if not on_link_concluido or not act_id:
            return
        try:
            on_link_concluido(str(act_id), desfecho)
        except Exception as exc:
            logger.debug('nao consegui marcar %s como concluido: %s', act_id, exc)

    def _cb(stage, message, pct):
        if progress_callback:
            try:
                progress_callback(stage, message, pct)
            except Exception:
                pass

    activities_processed = 0
    activities_skipped   = 0
    activities_failed    = 0
    activities_ignored   = 0
    # POR QUE TRES CONTADORES E NAO UM: ate 20/08/2026 a tela dizia "N fora do
    # periodo ou de modalidade nao pontuada" para QUALQUER atividade ignorada,
    # e o dono viu "180 fora" numa importacao do proprio perfil. Medido no log
    # daquele dia: as 494 ignoradas do perfil <id-2> eram TODAS de outras
    # pessoas (treino em grupo, regra 4-D); nenhuma por periodo (ultima em
    # 06/08) e nenhuma por modalidade (ultima em 06/06). A frase escolhia
    # justamente os dois motivos que nao aconteceram, e frase que mente sobre
    # o motivo faz o dono duvidar de km que estao certos.
    ignoradas_outro_atleta  = 0
    ignoradas_outro_periodo = 0
    ignoradas_modalidade    = 0
    links_scrapeados     = 0
    ja_tentados_neste_job = 0
    pending_details      = []
    link_pairs           = []

    def _add_pending(url, reason, activity_id='', stage='scraping', retryable=None,
                     autor=None, quando=None):
        if retryable is None:
            retryable = stage not in _PENDING_NONRETRYABLE_STAGES
        pending_details.append({
            'url': url,
            'activity_id': str(activity_id or ''),
            'reason': reason,
            'stage': stage,
            'retryable': bool(retryable),
            # Quem postou e quando: sem isso o admin nao tem como pedir o link
            # certo a pessoa (15/09/2026).
            'autor': autor or '',
            'quando': quando.strftime('%d/%m/%Y %H:%M') if hasattr(quando, 'strftime') else '',
        })

    try:
        _cb('reading', 'Fase 1/3: Lendo mensagens e identificando links do Strava...', 40)

        with open(txt_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        content = ''.join(lines)

        link_pairs  = extract_links_with_phones(content)
        total_links = len(link_pairs)
        logger.info(f"TXT lido: {total_links} links Strava encontrados no arquivo")
        _cb('reading', f'Fase 1/3: {total_links} links do Strava encontrados', 45)

        # Atividades de outro mes achadas nesta rodada — viram um e-mail so
        # no fim, nunca um por link.
        fora_do_periodo = []

        current_month = datetime.now().month
        current_year  = datetime.now().year
        if periodos is None:
            if throttle_profile == 'todos_periodos':
                # Selecao explicita de "todos os periodos" no upload: periodos=None
                # aqui significa "sem restricao", nao o fallback legado de mes atual.
                periodos_alvo = None
                usar_prefiltro_mensagem = False
            else:
                periodos_alvo = (
                    {(current_year, current_month)} if process_current_month else None
                )
                # O pre-filtro pela data da mensagem existe apenas no modo legado.
                # Para periodo explicito, a mensagem pode ter sido enviada depois da
                # virada do mes e a data correta esta na atividade do Strava.
                usar_prefiltro_mensagem = process_current_month
        else:
            periodos_alvo = {(int(ano), int(mes)) for ano, mes in periodos}
            usar_prefiltro_mensagem = False

        # Pre-scan rapido para contar candidatos (sem I/O de rede)
        if usar_prefiltro_mensagem:
            candidates = [
                (p, u, d) for p, u, d in link_pairs
                if not (d and (d.month != current_month or d.year != current_year))
            ]
            skipped_prefilter = total_links - len(candidates)
            last_success = (
                ProcessedFile.query
                .filter(
                    ProcessedFile.cidade == cidade,
                    ProcessedFile.status.in_(('sucesso', 'vazio')),
                )
                .order_by(ProcessedFile.processado_em.desc())
                .first()
            )
            if last_success and last_success.processado_em:
                cutoff_date = last_success.processado_em.date()
                before_recent_cut = len(candidates)
                candidates = [
                    (p, u, d) for p, u, d in candidates
                    if not (d and d < cutoff_date)
                ]
                skipped_old_uploads = before_recent_cut - len(candidates)
            else:
                skipped_old_uploads = 0
            logger.info(
                f"Pre-filtro de data: {skipped_prefilter} links de meses anteriores ignorados, "
                f"{skipped_old_uploads} links anteriores ao ultimo upload ignorados, "
                f"{len(candidates)} candidatos para processar"
            )
        else:
            if periodos_alvo is not None:
                # Periodo explicito: pre-filtro pela data da mensagem do WhatsApp.
                # A mensagem nunca antecede a atividade, entao links fora da
                # janela nao podem conter atividades do periodo selecionado.
                janela_ini, janela_fim = _janela_prefiltro_mensagem(periodos_alvo)
                candidates = [
                    (p, u, d) for p, u, d in link_pairs
                    if d is None or (janela_ini <= d <= janela_fim)
                ]
                skipped_prefilter = total_links - len(candidates)
                logger.info(
                    f"Pre-filtro por periodo explicito: {skipped_prefilter} links fora da janela "
                    f"{janela_ini}..{janela_fim} ignorados, {len(candidates)} candidatos"
                )
            else:
                candidates = list(link_pairs)
                skipped_prefilter = 0
            skipped_old_uploads = 0

        _cb('filtering',
            f'Fase 2/3: Preparando {len(candidates)} atividades para verificar...',
            52)

        existing_activity_ids = {
            str(row[0])
            for row in db.session.query(Activity.id_atividade)
            .filter_by(cidade=cidade)
            .all()
            if row[0]
        }
        raw_candidatos = len(candidates)
        cancelled = False
        rate_limited = False
        consecutive_failures = 0

        # Controle de taxa: usado ja na resolucao dos shortlinks (Passo 1).
        throttle = build_strava_throttle(
            respeitar_teto=respeitar_teto,
            mode=throttle_profile,
            cidade=cidade,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        # Deadline global do job (resolucao + scraping), adaptado ao ritmo.
        # O teto vem do perfil: "todos_periodos" pode levar dias no ritmo
        # anti-bloqueio; os perfis mensais mantem o teto curto de sempre.
        perfil_job = STRAVA_THROTTLE_PROFILES.get(
            throttle_profile or '', _STRAVA_THROTTLE_DEFAULT)
        deadline_cap = float(perfil_job.get('deadline_max') or _JOB_DEADLINE_MAX)
        estimativa = raw_candidatos * throttle.interval * 1.6
        if throttle.batch_size:
            estimativa += (raw_candidatos // throttle.batch_size) * throttle.batch_pause
        orcamento = min(deadline_cap, max(_JOB_DEADLINE_SECONDS, estimativa + 600))
        deadline = time.monotonic() + orcamento

        def _pause_por_bloqueio():
            """Pausa de bloqueio cancelavel; retorna True se o usuario cancelar."""
            try:
                throttle.block_pause()
            except ScrapeCancelled:
                return True
            return False

        # ===== PASSOS 1-3 EM CICLOS: resolver lote -> triagem -> scrapear =====
        # Cada ciclo resolve shortlinks (cache primeiro, rede depois) ate juntar
        # "cycle_size" atividades NOVAS -- deduplicadas contra o arquivo e o
        # banco --, scrapeia e salva esse lote (commit por atividade) e so entao
        # volta a resolver o proximo. O scraping comeca cedo e uma interrupcao
        # perde no maximo o andamento do ciclo atual: lotes anteriores ja estao
        # no banco e o cache de shortlinks faz a proxima rodada retomar rapido.
        seen_ids = set()
        skipped_existing_prefilter = 0
        cached_shortlinks = 0
        novos_acumulados = 0
        shortlinks_resolvidos_rede = 0
        cycle_size = max(1, int(perfil_job.get('cycle_size') or 500))
        pos = 0
        ciclo = 0
        deadline_hit = False

        while pos < raw_candidatos and not (cancelled or rate_limited or deadline_hit):
            ciclo += 1
            resolved_candidates = []   # (phone, direct_url, act_id, wa_date)

            # ----- Resolucao + deduplicacao do lote deste ciclo -----
            for res_pos in range(pos, raw_candidatos):
                phone, url, wa_date = candidates[res_pos]
                pos = res_pos + 1
                if cancel_check and cancel_check():
                    cancelled = True
                    break
                if time.monotonic() > deadline:
                    logger.warning('Deadline atingido na resolucao; restante fica pendente')
                    deadline_hit = True
                    pos = res_pos
                    break

                classe = classify_strava_link(url)
                if classe == 'invalid':
                    activities_failed += 1
                    _add_pending(url, 'Link nao e de atividade do Strava.', stage='validacao')
                    continue
                if classe == 'nao_atividade':
                    activities_failed += 1
                    _add_pending(url, _MOTIVO_DA_ETAPA['nao_atividade'][0],
                                 stage='nao_atividade', autor=phone, quando=wa_date)
                    continue

                direct_url = url
                act_id = None
                if classe == 'short':
                    cached = _get_cached_shortlink(url)
                    if cached and cached.get('resolved_url'):
                        cached_shortlinks += 1
                        direct_url = cached['resolved_url']
                        act_id = str(cached.get('activity_id') or '') or _activity_id_from_url(direct_url)
                    else:
                        pct0 = 40 + int((res_pos / max(raw_candidatos, 1)) * 10)
                        msg_shortlink = (
                            f'Ciclo {ciclo} - Fase 1/3: Resolvendo links encurtados... '
                            f'({res_pos + 1}/{raw_candidatos})')
                        throttle.note_progresso(msg_shortlink, pct0)
                        _cb('reading', msg_shortlink, pct0)
                        try:
                            direct_url = resolve_strava_shortlink(url, throttle=throttle)
                        except ScrapeCancelled:
                            cancelled = True
                            break
                        except ScrapePermanent as perm:
                            # Resposta definitiva do encurtador: nao conta como
                            # falha de bloqueio (o Strava respondeu de verdade).
                            activities_failed += 1
                            _add_pending(url, perm.motivo, stage=perm.stage,
                                         retryable=False, autor=phone,
                                         quando=wa_date)
                            consecutive_failures = 0
                            continue
                        except ScrapeTimeout:
                            activities_failed += 1
                            _add_pending(url, 'Tempo esgotado ao resolver o link (retentavel).',
                                         stage='timeout')
                            consecutive_failures += 1
                            if consecutive_failures >= _STRAVA_BLOCK_FAILURE_STREAK:
                                if _pause_por_bloqueio():
                                    cancelled = True
                                    break
                                consecutive_failures = 0
                            continue
                        except StravaRateLimited:
                            logger.warning('Backoff esgotado na resolucao; restante vira rate_limit')
                            rate_limited = True
                            pos = res_pos
                            break
                        if not direct_url:
                            activities_failed += 1
                            _add_pending(url, 'Shortlink nao resolvido.', stage='shortlink')
                            consecutive_failures += 1
                            if consecutive_failures >= _STRAVA_BLOCK_FAILURE_STREAK:
                                if _pause_por_bloqueio():
                                    cancelled = True
                                    break
                                consecutive_failures = 0
                            continue
                        _save_cached_shortlink(url, direct_url)
                        shortlinks_resolvidos_rede += 1
                        act_id = _activity_id_from_url(direct_url)
                        consecutive_failures = 0
                else:  # direct
                    act_id = _activity_id_from_url(direct_url)

                if not act_id:
                    activities_failed += 1
                    _add_pending(direct_url, 'Nao foi possivel extrair o ID da atividade.', stage='validacao')
                    continue

                # Deduplicacao dentro do proprio arquivo (mesmo id em varios links).
                if act_id in seen_ids:
                    activities_skipped += 1
                    continue
                seen_ids.add(act_id)
                # Ja resolvido nesta MESMA importacao (salvo, ignorado ou
                # falhado): nao se pergunta duas vezes ao Strava a mesma coisa
                # dentro de uma rodada. E o que impede o laco de 20/08.
                if act_id in ids_ja_tentados:
                    ja_tentados_neste_job += 1
                    continue
                # Deduplicacao contra o banco (consulta unica ja carregada em memoria).
                if act_id in existing_activity_ids:
                    activities_skipped += 1
                    skipped_existing_prefilter += 1
                    continue

                resolved_candidates.append((phone, direct_url, act_id, wa_date))
                if len(resolved_candidates) >= cycle_size:
                    break

            # ----- Triagem do ciclo (numeros honestos, posicao global) -----
            novos_ciclo = len(resolved_candidates)
            novos_acumulados += novos_ciclo
            logger.info(
                f'Ciclo {ciclo}: triagem {pos}/{raw_candidatos} links -> '
                f'{novos_ciclo} novos neste lote (acumulado: {novos_acumulados} novos, '
                f'{skipped_existing_prefilter} ja no banco, '
                f'{cached_shortlinks} shortlinks via cache)'
            )
            pct_ciclo = 52 + int((pos / max(raw_candidatos, 1)) * 43)
            _cb('filtering',
                f'Ciclo {ciclo}: {pos}/{raw_candidatos} links triados -> '
                f'{novos_ciclo} novos para buscar neste lote',
                pct_ciclo)

            # Resolucao interrompida: o lote resolvido (ainda nao scrapeado)
            # vira pendencia retentavel; o restante do arquivo e tratado apos o
            # laco de ciclos.
            if rate_limited or deadline_hit:
                motivo = ('Strava limitou o acesso (rate limit). Sera retentado depois.'
                          if rate_limited else 'Nao processado dentro do tempo limite.')
                stage = 'rate_limit' if rate_limited else 'timeout'
                for _rp, _ru, _rid, _rd in resolved_candidates:
                    _add_pending(_ru, motivo, activity_id=_rid, stage=stage)
                break
            if cancelled:
                break

            # ----- Scraping + salvamento do lote deste ciclo -----
            total_novos = novos_ciclo
            scrape_index = 0
            for scr_pos, (phone, direct_url, act_id, wa_date) in enumerate(resolved_candidates):
                # Verificar cancelamento antes de cada atividade
                if cancel_check and cancel_check():
                    logger.info('Processamento cancelado pelo usuario')
                    cancelled = True
                    break

                # Rede de seguranca: nunca deixar um job rodando indefinidamente.
                # As atividades restantes entram como pendencia (timeout).
                if time.monotonic() > deadline:
                    logger.warning(
                        f"Deadline de {int(orcamento)}s atingido no scraping; "
                        f"{total_novos - scrape_index} atividades do lote ficam pendentes"
                    )
                    for _rp, _ru, _rid, _rd in resolved_candidates[scr_pos:]:
                        _add_pending(_ru, 'Nao processado dentro do tempo limite.', stage='timeout')
                    deadline_hit = True
                    break

                scrape_index += 1
                msg_scraping = (
                    f'Ciclo {ciclo} - Fase 3/3: Buscando dados no Strava... '
                    f'({scrape_index}/{total_novos})')
                # O throttle guarda a mensagem para poder REPETI-LA enquanto
                # espera uma requisicao demorada, em vez de apagar a posicao
                # ("15 de 23") com um texto generico.
                throttle.note_progresso(msg_scraping, pct_ciclo)
                _cb('scraping', msg_scraping, pct_ciclo)
                url = direct_url  # ja resolvido e deduplicado neste ciclo

                # Scraping
                logger.info(f"Scraping: {url}")
                links_scrapeados += 1
                # MARCAR A TENTATIVA ANTES DE FAZE-LA, e nao depois.
                # Se a requisicao CONGELAR (P-80: ja aconteceu passar dos 75s
                # do _hard_get sem desistir), o desfecho nunca seria gravado —
                # e a retomada seguinte cairia exatamente no mesmo link, para
                # sempre. Foi esse o laco de 20/08. Marcando antes, um link que
                # trava e tentado UMA vez por rodada: o job anda, e a atividade
                # fica registrada como pendencia retentavel para uma proxima
                # importacao (nao se perde, so nao se repete agora).
                _concluido(act_id, 'tentando')
                _t_link = time.monotonic()
                try:
                    act_data = parse_strava_activity(url, cancel_check=cancel_check, throttle=throttle)
                except ScrapeCancelled:
                    logger.info('Processamento cancelado pelo usuario durante scraping')
                    cancelled = True
                    break
                except ScrapePermanent as perm:
                    # Resposta definitiva do Strava (apagada/privada): pendencia
                    # permanente e nao conta para a deteccao de bloqueio.
                    activities_failed += 1
                    _add_pending(url, perm.motivo, activity_id=act_id,
                                 stage=perm.stage, retryable=False,
                                 autor=phone, quando=wa_date)
                    _concluido(act_id, 'falha_permanente')
                    consecutive_failures = 0
                    continue
                except StravaRateLimited:
                    logger.warning('Backoff esgotado no scraping; restante vira rate_limit')
                    for _rp, _ru, _rid, _rd in resolved_candidates[scr_pos:]:
                        _add_pending(_ru, 'Strava limitou o acesso (rate limit). Sera retentado depois.',
                                     stage='rate_limit')
                    rate_limited = True
                    break
                except ScrapeTimeout:
                    logger.warning(f"Timeout ao buscar {url}")
                    activities_failed += 1
                    _add_pending(url, 'Tempo esgotado ao buscar a atividade (retentavel).',
                                 activity_id=act_id, stage='timeout',
                                 autor=phone, quando=wa_date)
                    _concluido(act_id, 'timeout')
                    consecutive_failures += 1
                    if consecutive_failures >= _STRAVA_BLOCK_FAILURE_STREAK:
                        if _pause_por_bloqueio():
                            cancelled = True
                            break
                        consecutive_failures = 0
                    continue
                except Exception as exc:
                    # ERRO NAO CATEGORIZADO (20/08/2026). Ate hoje, qualquer
                    # excecao fora das 4 acima subia e derrubava o JOB
                    # INTEIRO -- todos os meses/links restantes junto, mesmo
                    # sendo um problema de UM link so. Vira pendencia
                    # retentavel como as outras, com o MAXIMO de detalhe
                    # possivel (tipo da excecao, mensagem, traceback
                    # completo) pra dar pra investigar depois -- e nunca mais
                    # "job morreu, ninguem sabe por que".
                    logger.error('ERRO NAO CATEGORIZADO ao raspar %s: %s: %s',
                                 url, type(exc).__name__, exc, exc_info=True)
                    activities_failed += 1
                    _add_pending(url, f'Erro inesperado ({type(exc).__name__}): {exc}',
                                 activity_id=act_id, stage='erro_inesperado',
                                 autor=phone, quando=wa_date)
                    _concluido(act_id, 'erro_inesperado')
                    consecutive_failures += 1
                    if consecutive_failures >= _STRAVA_BLOCK_FAILURE_STREAK:
                        if _pause_por_bloqueio():
                            cancelled = True
                            break
                        consecutive_failures = 0
                    continue
                _demora_link = time.monotonic() - _t_link
                if _demora_link > 60:
                    logger.warning(
                        'LINK LENTO: %ds em %s. Se nao houver "abandonada por '
                        'prazo" nem "LEITURA LENTA" acima, o tempo foi em outro '
                        'lugar -- anotar em ERROS.md (P-80).',
                        int(_demora_link), url)
                if not act_data:
                    logger.warning(f"Falha ao obter dados de {url}")
                    activities_failed += 1
                    _add_pending(
                        url,
                        'Nao foi possivel obter dados publicos da atividade.',
                        activity_id=act_id, autor=phone, quando=wa_date,
                    )
                    _concluido(act_id, 'sem_dados')
                    consecutive_failures += 1
                    if consecutive_failures >= _STRAVA_BLOCK_FAILURE_STREAK:
                        try:
                            throttle.block_pause()
                        except ScrapeCancelled:
                            cancelled = True
                            break
                        consecutive_failures = 0
                    continue

                # Scraping bem-sucedido: zera a sequencia de falhas consecutivas.
                consecutive_failures = 0

                dt         = act_data.get('data_hora')
                modalidade = act_data.get('modalidade', '')

                # SO O ATLETA PEDIDO — E ANTES DE QUALQUER OUTRO FILTRO.
                # Vale para importacao por perfil e para o lancamento pela
                # conferencia: a listagem do Strava devolve tambem treino em
                # grupo de outras pessoas e entidades que nem sao atividade.
                #
                # A ORDEM AQUI E A CORRECAO. Em 06/08/2026 esta guarda vinha
                # DEPOIS do filtro de periodo, e o admin recebeu tres e-mails de
                # "atividade de outro mes" por causa de gente de 2019 que nunca
                # pisou na cidade — tres atletas estrangeiros (<id-8>,
                # outros dois) —, tudo durante a importacao do
                # perfil de uma atleta. Nenhum deles entrou no ranking (a guarda
                # fazia o seu trabalho), mas o aviso saiu, e aviso falso ensina
                # o admin a ignorar aviso. A regra 4-I fala de LINK QUE ALGUEM
                # POSTOU; atividade de terceiro colhida do feed nao e link
                # postado, e descarta-la nao e silencio — e limpeza (regra 4-D).
                if atleta_esperado and str(act_data.get('id_usuario_strava') or '') \
                        != str(atleta_esperado):
                    logger.info(
                        'Atividade %s ignorada: e do atleta %s, nao do %s.',
                        act_id, act_data.get('id_usuario_strava'), atleta_esperado)
                    activities_ignored += 1
                    ignoradas_outro_atleta += 1
                    _concluido(act_id, 'outro_atleta')
                    continue

                # Filtro de mes/ano pelo dado real da atividade Strava. A data da
                # mensagem do WhatsApp nunca define a qual ranking a atividade vai.
                if periodos_alvo is not None:
                    if not dt or (dt.year, dt.month) not in periodos_alvo:
                        periodo = dt.strftime('%m/%Y') if dt else 'desconhecido'
                        logger.info(
                            f"Atividade {act_id} ignorada: data da atividade "
                            f"({periodo}) fora do periodo selecionado"
                        )
                        # NAO E SO IGNORAR: e registrar que ignorou.
                        # Em 05/08/2026 uma caminhada de 104,99 km feita em
                        # 31/07 foi postada no grupo ao meio-dia. O job era de
                        # agosto, o filtro descartou (certo: vale a data da
                        # ATIVIDADE, nao a do post) e sobrou uma linha de log.
                        # Julho fechou sete horas depois sem esses km e ninguem
                        # soube. O filtro estava certo; o silencio e que nao.
                        if dt and modalidade in ('ciclismo', 'corrida_caminhada') \
                                and (act_data.get('distancia') or 0) > 0:
                            try:
                                from app import foradoperiodo
                                if foradoperiodo.registrar(
                                        cidade, act_id,
                                        _rotulo_periodos(periodos_alvo),
                                        act_data, url):
                                    fora_do_periodo.append(act_id)
                            except Exception as exc:
                                logger.warning(
                                    'Nao consegui registrar %s como fora do '
                                    'periodo: %s', act_id, exc)
                        activities_ignored += 1
                        ignoradas_outro_periodo += 1
                        _concluido(act_id, 'outro_periodo')
                        continue

                # Filtro de modalidade e distancia
                if (modalidade not in ('ciclismo', 'corrida_caminhada') or
                        act_data.get('distancia', 0) <= 0):
                    logger.info(
                        f"Atividade {act_id} ignorada: "
                        f"modalidade={modalidade}, distancia={act_data.get('distancia', 0)}"
                    )
                    activities_ignored += 1
                    ignoradas_modalidade += 1
                    _concluido(act_id, 'modalidade')
                    continue

                _cb('saving',
                    f'Fase 3/3: Salvando atividade de {act_data.get("nome_usuario", "?")}...', 95)

                # Criar registro
                new_act = Activity(
                    id_atividade      = act_data['id_atividade'],
                    id_usuario_strava = act_data.get('id_usuario_strava', '0'),
                    nome_usuario      = act_data.get('nome_usuario', 'Desconhecido'),
                    modalidade        = modalidade,
                    distancia         = act_data['distancia'],
                    data_hora         = dt,
                    cidade            = cidade,
                    id_admin_upload   = admin_id,
                    arquivo_origem    = origem,
                    link_original     = url,
                    gps_fingerprint   = act_data.get('gps_fingerprint'),
                    telefone_whatsapp = phone,
                    tempo_minutos     = act_data.get('tempo_minutos'),
                    elevacao_total    = act_data.get('elevacao_total'),
                    velocidade_media  = act_data.get('velocidade_media'),
                    pace_min_km       = act_data.get('pace_min_km'),
                    distancia_raw     = act_data.get('distancia_raw'),
                    processed_file_id = processed_file_id,
                    fora_do_grupo     = fora_do_grupo,
                    esporte_strava    = act_data.get('esporte_strava'),
                )

                db.session.add(new_act)
                db.session.commit()

                # Anti-cheat
                _cb('anticheat', 'Verificando autenticidade da atividade (anti-cheat)...', 97)
                try:
                    from app.blueprints.anticheat import process_activity_anticheat
                    flags = process_activity_anticheat(new_act)
                    db.session.commit()
                    if flags:
                        logger.info(
                            f"Atividade {act_id}: {len(flags)} flag(s) de anti-cheat detectada(s)"
                        )
                except ImportError:
                    pass
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"Erro anti-cheat para {act_id}: {e}", exc_info=True)

                activities_processed += 1
                existing_activity_ids.add(act_id)
                _concluido(act_id, 'salvo')
                logger.info(
                    f"[OK] Salvo: {act_id} | {act_data['distancia']}km | "
                    f"{modalidade} | {act_data.get('nome_usuario')} | "
                    f"vel={act_data.get('velocidade_media')}km/h | "
                    f"pace={act_data.get('pace_min_km')}min/km"
                )
                # O pacing entre requisicoes e feito pelo StravaThrottle (rate
                # limiter), nao mais por um sleep fixo aqui.

            if activities_processed:
                logger.info(
                    f'Ciclo {ciclo} concluido: {activities_processed} atividades '
                    f'salvas ate agora ({pos}/{raw_candidatos} links triados)'
                )

        # Interrupcao antes do fim do arquivo: o restante ainda nao triado
        # vira pendencia retentavel (uma nova rodada continua de onde parou).
        if pos < raw_candidatos and (rate_limited or deadline_hit) and not cancelled:
            motivo = ('Strava limitou o acesso (rate limit). Sera retentado depois.'
                      if rate_limited else 'Nao processado dentro do tempo limite.')
            stage = 'rate_limit' if rate_limited else 'timeout'
            for _rp, _ru, _rd in candidates[pos:]:
                _add_pending(_ru, motivo, stage=stage)

        if ciclo == 0:
            logger.info(
                f'Triagem: {raw_candidatos} links do periodo -> 0 unicos -> 0 novos '
                f'(0 ja no banco, 0 shortlinks via cache)'
            )

        if rate_limited:
            logger.warning(
                'Job encerrado por rate limit do Strava com resultados parciais '
                f'({activities_processed} salvas, {novos_acumulados} novas identificadas, '
                f'{pos}/{raw_candidatos} links triados)'
            )

        # Reconciliacao: verificar se ha links do mes nao processados
        total_candidates = len(candidates) if 'candidates' in dir() else 0
        accounted = (
            activities_processed + activities_skipped +
            activities_failed + activities_ignored
        )
        pendentes = max(0, total_candidates - accounted)
        if pendentes > 0 and not cancelled:
            logger.warning(
                f'RECONCILIACAO: {pendentes} atividades do mes sem classificacao '
                f'(candidatos={total_candidates}, salvas={activities_processed}, '
                f'duplicatas={activities_skipped}, falhas={activities_failed}, '
                f'ignoradas={activities_ignored}) -- possivel interrupcao'
            )
        if activities_failed or activities_ignored:
            logger.info(
                f'Links sem atividade salva: {activities_failed} falharam no scraping, '
                f'{activities_ignored} foram ignorados por filtro '
                f'(outro atleta={ignoradas_outro_atleta}, '
                f'outro periodo={ignoradas_outro_periodo}, '
                f'modalidade={ignoradas_modalidade})'
            )

        logger.info(
            f'Processamento concluido: {activities_processed} atividades salvas, '
            f'{activities_skipped} duplicatas ignoradas, '
            f'{total_links} links totais no arquivo'
        )

        # UM e-mail no fim, nunca um por link. O admin precisa saber que chegou
        # km de outro mes para decidir o que fazer — pode estar dentro do prazo
        # de lancar, ou o mes ja ter fechado. Falhar aqui nao pode derrubar o
        # processamento: as atividades ja ficaram registradas, e o proximo job
        # tenta avisar de novo.
        if fora_do_periodo:
            try:
                from app import foradoperiodo
                foradoperiodo.avisar_pendentes(cidade)
            except Exception as exc:
                logger.warning('Nao consegui avisar sobre atividade de outro '
                               'mes: %s', exc)

        # LINK POSTADO QUE NAO VIRA KM NAO PODE MORRER EM SILENCIO
        # (15/09/2026). Quem postou acha que lancou a atividade e so descobre
        # que ficou de fora quando o ranking sai sem os km dele. Vale para o
        # link do Strava que nao e atividade (perfil, clube...) e para a
        # atividade cujos km nao deu para coletar em definitivo: um e-mail no
        # fim, com o link, o motivo, quem postou e quando. Pendencia
        # RETENTAVEL nao entra: a proxima rodada ainda pode resolver sozinha.
        sem_km = [p for p in pending_details if not p.get('retryable')]
        if sem_km:
            try:
                from app import linksemkm
                linksemkm.avisar(cidade, sem_km)
            except Exception as exc:
                logger.warning('Nao consegui avisar sobre link que nao virou '
                               'km: %s', exc)

    except Exception as e:
        db.session.rollback()
        logger.error(f'Erro em process_whatsapp_txt: {e}', exc_info=True)
        raise

    _candidates_count = len(candidates) if 'candidates' in dir() else 0
    return {
        'total_links_found':       len(link_pairs),
        'activities_processed':    activities_processed,
        'activities_skipped':      activities_skipped,
        'activities_failed':       activities_failed,
        'activities_ignored':      activities_ignored,
        'ignoradas_outro_atleta':  ignoradas_outro_atleta,
        'ignoradas_outro_periodo': ignoradas_outro_periodo,
        'ignoradas_modalidade':    ignoradas_modalidade,
        'ja_tentados_neste_job':   ja_tentados_neste_job,
        'links_scrapeados':        links_scrapeados,
        'pending_details':         pending_details,
        'pendencias_retentaveis':  sum(1 for p in pending_details if p.get('retryable')),
        'pendencias_permanentes':  sum(1 for p in pending_details if not p.get('retryable')),
        'links_encontrados_mes':   _candidates_count,
        'pendentes':               max(
            0,
            activities_failed + (
                _candidates_count - activities_processed - activities_skipped -
                activities_failed - activities_ignored
            )
        ),
        # RECONCILIACAO VIRA SINAL, NAO SO LOG (20-21/08/2026). O aviso
        # "RECONCILIACAO: N atividades sem classificacao" so ficava no log --
        # ninguem agia sobre ele. Agora o numero sai daqui tambem, para quem
        # chama (upload.py) decidir NAO marcar o mes como concluido quando
        # sobrar pendente, do mesmo jeito que ja faz com deadline_hit/
        # rate_limited -- assim uma proxima retomada tem chance real de
        # fechar a conta, em vez do gap so ficar registrado e esquecido.
        'reconciliacao_pendente':  pendentes if 'pendentes' in dir() else 0,
        'cancelled':               cancelled if 'cancelled' in dir() else False,
        # Sinais para a continuacao automatica (upload.py): interrupcao por
        # teto/bloqueio + se a rodada rendeu algo (salvou ou resolveu na rede).
        'deadline_hit':            deadline_hit if 'deadline_hit' in dir() else False,
        'rate_limited':            rate_limited if 'rate_limited' in dir() else False,
        'shortlinks_resolvidos':   (shortlinks_resolvidos_rede
                                    if 'shortlinks_resolvidos_rede' in dir() else 0),
    }
