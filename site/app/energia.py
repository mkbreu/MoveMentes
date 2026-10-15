# -*- coding: utf-8 -*-
"""A que horas este servidor desliga e a que horas ele volta.

POR QUE ISTO EXISTE (26/08/2026)
O DS215j **nao e 24/7**: ele se desliga sozinho a noite e volta de manha. Isso
sempre foi verdade e nunca esteve no codigo — o `CLAUDE.md` chegava a dizer
"sempre ligado". A consequencia pratica aparecia como defeito: uma importacao de
atleta em andamento na hora do desligamento morria no meio, e no dia seguinte
voltava contando aquilo como mais uma interrupcao (ha um teto de 3 antes de o
sistema desistir do trabalho).

DE ONDE VEM CADA HORARIO — **do proprio Synology, nunca escrito aqui**:

  DESLIGAR  `/etc/crontab`, linha do `syno_sched_poweroff`. E' o que o painel
            do DSM grava quando o dono muda o agendamento de energia. Ele vai
            mudar (a tendencia e antecipar para entre 21h e 23h) e nada aqui
            precisa ser editado.

  LIGAR     `/proc/stat`, campo `btime` — o instante em que a maquina subiu.
            **Medido, nao configurado:** o horario de ligar mora na BIOS/no
            agendamento do DSM, fora do alcance de um arquivo de texto. Usar o
            boot de hoje como previsao do de amanha e' a resposta mais honesta
            disponivel, e se o dono mudar o horario o sistema aprende sozinho no
            dia seguinte.

CUSTO: os dois arquivos sao lidos no maximo de 30 em 30 minutos (cache). Numa
maquina que passa metade do tempo esperando disco, ler `/etc/crontab` a cada
consulta seria exatamente o tipo de desperdicio que o `CLAUDE.md` proibe.
"""
import logging
import re
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_CRONTAB = '/etc/crontab'
_PROC_STAT = '/proc/stat'
_CACHE_SEG = 30 * 60

# Quanto tempo antes da parada os trabalhos longos sao congelados. 15 minutos e'
# a mesma antecedencia que o vigia ja usa para o Wake-on-LAN: tempo de fechar o
# mes que esta sendo lido e gravar onde parou, sem correria.
MARGEM_PARADA_MIN = 15

_cache = {'quando': 0.0, 'desliga': None, 'liga': None}


def _ler_horario_de_desligar():
    """(hora, minuto) do `syno_sched_poweroff` no crontab, ou None."""
    try:
        with open(_CRONTAB, 'r', encoding='utf-8', errors='replace') as fh:
            for linha in fh:
                if 'poweroff' not in linha or linha.lstrip().startswith('#'):
                    continue
                campos = re.split(r'\s+', linha.strip())
                if len(campos) >= 2 and campos[0].isdigit() and campos[1].isdigit():
                    return int(campos[1]), int(campos[0])   # (hora, minuto)
    except OSError as exc:
        logger.debug('Nao consegui ler o horario de desligar: %s', exc)
    return None


def _ler_horario_de_ligar():
    """(hora, minuto) do ultimo boot desta maquina, ou None."""
    try:
        with open(_PROC_STAT, 'r') as fh:
            for linha in fh:
                if linha.startswith('btime '):
                    quando = datetime.fromtimestamp(float(linha.split()[1]))
                    return quando.hour, quando.minute
    except (OSError, IndexError, ValueError) as exc:
        logger.debug('Nao consegui ler a hora do ultimo boot: %s', exc)
    return None


def _horarios():
    agora = time.time()
    if agora - _cache['quando'] > _CACHE_SEG:
        _cache['desliga'] = _ler_horario_de_desligar()
        _cache['liga'] = _ler_horario_de_ligar()
        _cache['quando'] = agora
    return _cache['desliga'], _cache['liga']


def proxima_parada(agora=None):
    """Quando este servidor desliga da proxima vez. `None` = nao da para saber.

    NAO SABER NUNCA VIRA "PODE TRABALHAR A VONTADE" no chamador: quem pergunta
    trata `None` como "sem informacao" e segue o comportamento de antes, que e'
    o que sempre existiu. Este modulo so tem o direito de ADIAR trabalho quando
    tem certeza da hora.
    """
    agora = agora or datetime.now()
    desliga, _ = _horarios()
    if not desliga:
        return None
    hora, minuto = desliga
    alvo = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if alvo <= agora:
        alvo += timedelta(days=1)
    return alvo


def minutos_ate_parar(agora=None):
    """Quantos minutos faltam para o servidor desligar. `None` = nao sei."""
    agora = agora or datetime.now()
    alvo = proxima_parada(agora)
    if alvo is None:
        return None
    return int((alvo - agora).total_seconds() // 60)


def perto_de_parar(agora=None, margem=MARGEM_PARADA_MIN):
    """Esta na hora de congelar trabalho longo? `None` (nao sei) vira False."""
    faltam = minutos_ate_parar(agora)
    return faltam is not None and faltam <= margem


def instante_do_boot():
    """Quando esta maquina ligou, em segundos (o mesmo relogio do `time.time()`).

    É COM ISTO QUE SE SABE QUE A MAQUINA JA VOLTOU. Um trabalho congelado antes
    do desligamento guarda a hora em que parou; se o boot for MAIS NOVO que essa
    hora, a maquina desligou e voltou no meio — pode retomar. Comparar com "e'
    depois das 6h?" erraria em todo reinicio fora de hora.
    """
    try:
        with open(_PROC_STAT, 'r') as fh:
            for linha in fh:
                if linha.startswith('btime '):
                    return float(linha.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return None


def horario_de_voltar():
    """(hora, minuto) em que a maquina costuma voltar. `None` = nao sei."""
    _, liga = _horarios()
    return liga


def resumo():
    """Texto curto para tela e log — em português, sem jargão (convenção §3)."""
    desliga, liga = _horarios()
    if not desliga:
        return 'Não sei a que horas este servidor desliga.'
    faltam = minutos_ate_parar()
    if faltam is None:
        quanto = ''
    elif faltam >= 120:
        quanto = ' (faltam %d horas)' % (faltam // 60)
    elif faltam >= 60:
        quanto = ' (falta 1 hora e %d minutos)' % (faltam % 60)
    else:
        quanto = ' (faltam %d minutos)' % faltam
    texto = 'Este servidor desliga às %02d:%02d%s' % (desliga[0], desliga[1], quanto)
    if liga:
        texto += ' e costuma voltar às %02d:%02d.' % (liga[0], liga[1])
    else:
        texto += '.'
    return texto
