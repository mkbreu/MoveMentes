#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Faxina do que sobrou de sessao apagada — perfis e bancos orfaos.

POR QUE ISTO EXISTE (08/09/2026)
Medido no teste de exclusao do suplente: quando uma sessao e apagada pela
tela, o WAHA faz quase tudo certo — some com a sessao E derruba o banco de
credencial dela (`waha_{sns}_{sessao}`). Mas o PERFIL DO CHROMIUM fica:
`/app/.wwebjs_auth/RemoteAuth-<sessao>` sobrou com 88 MB dentro do container,
sem dono e sem ninguem para apaga-lo.

Como `docker compose up -d` nao recria container que ja existe, esse orfao
sobrevive a reinicios e so' sai quando alguem recria a instancia na mao. Numa
maquina com 3,8 GB de teto, e' desperdicio que se acumula.

=====================================================================
A REGRA QUE ESTE SCRIPT NAO PODE QUEBRAR (incidente 4.40, ERROS.md)
=====================================================================
Em 08/09/2026 eu apaguei o banco `waha_principal-pls` numa limpeza,
classificando-o como sobra porque o NOME parecia antigo — nao tinha o prefixo
`s-` dos bancos de credencial. Era o banco de CONFIGURACAO do WAHA, onde mora
a lista de sessoes. A API caiu na hora, com HTTP 500, e so' voltou porque
havia `pg_dumpall` de horas antes.

Dai as tres travas deste script:

  1. NADA e' apagado por PADRAO DE NOME. O que decide e' a comparacao com o
     estado REAL: pergunta-se ao WAHA quais sessoes existem, e so' sai o que
     nao aparece na resposta.
  2. O BANCO DE CONFIGURACAO (`waha_{WAHA_NAMESPACE}`) esta numa lista de
     protegidos e nao sai NUNCA, aconteca o que acontecer.
  3. ENSAIO E O PADRAO. Sem `--executar`, o script so' diz o que faria. Quem
     apaga tem de pedir.

E mais uma, aprendida em 05/08 e que vale aqui igual: se a pergunta ao WAHA
falhar, o script NAO apaga nada. "Nao consegui saber" nunca pode virar
"entao nao existe" — foi assim que uma faxina quase apagou uma sessao com
copia gravada.
"""
import argparse
import json
import os
import re
import subprocess
import sys

CONTAINER_PADRAO = 'auto-waha-pls'
PASTA_PERFIS = '/app/.wwebjs_auth'
PREFIXO_PERFIL = 'RemoteAuth-'


def rodar(*args, timeout=60):
    """Executa e devolve (codigo, saida). Nunca levanta."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except Exception as exc:
        return 1, str(exc)


def env_do_container(container, nome):
    """Le uma variavel de ambiente DO CONTAINER que esta de pe.

    Do container, e nao do `.env`: o arquivo guarda a intencao, o container
    guarda o que de fato esta valendo. Quando os dois divergem, quem manda e'
    o que esta rodando — e a divergencia e' justamente o caso em que uma
    faxina erra o alvo.
    """
    cod, saida = rodar('docker', 'inspect', container,
                       '--format', '{{range .Config.Env}}{{println .}}{{end}}')
    if cod != 0:
        return None
    for linha in saida.splitlines():
        if linha.startswith(nome + '='):
            return linha.split('=', 1)[1].strip()
    return None


def sessoes_vivas(container, chave):
    """Nomes das sessoes que o WAHA conhece. (lista, erro).

    `all=true` de proposito: sessao STOPPED existe e nao e' orfa. Perguntar
    so' pelas rodando faria a faxina apagar o perfil de uma sessao parada que
    ia voltar no proximo ciclo.
    """
    cod, saida = rodar('docker', 'exec', container, 'curl', '-s', '-m', '15',
                       '-H', 'X-Api-Key: ' + (chave or ''),
                       'http://localhost:3000/api/sessions?all=true')
    if cod != 0 or not saida.strip():
        return None, 'o WAHA nao respondeu'
    try:
        dados = json.loads(saida)
    except Exception:
        return None, 'resposta do WAHA nao e JSON: %s' % saida[:120]
    if not isinstance(dados, list):
        return None, 'resposta inesperada do WAHA: %s' % saida[:120]
    return [s.get('name') for s in dados if isinstance(s, dict) and s.get('name')], None


def perfis_em_disco(container):
    cod, saida = rodar('docker', 'exec', container, 'ls', PASTA_PERFIS)
    if cod != 0:
        return []
    return [n.strip() for n in saida.splitlines()
            if n.strip().startswith(PREFIXO_PERFIL)]


