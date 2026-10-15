# app/ratepacer.py
# -*- coding: utf-8 -*-
"""Espacamento de requisicoes compartilhado entre processos.

Cada job de scraping tem seu proprio StravaThrottle, o que resolve o ritmo de
UM job. Dois uploads simultaneos (dois admins da mesma cidade, ou o uWSGI
rodando uma continuacao enquanto alguem envia um arquivo novo) somavam as
requisicoes e dobravam a taxa vista pelo Strava -- exatamente o que o controle
de taxa existe para evitar.

Este modulo guarda em arquivo o instante do proximo slot livre, de forma que
todos os jobs de todos os workers peguem senha na MESMA fila. O estado e
minusculo (um JSON com um numero) e a secao critica e protegida por um lock
de arquivo no mesmo padrao ja usado para o claim de retomada de job.

Falha e sempre para o lado seguro: qualquer erro de I/O devolve espera zero e
o chamador continua com o pacing local que ja tinha antes -- nunca fica mais
rapido do que o comportamento atual, no pior caso fica igual.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR_PADRAO = Path(__file__).resolve().parents[1] / 'temp_extract' / 'pacer'
_NOME_RE = re.compile(r'[^a-z0-9_]+')
# Tempo maximo esperando a vez de entrar na secao critica.
_LOCK_TIMEOUT = 5.0
# Lock mais velho que isso e orfao (processo morreu segurando o arquivo).
_LOCK_STALE = 30.0
# Teto do que a fila pode mandar esperar. Protege de estado corrompido com uma
# data absurda no futuro, que congelaria todos os jobs.
_ESPERA_MAXIMA = 300.0

# ===== TETO DE VOLUME (27/08/2026) =====
#
# As janelas seguem os limites NATURAIS, como o Strava alinha os dele: o quarto
# de hora fecha em 0/15/30/45, a hora fecha na hora cheia e o dia fecha a
# meia-noite UTC. Alinhar assim faz o teto liberar junto com o do Strava, em vez
# de arrastar um atraso proprio.
_JANELAS = (('q15', 900), ('hora', 3600), ('dia', 86400))

# DE ONDE SAEM ESTES NUMEROS (revisto em 29/08/2026).
#
# Ate aqui o comentario dizia que 100/15min e 1.000/dia eram "o teto do
# Strava". Conferido na fonte, sao os limites publicados da **API OFICIAL**
# (developers.strava.com/docs/rate-limits) -- e este projeto NAO usa a API
# oficial: le a pagina com cookie de sessao, que nao tem limite publicado.
# Estavamos aplicando ao scraping um teto que e' de outra coisa, e ainda por
# baixo: o proprio tier Standard da API permite 200/15min e 2.000/dia.
#
# O dia sobe para 2.000 por decisao do dono (29/08/2026), que e' quem assume
# o risco -- e o historico do projeto ja passou disso varias vezes sem
# bloqueio. O que torna isso seguro nao e' o numero: e a trava de
# `app/stravasinal.py`, que LE a resposta do Strava e para de verdade quando
# ele reclama, em vez de insistir. Teto alto com trava boa protege mais que
# teto baixo sem diagnostico.
#
# As janelas curtas ficam ociosas na pratica (medido em 29/08: 8 requisicoes
# nos ultimos 15 min, teto 100). Quem trava e' o dia.
# O quarto de hora acompanhou o dia (29/08/2026, medido). Com 2.000/dia e
# 100/15min, a janela curta virou funil: o job de fotos travou em 55 de 77
# com o contador em 96/100, porque gasta uma requisicao por atleta em
# rajada e dividia a janela com uma importacao. Os numeros da API oficial
# andam juntos (200/15min COM 2.000/dia) -- os nossos passam a andar tambem.
_TETOS_PADRAO = {
    'q15':  int(os.getenv('STRAVA_TETO_15MIN', '200')),
    'hora': int(os.getenv('STRAVA_TETO_HORA', '600')),
    'dia':  int(os.getenv('STRAVA_TETO_DIA', '2000')),
}

# Limites de sanidade da tela: nem o dono consegue pedir absurdo por engano.
_TETO_MAXIMO = {'q15': 400, 'hora': 1600, 'dia': 6000}
_TETO_MINIMO = 10
_ARQUIVO_TETOS = 'tetos.json'

_ROTULO_JANELA = {'q15': '15 minutos', 'hora': '1 hora', 'dia': '24 horas'}


def _inicio_da_janela(agora, tamanho):
    """O comeco da janela natural que contem `agora` (epoch, UTC)."""
    return int(agora // tamanho) * tamanho


class SharedPacer:
    """Fila unica de slots para um recurso externo (ex.: o site do Strava)."""

    def __init__(self, nome, diretorio=None):
        self.nome = _NOME_RE.sub('', str(nome or '').lower()) or 'default'
        self.dir = Path(diretorio) if diretorio else _DIR_PADRAO
        self.arquivo = self.dir / f'{self.nome}.json'
        self.lock = self.dir / f'{self.nome}.lock'

    # ----- secao critica -----

    def _adquirir(self):
        limite = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    idade = time.time() - self.lock.stat().st_mtime
                    if idade > _LOCK_STALE:
                        self.lock.unlink()
                        logger.warning('Lock orfao do pacer %s removido (%ss)',
                                       self.nome, int(idade))
                        continue
                except OSError:
                    pass
                if time.monotonic() >= limite:
                    return False
                time.sleep(0.05)
            except OSError as exc:
                logger.debug('Pacer %s: falha ao criar lock: %s', self.nome, exc)
                return False

    def _liberar(self):
        try:
            self.lock.unlink()
        except OSError:
            pass

    # ----- estado -----

    def _ler(self):
        try:
            with open(self.arquivo, 'r', encoding='utf-8') as fh:
                dados = json.load(fh)
            return dados if isinstance(dados, dict) else {}
        except (OSError, ValueError):
            return {}

    def _gravar(self, dados):
        tmp = self.arquivo.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(dados, fh)
        os.replace(tmp, self.arquivo)

    # ----- API -----

    def reserve(self, interval):
        """Pega o proximo slot e devolve quantos segundos esperar por ele.

        Reserva primeiro e dorme depois: como o proximo slot ja foi empurrado
        para frente antes de qualquer espera, dois processos que chamam ao
        mesmo tempo recebem tempos diferentes e nao disparam juntos.
        """
        interval = max(0.0, float(interval or 0))
        if not interval:
            return 0.0
        if not self._adquirir():
            logger.debug('Pacer %s: sem lock a tempo, usando pacing local', self.nome)
            return 0.0
        try:
            agora = time.time()
            estado = self._ler()
            try:
                proximo = float(estado.get('next_at') or 0)
            except (TypeError, ValueError):
                proximo = 0.0
            if proximo < agora:
                proximo = agora
            espera = proximo - agora
            if espera > _ESPERA_MAXIMA:
                # Estado incoerente (relogio mexido, arquivo corrompido).
                logger.warning('Pacer %s: espera absurda de %ss, fila reiniciada',
                               self.nome, int(espera))
                proximo, espera = agora, 0.0
            # ESCREVER O ESTADO INTEIRO, NUNCA SO' O SLOT (28/08/2026). Este
            # `_gravar` recebia um dicionario novo e APAGAVA `janelas` --
            # entao o contador de volume voltava a zero a cada espacamento,
            # e o teto nunca chegava a valer. Reproduzido: tres
            # reserve+registrar deixavam o contador em 1.
            estado['next_at'] = proximo + interval
            estado['updated_at'] = agora
            self._gravar(estado)
            return max(0.0, espera)
        except Exception as exc:
            logger.debug('Pacer %s: erro ao reservar slot: %s', self.nome, exc)
            return 0.0
        finally:
            self._liberar()

    def registrar(self, quantas=1):
        """Conta requisicoes JA FEITAS nas tres janelas. Chamado DEPOIS do GET.

        Contar depois, e nao antes, e de proposito: requisicao que nem chegou a
        sair (cancelamento, erro de montagem) nao gasta cota.
        """
        if not self._adquirir():
            return
        try:
            agora = time.time()
            estado = self._ler()
            janelas = estado.get('janelas') or {}
            for nome, tamanho in _JANELAS:
                inicio = _inicio_da_janela(agora, tamanho)
                atual = janelas.get(nome) or {}
                if int(atual.get('inicio') or 0) != inicio:
                    atual = {'inicio': inicio, 'n': 0}
                atual['n'] = int(atual.get('n') or 0) + int(quantas)
                janelas[nome] = atual
            estado['janelas'] = janelas
            self._gravar(estado)
        except Exception as exc:
            logger.debug('Pacer %s: erro ao contar requisicao: %s', self.nome, exc)
        finally:
            self._liberar()

    def _caminho_tetos(self):
        return str(self.dir / _ARQUIVO_TETOS)

    def tetos(self):
        """Os tetos em vigor: o que a tela gravou, senao o padrao."""
        limites = dict(_TETOS_PADRAO)
        try:
            with open(self._caminho_tetos(), 'r') as f:
                gravado = json.load(f) or {}
            for nome in ('q15', 'hora', 'dia'):
                valor = gravado.get(nome)
                if valor:
                    limites[nome] = max(_TETO_MINIMO,
                                        min(int(valor), _TETO_MAXIMO[nome]))
        except Exception:
            pass          # sem arquivo, valem os padroes
        return limites

    def definir_tetos(self, novos, por=''):
        """Grava os tetos escolhidos na tela. Devolve os que ficaram valendo.

        MUDAR O TETO VALE NA HORA (regra do dono, 29/08/2026): se o contador
        do dia ja passou do novo valor, o proximo pedido ao Strava ja para;
        se o novo valor passou a caber, os trabalhos congelados voltam
        sozinhos na proxima passada do ticker. Nao ha nada a reiniciar.
        """
        limites = dict(_TETOS_PADRAO)
        for nome in ('q15', 'hora', 'dia'):
            valor = (novos or {}).get(nome)
            if valor:
                limites[nome] = max(_TETO_MINIMO,
                                    min(int(valor), _TETO_MAXIMO[nome]))
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._caminho_tetos() + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(dict(limites, por=por, em=time.time()), f)
            os.replace(tmp, self._caminho_tetos())
        except Exception as exc:
            logger.warning('Pacer %s: nao consegui gravar os tetos: %s',
                           self.nome, exc)
        return limites

    def estourou(self, tetos=None):
        """Alguma janela ja bateu o teto? Devolve (rotulo, segundos_para_liberar).

        (None, 0) = pode seguir. NAO reserva nada e NAO espera: quem chama
        decide o que fazer com a informacao — no caso dos trabalhos longos,
        congelar e voltar depois (regra: esperar tem prazo, e prazo tem
        desfecho).
        """
        limites = self.tetos()
        limites.update(tetos or {})
        try:
            agora = time.time()
            janelas = (self._ler() or {}).get('janelas') or {}
            for nome, tamanho in _JANELAS:
                teto = int(limites.get(nome) or 0)
                if teto <= 0:
                    continue
                atual = janelas.get(nome) or {}
                inicio = int(atual.get('inicio') or 0)
                if inicio != _inicio_da_janela(agora, tamanho):
                    continue          # janela virou: contador zerado
                if int(atual.get('n') or 0) >= teto:
                    falta = (inicio + tamanho) - agora
                    return _ROTULO_JANELA.get(nome, nome), max(1.0, falta)
        except Exception as exc:
            logger.debug('Pacer %s: erro ao conferir teto: %s', self.nome, exc)
        return None, 0.0

    def uso(self):
        """Quanto ja foi gasto em cada janela — so para tela e log."""
        agora = time.time()
        janelas = (self._ler() or {}).get('janelas') or {}
        saida = {}
        for nome, tamanho in _JANELAS:
            atual = janelas.get(nome) or {}
            if int(atual.get('inicio') or 0) != _inicio_da_janela(agora, tamanho):
                saida[nome] = 0
            else:
                saida[nome] = int(atual.get('n') or 0)
        return saida

    def peek(self):
        """Segundos ate o proximo slot livre, sem reservar (so para diagnostico)."""
        try:
            proximo = float((self._ler() or {}).get('next_at') or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, proximo - time.time())
