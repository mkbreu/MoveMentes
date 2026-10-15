# -*- coding: utf-8 -*-
"""O que o Strava esta dizendo quando a leitura nao sai.

POR QUE ESTE ARQUIVO EXISTE (29/08/2026, regra do dono).
Ate aqui o codigo tinha uma pergunta so -- "isto e' bloqueio?" -- e um remedio
so: esperar e repetir. Repetir em cima de bloqueio e' o jeito mais rapido de
transformar "o Strava pediu calma" em "o Strava fechou a porta". E esperar em
cima de erro que nao e' bloqueio e' jogar fora horas de trabalho por nada.

Entao a resposta se LE antes de qualquer nova tentativa, e cada leitura tem um
remedio proprio. O discriminante mais importante nao e' o codigo HTTP, e' o
cabecalho:

    429 COM `Retry-After`  -> limite honesto. O servidor esta dizendo quanto
                              esperar; obedecer e' o certo, e nao ha risco.
    429 SEM `Retry-After`  -> deteccao de bot. Limitador de verdade quase
                              sempre manda o cabecalho; a AUSENCIA dele e' o
                              indicio de que quem respondeu foi o anti-bot.
                              Aqui repetir PIORA.

O resto (403 privado x 403 CloudFlare, 400 de mes que nao existe, 200 com
casca de login) so parece detalhe ate o dia em que o job passa seis horas
repetindo o que nunca ia dar certo.
"""
import re

# Marcas de pagina de bloqueio no corpo (403/503 e 200 com challenge).
MARCAS_BLOQUEIO = (
    'cloudflare', 'access denied', 'attention required',
    'too many requests', 'rate limit',
)
MARCAS_CHALLENGE = (
    'just a moment', 'cf-browser-verification', 'cf-challenge',
    '/cdn-cgi/challenge', 'cf_chl_', 'attention required', 'access denied',
)
# A casca de login: HTTP 200, mas o que veio foi a pagina de entrar.
MARCAS_LOGIN = (
    'strava.com/login', 'id="login-form"', 'name="user[email]"',
    '/register/free',
)

# Os diagnosticos. O nome e' o que aparece no log e na tela -- em portugues,
# porque quem le e' o dono e o admin, nao um dev (regra 12).
OK = 'ok'
LIMITE_HONESTO = 'limite_honesto'
BLOQUEIO = 'bloqueio'
PRIVADA = 'privada'
INEXISTENTE = 'inexistente'
MES_SEM_CONTA = 'mes_sem_conta'
SEM_SESSAO = 'sem_sessao'
INSTAVEL = 'instavel'
DESCONHECIDO = 'desconhecido'

# O que fazer com cada um. `parar` = nao adianta repetir agora.
_EXPLICACAO = {
    OK:             ('Resposta normal.', False),
    LIMITE_HONESTO: ('O Strava pediu uma pausa e disse por quanto tempo. '
                     'Esperar o que ele mandou resolve.', False),
    BLOQUEIO:       ('O Strava fechou o acesso — veio um 429 sem prazo, ou a '
                     'página do CloudFlare. Insistir agora piora.', True),
    PRIVADA:        ('A atividade não é pública (perfil fechado ou só para '
                     'seguidores). Repetir dá no mesmo.', False),
    INEXISTENTE:    ('A atividade não existe mais no Strava.', False),
    MES_SEM_CONTA:  ('A conta do atleta ainda não existia neste mês. Não é '
                     'erro: é o fim do histórico dele.', False),
    SEM_SESSAO:     ('O Strava devolveu a página de login: o cookie da sessão '
                     'não vale mais. Recolar o cookie resolve.', True),
    INSTAVEL:       ('O Strava respondeu com erro do lado dele. Tentar de novo '
                     'mais tarde costuma resolver.', False),
    DESCONHECIDO:   ('Não consegui entender a resposta do Strava. Por '
                     'precaução, trato como bloqueio.', True),
}


def explicar(diagnostico):
    """Frase em portugues + se e' caso de PARAR. `nao sei` para pelo sim."""
    return _EXPLICACAO.get(diagnostico, _EXPLICACAO[DESCONHECIDO])


def _corpo(resp, limite=8000):
    try:
        bruto = resp.content or b''
    except Exception:
        return ''
    if not bruto:
        return ''
    return bruto[:limite].decode('utf-8', 'ignore').lower()


