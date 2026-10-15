#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Renomeia cidades no banco do Movimente-se sem perder nada.

Contexto: a cidade "Sistema" (criada pelo copiar_palmeiropolis_para_sistema.py)
e a que recebe os uploads reais; a cidade "Palmeiropolis" ficou como copia
velha. Este script troca os nomes:

    Palmeiropolis -> teste        (primeiro, para liberar o nome)
    Sistema       -> Palmeiropolis

O nome da cidade e a chave de tenancy do sistema: ele aparece em varias
tabelas e no caminho das logos de patrocinador. O script:

- descobre SOZINHO toda tabela que tenha coluna "cidade" (nada fica para tras
  se o schema crescer depois);
- roda tudo em UMA transacao (ou aplica tudo, ou nada);
- tira um snapshot do banco pela API .backup() do SQLite antes de aplicar;
- renomeia tambem a pasta de logos (uploads/sponsors/<slug>);
- so escreve com --aplicar. Sem argumento nenhum, apenas simula.

Links publicos NAO quebram: /r/<slug> resolve pelo slug e regenera o token a
partir da cidade gravada na linha, que este script atualiza junto.

Uso (no NAS, com o perfil web parado):
    venv/bin/python scripts/renomear_cidades.py            # simulacao
    venv/bin/python scripts/renomear_cidades.py --aplicar  # aplica
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
BANCO = RAIZ / 'instance' / 'movimente_se.db'
PASTA_SPONSORS = RAIZ / 'uploads' / 'sponsors'

# Acento por escape: mantem o arquivo em ASCII puro (convencao do projeto) e
# garante a grafia exata gravada no banco -- 'o' acentuado composto (U+00F3),
# conferido linha a linha em todas as tabelas antes de escrever este script.
PALMEIROPOLIS = 'Palmeir\u00f3polis'
SISTEMA = 'Sistema'
TESTE = 'teste'

# Ordem importa: liberar o nome antes de reutiliza-lo.
RENOMEACOES = [
    (PALMEIROPOLIS, TESTE),
    (SISTEMA, PALMEIROPOLIS),
]


def slug_cidade(cidade):
    """Mesma regra de _slugify_city em ranking.py (usada nas pastas e URLs)."""
    base = re.sub(r'[^A-Za-z0-9]+', '-', (cidade or '').strip()).strip('-').lower()
    return base or 'cidade'


