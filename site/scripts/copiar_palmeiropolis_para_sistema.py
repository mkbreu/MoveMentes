# scripts/copiar_palmeiropolis_para_sistema.py
# -*- coding: utf-8 -*-
"""Copia as atividades de Palmeiropolis para a cidade Sistema (rodar uma vez).

Objetivo: a cidade "Sistema" consolida o historico do grupo desde 2021 sem
re-scrapear o que Palmeiropolis ja coletou (6k+ atividades ja no banco).

Garantias:
- Separacao por cidade preservada: as copias nascem com cidade='Sistema';
  cada cidade continua enxergando somente as proprias linhas.
- Idempotente: so copia id_atividade que Sistema ainda nao tem. Rodar de novo
  nao duplica nada.
- processed_file_id vira NULL e arquivo_origem recebe um marcador proprio,
  entao nenhuma gestao de upload (cancelar/apagar) de nenhuma das cidades
  consegue apagar as copias por engano.
- Backup fisico do banco e criado antes de qualquer escrita.

Uso (no NAS):
    cd /volume1/web/movimente-se && venv/bin/python scripts/copiar_palmeiropolis_para_sistema.py
"""
import os
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, 'instance', 'movimente_se.db')
ORIGEM = 'Palmeirópolis'
DESTINO = 'Sistema'
MARCADOR = 'copia_palmeiropolis_' + datetime.now().strftime('%Y%m%d')


def main():
    if not os.path.exists(DB):
        print('ERRO: banco nao encontrado em ' + DB)
        return 1

    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=30000')

    # Backup fisico antes de escrever (API de backup respeita o WAL).
    backup_path = os.path.join(
        BASE, 'instance',
        'movimente_se.pre_copia_sistema_%s.db'
        % datetime.now().strftime('%Y%m%d_%H%M%S'))
    dst = sqlite3.connect(backup_path)
    with dst:
        con.backup(dst)
    dst.close()
    print('Backup criado: ' + backup_path)

    cols = [r[1] for r in con.execute('PRAGMA table_info(activities)')]
    if 'id_atividade' not in cols or 'cidade' not in cols:
        print('ERRO: schema inesperado de activities: %s' % cols)
        return 1

    copy_cols = [c for c in cols if c != 'id']
    params = []
    select_parts = []
    for c in copy_cols:
        if c == 'cidade':
            select_parts.append('?')
            params.append(DESTINO)
        elif c == 'arquivo_origem':
            select_parts.append('?')
            params.append(MARCADOR)
        elif c == 'processed_file_id':
            select_parts.append('NULL')
        else:
            select_parts.append('"%s"' % c)

    sql = (
        'INSERT INTO activities (%s) SELECT %s FROM activities '
        'WHERE cidade = ? AND id_atividade NOT IN '
        '(SELECT id_atividade FROM activities WHERE cidade = ?)'
        % (', '.join('"%s"' % c for c in copy_cols), ', '.join(select_parts))
    )
    params.extend([ORIGEM, DESTINO])

    antes = con.execute(
        'SELECT COUNT(*) FROM activities WHERE cidade = ?', (DESTINO,)).fetchone()[0]
    cur = con.execute(sql, params)
    con.commit()
    depois = con.execute(
        'SELECT COUNT(*) FROM activities WHERE cidade = ?', (DESTINO,)).fetchone()[0]

    print('Atividades em %s antes:  %d' % (DESTINO, antes))
    print('Copiadas de %s: %d' % (ORIGEM, cur.rowcount))
    print('Atividades em %s depois: %d' % (DESTINO, depois))
    print('')
    print('Resumo de %s por ano (todas as modalidades):' % DESTINO)
    for ano, qtd, km in con.execute(
            "SELECT substr(data_hora, 1, 4) AS ano, COUNT(*), "
            "ROUND(SUM(distancia), 1) FROM activities WHERE cidade = ? "
            "GROUP BY ano ORDER BY ano", (DESTINO,)):
        print('  %s: %5d atividades | %10.1f km' % (ano, qtd, km or 0.0))
    con.close()
    print('')
    print('Concluido. Abra o ranking dos meses desejados para regerar os snapshots.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