def tamanho(container, caminho):
    cod, saida = rodar('docker', 'exec', container, 'du', '-sh', caminho)
    return saida.split()[0] if cod == 0 and saida.split() else '?'


def bancos_waha(container_pg='auto-postgres', usuario='automation'):
    cod, saida = rodar('docker', 'exec', container_pg, 'psql', '-U', usuario,
                       '-d', 'postgres', '-tAc',
                       "select datname from pg_database where datname like 'waha%';")
    if cod != 0:
        return None
    return [b.strip() for b in saida.splitlines() if b.strip()]


def main():
    ap = argparse.ArgumentParser(description='Apaga perfis e bancos de sessoes que nao existem mais.')
    ap.add_argument('--container', default=CONTAINER_PADRAO)
    ap.add_argument('--executar', action='store_true',
                    help='apaga de verdade (sem isto, so mostra o que faria)')
    args = ap.parse_args()

    print('Faxina de orfaos — container %s' % args.container)
    print('modo: %s\n' % ('APAGANDO' if args.executar else 'ENSAIO (nada sera apagado)'))

    chave = env_do_container(args.container, 'WAHA_API_KEY')
    namespace = env_do_container(args.container, 'WAHA_NAMESPACE')
    sess_ns = env_do_container(args.container, 'WAHA_SESSION_NAMESPACE')
    if not namespace:
        print('  ERRO: nao consegui ler WAHA_NAMESPACE do container. Nada foi feito.')
        return 2

    # A LISTA DE INTOCAVEIS, montada do que esta rodando (incidente 4.40).
    protegidos = {'waha_%s' % namespace}
    print('  protegido (banco de configuracao): %s' % ', '.join(sorted(protegidos)))

    vivas, erro = sessoes_vivas(args.container, chave)
    if erro:
        print('  ERRO: %s' % erro)
        print('  NADA foi apagado: sem saber quais sessoes existem, tudo pareceria orfao.')
        return 2
    print('  sessoes vivas: %s\n' % (', '.join(vivas) if vivas else '(nenhuma)'))

    vivas_min = {v.lower() for v in vivas}
    removidos = 0

    # ---- perfis do Chromium ----
    print('PERFIS EM %s' % PASTA_PERFIS)
    for perfil in perfis_em_disco(args.container):
        dono = perfil[len(PREFIXO_PERFIL):]
        caminho = '%s/%s' % (PASTA_PERFIS, perfil)
        if dono.lower() in vivas_min:
            print('  [manter ] %-34s sessao "%s" existe' % (perfil, dono))
            continue
        tam = tamanho(args.container, caminho)
        if not args.executar:
            print('  [apagaria] %-33s %s — sem sessao "%s"' % (perfil, tam, dono))
            continue
        cod, saida = rodar('docker', 'exec', args.container, 'rm', '-rf', caminho)
        if cod == 0:
            print('  [APAGADO ] %-33s %s' % (perfil, tam))
            removidos += 1
        else:
            print('  [falhou  ] %-33s %s' % (perfil, saida[:60]))

    # ---- bancos de credencial ----
    print('\nBANCOS NO POSTGRES')
    bancos = bancos_waha()
    if bancos is None:
        print('  nao consegui listar os bancos — nada a fazer aqui.')
    else:
        prefixo = 'waha_%s_' % (sess_ns or '')
        for banco in sorted(bancos):
            if banco in protegidos:
                print('  [PROTEGIDO] %-38s banco de configuracao do WAHA' % banco)
                continue
            if not sess_ns or not banco.startswith(prefixo):
                # Nome que este script nao entende NAO e' apagado. Foi
                # exatamente assim que o `waha_principal-pls` morreu.
                print('  [manter  ] %-38s fora do padrao conhecido — nao mexo' % banco)
                continue
            dono = banco[len(prefixo):]
            if dono.lower() in vivas_min:
                print('  [manter  ] %-38s sessao "%s" existe' % (banco, dono))
                continue
            if not args.executar:
                print('  [apagaria] %-38s sem sessao "%s"' % (banco, dono))
                continue
            cod, saida = rodar('docker', 'exec', 'auto-postgres', 'psql', '-U',
                               'automation', '-d', 'postgres', '-c',
                               'DROP DATABASE IF EXISTS "%s";' % banco)
            if cod == 0:
                print('  [APAGADO ] %s' % banco)
                removidos += 1
            else:
                print('  [falhou  ] %-38s %s' % (banco, saida[:60]))

    print('\n%s' % ('%d item(ns) removido(s).' % removidos if args.executar
                    else 'Ensaio. Para apagar de verdade: --executar'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
