# -*- coding: utf-8 -*-
"""O relógio do horário de publicação: pede a rodada no MINUTO certo.

O QUE ISTO CORRIGE (pedido do dono, 21/09/2026)
A primeira versão da P-139 deixou o pedido da rodada com o vigia, que passa de
15 em 15 minutos. No teste das 00:25 o pedido nasceu às 00:25:51 — deu certo por
sorte. Se a passada do vigia tivesse caído às 00:39, o ranking sairia 14 minutos
atrasado, e ninguém entenderia por quê. **O horário está na tela para ser
obedecido, não para ser aproximado.**

COMO FUNCIONA
Uma linha do tempo só: a cada volta, este relógio pergunta à agenda de todas as
cidades ativas qual é o **próximo** horário de publicação, dorme até ele e, ao
acordar, registra o pedido. Dormir até o instante certo custa zero — é o mesmo
que o sistema operacional faz com qualquer temporizador — e o NAS tem 490 MB
para cuidar (regra do menor custo de hardware).

O VIGIA CONTINUA SENDO A REDE DE SEGURANÇA: se este relógio morrer (exceção,
reinício no minuto errado), a passada de 15 em 15 minutos ainda pede, atrasada
mas pede. Duas peças olhando o mesmo horário não geram rodada dupla, porque
quem decide é a mesma guarda das duas: `vigia._pedir_rodada_no_horario`, que
não pede se a rodada daquela janela já aconteceu nem se já pediu neste horário.

O QUE ELE NÃO FAZ: não publica, não fala com o WhatsApp, não liga máquina. Só
acorda na hora e registra o pedido — quem acorda a máquina é o Wake-on-LAN, 15
minutos antes, pela mesma agenda.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Nunca dormir mais que isto de uma vez: se o dono mudar o horário na tela, o
# relógio precisa enxergar a mudança sem esperar até amanhã.
MAX_SONO_SEG = 300
# ACORDA UM POUCO DEPOIS DO HORARIO, NUNCA ANTES (medido em 21/09/2026).
#
# A primeira versao acordava 2 segundos ANTES, para "nao perder o minuto". Foi
# o que fez o teste das 00:40 falhar: a guarda do pedido exige
# `horario <= agora`, e as 00:39:58 ela recusou, com razao. Na volta seguinte o
# proximo horario ja era o de amanha, e o pedido nunca nasceu — o ranking so
# saiu as 00:49:29, quando a passada de 15 em 15 min do vigia pegou.
#
# Agora ele acorda 3 segundos DEPOIS. O atraso e' invisivel e o pedido nasce.
ATRASO_SEG = 3

_ligado = False


def proximo_horario(agora=None):
    """(quando, cidade, horario) da próxima publicação — ou None.

    Olha todas as cidades ativas. Horário desligado ou que não publica ranking
    não conta: pedir rodada para um horário que só manda patrocinador seria
    publicar o que ninguém pediu.
    """
    agora = agora or datetime.now()
    try:
        from app import consolidado, publicador
        cidades = list(consolidado.cidades_ativas())
    except Exception as exc:
        logger.warning('relogio: nao consegui listar as cidades (%s)', exc)
        return None

    melhor = None
    for cidade in cidades:
        try:
            linhas = publicador.agenda(cidade) or ()
        except Exception as exc:
            logger.debug('relogio: sem agenda para %s (%s)', cidade, exc)
            continue
        for item in linhas:
            if not getattr(item, 'ativo', False):
                continue
            if not getattr(item, 'envia_ranking', False):
                continue
            try:
                hora, minuto = (int(p) for p in str(item.horario).split(':'))
            except (ValueError, AttributeError):
                continue
            quando = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
            if quando <= agora:
                quando += timedelta(days=1)
            if melhor is None or quando < melhor[0]:
                melhor = (quando, cidade, item.horario)
    return melhor


def _uma_volta(app):
    """Dorme até o próximo horário (ou até `MAX_SONO_SEG`) e pede na hora."""
    with app.app_context():
        alvo = proximo_horario()
    if alvo is None:
        return MAX_SONO_SEG
    quando, cidade, horario = alvo
    falta = (quando - datetime.now()).total_seconds() + ATRASO_SEG
    if falta > MAX_SONO_SEG:
        return MAX_SONO_SEG          # confere de novo daqui a pouco
    if falta > 0:
        time.sleep(falta)
    # CHEGOU A HORA. Quem decide se pede de fato é a mesma função que o vigia
    # usa — uma regra só, nos dois caminhos.
    with app.app_context():
        try:
            from app import vigia
            vigia._pedir_rodada_no_horario(cidade, datetime.now())
        except Exception as exc:
            logger.error('relogio (%s %s): %s', cidade, horario, exc, exc_info=True)
    # Sai da janela do horário antes de recomeçar, senão pediria de novo no
    # mesmo minuto (a guarda recusaria, mas seria trabalho à toa).
    return 70


def iniciar(app):
    """Liga o relógio. Idempotente: uma thread por processo, no máximo."""
    global _ligado
    if _ligado:
        return False
    _ligado = True

    def _laco():
        # Espera o boot assentar, como as outras rotinas do site fazem.
        time.sleep(60)
        while True:
            try:
                espera = _uma_volta(app)
            except Exception as exc:
                logger.error('relogio da rodada: %s', exc, exc_info=True)
                espera = MAX_SONO_SEG
            time.sleep(max(5, espera))

    threading.Thread(target=_laco, daemon=True,
                     name='relogio-da-rodada').start()
    logger.info('Relogio da rodada ligado: pede no minuto do horario da tela.')
    return True
