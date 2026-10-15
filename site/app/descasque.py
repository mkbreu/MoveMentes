# -*- coding: utf-8 -*-
"""O trabalho adiantado de quem esta ESPERANDO na fila.

POR QUE ISTO EXISTE (29/08/2026, desenho do dono).

Uma importacao de atleta tem duas fases, e so a primeira custa Strava:

    A) DESCOBERTA -- uma requisicao `/interval` por mes. Ela ja devolve TODAS
       as atividades daquele mes, com quilometragem, tipo, dono e data.
    B) GRAVACAO   -- salvar no banco, anticheat, consolidacao. ZERO requisicao.

A prova de que a B e' de graca: o job de um atleta gravou 1.414 atividades em 12
minutos. Com uma requisicao por atividade isso seria impossivel -- foram ~70
requisicoes, uma por mes.

Enquanto um trabalho ocupa a vaga do servidor, os que estao na fila ficam
parados sem fazer nada. Aqui eles adiantam a fase A e guardam o resultado. Na
vez deles, so falta a fase B -- que roda em segundos e devolve a vaga logo.

O QUE ISTO **NAO** FAZ: nao cria requisicao a mais nem "burla" teto. O total
gasto no Strava e' exatamente o mesmo; o que muda e' QUANDO ele e' gasto, e
quanto tempo a vaga fica ocupada. Por isso o descasque respeita o mesmo pacer
e so' roda quando ha folga de cota -- adiantar trabalho nao pode atrapalhar
quem esta na vez.
"""
import json
import os
import time
from datetime import datetime

_RAIZ = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'temp_extract', 'descasque')

# Descasque velho nao serve: o atleta pode ter apagado ou editado atividade.
VALIDADE_HORAS = 12


def _caminho(job_id):
    seguro = ''.join(c for c in str(job_id or '') if c.isalnum() or c in '-_')
    return os.path.join(_RAIZ, '%s.json' % seguro) if seguro else None


def _para_json(entradas):
    """`data_hora` e datetime; o resto ja e texto/numero."""
    saida = []
    for e in entradas or ():
        copia = dict(e)
        quando = copia.get('data_hora')
        if isinstance(quando, datetime):
            copia['data_hora'] = quando.isoformat()
        saida.append(copia)
    return saida


def _de_json(entradas):
    saida = []
    for e in entradas or ():
        copia = dict(e)
        quando = copia.get('data_hora')
        if isinstance(quando, str):
            try:
                copia['data_hora'] = datetime.fromisoformat(quando)
            except ValueError:
                copia['data_hora'] = None
        saida.append(copia)
    return saida


def _ler(job_id):
    caminho = _caminho(job_id)
    if not caminho:
        return {}
    try:
        with open(caminho, 'r') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _gravar(job_id, dados):
    caminho = _caminho(job_id)
    if not caminho:
        return
    try:
        if not os.path.isdir(_RAIZ):
            os.makedirs(_RAIZ)
        tmp = caminho + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(dados, f)
        os.replace(tmp, caminho)
    except Exception:
        # Sem cache o sistema volta ao comportamento antigo -- que funciona.
        # Nunca deixar isso derrubar a importacao.
        pass


def guardar(job_id, ano, mes, entradas, total_km):
    """Anota o resultado da fase A de um mes."""
    dados = _ler(job_id)
    meses = dados.get('meses') or {}
    meses['%04d-%02d' % (int(ano), int(mes))] = {
        'entradas': _para_json(entradas),
        'total_km': float(total_km or 0.0),
        'em': time.time(),
    }
    dados['meses'] = meses
    dados['atualizado'] = time.time()
    _gravar(job_id, dados)


def pegar(job_id, ano, mes):
    """(entradas, total_km) ja descascados, ou None se nao ha (ou venceu)."""
    mes_chave = '%04d-%02d' % (int(ano), int(mes))
    guardado = ((_ler(job_id).get('meses') or {}).get(mes_chave)) or None
    if not guardado:
        return None
    if (time.time() - float(guardado.get('em') or 0)) > VALIDADE_HORAS * 3600:
        # VENCIDO NAO E' MELHOR QUE NADA (regra do projeto sobre cache): a
        # pessoa pode ter apagado ou corrigido atividade nesse meio tempo.
        return None
    return _de_json(guardado.get('entradas')), float(guardado.get('total_km') or 0.0)


def meses_prontos(job_id):
    """Quais meses ja estao descascados e ainda valem."""
    agora = time.time()
    prontos = set()
    for chave, item in (_ler(job_id).get('meses') or {}).items():
        if (agora - float((item or {}).get('em') or 0)) <= VALIDADE_HORAS * 3600:
            try:
                ano, mes = chave.split('-')
                prontos.add((int(ano), int(mes)))
            except ValueError:
                continue
    return prontos


def limpar(job_id):
    """Trabalho acabou: o descasque nao serve mais para ninguem."""
    caminho = _caminho(job_id)
    try:
        if caminho and os.path.isfile(caminho):
            os.remove(caminho)
    except Exception:
        pass


def faxina(ids_vivos):
    """Some com o descasque de trabalho que nao existe mais."""
    try:
        if not os.path.isdir(_RAIZ):
            return 0
        vivos = set(str(i) for i in (ids_vivos or ()))
        apagados = 0
        for nome in os.listdir(_RAIZ):
            if not nome.endswith('.json'):
                continue
            if nome[:-5] not in vivos:
                try:
                    os.remove(os.path.join(_RAIZ, nome))
                    apagados += 1
                except Exception:
                    pass
        return apagados
    except Exception:
        return 0


def resumo(job_id):
    """Para a tela: quantos meses ja estao prontos e quantos km eles somam."""
    prontos = _ler(job_id).get('meses') or {}
    agora = time.time()
    validos = [m for m in prontos.values()
               if (agora - float((m or {}).get('em') or 0)) <= VALIDADE_HORAS * 3600]
    return {
        'meses': len(validos),
        'atividades': sum(len(m.get('entradas') or ()) for m in validos),
    }
