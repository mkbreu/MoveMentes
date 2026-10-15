#!/usr/bin/env python3
"""Quem e a sessao viva desta instancia do WAHA — descoberta pelo ESTADO.

A REGRA DE OURO (docs/DICIONARIO.md secao 1):

    A instancia e o endereco. O nome da sessao e so um rotulo.

Cada instancia WAHA tem UM worker e UMA sessao. Se so existe uma, procura-la
pelo nome nao acrescenta nada — so cria a chance de errar o nome. E foi
exatamente assim que o ranking nao saiu em 02 e 03/08/2026:

    02/08 22:00        procurava principal-pls   existia default   nao saiu
    02/08 22:50        procurava principal-pls   existia default   travou 150s
    03/08 00:14-01:05  procurava principal-pls   existia default   HTTP 422 (6x)

Este modulo e o UNICO lugar que decide qual sessao usar. Todo o resto
(executar.sh, send_ranking.py, conectar_whatsapp.sh) pergunta aqui. Assim as
pecas nao tem como divergir entre si — que era a causa raiz.

O nome do .env continua servindo para duas coisas, e so para elas:
  1. CRIAR uma sessao quando a instancia esta vazia;
  2. desempatar, quando por algum motivo existir mais de uma no mesmo estado.

Uso pela linha de comando (e o que os .sh chamam):

    python3 scripts/wahasessao.py
        imprime  nome|status|estado|quantas
        codigo 0 = WORKING e CONNECTED (pronta de verdade)
        codigo 1 = existe alguma, mas nao esta pronta
        codigo 2 = a instancia nao respondeu
        codigo 3 = a instancia respondeu e nao tem sessao nenhuma

    python3 scripts/wahasessao.py --nome      so o nome, para usar em URL
    python3 scripts/wahasessao.py --base http://localhost:3001 --chave XYZ
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / '.env'

# Ordem de preferencia. O primeiro grupo que tiver alguem, ganha.
# WORKING sozinho NAO e "pronta" (o motor WEBJS pode estar em UNLAUNCHED, e ai
# ler mensagem devolve 500) — mas ja e a sessao certa para trabalhar, entao ela
# ganha de qualquer outra. Quem espera o CONNECTED e quem chamou.
_PREFERENCIA = (
    lambda status, estado: status == 'WORKING' and estado == 'CONNECTED',
    lambda status, estado: status == 'WORKING',
    lambda status, estado: status in ('STARTING', 'SCAN_QR_CODE'),
    lambda status, estado: status in ('STOPPED', 'FAILED'),
)


def carregar_env(caminho=None):
    """Le o .env sem depender de biblioteca. Corta no PRIMEIRO '='.

    Cortar no primeiro e o que importa: a senha do Postgres tem '@' e '=' no
    meio, e um parser ingenuo (cut -d= -f2) devolve a chave truncada. Isso ja
    custou uma tarde de 401 no WAHA.
    """
    env = {}
    caminho = Path(caminho or ENV_PATH)
    if not caminho.exists():
        return env
    for linha in caminho.read_text(encoding='utf-8', errors='replace').splitlines():
        linha = linha.strip()
        if not linha or linha.startswith('#') or '=' not in linha:
            continue
        if linha.startswith('export '):
            linha = linha[7:]
        chave, _, valor = linha.partition('=')
        env[chave.strip()] = valor.strip().strip('"').strip("'")
    return env


def _cabecalho(chave):
    return {'X-Api-Key': chave or ''}


def listar(base, chave, timeout=10):
    """Todas as sessoes da instancia. None quando ela nao respondeu.

    None e [] sao coisas diferentes e a diferenca importa: [] significa
    "instancia de pe e vazia" (criar e parear); None significa "nao sei"
    (esperar, nunca apagar nada).
    """
    try:
        resp = requests.get('%s/api/sessions' % base.rstrip('/'),
                            params={'all': 'true'},
                            headers=_cabecalho(chave), timeout=timeout)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        dados = resp.json()
    except ValueError:
        return None
    return dados if isinstance(dados, list) else None


def detalhar(base, chave, nome, timeout=10):
    """A sessao completa, pelo nome. So para completar o que a lista nao trouxe."""
    try:
        resp = requests.get('%s/api/sessions/%s' % (base.rstrip('/'), nome),
                            headers=_cabecalho(chave), timeout=timeout)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        dados = resp.json()
    except ValueError:
        return None
    return dados if isinstance(dados, dict) else None


def mesmo_nome(a, b):
    """Dois nomes de sessao sao o mesmo? Caixa NAO conta.

    Quem digita o nome e gente, e gente digita "Principal-PLS", "principal-pls"
    e "Principal-pls" sem pensar duas vezes — foi assim que o dono repareou em
    06/08, de proposito, para ver se o sistema tropecava. O que identifica a
    sessao e a sequencia de letras; se a inicial e maiuscula ou nao e detalhe de
    digitacao, e detalhe de digitacao nao pode derrubar ranking.

    O Postgres ja trata assim (nome de banco nao citado vira minusculo), entao
    comparar com caixa aqui deixava as duas metades do sistema em desacordo.
    """
    return (a or '').strip().lower() == (b or '').strip().lower()


def estado_de(item):
    """(status, engine.state) de um item da lista. Campos podem faltar."""
    if not isinstance(item, dict):
        return None, None
    engine = item.get('engine') or {}
    if not isinstance(engine, dict):
        engine = {}
    return item.get('status'), engine.get('state')


def escolher(sessoes, preferida=None):
    """A sessao que devemos usar, pelo estado. None quando a lista esta vazia.

    O nome so entra como criterio de DESEMPATE dentro do mesmo estado. Nunca
    como filtro: filtrar por nome e o defeito que este modulo existe para
    corrigir.
    """
    if not sessoes:
        return None
    for cabe in _PREFERENCIA:
        candidatos = [s for s in sessoes if cabe(*estado_de(s))]
        if not candidatos:
            continue
        if preferida:
            for item in candidatos:
                if mesmo_nome(item.get('name'), preferida):
                    return item
        return candidatos[0]
    return sessoes[0]


def resolver(base=None, chave=None, preferida=None, env=None):
    """(nome, status, estado, quantas) da sessao viva da instancia.

    nome=None significa que nao ha sessao utilizavel — ou porque a instancia
    nao respondeu (quantas=None) ou porque ela esta vazia (quantas=0).
    """
    env = env if env is not None else carregar_env()
    base = base or env.get('WAHA_BASE') or 'http://localhost:3000'
    chave = chave if chave is not None else env.get('WAHA_API_KEY', '')
    preferida = preferida if preferida is not None else env.get('WAHA_SESSION')

    sessoes = listar(base, chave)
    if sessoes is None:
        return None, None, None, None
    if not sessoes:
        return None, None, None, 0

    escolhida = escolher(sessoes, preferida)
    nome = (escolhida or {}).get('name')
    status, estado = estado_de(escolhida)
    # Nem toda versao do WAHA traz o engine na LISTA. Sem o engine.state nao da
    # para distinguir "pronta" de "WORKING com o motor ainda em UNLAUNCHED", e
    # e justamente nesse intervalo que ler mensagem devolve 500. Entao pergunta.
    if nome and status == 'WORKING' and not estado:
        completo = detalhar(base, chave, nome)
        if completo:
            _, estado = estado_de(completo)
    return nome, status, estado, len(sessoes)


def pronta(status, estado):
    """WORKING sozinho nao basta: tem que ser WORKING E CONNECTED."""
    return status == 'WORKING' and estado == 'CONNECTED'


# ==================== AS INSTANCIAS DA CIDADE ====================
# Cada admin que publica tem a PROPRIA instancia WAHA, com porta e chave
# proprias (principal na 3000, suplente na 3001). Ate aqui o agente so
# enxergava a sua; para o alerta cruzado ele precisa enxergar as duas.


def _trocar_porta(base, porta):
    # `subn` em vez de comparar o texto: quando a porta pedida e a MESMA que ja
    # esta na base, a troca acontece e o resultado fica igual — e o teste antigo
    # (`novo != base`) lia isso como "nao troquei" e devolvia
    # `http://localhost:3000:3000`. Endereco invalido, uma espera de rede jogada
    # fora a cada passada. Medido em 13/09/2026.
    novo, trocas = re.subn(r':\d+$', ':%d' % porta, base)
    return novo if trocas else '%s:%d' % (base, porta)


def _instancias_registradas():
    """As instancias que o registro local conhece, principal primeiro.

    POR QUE ISTO EXISTE (13/09/2026). Em 12/09 as 15:33 o provisionamento criou
    o container do PRINCIPAL na porta 3001 (a 3000 parecia ocupada) e a pipeline
    continuou perguntando so na WAHA_BASE, que e a 3000. Resultado medido: a
    sessao estava VIVA e vinculada uma porta ao lado, e mesmo assim as passadas
    das 05:30, 15:30 e 16:07 de 13/09 morreram em "NENHUMA sessao de pe em
    instancia nenhuma" — dois rankings perdidos por causa de um numero de porta.

    O endereco passa a sair tambem do `.instancias.json`, que o
    `registrar_instancias.py` reescreve a cada passada com o que existe DE FATO
    nesta maquina. Registro ausente ou ilegivel nao e erro: volta lista vazia e
    o comportamento antigo continua valendo.
    """
    caminho = BASE_DIR / '.instancias.json'
    try:
        with open(str(caminho), encoding='utf-8') as arquivo:
            dados = json.load(arquivo)
    except Exception:
        return []
    if not isinstance(dados, dict):
        return []
    saida = []
    for porta, item in dados.items():
        if not isinstance(item, dict):
            continue
        try:
            numero = int(str(porta).strip())
        except (TypeError, ValueError):
            continue
        saida.append({
            'papel': (item.get('papel') or 'principal').strip().lower(),
            'porta': numero,
            'chave': item.get('api_key') or '',
            'container': item.get('container') or '',
        })
    # PRINCIPAL ANTES DO SUPLENTE (regra 16-0): quem publica e o principal; o
    # suplente so entra quando o principal nao responde. Porta menor desempata.
    saida.sort(key=lambda i: (0 if i['papel'] == 'principal' else 1, i['porta']))
    return saida


def instancias(env=None):
    """As instancias WAHA que este agente alcanca, principal primeiro.

    Hoje sai do .env: a principal em WAHA_BASE, e a suplente na porta 3001 do
    mesmo host — quem diz que ela existe e a chave propria dela
    (WAHA_API_KEY_SUPLENTE), porque chave de uma nao abre a outra.

    Quando houver mais de uma cidade isto tem que vir do SITE, nao do .env: o
    agente nao deve decidir topologia sozinho. Ate la, WAHA_BASE_SUPLENTE
    permite apontar para outro host sem mexer no codigo.
    """
    env = env if env is not None else carregar_env()
    base = (env.get('WAHA_BASE') or 'http://localhost:3000').rstrip('/')
    saida = [{'papel': 'principal', 'base': base,
              'chave': env.get('WAHA_API_KEY', '')}]
    vistas = {base}
    # O REGISTRO LOCAL TAMBEM DIZ ONDE PROCURAR (13/09/2026, ver
    # _instancias_registradas): porta fixa no .env ja custou dois rankings.
    for item in _instancias_registradas():
        outra = _trocar_porta(base, item['porta']).rstrip('/')
        if outra in vistas:
            continue
        vistas.add(outra)
        saida.append({'papel': item['papel'], 'base': outra,
                      'chave': item['chave'] or env.get('WAHA_API_KEY', '')})
    chave_suplente = env.get('WAHA_API_KEY_SUPLENTE', '')
    if chave_suplente:
        base_suplente = (env.get('WAHA_BASE_SUPLENTE')
                         or _trocar_porta(base, 3001)).rstrip('/')
        if base_suplente not in vistas:
            vistas.add(base_suplente)
            saida.append({'papel': 'suplente', 'base': base_suplente,
                          'chave': chave_suplente})
    return saida


def numero_da_sessao(item):
    """Telefone da sessao, so digitos. '' quando o WAHA nao informou.

    Serve para uma coisa so, e ela e importante: NAO mandar alerta para o
    numero da propria sessao que envia. Mandar para si mesmo devolve HTTP 500
    (regra 20 do CLAUDE.md) — e um alerta que da erro e um alerta perdido.
    """
    me = (item or {}).get('me') or {}
    if not isinstance(me, dict):
        return ''
    bruto = me.get('id') or me.get('user') or ''
    return re.sub(r'\D', '', str(bruto).split('@')[0])


def instancia_viva(base, chave, preferida=None, env=None):
    """A sessao de pe nesta instancia, com o numero dela. None se nao ha."""
    sessoes = listar(base, chave)
    if not sessoes:
        return None
    escolhida = escolher(sessoes, preferida)
    if not escolhida:
        return None
    nome = escolhida.get('name')
    status, estado = estado_de(escolhida)
    if nome and status == 'WORKING' and not estado:
        completo = detalhar(base, chave, nome)
        if completo:
            _, estado = estado_de(completo)
            escolhida = completo
    return {'base': base, 'chave': chave, 'nome': nome, 'status': status,
            'estado': estado, 'numero': numero_da_sessao(escolhida),
            'pronta': pronta(status, estado)}


def sessoes_prontas(env=None):
    """Todas as sessoes WORKING+CONNECTED que este agente alcanca.

    E a lista de quem AINDA consegue falar. Vazia significa que a cidade
    inteira esta muda no WhatsApp — e ai o unico caminho e o e-mail.
    """
    env = env if env is not None else carregar_env()
    saida = []
    for inst in instancias(env):
        viva = instancia_viva(inst['base'], inst['chave'],
                              env.get('WAHA_SESSION'), env)
        if viva and viva['pronta']:
            viva['papel'] = inst['papel']
            saida.append(viva)
    return saida


# ==================== A COPIA DA SESSAO ====================
# Conectado e gravado sao coisas diferentes. A copia (RemoteAuth-<sessao>.zip,
# dentro do banco da sessao) e a UNICA coisa que faz a sessao voltar sozinha
# depois de um reinicio. Por isso ela e o criterio da faxina: o que tem copia
# nao se apaga, porque apagar troca "reinicia e volta" por "precisa de QR e de
# alguem com o celular na mao".
POSTGRES_USER_PADRAO = 'automation'
POSTGRES_CONTAINER_PADRAO = 'auto-postgres'


def _nome_limpo(sessao):
    """O nome da sessao como o WAHA o usa no banco.

    O HIFEN FAZ PARTE DO NOME. Uma limpeza [^a-z0-9_] transformava
    "principal-pls" em "principalpls" e a consulta ia para um banco inexistente
    — a conferencia entao acusava "sem copia" numa sessao que TEM copia.
    Falso alarme e pior que alarme nenhum: ensina a ignorar o alerta.
    """
    return re.sub(r'[^a-z0-9_-]', '', (sessao or '').lower())


def bancos_waha(env=None):
    """(lista, erro) de todos os bancos waha_* que existem no Postgres."""
    saida, erro = _psql(
        'postgres',
        "select datname from pg_database where datname like 'waha%';", env=env)
    if erro is not None:
        return None, erro
    return [linha.strip() for linha in (saida or '').splitlines() if linha.strip()], None


def banco_da_sessao(sessao, env=None):
    """(banco, erro) — o banco REAL desta sessao, descoberto no Postgres.

    NAO SE CALCULA ESSE NOME A PARTIR DO .env. O session namespace e POR
    INSTANCIA (a 3000 usa "webjs" e a 3001 usa "webjs_suplente") e o .env guarda
    um valor so. Calculando, a consulta da suplente ia para
    "waha_webjs_suplente-pls" — que nao existe — e "banco nao existe" e lido
    como "nao tem copia".

    Isso foi pego no ensaio da faxina em 05/08/2026: ela ia apagar a
    "suplente-pls", que tem 13 MB de copia gravada. Descobrir o banco em vez de
    adivinhar o nome e o que impede a faxina de destruir o que quer proteger.

    banco=None sem erro significa: nao existe banco nenhum desta sessao — e ai
    nao ha copia mesmo.
    """
    env = env if env is not None else carregar_env()
    bancos, erro = bancos_waha(env)
    if erro is not None:
        return None, erro
    limpo = _nome_limpo(sessao)
    sufixo = '_' + limpo
    achados = [b for b in bancos if b.endswith(sufixo)]

    if len(achados) > 1:
        # DESEMPATE PELO SESSION NAMESPACE DECLARADO — e so desempate.
        #
        # A busca continua sendo por descoberta (o paragrafo acima vale
        # inteiro); o .env entra depois, para escolher entre os que ja foram
        # encontrados. E o mesmo tratamento que a regra 16 da ao nome da
        # sessao: serve para desempatar, nunca para procurar.
        #
        # Por que passou a ser preciso (06/08): a migracao da P-03 deixou de
        # proposito os bancos antigos no lugar — eles sao o desfazer. Com eles
        # la, "Principal-PLS" casa com TRES bancos: o credencial velho
        # (waha_webjs_principal-pls), o credencial novo
        # (waha_s-principal-pls_principal-pls) e ate o de CONFIG novo
        # (waha_principal-pls), que termina igual por coincidencia. Sem
        # desempate a prontidao passou a dizer "nao sei" na etapa da copia — e
        # e a copia que decide qual conserto tentar (regra 16-E-bis).
        #
        # O desempate so e confiavel porque hoje cada instancia declara o SEU
        # namespace no .env. Enquanto havia um valor so para as duas, era
        # exatamente isto que nao dava para fazer.
        declarados = [v for v in (env.get('WAHA_SESSION_NAMESPACE'),
                                  env.get('WAHA_SESSION_NAMESPACE_SUPLENTE'))
                      if (v or '').strip()]
        exatos = [b for b in achados
                  if b.lower() in ['waha_%s_%s' % (ns.strip().lower(), limpo)
                                   for ns in declarados]]
        if len(exatos) == 1:
            return exatos[0], None
        # Na duvida nao se apaga nada: devolver erro faz a faxina manter.
        return None, 'mais de um banco para "%s": %s' % (sessao, ', '.join(achados))
    return (achados[0] if achados else None), None


def _psql(banco, sql, env=None, timeout=40):
    """(saida, erro) de um psql dentro do container. Nunca levanta excecao."""
    env = env if env is not None else carregar_env()
    cmd = ['docker', 'exec',
           env.get('POSTGRES_CONTAINER') or POSTGRES_CONTAINER_PADRAO,
           'psql', '-U', env.get('POSTGRES_USER') or POSTGRES_USER_PADRAO,
           '-d', banco, '-t', '-A', '-c', sql]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return None, str(exc)
    if r.returncode != 0:
        return None, (r.stderr or '').strip()[:200]
    return (r.stdout or '').strip(), None


def copia_da_sessao(sessao, env=None):
    """(arquivos, quando, erro). arquivos=None significa NAO SEI.

    A diferenca entre 0 e None e o que segura a faxina: 0 e "nao ha nada a
    perder"; None e "nao consegui perguntar" — e nesse caso nao se apaga nada.
    """
    banco, erro = banco_da_sessao(sessao, env)
    if erro is not None:
        return None, None, erro
    if not banco:
        # Nenhum banco desta sessao. Quando o WhatsApp responde LOGOUT o WAHA
        # apaga o banco inteiro — entao isto e a ausencia total de copia, o caso
        # mais grave, e nao uma falha de consulta.
        return 0, None, None
    saida, erro = _psql(
        banco,
        "select count(*)||'|'||coalesce(max(created_at)::text,'-') from files;",
        env=env)
    if erro is not None:
        if 'does not exist' in erro or 'nao existe' in erro:
            return 0, None, None
        return None, None, erro
    if not saida or '|' not in saida:
        return None, None, 'resposta inesperada: %r' % (saida or '')[:80]
    quantos, quando = saida.split('|', 1)
    try:
        quantos = int(quantos)
    except ValueError:
        return None, None, 'contagem ilegivel: %r' % quantos
    return quantos, (quando if quando != '-' else None), None


# PAREAMENTO RECENTE NAO SE REINICIA (18/09/2026, plano A7 / P12-1.5).
# Em 12/09 a prontidao reiniciou uma sessao recem-pareada e o pareamento se
# perdeu. O `provisionar` grava a hora em que a sessao ficou WORKING depois do
# codigo; a prontidao consulta antes de reiniciar.
_PAREADA_DIR = BASE_DIR / 'estado-pareamento'


def marcar_pareada(sessao):
    """Grava agora como a hora em que `sessao` foi pareada. Nunca levanta."""
    try:
        import time as _t
        _PAREADA_DIR.mkdir(exist_ok=True)
        (_PAREADA_DIR / ('%s.txt' % sessao)).write_text(str(int(_t.time())))
    except Exception:
        pass


def pareada_ha(sessao):
    """Segundos desde o ultimo pareamento de `sessao`, ou None se nao sei."""
    try:
        import time as _t
        quando = int((_PAREADA_DIR / ('%s.txt' % sessao)).read_text().strip())
        return int(_t.time()) - quando
    except Exception:
        return None


def pagina_firmando(sessao, container=None, env=None):
    """A sessao foi pareada AGORA e a pagina ainda nao recarregou? True/False/None.

    18/09/2026 (E-C, medido no teste do dono). Depois de parear, o WhatsApp Web
    recarrega a pagina sozinho ~5 min 20 s depois (principal 18:18:40->18:24:00,
    suplente 18:29:12->18:34:34). Antes disso, ENVIAR falha ("getChat" undefined,
    18:23:55). O sinal e do proprio WAHA: a ultima conferencia da sessao no log
    diz "Session exists: false" ate a recarga, e "true" depois dela — e tambem
    numa sessao restaurada da copia (todas as subidas de 18/09). Sem linha nas
    ultimas 3 h: None (nao sei).
    """
    env = env if env is not None else carregar_env()
    container = container or env.get('WAHA_CONTAINER') or 'auto-waha-pls'
    try:
        r = subprocess.run(['docker', 'logs', '--since', '3h', container],
                           capture_output=True, text=True, timeout=30, errors='replace')
    except Exception:
        return None
    marca = 'session:%s - Session exists: ' % sessao
    ultima = None
    for linha in ((r.stdout or '') + (r.stderr or '')).splitlines():
        i = linha.find(marca)
        if i >= 0:
            ultima = linha[i + len(marca):i + len(marca) + 5]
    if ultima is None:
        return None
    return ultima.startswith('false')


def idade_da_copia(sessao, env=None):
    """(segundos, erro) — ha quanto tempo a copia foi gravada. None = NAO SEI.

    POR QUE EXISTE (12/09/2026, medido ao vivo nesta madrugada).
    `copia_da_sessao` responde "existe copia?", e isso deixou de bastar. Quando
    a injecao do whatsapp-web.js falha na subida ("Failed to inject utils" /
    "ready timeout"), a sessao fica WORKING + CONNECTED mas NUNCA fica pronta —
    e o job "RemoteAuth Backup Sync" nao chega a comecar. A copia antiga
    continua no banco, o "existe?" responde que sim, e no proximo reinicio o
    WAHA restaura um retrato de horas atras. Medido: subida das 23:51 e start
    das 00:02 sem o job; copia parada em 23:49 com a sessao de pe. Um `restart`
    da sessao fez o job voltar as 00:10 e a copia foi gravada as 00:11.

    A CONTA E FEITA DENTRO DO POSTGRES DE PROPOSITO: `created_at` e UTC e o
    relogio dos scripts e de Brasilia. Subtrair aqui daria 3 horas de erro —
    exatamente o tipo de engano que ja mordeu este projeto.
    """
    banco, erro = banco_da_sessao(sessao, env)
    if erro is not None:
        return None, erro
    if not banco:
        return None, 'esta sessao nao tem banco de copia'
    saida, erro = _psql(
        banco,
        "select coalesce(extract(epoch from (now() - max(created_at)))::bigint, -1) "
        "from files;",
        env=env)
    if erro is not None:
        return None, erro
    try:
        segundos = int((saida or '').strip())
    except ValueError:
        return None, 'resposta inesperada: %r' % (saida or '')[:80]
    if segundos < 0:
        return None, 'nao ha copia gravada'
    return segundos, None


# ==================== A FAXINA (P-02) ====================
# UMA INSTANCIA, UM WORKER, UMA SESSAO (DICIONARIO secao 7).
#
# Sobra de sessao nasce quando o NOME muda: a instancia fica com a antiga
# (morta) e ganha a nova. Foi assim que a 3000 acabou com "default" e
# "principal-pls" ao mesmo tempo em 02/08/2026.
#
# Por que a faxina roda so na hora de parear, e nao a cada rodada:
#   1. Depois da tarefa 31 a sobra nao quebra mais nada — a sessao e escolhida
#      pelo ESTADO, entao a morta e simplesmente ignorada. Virou desperdicio de
#      RAM, nao falha. Faxina automatica seria risco sem urgencia.
#   2. FAILED NAO quer dizer irrecuperavel. Medido em 05/08: a "suplente-pls"
#      esta FAILED com 13 MB de copia gravada — apaga-la trocaria um restart
#      por um QR Code. E a pendencia P-06 manda deixar essa sessao em paz.
#   3. Reparear exige um humano com o celular. Apagar sozinho nao adianta nada:
#      so transforma "parada" em "parada e sem volta".


def _papel_no_nome(nome):
    """O papel que este nome de sessao anuncia, ou None se nao anuncia nenhum.

    `principal-pls` -> principal · `suplente-pls` -> suplente ·
    `reserva2-abc`  -> reserva2  · `default`      -> None

    Serve para separar a sessao IRMA (parte do desenho da cidade) de uma sobra
    de nome antigo. Nao monta nome nenhum: apenas le o que o WAHA respondeu.
    """
    n = (nome or '').strip().lower()
    if n.startswith('principal'):
        return 'principal'
    if n.startswith('suplente'):
        return 'suplente'
    if n.startswith('reserva'):
        return n.split('-', 1)[0]
    return None


def limpar_instancia(base, chave, manter=None, env=None, executar=True):
    """Apaga as sobras da instancia antes de um pareamento novo.

    Devolve uma lista de (nome, acao, motivo) — sempre, mesmo sem apagar nada,
    para que quem chamou possa registrar no log o que foi feito e o que nao foi.

    NUNCA apaga:
      - a sessao em ``manter`` (a que vai ser usada/criada);
      - qualquer sessao que TENHA copia gravada;
      - qualquer sessao cuja copia nao deu para conferir (na duvida, mantem).

    Com ``executar=False`` so relata o que faria (ensaio).
    """
    env = env if env is not None else carregar_env()
    sessoes = listar(base, chave)
    if sessoes is None:
        return [(None, 'nada', 'a instancia nao respondeu')]
    if not sessoes:
        return [(None, 'nada', 'instancia vazia, nao ha o que limpar')]

    relatorio = []
    for item in sessoes:
        nome = item.get('name')
        status, _ = estado_de(item)
        if not nome:
            continue
        if manter and mesmo_nome(nome, manter):
            relatorio.append((nome, 'mantida', 'e a sessao desta instancia'))
            continue

        # A SESSAO IRMA NAO E SOBRA (08/09/2026, fusao por cidade).
        #
        # Ate aqui a faxina partia de "uma instancia = uma sessao": tudo que
        # nao fosse a sessao do momento era resto de nome trocado. Com o
        # desenho novo -- UM CONTAINER POR CIDADE, uma sessao por numero --
        # `principal-pls` e `suplente-pls` moram juntas de proposito.
        #
        # Sem esta guarda o estrago era certo e silencioso: parear o principal
        # apagaria a suplente recem-criada, que ainda esta STOPPED e SEM COPIA
        # (nunca foi pareada) -- exatamente o perfil que a faxina remove.
        #
        # O criterio e o PAPEL no nome: quem tem papel do desenho pertence a
        # cidade. Sobra de verdade e' nome fora do padrao (`default`,
        # `palmeiropolis` do tempo do slug), e essa continua saindo.
        if _papel_no_nome(nome):
            relatorio.append((nome, 'mantida',
                              'e a sessao irma desta cidade (papel "%s") — '
                              'faz parte do desenho, nao e sobra'
                              % _papel_no_nome(nome)))
            continue

        arquivos, quando, erro = copia_da_sessao(nome, env)
        if erro is not None:
            relatorio.append((nome, 'mantida',
                              'nao consegui conferir a copia (%s)' % erro))
            continue
        if arquivos:
            relatorio.append((nome, 'mantida',
                              'TEM copia (%d arquivo(s), de %s) — apagar custaria '
                              'um QR Code' % (arquivos, quando or 'data desconhecida')))
            continue
        if status == 'WORKING':
            # Sem copia mas de pe: apagar agora perderia a sessao viva. Quem
            # avisa que ela morre no proximo reinicio e a conferencia da copia.
            relatorio.append((nome, 'mantida',
                              'esta WORKING (sem copia: corre risco no reinicio, '
                              'mas apagar seria pior)'))
            continue

        if not executar:
            relatorio.append((nome, 'apagaria', 'sem copia e em %s' % (status or '?')))
            continue

        motivo = 'sem copia e em %s' % (status or '?')
        try:
            requests.delete('%s/api/sessions/%s' % (base.rstrip('/'), nome),
                            headers=_cabecalho(chave), timeout=30)
        except Exception as exc:
            relatorio.append((nome, 'falhou', 'DELETE nao passou: %s' % exc))
            continue
        # O banco esta vazio (foi o que acabamos de conferir), entao derruba-lo
        # e limpeza, nao perda. Serve para o nome nao reaparecer com lixo se a
        # sessao for recriada com o mesmo nome depois. Usa o banco DESCOBERTO:
        # derrubar um nome calculado seria derrubar o banco de outra instancia.
        banco_alvo, erro_banco = banco_da_sessao(nome, env)
        if banco_alvo and not erro_banco:
            _, erro_drop = _psql('postgres',
                                 'DROP DATABASE IF EXISTS "%s";' % banco_alvo, env=env)
            if erro_drop:
                motivo += ' (sessao apagada; o banco vazio ficou: %s)' % erro_drop
        relatorio.append((nome, 'apagada', motivo))
    return relatorio


def main():
    p = argparse.ArgumentParser(description='Descobre a sessao viva do WAHA pelo estado.')
    p.add_argument('--base', help='Endereco da instancia (padrao: WAHA_BASE do .env)')
    p.add_argument('--chave', help='Chave de API (padrao: WAHA_API_KEY do .env)')
    p.add_argument('--preferida', help='Nome a usar como desempate (padrao: WAHA_SESSION)')
    p.add_argument('--nome', action='store_true', help='Imprime so o nome')
    p.add_argument('--limpar', action='store_true',
                   help='Faxina antes de parear: apaga so sobra SEM copia gravada')
    p.add_argument('--manter', help='Sessao que a faxina nao deve tocar')
    p.add_argument('--ensaio', action='store_true',
                   help='Com --limpar, so mostra o que faria')
    p.add_argument('--copia', metavar='SESSAO',
                   help='Imprime arquivos|quando|erro da copia gravada da sessao')
    args = p.parse_args()

    if args.copia:
        arquivos, quando, erro = copia_da_sessao(args.copia)
        print('%s|%s|%s' % ('' if arquivos is None else arquivos,
                            quando or '', erro or ''))
        # 0 = tem copia · 1 = nao tem · 2 = nao deu para perguntar (nao apague!)
        return 2 if arquivos is None else (0 if arquivos else 1)

    if args.limpar:
        env = carregar_env()
        base = args.base or env.get('WAHA_BASE') or 'http://localhost:3000'
        chave = args.chave if args.chave is not None else env.get('WAHA_API_KEY', '')
        for nome_s, acao, motivo in limpar_instancia(
                base, chave, manter=args.manter, env=env, executar=not args.ensaio):
            print('%s|%s|%s' % (nome_s or '-', acao, motivo))
        return 0

    nome, status, estado, quantas = resolver(args.base, args.chave, args.preferida)

    if args.nome:
        print(nome or '')
    else:
        print('%s|%s|%s|%s' % (nome or '', status or '', estado or '',
                               '' if quantas is None else quantas))

    if quantas is None:
        return 2
    if not nome:
        return 3
    return 0 if pronta(status, estado) else 1


if __name__ == '__main__':
    sys.exit(main())
