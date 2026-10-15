#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FAXINA DOS RESTOS DE SESSAO — bancos mortos, volumes sem dono, copias velhas.

POR QUE ISTO EXISTE (16/08/2026, pedido do dono)

Cada troca de nome ou de namespace deixa lixo para tras: em 16/08 havia SEIS
bancos `waha_*` sem uso (dois deles com um zip de 13 MB de sessao que nao existe
mais), dois volumes Docker orfaos e tres copias em arquivo de ate 33 MB. Nada
disso volta a ser usado — e o pareamento, que e quando a sessao e refeita do
zero, e a hora natural de recolher.

AS TRES TRAVAS QUE FAZEM ISSO SER SEGURO

  1. **Nunca encosta no que esta em uso.** O banco vivo de cada sessao e
     DESCOBERTO (`wahasessao.banco_da_sessao`, regra 15-B), e tudo que aparece
     em `docker-compose.yml`, no `.env` ou no ambiente de um container de pe
     fica de fora — foi assim que o `waha_suplente` escapou: ele parece morto
     (nao tem tabela `files`) e e o banco de conexao do container do suplente.
  2. **Nunca apaga copia de sessao viva.** Banco com arquivo gravado so entra
     na lista se a sessao dele NAO existir mais no WAHA.
  3. **ENSAIO por padrao.** Sem `--apagar` ele so lista. E o mesmo desenho do
     modo ensaio da pipeline: o pior desfecho de um engano tem de ser "nao fiz
     nada", nunca "apaguei o que nao devia".

USO
    faxina_sessao.py                 # so lista (ensaio)
    faxina_sessao.py --apagar        # recolhe de verdade
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wahasessao  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
COMPOSE = BASE / 'docker-compose.yml'
ENVFILE = BASE / '.env'


