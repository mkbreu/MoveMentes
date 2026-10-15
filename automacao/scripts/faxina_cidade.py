#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FAXINA DA CIDADE — o botao "Fazer a faxina" da tela Sessao WhatsApp.

18/09/2026 (plano A10 + item 5, criterio definido pelo dono):

  * sessao que LE o grupo ............................ fica, nao e tocada
  * sessao pareada ha < 10 min ......................... fica (esta firmando)
  * sessao conectada que NAO le (ou WORKING sem CONNECTED):
        com copia gravada -> reinicia UMA vez e confere a leitura em 2 min;
                             voltou a ler? fica. Nao voltou? SAI (quebrada);
        sem copia         -> SAI (reiniciar nao traz de volta)
  * sessao FAILED / STOPPED / SCAN_QR_CODE ............. SAI (sem vinculo vivo)
  * sessao STARTING .................................... espera ate 90 s; se
                                                         seguir subindo, fica
  * container parado -> sobe; API muda por 2 min -> recria o container (as
    sessoes voltam da copia no Postgres — foi o que trouxe a principal de
    volta em 17/09 23:40)
  * depois: perfis, bancos e volumes SEM DONO desta cidade saem

ALCANCE: so o container e os nomes com o sufixo DESTA cidade. Quem diz qual
cidade e o site (a cidade ativa de quem clicou); este script so obedece, e
recusa qualquer nome que nao carregue o sufixo.

USO
    faxina_cidade.py --do-site            # busca o pedido no site e responde
    faxina_cidade.py --container auto-waha-pls --ensaio
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests          # noqa: E402
import wahasessao        # noqa: E402
import prontidao         # noqa: E402
import faxina_sessao     # noqa: E402

BASE = Path(__file__).resolve().parent.parent


def _docker(*args, timeout=120):
    try:
        r = subprocess.run(['docker'] + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout or '').strip() or (r.stderr or '').strip()
    except Exception as exc:
        return 1, str(exc)[:200]


