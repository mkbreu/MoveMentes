"""Conta ao site quais instancias WAHA esta maquina tem de fato.

POR QUE ISTO EXISTE (14/08/2026)

O site precisava falar com o WAHA para tres coisas — entregar a fila na hora,
vigiar se a automacao esta prestando e mostrar a tela de sessao — e o endereco,
a porta e a chave eram DIGITADOS na tela, um par por cidade. So que a cidade
tem DUAS instancias, com chaves diferentes:

    auto-waha-principal-pls  -> porta 3000, chave A
    auto-waha-suplente-pls   -> porta 3001, chave B

Com a chave da outra, o WAHA responde HTTP 401. Foi o que aconteceu neste dia:
o site concluiu "automacao quebrada" e mandou e-mail de 15 em 15 minutos sobre
uma sessao que estava WORKING + CONNECTED, publicando normalmente. E, sem
conseguir listar as sessoes, o site tambem nao descobria o nome real
(`principal-pls`): caia no slug da cidade e o botao Conectar pedia uma
instancia NOVA (`principal-palmeiropolis`, porta 3001) em cima de uma que ja
existia — que e a P-64 inteira, nascida de um 401.

Quem sabe a verdade sobre container, porta, chave e sessao e ESTA maquina.
Entao ela conta, e o site para de adivinhar.

SOBRE MANDAR A CHAVE
O `provisionar.py` dizia "a chave fica aqui e nao volta para o site". Aquilo
valia quando so o agente falava com o WAHA. Hoje o site tambem fala, e chave
que o site precisa e nao tem vira campo digitado a mao — que foi exatamente o
defeito. Ela vai pelo mesmo HTTPS autenticado que ja leva o token da automacao
e mora no cofre, que nunca devolve valor para a tela.

Uso:
    ./.venv/bin/python scripts/registrar_instancias.py          # conta ao site
    ./.venv/bin/python scripts/registrar_instancias.py --ver    # so mostra
"""
import io
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
ENV = BASE / '.env'

log = logging.getLogger('registrar_instancias')

PREFIXO = 'auto-waha-'
TIMEOUT = 8


def carregar_env():
    """Le o .env sem quebrar em senha com '@' — ver COMANDOS.md secao 6."""
    env = {}
    if not ENV.exists():
        return env
    for linha in ENV.read_text(encoding='utf-8').splitlines():
        linha = linha.strip()
        if linha and not linha.startswith('#') and '=' in linha:
            chave, valor = linha.split('=', 1)
            env[chave.strip()] = valor.strip().strip('"').strip("'")
    return env


CFG = carregar_env()
API_URL = CFG.get('MOVIMENTE_API_URL', 'https://movementes.engekon.com').rstrip('/')
API_TOKEN = CFG.get('MOVIMENTE_API_TOKEN', '')
# O ID DO GRUPO VAI JUNTO (15/08/2026). Sem ele o site nao consegue entregar
# mensagem de grupo por conta propria — "grupo" e uma palavra, nao um destino —
# e todo envio ao grupo ficava esperando o ciclo da maquina, ate 15 minutos
# depois do clique do admin.
GRUPO = CFG.get('WHATSAPP_GROUP_ID', '')


