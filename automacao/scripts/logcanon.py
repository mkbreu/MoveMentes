# -*- coding: utf-8 -*-
"""LOG CANONICO, lado do WSL: um jeito so de escrever (17/09/2026, item D1).

POR QUE ISTO EXISTE
O rastro de um acontecimento morava em arquivos demais — fila, guardiao,
send_ranking, provisionar, o log do site, o de acesso. Montar a linha do tempo
de vinte minutos exigia abrir todos. Agora toda linha tem o MESMO formato nas
tres camadas (Windows, WSL, site), no horario de Brasilia:

    AAAA-MM-DD HH:MM:SS | MAQUINA | CAMADA | ORIGEM | NIVEL | mensagem

e mora num arquivo por semana que vira na SEGUNDA-FEIRA AS 05:00 — antes da
primeira janela do ranking, para a madrugada de domingo nao partir em dois.

Do lado do Dell, Windows e WSL escrevem no MESMO arquivo (este script pelo
caminho /mnt/c), porque sao a mesma maquina: `Movimente-se_Log.ps1` e este
modulo sao dois escritores do mesmo livro. O site escreve o seu no NAS.

COMO SE USA: logo depois do `logging.basicConfig` do script,

    import logcanon; logcanon.ligar('PIPELINE', 'send_ranking')

Cada linha que o script ja registra passa a cair tambem no canonico. O log
detalhado do script (`send_ranking_*.log`) continua existindo como anexo.

Falhar aqui nunca pode derrubar quem chamou: o log e' testemunha, nao
participante.
"""
import logging
import os
import time
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo('America/Sao_Paulo')
except Exception:                       # sem base de fusos: usa o relogio local
    _TZ = None

PASTA = '/mnt/c/Users/USUARIO/Documents/SCRIPTS/MOVIMENTE/logs/canonico'
SEMANAS_GUARDADAS = 12
_NIVEL = {'CRITICAL': 'ERRO', 'ERROR': 'ERRO', 'WARNING': 'AVISO',
          'INFO': 'INFO', 'DEBUG': 'INFO'}


def agora():
    return datetime.now(_TZ) if _TZ else datetime.now()


def semana(quando):
    """'AAAA-Sss' da semana ISO de (quando - 5h): vira na segunda as 05:00."""
    ano, sem, _dia = (quando - timedelta(hours=5)).isocalendar()
    return '%d-S%02d' % (ano, sem)


def _expurgar():
    """So quando nasce o arquivo da semana — sem temporizador nem varredura."""
    limite = time.time() - SEMANAS_GUARDADAS * 7 * 86400
    try:
        for nome in os.listdir(PASTA):
            if nome.startswith('canonico_') and nome.endswith('.log'):
                caminho = os.path.join(PASTA, nome)
                if os.path.getmtime(caminho) < limite:
                    os.remove(caminho)
    except Exception:
        pass


def escrever(camada, origem, mensagem, nivel='INFO'):
    try:
        momento = agora()
        os.makedirs(PASTA, exist_ok=True)
        arquivo = os.path.join(PASTA, 'canonico_%s.log' % semana(momento))
        if not os.path.exists(arquivo):
            _expurgar()
        texto = ' '.join(str(mensagem).split('\n')).rstrip()
        linha = '%s | DELL | %s | %s | %s | %s\n' % (
            momento.strftime('%Y-%m-%d %H:%M:%S'), camada, origem, nivel, texto)
        with open(arquivo, 'a', encoding='utf-8') as f:
            f.write(linha)
    except Exception:
        pass


class _Handler(logging.Handler):
    def __init__(self, camada, origem):
        logging.Handler.__init__(self, level=logging.INFO)
        self.camada = camada
        self.origem = origem

    def emit(self, record):
        try:
            escrever(self.camada, self.origem, record.getMessage(),
                     _NIVEL.get(record.levelname, 'INFO'))
        except Exception:
            pass


def ligar(camada, origem):
    """Pendura o canonico no logger raiz do script. Chamar uma vez."""
    try:
        raiz = logging.getLogger()
        if not any(isinstance(h, _Handler) for h in raiz.handlers):
            raiz.addHandler(_Handler(camada, origem))
    except Exception:
        pass