def _segundos_do_retry_after(valor):
    """`Retry-After` vem em segundos ou como data HTTP. Só o numero interessa."""
    if valor is None:
        return None
    texto = str(valor).strip()
    if re.match(r'^\d+$', texto):
        try:
            return max(0, int(texto))
        except ValueError:
            return None
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        quando = parsedate_to_datetime(texto)
        if quando is None:
            return None
        if quando.tzinfo is None:
            quando = quando.replace(tzinfo=timezone.utc)
        return max(0, int((quando - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return None


def ler(resp, url=''):
    """Le a resposta e devolve (diagnostico, segundos_de_espera_ou_None).

    `segundos` so vem preenchido quando o proprio servidor disse quanto.
    """
    status = getattr(resp, 'status_code', 0) or 0
    cabecalhos = getattr(resp, 'headers', None) or {}

    if status == 429:
        espera = _segundos_do_retry_after(
            cabecalhos.get('Retry-After') or cabecalhos.get('retry-after'))
        # ESTE E' O DISCRIMINANTE. Ver o cabecalho deste modulo.
        if espera is not None:
            # Teto de sanidade: relogio torto ou data absurda no cabecalho nao
            # pode congelar a fila por anos. Acima de 24 h isso e' porta
            # fechada, nao pedido de calma -- trata como bloqueio e escalona.
            if espera > 86400:
                return BLOQUEIO, None
            return LIMITE_HONESTO, espera
        return BLOQUEIO, None

    if status in (403, 503):
        corpo = _corpo(resp, 4000)
        if any(m in corpo for m in MARCAS_BLOQUEIO):
            return BLOQUEIO, None
        if status == 503:
            return INSTAVEL, None
        # 403 limpo em atividade e' perfil fechado, nao bloqueio nosso.
        return PRIVADA, None

    if status in (404, 410):
        return INEXISTENTE, None

    if status == 400:
        # A LISTAGEM `/interval` RESPONDE 400 quando o mes pedido e' anterior a
        # criacao da conta. Isso nao e' falha: e' o fim do historico. Tratar
        # como erro fez o job continuar descendo mes a mes -- 57 requisicoes
        # desperdicadas por atleta novo, medido em 28/08/2026.
        return MES_SEM_CONTA, None

    if status >= 500:
        return INSTAVEL, None

    if status == 200:
        corpo = _corpo(resp)
        tamanho = len(corpo)
        # O challenge do CloudFlare tambem vem com 200, e e' pequeno.
        if 0 < tamanho < 30000 and any(m in corpo for m in MARCAS_CHALLENGE):
            return BLOQUEIO, None
        if any(m in corpo for m in MARCAS_LOGIN) and 'pageview.activity' not in corpo:
            return SEM_SESSAO, None
        return OK, None

    if status == 0:
        return DESCONHECIDO, None
    return OK, None


def e_bloqueio(diagnostico):
    """So `bloqueio` e `nao sei` acionam a trava (regra 21-G, por analogia)."""
    return diagnostico in (BLOQUEIO, DESCONHECIDO)


# ---------------------------------------------------------------------------
# A TRAVA, e por que ela escalona em vez de dormir 24 h de cara.
#
# Desenho do dono (29/08/2026): bloqueou -> espera algumas horas e volta
# DEVAGAR; bloqueou de novo -> ai sim para ate o contador diario do Strava
# virar, que e' meia-noite UTC (documentado por eles). Dormir 24 h no primeiro
# sinal custa um dia inteiro de importacao por um susto que podia ser passageiro;
# insistir depois do segundo e' que vira teimosia.
#
# O estado fica em disco porque quem precisa dele sao processos diferentes (o
# site, o jobrunner, a conferencia) e porque um reinicio nao pode apagar a
# memoria de que acabamos de ser barrados.
# ---------------------------------------------------------------------------
import json
import os
import time
from datetime import datetime, timedelta

_PASTA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'temp_extract', 'pacer')
_ARQUIVO = os.path.join(_PASTA, 'bloqueio.json')

# 1o bloqueio: 3 h. Curto o bastante para nao perder o dia, longo o bastante
# para a janela de 15 min do Strava virar varias vezes.
PRIMEIRA_PAUSA = 3 * 3600
# Depois de 24 h limpo, esquece que ja fomos barrados: senao um susto de
# terca-feira ainda estaria punindo a fila na sexta.
ESQUECE_APOS = 24 * 3600


def _ler_estado():
    try:
        with open(_ARQUIVO, 'r') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _gravar_estado(dados):
    try:
        if not os.path.isdir(_PASTA):
            os.makedirs(_PASTA)
        tmp = _ARQUIVO + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(dados, f)
        os.replace(tmp, _ARQUIVO)
    except Exception:
        # Falha de disco NUNCA pode travar a coleta: sem estado, o pior caso
        # e' voltar ao comportamento antigo, nao parar tudo.
        pass


def _proxima_meia_noite_utc(agora=None):
    agora = agora or datetime.utcnow()
    amanha = (agora + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return time.time() + max(60, (amanha - agora).total_seconds())


def registrar_bloqueio(motivo=BLOQUEIO, agora=None):
    """Anota que fomos barrados e devolve (ate_quando, tentativa, explicacao)."""
    agora = agora or time.time()
    estado = _ler_estado()
    ultimo = float(estado.get('ultimo') or 0)
    tentativa = int(estado.get('tentativa') or 0)
    if ultimo and (agora - ultimo) > ESQUECE_APOS:
        tentativa = 0
    tentativa += 1
    if tentativa <= 1:
        ate = agora + PRIMEIRA_PAUSA
        recado = ('O Strava barrou o acesso. Pausa de 3 horas e volto devagar; '
                  'nada se perde — a fila continua onde parou.')
    else:
        ate = _proxima_meia_noite_utc()
        recado = ('O Strava barrou de novo. Agora espero o contador diário '
                  'dele virar (meia-noite UTC) antes de tentar outra vez.')
    _gravar_estado({'ultimo': agora, 'tentativa': tentativa,
                    'ate': ate, 'motivo': motivo})
    return ate, tentativa, recado


def bloqueado_ate(agora=None):
    """Timestamp ate quando estamos travados, ou None se pode seguir."""
    agora = agora or time.time()
    estado = _ler_estado()
    ate = float(estado.get('ate') or 0)
    return ate if ate > agora else None


def liberar():
    """Leitura deu certo: some com a trava (mas guarda que ja fomos barrados)."""
    estado = _ler_estado()
    if not estado:
        return
    estado.pop('ate', None)
    _gravar_estado(estado)


def situacao(agora=None):
    """Para a tela: o que a trava sabe agora, em portugues."""
    agora = agora or time.time()
    estado = _ler_estado()
    ate = float(estado.get('ate') or 0)
    return {
        'travado': ate > agora,
        'ate': ate or None,
        'faltam_segundos': int(ate - agora) if ate > agora else 0,
        'tentativa': int(estado.get('tentativa') or 0),
        'motivo': estado.get('motivo') or '',
    }