def docker(*args, timeout=30):
    try:
        r = subprocess.run(['docker'] + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()
    except Exception as exc:
        return 1, str(exc)


def ip_do_windows():
    """O IP da MAQUINA na rede local -- nao o do WSL, e nao um IP morto.

    O site mora no Synology e alcanca o WAHA pela ponte do Windows
    (portproxy 0.0.0.0:3000). O `hostname -I` daqui devolve 172.26.x.x, que e
    o endereco interno do WSL e nao serve para ninguem de fora.

    ===== POR QUE NAO BASTA "O PRIMEIRO IP QUE APARECER" (08/09/2026) =====
    Ate hoje esta funcao devolvia a primeira placa da lista. Isso funcionou
    enquanto havia uma so ligada. Em 08/09 o switch da casa quebrou: a
    Ethernet do Dell ficou com IP configurado mas SEM rede (192.168.X.A) e
    a maquina passou a falar pelo Wi-Fi (192.168.X.B). A funcao continuou
    anunciando o .246 ao site -- um endereco que nao responde a ninguem.
    O estrago foi silencioso e total: o site marcou as instancias como
    inalcancaveis, a pipeline nao achou a API, e o tunel de outro servico passou a
    devolver 502.

    A CORRECAO: perguntar qual placa tem GATEWAY PADRAO. Placa sem gateway nao
    fala com o roteador, entao nao adianta anunciar o IP dela. Assim vale para
    cabo ou Wi-Fi, e volta sozinho ao normal quando o cabo for religado.
    """
    consultas = [
        # 1) A placa que realmente fala com o roteador (tem gateway padrao).
        "(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -ne $null "
        "-and $_.IPv4Address.IPAddress -notlike '172.*' } "
        "| Select-Object -First 1).IPv4Address.IPAddress",
        # 2) Rede antiga: se a consulta acima nao existir nesta versao do
        #    Windows, cai no comportamento de antes -- melhor um palpite que
        #    nenhum endereco.
        "(Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object {$_.PrefixOrigin -ne 'WellKnown' -and "
        "$_.IPAddress -notlike '172.*' -and "
        "$_.IPAddress -notlike '127.*'}).IPAddress",
    ]
    for i, consulta in enumerate(consultas):
        try:
            saida = subprocess.run(
                ['powershell.exe', '-NoProfile', '-Command', consulta],
                capture_output=True, text=True, timeout=20).stdout
        except Exception as exc:
            log.warning('nao consegui perguntar o IP ao Windows: %s', exc)
            continue
        for linha in (saida or '').splitlines():
            ip = linha.strip()
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                if i:
                    log.warning('IP do Windows achado pelo caminho antigo (%s) '
                                '-- confira se a placa certa esta ativa.', ip)
                return ip
    log.error('nenhuma placa do Windows com gateway padrao — o site nao vai '
              'conseguir alcancar o WAHA.')
    return None


def porta_publicada(nome):
    """A porta que o container publica no host. None quando nao publica nada."""
    codigo, saida = docker('port', nome, '3000')
    if codigo != 0 or not saida:
        return None
    for linha in saida.splitlines():
        achado = re.search(r':(\d+)\s*$', linha.strip())
        if achado:
            return int(achado.group(1))
    return None


def chave_do_container(nome):
    codigo, saida = docker(
        'inspect', nome, '--format', '{{range .Config.Env}}{{println .}}{{end}}')
    if codigo != 0:
        return None
    for linha in saida.splitlines():
        if linha.startswith('WAHA_API_KEY='):
            return linha.split('=', 1)[1].strip()
    return None


def papel_da_sessao(nome):
    """principal / suplente / reserva-N, lido do NOME DA SESSAO.

    ANTES O PAPEL VINHA DO NOME DO CONTAINER (ate 08/09/2026), e isso so
    funcionava enquanto valia "um container por NUMERO": de
    `auto-waha-principal-pls` saia `principal`. Com a fusao (um container por
    CIDADE, uma sessao por numero dentro dele) o container passou a hospedar
    os dois papeis, e o nome dele nao identifica mais ninguem.

    Quem carrega o papel agora e a SESSAO -- `principal-pls`, `suplente-pls`.
    Continua valendo a regra de ouro: o nome vem do que o WAHA respondeu, nao
    e' montado aqui.
    """
    n = (nome or '').strip().lower()
    if n.startswith('principal'):
        return 'principal'
    if n.startswith('suplente'):
        return 'suplente'
    if n.startswith('reserva'):
        return n.split('-', 1)[0]
    return None


def sessoes_da_instancia(porta, chave):
    """TODAS as sessoes daquela instancia, uma entrada por sessao.

    Era `sessao_da_instancia`, no singular, e devolvia so uma -- a WORKING, ou
    a primeira. Isso bastava quando cada container tinha uma sessao so. Depois
    da fusao, escolher uma seria APAGAR A OUTRA do relato: o site so enxerga o
    que chega aqui, e a sessao nao relatada vira uma sessao que "nao existe".

    A REGRA DE OURO CONTINUA: para ACHAR a sessao, pergunte e use a que voltar
    -- nunca monte o nome (DICIONARIO secao 1).
    """
    cab = {'X-Api-Key': chave} if chave else {}
    try:
        resp = requests.get('http://localhost:%d/api/sessions' % porta,
                            params={'all': 'true'}, headers=cab, timeout=TIMEOUT)
        if resp.status_code != 200:
            return [], 'HTTP %s' % resp.status_code
        sessoes = resp.json() or []
    except Exception as exc:
        return [], 'sem resposta (%s)' % str(exc)[:40]
    if not sessoes:
        return [], 'SEM_SESSAO'

    achadas = []
    for s in sessoes:
        if not isinstance(s, dict) or not s.get('name'):
            continue
        nome = s.get('name')
        numero = ((s.get('me') or {}).get('id') or '').split('@')[0]

        # O MOTOR NAO VEM NA LISTA (medido em 06/08): perguntar por sessao e
        # obrigatorio, senao toda sessao saudavel parece quebrada.
        motor = None
        try:
            detalhe = requests.get(
                'http://localhost:%d/api/sessions/%s' % (porta, nome),
                headers=cab, timeout=TIMEOUT)
            if detalhe.status_code == 200:
                motor = ((detalhe.json() or {}).get('engine') or {}).get('state')
        except Exception as exc:
            log.debug('sem motor para %s: %s', nome, exc)

        achadas.append({
            'sessao': nome,
            'papel': papel_da_sessao(nome),
            'status': s.get('status'),
            'motor': motor,
            'numero': numero or None,
        })
    return achadas, None


def alcancavel_de_fora(ip, porta):
    """O site consegue chegar nesta porta? Sem ponte do Windows, nao.

    A 3001 nao tem portproxy (P-73): de dentro do WSL ela responde, de fora
    nao. Dizer ao site que ela esta la faria a tela oferecer uma instancia que
    ele nunca alcanca — e "nao sei" e melhor que uma promessa falsa.
    """
    if not ip:
        return False
    try:
        resp = requests.get('http://%s:%d/ping' % (ip, porta), timeout=4)
        return resp.status_code < 500
    except Exception:
        return False


def coletar():
    """Um item POR SESSAO, nao por container (08/09/2026).

    Antes era um item por container, com o papel deduzido do nome dele. Depois
    da fusao um container hospeda os dois numeros, entao um item por container
    esconderia metade da verdade -- e o site so sabe o que chega daqui.

    Container SEM sessao nao vira item: o site recusaria a linha (nao da para
    identificar de quem e') e a recusa entraria como aviso a cada rodada.
    Fica registrado aqui no log, que e' onde alguem vai procurar.
    """
    codigo, saida = docker('ps', '-a', '--filter', 'name=%s' % PREFIXO,
                           '--format', '{{.Names}}')
    if codigo != 0:
        log.error('nao consegui listar os containers: %s', saida[:200])
        return None, []
    ip = ip_do_windows()
    itens = []
    for nome in sorted(n.strip() for n in saida.splitlines() if n.strip()):
        porta = porta_publicada(nome)
        chave = chave_do_container(nome)
        if not porta:
            log.warning('container %s nao publica porta — nao da para perguntar '
                        'as sessoes dele; nada relatado.', nome)
            continue
        alcanca = alcancavel_de_fora(ip, porta)
        sessoes, motivo = sessoes_da_instancia(porta, chave)
        if not sessoes:
            # INSTANCIA SEM SESSAO CONTINUA SENDO A CASA DA CIDADE (15/09/2026).
            # Isto pulava o container, e o site nunca reaprendia onde a cidade
            # mora: em 14/09 23:26 o dono limpou a unica sessao, o registro do
            # site foi junto, e o Conectar seguinte ficou "ainda nao sei em que
            # instancia a cidade roda o WhatsApp" com o container de pe.
            # O site exige sessao para identificar a linha; vai a PREVISTA do
            # .env, com estado SEM_SESSAO — nunca inventada como conectada.
            prevista = (CFG.get('WAHA_SESSION') or '').strip()
            if motivo == 'SEM_SESSAO' and prevista and chave:
                itens.append({'container': nome, 'papel': 'principal',
                              'porta': porta, 'chave': chave, 'sessao': prevista,
                              'status': 'SEM_SESSAO', 'motor': None,
                              'numero': None, 'host': ip, 'alcancavel': alcanca})
                log.info('container %s (porta %s): sem sessao — relatado como '
                         'instancia vazia (%s)', nome, porta, prevista)
                continue
            log.warning('container %s (porta %s) sem sessao para relatar (%s) — '
                        'container sem sessao so gasta memoria; considere remove-lo.',
                        nome, porta, motivo or 'motivo desconhecido')
            continue
        for s in sessoes:
            itens.append({
                'container': nome,
                'papel': s['papel'],
                'porta': porta,
                'chave': chave,
                'sessao': s['sessao'],
                'status': s['status'],
                'motor': s['motor'],
                'numero': s['numero'],
                'host': ip,
                'alcancavel': alcanca,
            })
        log.info('container %s (porta %s): %d sessao(oes) relatada(s) — %s',
                 nome, porta, len(sessoes),
                 ', '.join('%s/%s' % (x['sessao'], x['status']) for x in sessoes))
    return ip, itens


def reconciliar_registro(itens):
    """Poe o `.instancias.json` de acordo com o que o Docker acabou de dizer.

    POR QUE ISTO EXISTE (13/09/2026)
    O arquivo e escrito pelo `provisionar.py` com a porta que foi PEDIDA, e
    nunca mais conferido. Em 12/09 o container `auto-waha-principal-pls` nasceu
    na 3001 porque a 3000 estava ocupada, e o registro ficou dizendo que ele era
    da 3000 — a mesma armadilha de 16/08, quando o arquivo passou a mentir sobre
    a 3001 e todo pareamento do suplente levaria HTTP 401.

    Isto importa mais desde 13/09, porque a busca da instancia
    (`wahasessao._instancias_registradas`) passou a ler este arquivo para achar
    o WAHA quando o `.env` aponta para o lugar errado.

    AS TRAVAS, na linha da regra 15-A e do incidente de 05/08:
      * so roda com medicao NA MAO (`itens` cheio). Lista vazia e "nao consegui
        perguntar", e "nao sei" nunca vira "entao nao existe";
      * o que ja estava na linha e PRESERVADO (cidade, sufixo, painel — que so
        o provisionamento conhece); troca-se o que foi medido;
      * linha de porta velha so sai quando o container dela foi visto AGORA em
        OUTRA porta. Container que nao apareceu na medicao nao e tocado.
    """
    if not itens:
        return
    caminho = BASE / '.instancias.json'
    try:
        with io.open(str(caminho), encoding='utf-8') as arquivo:
            reg = json.load(arquivo)
    except Exception:
        reg = {}
    if not isinstance(reg, dict):
        reg = {}

    antes = json.dumps(reg, sort_keys=True)
    vistos = {}
    for item in itens:
        porta = str(item.get('porta') or '')
        nome = item.get('container') or ''
        if not porta or not nome:
            continue
        vistos[nome] = porta
        linha = dict(reg.get(porta) or {})
        linha.update({'api_key': item.get('chave') or linha.get('api_key') or '',
                      'container': nome,
                      'sessao': item.get('sessao') or linha.get('sessao'),
                      'papel': item.get('papel') or linha.get('papel')})
        linha.setdefault('sufixo', (item.get('sessao') or ''))
        linha.setdefault('painel', 'adm%s' % (linha.get('sufixo') or ''))
        reg[porta] = linha

    for porta in list(reg.keys()):
        nome = (reg.get(porta) or {}).get('container') or ''
        if nome in vistos and vistos[nome] != porta:
            log.warning('registro dizia que %s era da porta %s; ele esta na %s '
                        '— corrigido.', nome, porta, vistos[nome])
            reg.pop(porta, None)

    if json.dumps(reg, sort_keys=True) == antes:
        return
    try:
        with io.open(str(caminho), 'w', encoding='utf-8') as arquivo:
            json.dump(reg, arquivo, indent=2, ensure_ascii=False)
        log.info('registro local reconciliado com o Docker (%d porta(s)).', len(reg))
    except Exception as exc:
        log.warning('nao consegui gravar o registro local: %s', exc)


def _ip_do_wsl():
    """Primeiro IP do WSL (`hostname -I`), ou '' — so exibicao na tela."""
    try:
        import subprocess
        saida = subprocess.run(['hostname', '-I'], capture_output=True,
                               text=True, timeout=5).stdout.split()
        return saida[0] if saida else ''
    except Exception:
        return ''


def enviar(ip, itens):
    if not API_TOKEN:
        log.error('sem MOVIMENTE_API_TOKEN no .env — nao ha como contar ao site')
        return False
    try:
        resp = requests.post(
            '%s/api/instancias' % API_URL,
            headers={'X-Automation-Token': API_TOKEN,
                     'Content-Type': 'application/json'},
            json={'host': ip, 'instancias': itens, 'grupo': GRUPO,
                  # IP do WSL para o topo da tela por camada (18/09/2026, A3).
                  'wsl_ip': _ip_do_wsl()}, timeout=20)
    except Exception as exc:
        log.error('nao consegui falar com o site: %s', exc)
        return False
    if resp.status_code != 200:
        log.error('o site recusou (HTTP %s): %s', resp.status_code, resp.text[:200])
        return False
    dados = resp.json() or {}
    for aviso in dados.get('avisos') or []:
        log.info('   site: %s', aviso)
    log.info('%s instancia(s) registradas no site', dados.get('gravadas'))
    return True


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    # LOG CANONICO (17/09/2026, item D1): cada linha deste script cai tambem
    # no livro semanal comum a Windows, WSL e site. Ver scripts/logcanon.py.
    try:
        import logcanon
        logcanon.ligar('SESSAO', 'registrar_instancias')
    except Exception:
        pass
    so_ver = '--ver' in sys.argv

    ip, itens = coletar()
    if itens is None:
        return 1
    for item in itens:
        log.info('%-28s papel=%-9s porta=%-5s sessao=%-16s %s/%s%s',
                 item['container'], item['papel'] or '?', item['porta'] or '-',
                 item['sessao'] or '-', item['status'] or '-',
                 item['motor'] or '-',
                 '' if item['alcancavel'] else '   [o site NAO alcanca esta porta]')
    if not itens:
        log.warning('nenhum container %s* nesta maquina', PREFIXO)
        return 0
    if so_ver:
        log.info(json.dumps([{k: ('***' if k == 'chave' and v else v)
                              for k, v in i.items()} for i in itens],
                            indent=2, ensure_ascii=False))
        return 0
    # O REGISTRO LOCAL TEM DE CONTAR A MESMA VERDADE QUE VAI PARA O SITE.
    reconciliar_registro(itens)
    return 0 if enviar(ip, itens) else 1


if __name__ == '__main__':
    sys.exit(main())
