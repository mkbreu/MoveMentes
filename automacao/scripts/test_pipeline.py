#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Teste que EXECUTA pipeline() com dados falsos, sem tocar em WAHA/Strava/site.

Trava contra o NameError de 15/08 (ver Z:\\Backups\\extra\\docs\\ERROS.md): dentro
de pipeline(), o trecho que guarda os ultimos 5 marcos (`estado.get('marcos')`,
regra do P-76) usava `estado` sem nunca ter chamado load_state() naquele
escopo. A pipeline quebrava DEPOIS de processar e ANTES de publicar -- por
isso so aparecia rodando de verdade; `ast.parse`/`py_compile` nao pegam
NameError de variavel usada sem ser atribuida em nenhum caminho do escopo.

O teste forca exatamente esse trecho a executar: mensagens nao-vazias,
upload/poll simulados como sucesso, e etapa_ranking cortando a rodada logo
em seguida (retorna None) -- assim a pipeline nunca chega nas etapas de
envio de verdade (grupo, patrocinador, parabens), so no checkpoint que
quebrava.

Rodar (dentro do WSL, mesmo python do cron):
    cd ~/movimente-automacao/scripts && python3 test_pipeline.py
"""
import argparse
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send_ranking as sr


def _args(**over):
    base = dict(dry_run=False, force_collect=False, periodo='2026-08',
                sem_envio=False, sem_patrocinadores=True, sem_parabens=True,
                sem_avisos=True, sem_fila=True, teste=False)
    base.update(over)
    return argparse.Namespace(**base)


def _mensagens_falsas():
    return [
        {'id': 'msg1', 'ts': 1000, 'sender': 'Fulano',
         'body': 'https://strava.com/activities/123'},
        {'id': 'msg2', 'ts': 2000, 'sender': 'Ciclana',
         'body': 'https://strava.com/activities/456'},
    ]


def testar_checkpoint_de_marcos_sem_nameerror():
    """O cenario exato do bug: estado anterior JA TEM marcos (rodada de
    verdade nunca comeca do zero), a coleta traz mensagem nova, e o
    checkpoint tem de somar o marco novo aos anteriores sem NameError."""
    estado_anterior = {'last_ts': 500, 'marcos': [500, 400, 300]}
    chamadas_save_state = []

    def _save_state_fake(**kwargs):
        chamadas_save_state.append(kwargs)

    with patch.object(sr, 'etapa_api', return_value=True), \
         patch.object(sr, 'etapa_waha', return_value=True), \
         patch.object(sr, 'etapa_prontidao', return_value=(True, {})), \
         patch.object(sr, 'coletar_com_sincronizacao',
                      return_value=(_mensagens_falsas(), True)), \
         patch.object(sr, 'etapa_upload', return_value='job-fake'), \
         patch.object(sr, 'etapa_poll', return_value={'stage': 'done'}), \
         patch.object(sr, 'etapa_ranking', return_value=None), \
         patch.object(sr, 'load_state', return_value=dict(estado_anterior)), \
         patch.object(sr, 'save_state', side_effect=_save_state_fake):
        resultado = sr.pipeline(_args())

    assert resultado is True, 'pipeline() deveria terminar OK (etapa_ranking=None encerra cedo, sem erro)'
    assert len(chamadas_save_state) == 1, (
        'esperava 1 checkpoint salvo, veio %d' % len(chamadas_save_state))
    salvo = chamadas_save_state[0]
    assert salvo['last_ts'] == 2000, 'checkpoint tem de avancar para o ts da mensagem mais nova'
    assert salvo['marcos'][0] == 2000, 'marco novo entra na frente da lista'
    assert 500 in salvo['marcos'] and 400 in salvo['marcos'], (
        'marcos anteriores tem de sobreviver (regra P-76: voltar 3 marcos '
        'exige que eles continuem guardados) -- veio %r' % (salvo['marcos'],))
    print('OK: checkpoint de marcos roda sem NameError e preserva o historico')


def testar_checkpoint_com_estado_vazio_first_run():
    """Primeira rodada da vida (ou depois de um reset): load_state() sem
    'marcos' nenhum. estado.get('marcos') tem de virar lista vazia, nao
    quebrar."""
    with patch.object(sr, 'etapa_api', return_value=True), \
         patch.object(sr, 'etapa_waha', return_value=True), \
         patch.object(sr, 'etapa_prontidao', return_value=(True, {})), \
         patch.object(sr, 'coletar_com_sincronizacao',
                      return_value=(_mensagens_falsas(), True)), \
         patch.object(sr, 'etapa_upload', return_value='job-fake'), \
         patch.object(sr, 'etapa_poll', return_value={'stage': 'done'}), \
         patch.object(sr, 'etapa_ranking', return_value=None), \
         patch.object(sr, 'load_state', return_value={}), \
         patch.object(sr, 'save_state') as save_state_mock:
        resultado = sr.pipeline(_args())

    assert resultado is True
    assert save_state_mock.call_count == 1
    marcos = save_state_mock.call_args.kwargs['marcos']
    assert marcos == [2000], 'sem historico anterior, marcos e so o novo -- veio %r' % (marcos,)
    print('OK: primeira rodada (sem marcos anteriores) tambem nao quebra')


if __name__ == '__main__':
    testes = [testar_checkpoint_de_marcos_sem_nameerror,
              testar_checkpoint_com_estado_vazio_first_run]
    falhou = False
    for teste in testes:
        try:
            teste()
        except AssertionError as exc:
            falhou = True
            print('FALHOU: %s -- %s' % (teste.__name__, exc))
        except Exception as exc:
            falhou = True
            print('ERRO INESPERADO: %s -- %s: %s'
                  % (teste.__name__, type(exc).__name__, exc))
    if falhou:
        print('\n--- RESULTADO: FALHOU ---')
        sys.exit(1)
    print('\n--- RESULTADO: TUDO OK ---')