def _compose(*args, timeout=300):
    try:
        r = subprocess.run(['docker', 'compose'] + list(args), cwd=str(BASE),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()[-300:]
    except Exception as exc:
        return 1, str(exc)[:200]


def _da_cidade(nome, sufixo):
    return faxina_sessao._da_cidade(nome, sufixo)


def _endereco(container):
    """(base, chave, servico) do container, lidos do proprio Docker."""
    _c, portas = _docker('port', container, '3000/tcp')
    porta = None
    for linha in (portas or '').splitlines():
        achado = re.search(r':(\d+)\s*$', linha.strip())
        if achado:
            porta = achado.group(1)
            break
    _c, amb = _docker('inspect', container, '--format',
                      '{{range .Config.Env}}{{println .}}{{end}}')
    chave = ''
    for linha in (amb or '').splitlines():
        if linha.startswith('WAHA_API_KEY='):
            chave = linha.split('=', 1)[1].strip()
    _c, servico = _docker('inspect', container, '--format',
                          '{{index .Config.Labels "com.docker.compose.service"}}')
    return ('http://localhost:%s' % porta) if porta else None, chave, (servico or '').strip()


def _situacao(container):
    codigo, s = _docker('inspect', container, '--format', '{{.State.Status}}')
    # Container inexistente: o Docker devolve erro (codigo != 0) com o texto
    # "no such object" — isso NAO e um estado (achado no ensaio de 18/09).
    return (s or '').strip() if codigo == 0 else ''


def _esperar_api(base, chave, limite):
    fim = time.time() + limite
    while time.time() < fim:
        if base and wahasessao.listar(base, chave) is not None:
            return True
        time.sleep(5)
    return False


def faxinar(container, sufixo, ensaio=False, log=print):
    """Faz a faxina. Devolve a lista de linhas do que aconteceu."""
    linhas = []

    def conta(texto):
        linhas.append(texto)
        log('   faxina: ' + texto)

    if not container or not sufixo or not _da_cidade(container, sufixo):
        conta('RECUSADA: container "%s" nao e da cidade "%s".' % (container, sufixo))
        return linhas
    grupo = wahasessao.carregar_env().get('WHATSAPP_GROUP_ID', '')

    # ---- 1. O CONTAINER ----
    situacao = _situacao(container)
    if not situacao:
        conta('container %s nao existe nesta maquina — nada a fazer.' % container)
        return linhas
    base, chave, servico = _endereco(container)
    if situacao != 'running':
        conta('container %s estava %s — subindo.' % (container, situacao))
        if not ensaio:
            _compose('up', '-d', servico or 'waha')
            base, chave, servico = _endereco(container)
    if not _esperar_api(base, chave, 120 if not ensaio else 5):
        # TEMPO SOZINHO NAO E PROVA (18/09/2026, E-F — regra do dono). Com o
        # Synology/Dell lentos a API demora e o container esta bom. So recria
        # com prova do proprio Docker: healthcheck "unhealthy", reiniciando em
        # laco, ou parado depois de mandar subir. Depois, quem valida e a
        # LEITURA DO GRUPO de cada sessao (passo 2, com o reinicio unico).
        _c, saude = _docker('inspect', container, '--format',
                            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.RestartCount}}')
        partes = (saude or '').split()
        situ = partes[0] if partes else ''
        hc = partes[1] if len(partes) > 2 else ''
        try:
            reinicios = int(partes[-1])
        except (ValueError, IndexError):
            reinicios = 0
        provas = []
        if hc == 'unhealthy':
            provas.append('verificacao de saude do Docker: unhealthy')
        if situ in ('restarting', 'exited', 'dead'):
            provas.append('container %s' % situ)
        if reinicios >= 3:
            provas.append('%d reinicios' % reinicios)
        if not provas:
            conta('container %s nao respondeu em 2 min, mas o Docker diz que esta '
                  '%s%s — sem prova de defeito, NAO recriei (lentidao da maquina?). '
                  'Nada foi apagado.' % (container, situ or '?', (' / ' + hc) if hc else ''))
            return linhas
        # REINICIO LIMPO ANTES DE RECRIAR (19/09/2026, regra do dono): se um
        # `docker restart` (stop com 30 s, que deixa a copia ser gravada)
        # resolve, o container nao era o problema de fato.
        conta('container %s com defeito (%s) — primeiro um reinicio com parada '
              'limpa.' % (container, ', '.join(provas)))
        if not ensaio:
            _docker('restart', '-t', '30', container, timeout=180)
            base, chave, servico = _endereco(container)
            if _esperar_api(base, chave, 150):
                conta('o reinicio resolveu — container NAO recriado.')
                provas = []
        if provas:
            conta('o reinicio nao resolveu: container %s QUEBRADO — recriando '
                  '(as sessoes voltam da copia no Postgres).' % container)
        if provas and not ensaio:
            codigo, saida = _compose('up', '-d', '--force-recreate', servico or 'waha')
            conta('recriar: %s' % ('ok' if codigo == 0 else 'falhou (%s)' % saida))
            base, chave, servico = _endereco(container)
            if not _esperar_api(base, chave, 150):
                conta('mesmo recriado, o WhatsApp nao respondeu. Parei aqui — '
                      'nada foi apagado.')
                return linhas
        elif ensaio:
            return linhas
        # reinicio resolveu: segue para conferir as sessoes (passo 2)
    else:
        conta('container %s de pe e respondendo.' % container)

    # ---- 2. AS SESSOES ----
    sessoes = wahasessao.listar(base, chave) or []
    if not sessoes:
        conta('nenhuma sessao no container.')
    apagadas = []
    for item in sessoes:
        nome = item.get('name') or ''
        if not _da_cidade(nome, sufixo):
            conta('sessao "%s": nao e desta cidade — nao toco.' % nome)
            continue
        status, motor = wahasessao.estado_de(item)
        if status == 'WORKING' and not motor:
            _, motor = wahasessao.estado_de(wahasessao.detalhar(base, chave, nome) or {})
        if status == 'STARTING':
            for _ in range(9):
                time.sleep(10)
                status, motor = wahasessao.estado_de(wahasessao.detalhar(base, chave, nome) or {})
                if status != 'STARTING':
                    break
            if status == 'STARTING':
                conta('sessao "%s": ainda subindo depois de 90 s — fica.' % nome)
                continue
        ha = wahasessao.pareada_ha(nome)
        if ha is not None and 0 <= ha < 600:
            conta('sessao "%s": pareada ha %d min — fica (esta firmando).' % (nome, ha // 60))
            continue

        if status == 'WORKING':
            if wahasessao.pronta(status, motor) and prontidao._etapa_grupo(base, chave, nome, grupo)['ok'] is not False:
                conta('sessao "%s": le o grupo — fica.' % nome)
                continue
            arquivos, _q, _e = wahasessao.copia_da_sessao(nome)
            if arquivos:
                conta('sessao "%s": conectada mas nao le — reiniciando uma vez '
                      '(tem copia gravada).' % nome)
                if ensaio:
                    continue
                prontidao._reiniciar_sessao(base, chave, nome)
                time.sleep(120)
                st2, mo2 = wahasessao.estado_de(wahasessao.detalhar(base, chave, nome) or {})
                if wahasessao.pronta(st2, mo2) and prontidao._etapa_grupo(base, chave, nome, grupo)['ok'] is not False:
                    conta('sessao "%s": VOLTOU a ler depois do reinicio — fica.' % nome)
                    continue
                conta('sessao "%s": o reinicio nao trouxe a leitura (%s/%s) — '
                      'QUEBRADA, sai.' % (nome, st2 or '?', mo2 or '?'))
            else:
                conta('sessao "%s": conectada mas nao le, e SEM copia — '
                      'reiniciar nao traz de volta: sai.' % nome)
        elif status in ('FAILED', 'STOPPED', 'SCAN_QR_CODE'):
            conta('sessao "%s": %s, sem vinculo vivo — sai.' % (nome, status))
        else:
            conta('sessao "%s": estado %s/%s que eu nao conheco — fica.' % (nome, status, motor))
            continue

        if ensaio:
            continue
        try:
            r = requests.delete('%s/api/sessions/%s' % (base, nome),
                                headers={'X-Api-Key': chave} if chave else {}, timeout=60)
            ok = r.status_code < 400 or r.status_code == 404
            conta('sessao "%s": %s.' % (nome, 'apagada' if ok else 'o WhatsApp recusou apagar (HTTP %s)' % r.status_code))
            if ok:
                apagadas.append(nome)
        except Exception as exc:
            conta('sessao "%s": nao consegui apagar (%s).' % (nome, str(exc)[:80]))

    # ---- 3. PERFIS SEM DONO DENTRO DO CONTAINER ----
    vivas = {(i.get('name') or '').lower() for i in (wahasessao.listar(base, chave) or [])}
    _c, dirs = _docker('exec', container, 'ls', '/app/.wwebjs_auth/')
    for d in (dirs or '').split():
        m = re.match(r'^(RemoteAuth-|wwebjs_temp_session_)(.+)$', d)
        if not m or not _da_cidade(m.group(2), sufixo):
            continue
        dono = m.group(2).lower()
        if dono in vivas and d.startswith('RemoteAuth-'):
            continue                       # perfil de sessao viva: fica
        if dono in vivas and d.startswith('wwebjs_temp_session_'):
            continue                       # pode ser backup em curso: fica
        conta('perfil sem dono: %s — %s.' % (d, 'sairia' if ensaio else 'apagado'))
        if not ensaio:
            _docker('exec', container, 'rm', '-rf', '/app/.wwebjs_auth/%s' % d)

    # ---- 4. BANCOS E VOLUMES SEM DONO DESTA CIDADE ----
    bancos, volumes = faxina_sessao.levantar(sufixo)
    for banco, motivo in bancos:
        if ensaio:
            conta('banco %s sairia (%s).' % (banco, motivo))
            continue
        _s, erro = faxina_sessao._psql('postgres', 'DROP DATABASE IF EXISTS "%s";' % banco)
        conta('banco %s: %s.' % (banco, 'apagado' if not erro else 'erro %s' % erro))
    for vol in volumes:
        if ensaio:
            conta('volume %s sairia (sem dono).' % vol)
            continue
        codigo, saida = _docker('volume', 'rm', vol)
        conta('volume %s: %s.' % (vol, 'apagado' if codigo == 0 else saida[:80]))

    if apagadas:
        conta('para usar de novo: %s — clique em Conectar.' % ', '.join(apagadas))
    conta('faxina concluida%s.' % (' (ENSAIO: nada foi apagado)' if ensaio else ''))
    return linhas


def limpar_copias(copias, sufixo, log=print):
    """Apaga as copias EM ARQUIVO de sessao refeita (19/09/2026, item 62).

    So `copias-sessao/RemoteAuth-<sessao>_*.zip` da sessao pedida, so se o nome
    da sessao carregar o sufixo desta cidade, e so os arquivos MAIS ANTIGOS que
    o "Refazer a conexao" — a copia nova da mesma sessao (mesmo nome) fica.
    """
    from datetime import datetime
    pasta = BASE / 'copias-sessao'
    linhas = []
    for item in copias or []:
        sessao = (item.get('sessao') or '').strip()
        if not sessao or not sufixo or not _da_cidade(sessao, sufixo):
            linhas.append('copias de "%s": nao e desta cidade — nao toco.' % sessao)
            continue
        try:
            corte = datetime.fromisoformat(item.get('antes_de')).timestamp()
        except Exception:
            linhas.append('copias de "%s": pedido sem data — nao toco.' % sessao)
            continue
        for arq in sorted(pasta.glob('RemoteAuth-%s_*.zip' % sessao)):
            if arq.stat().st_mtime < corte:
                arq.unlink()
                linhas.append('copia antiga de %s apagada: %s' % (sessao, arq.name))
    for l in linhas:
        log('   faxina: ' + l)
    return linhas


def _do_site():
    env = wahasessao.carregar_env()
    api = (env.get('MOVIMENTE_API_URL') or env.get('API_URL') or '').rstrip('/')
    token = env.get('MOVIMENTE_API_TOKEN') or ''
    cab = {'X-Automation-Token': token}
    try:
        r = requests.post('%s/api/faxina/pendente' % api, headers=cab, timeout=30)
        resp = (r.json() or {}) if r.status_code == 200 else {}
        pedido = resp.get('faxina')
    except Exception as exc:
        print('faxina: nao consegui perguntar ao site (%s)' % exc)
        return 0
    if resp.get('copias'):
        limpar_copias(resp.get('copias'), resp.get('sufixo'))
    if not pedido:
        print('faxina: nenhum pedido.')
        return 0
    print('faxina pedida por %s em %s: container %s' % (pedido.get('por'), pedido.get('em'), pedido.get('container')))
    try:
        linhas = faxinar(pedido.get('container'), pedido.get('sufixo'))
    except Exception as exc:
        linhas = ['a faxina parou com erro: %s' % str(exc)[:200]]
        print(linhas[0])
    try:
        requests.post('%s/api/faxina/resultado' % api, headers=cab,
                      json={'linhas': linhas}, timeout=30)
    except Exception as exc:
        print('faxina: nao consegui contar o resultado ao site (%s)' % exc)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--do-site', action='store_true')
    p.add_argument('--container')
    p.add_argument('--ensaio', action='store_true')
    a = p.parse_args()
    if a.do_site:
        return _do_site()
    if not a.container:
        p.error('informe --do-site ou --container')
        return 2
    suf = a.container.split('auto-waha-', 1)[-1]
    faxinar(a.container, suf, ensaio=a.ensaio)
    return 0


if __name__ == '__main__':
    sys.exit(main())