def _docker(*args, timeout=90):
    try:
        r = subprocess.run(['docker'] + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()
    except Exception as exc:
        return 1, str(exc)


def _psql(banco, sql):
    env = wahasessao.carregar_env()
    codigo, saida = _docker(
        'exec', env.get('POSTGRES_CONTAINER') or 'auto-postgres',
        'psql', '-U', env.get('POSTGRES_USER') or 'automation',
        '-d', banco, '-t', '-A', '-c', sql)
    return (saida if codigo == 0 else None), (None if codigo == 0 else saida)


def _texto_das_configuracoes():
    """Tudo que uma configuracao viva menciona — compose, .env e containers."""
    partes = []
    for arquivo in (COMPOSE, ENVFILE):
        try:
            partes.append(arquivo.read_text(encoding='utf-8', errors='ignore'))
        except Exception:
            pass
    codigo, saida = _docker('ps', '--format', '{{.Names}}')
    if codigo == 0:
        for nome in (saida or '').splitlines():
            _c, env = _docker('inspect', nome.strip(), '--format',
                              '{{range .Config.Env}}{{println .}}{{end}}')
            partes.append(env or '')
            _c, mounts = _docker('inspect', nome.strip(), '--format',
                                 '{{range .Mounts}}{{println .Source}}{{end}}')
            partes.append(mounts or '')
    return '\n'.join(partes)


def _sessoes_vivas():
    """Nome de cada sessao que existe hoje, em qualquer instancia."""
    env = wahasessao.carregar_env()
    vivas, bancos_vivos = set(), set()
    for porta_chave in _instancias(env):
        base, chave = porta_chave
        try:
            import urllib.request
            req = urllib.request.Request('%s/api/sessions?all=true' % base,
                                         headers={'X-Api-Key': chave})
            with urllib.request.urlopen(req, timeout=8) as resp:
                for item in json.loads(resp.read().decode('utf-8')):
                    nome = (item or {}).get('name')
                    if nome:
                        vivas.add(nome.lower())
                        banco, erro = wahasessao.banco_da_sessao(nome)
                        if banco and not erro:
                            bancos_vivos.add(banco.lower())
        except Exception as exc:
            print('   (nao consegui listar as sessoes de %s: %s)' % (base, exc))
    return vivas, bancos_vivos


def _instancias(env):
    """(base, chave) de cada instancia WAHA de pe, lida do proprio container."""
    saida = []
    codigo, nomes = _docker('ps', '--filter', 'name=waha', '--format', '{{.Names}}')
    if codigo != 0:
        return saida
    for nome in (nomes or '').splitlines():
        nome = nome.strip()
        if not nome:
            continue
        _c, portas = _docker('port', nome, '3000/tcp')
        porta = None
        for linha in (portas or '').splitlines():
            achado = re.search(r':(\d+)\s*$', linha.strip())
            if achado:
                porta = achado.group(1)
                break
        _c, ambiente = _docker('inspect', nome, '--format',
                               '{{range .Config.Env}}{{println .}}{{end}}')
        chave = ''
        for linha in (ambiente or '').splitlines():
            if linha.startswith('WAHA_API_KEY='):
                chave = linha.split('=', 1)[1].strip()
        if porta:
            saida.append(('http://localhost:%s' % porta, chave))
    return saida


def _da_cidade(nome, sufixo):
    """O nome carrega o sufixo da cidade como pedaco proprio? (18/09/2026)

    `principal-pls`, `waha_s-principal-pls_principal-pls`, `waha-sessions-
    principal-pls` -> sim para `pls`. Sem sufixo (uso antigo) -> tudo vale.
    """
    if not sufixo:
        return True
    return sufixo.lower() in re.split(r'[-_.]', (nome or '').lower())


def levantar(sufixo=None):
    """O que da para recolher. Devolve (bancos, volumes) — so os seguros.

    `sufixo` (18/09/2026, botao de faxina da cidade): so entra o que for da
    cidade daquele container. Sem ele, o comportamento de antes.
    """
    configuracoes = _texto_das_configuracoes()
    vivas, bancos_vivos = _sessoes_vivas()
    print('Sessoes que existem hoje: %s' % (', '.join(sorted(vivas)) or 'nenhuma'))
    print('Bancos em uso por elas: %s' % (', '.join(sorted(bancos_vivos)) or 'nenhum'))

    todos, erro = wahasessao.bancos_waha()
    if erro:
        print('Nao consegui listar os bancos: %s' % erro)
        return [], []

    # O BANCO DE CONFIGURACAO NUNCA SAI (15/09/2026 — o incidente 4.40 quase
    # se repetiu). O WAHA monta o nome `waha_<WAHA_NAMESPACE>`: ele nao aparece
    # escrito no compose nem no ambiente (la esta so `WAHA_NAMESPACE=principal-pls`),
    # entao a trava "aparece na configuracao" deixou de pega-lo quando o container
    # orfao que o citava saiu. Medido em 15/09 14:40: esta faxina listou
    # `waha_principal-pls` como "vazio" — e ele tem session_config, session_worker,
    # me e api_key. Com --apagar (e o pareamento chama com --apagar) teria
    # derrubado a lista de sessoes do WAHA. Mesma regra da `faxina_orfaos.py`.
    protegidos = set()
    codigo_ps, nomes_ps = _docker('ps', '--format', '{{.Names}}')
    if codigo_ps == 0:
        for nome_c in (nomes_ps or '').splitlines():
            _c, env_c = _docker('inspect', nome_c.strip(), '--format',
                                '{{range .Config.Env}}{{println .}}{{end}}')
            for linha_env in (env_c or '').splitlines():
                if linha_env.startswith('WAHA_NAMESPACE='):
                    ns = linha_env.split('=', 1)[1].strip()
                    if ns:
                        protegidos.add(('waha_%s' % ns).lower())

    bancos = []
    for banco in todos:
        if not _da_cidade(banco, sufixo):
            continue                       # outra cidade: nem olha
        if banco.lower() in bancos_vivos:
            continue                       # trava 1: esta em uso agora
        if banco.lower() in protegidos:
            print('   PROTEGIDO (banco de configuracao do WAHA): %s' % banco)
            continue
        if banco in configuracoes:
            print('   fica (aparece na configuracao): %s' % banco)
            continue                       # trava 1: alguem aponta para ele
        saida, _erro = _psql(banco, 'select count(*) from files;')
        quantos = int(saida) if (saida or '').strip().isdigit() else 0
        if quantos:
            # Tem copia. So sai se a sessao dele nao existir mais.
            dono = banco.lower().split('_')[-1]
            if dono in vivas:
                print('   fica (copia de sessao viva): %s' % banco)
                continue
            bancos.append((banco, '%d copia(s) de sessao que nao existe mais' % quantos))
        else:
            # SEM TABELA DE COPIAS NAO E VAZIO. Banco com qualquer outra tabela
            # e de alguem (configuracao, app, n8n): nao se apaga por deducao.
            outras, _e = _psql(banco, "select count(*) from information_schema.tables "
                                      "where table_schema='public' and table_name <> 'files';")
            if (outras or '').strip().isdigit() and int(outras) > 0:
                print('   fica (tem %s tabela(s) que nao sao copia): %s'
                      % (outras.strip(), banco))
                continue
            if outras is None:
                print('   fica (nao consegui ler as tabelas — na duvida, fica): %s' % banco)
                continue
            bancos.append((banco, 'vazio, sem tabela nenhuma'))

    volumes = []
    codigo, saida = _docker('volume', 'ls', '-qf', 'dangling=true')
    if codigo == 0:
        volumes = [v.strip() for v in (saida or '').splitlines()
                   if v.strip() and v.strip() not in configuracoes
                   and _da_cidade(v.strip(), sufixo)]
    return bancos, volumes


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apagar', action='store_true',
                   help='recolhe de verdade (sem isto, so lista)')
    p.add_argument('--sufixo', help='so o que for desta cidade (ex.: pls)')
    args = p.parse_args()

    bancos, volumes = levantar(args.sufixo)
    if not bancos and not volumes:
        print('Nada a recolher.')
        return 0

    print()
    for banco, motivo in bancos:
        print('BANCO   %-38s %s' % (banco, motivo))
    for volume in volumes:
        print('VOLUME  %-38s sem dono' % volume)

    if not args.apagar:
        print('\nEnsaio: nada foi apagado. Para recolher de verdade, '
              'rode de novo com --apagar')
        return 0

    for banco, _motivo in bancos:
        _saida, erro = _psql('postgres', 'DROP DATABASE IF EXISTS "%s";' % banco)
        print('apagado: %s%s' % (banco, (' (erro: %s)' % erro) if erro else ''))
    for volume in volumes:
        codigo, saida = _docker('volume', 'rm', volume)
        print('volume %s: %s' % (volume, 'removido' if codigo == 0 else saida))
    return 0


if __name__ == '__main__':
    sys.exit(main())
