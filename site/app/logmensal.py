# -*- coding: utf-8 -*-
"""Log que se divide por MES, com teto de tamanho dentro do mes.

POR QUE ISTO EXISTE (regra 19, pedido do dono em 06/09/2026)
Ate aqui o log so se dividia por TAMANHO (2 MB, 10 copias). Isso resolve o
arquivo gigante -- o `movimente_se.log.6` chegou a 11 MB antes disso --, mas
nao resolve a pergunta que a gente realmente faz quando algo quebra: **"o que
aconteceu no dia 30?"**. Com divisao so por tamanho, o dia 30 pode estar em
qualquer um dos onze arquivos, e descobrir em qual exige abrir todos -- que e'
exatamente o que a regra 15 proibe.

Dividindo por mes, a resposta e' o nome do arquivo.

A POLITICA, NAS PALAVRAS DO DONO:
  "manter os logs completos de 60 dias e mais que isso um log de cada
   servico por mes"

Que aqui vira:
  * dentro do mes -> `movimente_se-2026-09.log` (+ `.1`, `.2` ... quando passa
    de 2 MB);
  * ate 60 dias   -> guarda tudo, inclusive as sobras de tamanho;
  * passou disso  -> fica so o arquivo principal do mes, as sobras saem.

VIRAR O MES NAO E' ROTACIONAR. Quando o mes muda, o handler apenas **troca de
arquivo** -- nao renomeia nada, nao mexe no arquivo do mes passado. Rotacionar
ali dentro criaria um `-2026-08.log.1` no dia 1o de setembro, que e' mentira
sobre quando aquilo foi escrito.
"""
import os
import re
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Quantos dias de log COMPLETO (com as sobras de tamanho) ficam de pe.
DIAS_COMPLETOS = 60

# Padrao dos arquivos que este modulo entende: prefixo-AAAA-MM.log[.N]
_PADRAO = re.compile(r'^(?P<prefixo>.+)-(?P<ano>\d{4})-(?P<mes>\d{2})\.log(?:\.(?P<sobra>\d+))?$')


class ArquivoMensal(RotatingFileHandler):
    """Um arquivo por mes; dentro do mes, divide por tamanho.

    `delay=True` continua valendo pelo mesmo motivo de antes: os dois workers
    do uWSGI abrem o mesmo arquivo no boot e podem disputar a hora de dividir.
    """

    def __init__(self, pasta, prefixo, maxBytes=2 * 1024 * 1024,
                 backupCount=10, encoding='utf-8', delay=True):
        self._pasta = pasta
        self._prefixo = prefixo
        self._mes = self._mes_de_hoje()
        RotatingFileHandler.__init__(
            self, self._caminho_do_mes(), maxBytes=maxBytes,
            backupCount=backupCount, encoding=encoding, delay=delay)

    @staticmethod
    def _mes_de_hoje():
        return datetime.now().strftime('%Y-%m')

    def _caminho_do_mes(self):
        return os.path.join(self._pasta, '%s-%s.log' % (self._prefixo, self._mes))

    def shouldRollover(self, record):
        # VIRADA DE MES PRIMEIRO, TAMANHO DEPOIS. Se o mes mudou, trocamos o
        # destino e dizemos "nao precisa rotacionar" -- o arquivo novo nasce
        # vazio sozinho, e o do mes passado fica intacto.
        mes_agora = self._mes_de_hoje()
        if mes_agora != self._mes:
            self._mes = mes_agora
            self.baseFilename = os.path.abspath(self._caminho_do_mes())
            if self.stream:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            return 0
        return RotatingFileHandler.shouldRollover(self, record)


def expurgar(pasta, dias_completos=DIAS_COMPLETOS, agora=None):
    """Aplica a politica: 60 dias completos, depois um arquivo por mes.

    Devolve (removidos, bytes_liberados). **Nunca levanta excecao** -- faxina
    que quebra a aplicacao e pior que faxina que nao acontece.

    So mexe em arquivo que casa com o padrao `prefixo-AAAA-MM.log[.N]`: log de
    outro formato (o `movimente_se.log` antigo, por exemplo) fica onde esta,
    porque apagar arquivo que nao se entende e' como apagar no escuro.
    """
    removidos, liberados = 0, 0
    agora = agora or time.time()
    limite = agora - dias_completos * 86400
    try:
        nomes = os.listdir(pasta)
    except Exception:
        return 0, 0

    for nome in nomes:
        casa = _PADRAO.match(nome)
        if not casa or not casa.group('sobra'):
            continue          # so as SOBRAS de tamanho saem; o do mes fica
        caminho = os.path.join(pasta, nome)
        try:
            if os.path.getmtime(caminho) >= limite:
                continue      # ainda dentro dos 60 dias: guarda tudo
            tamanho = os.path.getsize(caminho)
            os.remove(caminho)
            removidos += 1
            liberados += tamanho
        except Exception:
            continue
    return removidos, liberados
