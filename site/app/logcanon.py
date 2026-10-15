# -*- coding: utf-8 -*-
"""LOG CANONICO, lado do site: um jeito so de escrever (17/09/2026, item D1).

POR QUE ISTO EXISTE
O rastro de um acontecimento morava em arquivos demais — no Dell a fila, o
guardiao, a pipeline; aqui o log do site e o de acesso. Montar a linha do tempo
de vinte minutos exigia abrir todos. Agora as tres camadas (Windows, WSL e
site) escrevem no MESMO formato, no horario de Brasilia:

    AAAA-MM-DD HH:MM:SS | MAQUINA | CAMADA | ORIGEM | NIVEL | mensagem

num arquivo por semana que vira na SEGUNDA-FEIRA AS 05:00: aqui,
`logs/canonico_AAAA-Sss.log`; no Dell, `SCRIPTS\\MOVIMENTE\\logs\\canonico\\`.
Sao dois arquivos porque sao duas maquinas: o NAS desliga das 23:05 as 05:13
e o Dell dorme — cada uma precisa conseguir registrar a propria queda. A busca
por periodo junta os dois (`Movimente-se_BuscarLog.ps1`).

O QUE ENTRA DAQUI (e o que nao entra, de proposito)
- tudo que o site registra como AVISO ou ERRO;
- os INFO que contam a historia de uma acao: e-mail enviado, pedido de rodada
  atendido, trabalho longo comecando e terminando;
- os cliques de quem esta logado (`app/acesso.py`, camada DONO).
O resto dos INFO fica so no log mensal do site: 96 "startup" por dia do
processo de trabalho longo nao ajudam ninguem a achar nada.

ESCRITA SEM DISPUTA: cada linha abre, acrescenta e fecha. Os workers do uWSGI
escrevem no mesmo arquivo sem nenhum deles precisar "dividir" nada — o corte e'
so' o nome do arquivo mudar na segunda-feira.

Falhar aqui nunca pode derrubar quem chamou: o log e' testemunha, nao
participante.
"""
import logging
import os
import time
from datetime import datetime, timedelta

PASTA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
SEMANAS_GUARDADAS = 12
_NIVEL = {'CRITICAL': 'ERRO', 'ERROR': 'ERRO', 'WARNING': 'AVISO',
          'INFO': 'INFO', 'DEBUG': 'INFO'}

# INFO que contam a historia de uma acao — por modulo, e so' eles.
_INFO_QUE_ENTRA = {'correio', 'rodada', 'jobrunner', 'alertas', 'ligarmaquina'}
_CAMADA_DO_MODULO = {'correio': 'EMAIL', 'vigia': 'VIGIA', 'alertas': 'EMAIL'}


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
        agora = datetime.now()
        arquivo = os.path.join(PASTA, 'canonico_%s.log' % semana(agora))
        if not os.path.exists(arquivo):
            _expurgar()
        texto = ' '.join(str(mensagem).splitlines()).rstrip()
        with open(arquivo, 'a', encoding='utf-8') as f:
            f.write('%s | NAS | %s | %s | %s | %s\n' % (
                agora.strftime('%Y-%m-%d %H:%M:%S'), camada, origem, nivel, texto))
    except Exception:
        pass


class Handler(logging.Handler):
    """AVISO e ERRO de todo o site; INFO so' dos modulos da lista."""

    def __init__(self):
        logging.Handler.__init__(self, level=logging.INFO)

    def emit(self, record):
        try:
            modulo = record.module or record.name
            # O modulo vale pelo ARQUIVO de onde a chamada partiu OU pelo nome
            # do logger (`app.rodada` -> `rodada`): os dois dizem a mesma coisa
            # quando a chamada nasce no proprio modulo, e o segundo continua
            # certo quando outro codigo usa o logger dele.
            do_logger = (record.name or '').rsplit('.', 1)[-1]
            if (record.levelno < logging.WARNING
                    and modulo not in _INFO_QUE_ENTRA
                    and do_logger not in _INFO_QUE_ENTRA):
                return
            if do_logger in _INFO_QUE_ENTRA or do_logger in _CAMADA_DO_MODULO:
                modulo = do_logger
            escrever(_CAMADA_DO_MODULO.get(modulo, 'SITE'), modulo,
                     record.getMessage(), _NIVEL.get(record.levelname, 'INFO'))
        except Exception:
            pass


def ligar(logger):
    """Pendura o canonico num logger. Chamar uma vez, no `create_app`."""
    try:
        if not any(isinstance(h, Handler) for h in logger.handlers):
            logger.addHandler(Handler())
    except Exception:
        pass