def tabelas_com_cidade(conn):
    """Toda tabela real que tenha uma coluna chamada 'cidade'."""
    tabelas = []
    for (nome,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        colunas = [linha[1] for linha in conn.execute(f'PRAGMA table_info("{nome}")')]
        if 'cidade' in colunas:
            tabelas.append(nome)
    return tabelas


def contar(conn, tabelas, cidade):
    total = {}
    for tabela in tabelas:
        n = conn.execute(
            f'SELECT COUNT(*) FROM "{tabela}" WHERE cidade = ?', (cidade,)
        ).fetchone()[0]
        if n:
            total[tabela] = n
    return total


def checar_pre_condicoes(conn, tabelas):
    """Falha cedo e com mensagem clara se o banco nao estiver como esperado."""
    problemas = []
    cidades_config = {
        linha[0] for linha in conn.execute('SELECT cidade FROM config')
    }
    for origem, _destino in RENOMEACOES:
        if origem not in cidades_config:
            problemas.append(f'cidade de origem ausente em config: {origem!r}')
    if TESTE in cidades_config:
        problemas.append(
            f'ja existe uma cidade chamada {TESTE!r} em config: renomeie ou remova antes'
        )

    # Nenhuma tabela pode ficar com duas linhas iguais depois da troca.
    for tabela in tabelas:
        colunas = [linha[1] for linha in conn.execute(f'PRAGMA table_info("{tabela}")')]
        if 'id_atividade' in colunas:
            conflitos = conn.execute(
                f'''SELECT COUNT(*) FROM "{tabela}" a
                    JOIN "{tabela}" b ON a.id_atividade = b.id_atividade
                   WHERE a.cidade = ? AND b.cidade = ?''',
                (SISTEMA, TESTE),
            ).fetchone()[0]
            if conflitos:
                problemas.append(
                    f'{tabela}: {conflitos} atividade(s) colidiriam apos a troca'
                )
    return problemas


def renomear_pastas_sponsors(aplicar, log, pasta=None):
    """Acompanha a troca no disco: uploads/sponsors/<slug>."""
    pasta = Path(pasta) if pasta else PASTA_SPONSORS
    if not pasta.is_dir():
        log(f'  (pasta {pasta} nao existe: nada a fazer)')
        return
    for origem, destino in RENOMEACOES:
        de = pasta / slug_cidade(origem)
        para = pasta / slug_cidade(destino)
        if not de.is_dir():
            log(f'  {de.name}/ -> (nao existe, ignorado)')
            continue
        if para.exists():
            log(f'  ATENCAO: {para.name}/ ja existe; {de.name}/ nao foi movida')
            continue
        log(f'  {de.name}/ -> {para.name}/')
        if aplicar:
            shutil.move(str(de), str(para))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aplicar', action='store_true',
                        help='grava as mudancas (sem isso, apenas simula)')
    parser.add_argument('--banco', default=str(BANCO), help='caminho do SQLite')
    parser.add_argument('--pasta-sponsors', default=str(PASTA_SPONSORS),
                        help='pasta das logos (util para ensaiar em copia)')
    args = parser.parse_args()

    caminho = Path(args.banco)
    if not caminho.is_file():
        print(f'ERRO: banco nao encontrado em {caminho}')
        return 2

    modo = 'APLICANDO' if args.aplicar else 'SIMULACAO (use --aplicar para gravar)'
    print(f'== Renomeacao de cidades -- {modo} ==')
    print(f'Banco: {caminho}')

    conn = sqlite3.connect(str(caminho), timeout=60)
    conn.execute('PRAGMA busy_timeout=60000')
    try:
        tabelas = tabelas_com_cidade(conn)
        print(f'\nTabelas com coluna "cidade" ({len(tabelas)}): {", ".join(tabelas)}')

        problemas = checar_pre_condicoes(conn, tabelas)
        if problemas:
            print('\nERRO: pre-condicoes nao atendidas:')
            for p in problemas:
                print(f'  - {p}')
            return 3

        print('\nSituacao atual:')
        for origem, _ in RENOMEACOES:
            atual = contar(conn, tabelas, origem)
            print(f'  {origem}: ' + (', '.join(f'{t}={n}' for t, n in atual.items()) or 'sem linhas'))

        if args.aplicar:
            carimbo = datetime.now().strftime('%Y%m%d_%H%M%S')
            destino = caminho.parent / f'movimente_se.pre_rename_{carimbo}.db'
            print(f'\nSnapshot de seguranca: {destino.name}')
            copia = sqlite3.connect(str(destino))
            try:
                conn.backup(copia)
            finally:
                copia.close()

        print('\nRenomeando (transacao unica):')
        conn.execute('BEGIN IMMEDIATE')
        try:
            for origem, novo in RENOMEACOES:
                for tabela in tabelas:
                    cur = conn.execute(
                        f'UPDATE "{tabela}" SET cidade = ? WHERE cidade = ?',
                        (novo, origem),
                    )
                    if cur.rowcount:
                        print(f'  {tabela}: {cur.rowcount} linha(s) {origem} -> {novo}')
            if args.aplicar:
                conn.commit()
                print('  COMMIT feito.')
            else:
                conn.rollback()
                print('  ROLLBACK (simulacao).')
        except Exception:
            conn.rollback()
            raise

        print('\nPastas de logo de patrocinador:')
        renomear_pastas_sponsors(args.aplicar, print, args.pasta_sponsors)

        if args.aplicar:
            print('\nConferencia final:')
            for _, novo in RENOMEACOES:
                atual = contar(conn, tabelas, novo)
                print(f'  {novo}: ' + (', '.join(f'{t}={n}' for t, n in atual.items()) or 'sem linhas'))
            print('\nPronto. Reinicie o perfil web no DSM (ou touch wsgi.py) e')
            print('confira o ranking, os links publicos e o login de cada usuario.')
        else:
            print('\nNada foi gravado. Rode de novo com --aplicar quando quiser valer.')
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
