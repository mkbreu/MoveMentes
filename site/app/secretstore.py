# app/secretstore.py
# -*- coding: utf-8 -*-
"""Cofre de segredos operacionais em arquivo, fora do banco e fora do git.

Guarda valores sensiveis que NAO devem viajar em backup, dump de banco ou
template (hoje: o cookie de sessao do Strava usado na importacao por perfil).

Regras do modulo:
- O arquivo fica em ``instance/secrets.json`` (pasta ja ignorada pelo git e
  fora da lista de arquivos copiados pelo backup).
- O valor NUNCA e logado nem devolvido em telas: a UI usa ``info()``, que
  entrega apenas metadados e uma impressao digital curta.
- Cada segredo e guardado por escopo. O escopo padrao e global; passar uma
  cidade permite um valor proprio por cidade, com queda para o global. Isso
  mantem a leitura de hoje (uma instalacao, um cookie) e ja deixa o caminho
  pronto para operacao multi-cliente sem migracao de dados.
"""
import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SECRETS_PATH = Path(__file__).resolve().parents[1] / 'instance' / 'secrets.json'
# Saude da sessao do Strava por escopo (NAO e segredo: guarda so ok/mensagem/
# quando foi verificado, para a UI avisar quando o cookie expira em vez de
# falhar calado). Fica fora do secrets.json para nunca arriscar o cofre.
_SESSION_HEALTH_PATH = Path(__file__).resolve().parents[1] / 'instance' / 'strava_session_health.json'
_GLOBAL_SCOPE = '__global__'

# Nomes conhecidos (evita erro de digitacao espalhado pelo codigo).
STRAVA_SESSION = 'strava_session_cookie'
FLASK_SECRET = 'flask_secret_key'
AUTOMATION_TOKEN = 'automation_api_token'
# Endereco e chave do WAHA (WhatsApp) para a tela de reconexao. O WAHA roda no
# Dell, nao aqui no DS215j, entao o endereco e um IP da rede local — por
# exemplo http://192.168.X.Y:3000. Sem isso a tela nao tem como falar com ele.
WAHA_URL = 'waha_base_url'
WAHA_API_KEY = 'waha_api_key'
# Nome da sessao do WhatsApp desta cidade dentro do WAHA. Cada cidade e um
# aparelho vinculado independente, com banco proprio (waha_webjs_<sessao>) —
# uma nao contamina a outra. O padrao para cidade nova e o slug da cidade;
# instalacoes antigas continuam com a sessao "default" ate o operador migrar
# pela tela (ver admin.whatsapp_sessao).
WAHA_SESSION = 'waha_session_name'
# Como a cidade aparece DENTRO do nome da sessao (`<papel>-<cidade>`).
# Palmeiropolis usa `pls`, abreviacao herdada de quando o projeto tinha uma
# cidade so. Vazio = o slug da cidade, que e o padrao para cidade nova.
WAHA_CIDADE_CURTA = 'waha_cidade_curta'
# MODO TESTE (P-27). 'sim' = nada vai para o grupo; tudo cai no numero de
# teste. Guardado no site para o admin ligar e desligar sem editar .env no
# Dell. A pipeline pergunta antes de publicar (GET /api/config/envio).
# QUANDO O WINDOWS DA AUTOMACAO FALOU COM O SITE PELA ULTIMA VEZ.
#
# Nao e segredo — mora aqui porque e o cofre por cidade que ja existe e nao
# exige tabela nova. Serve para o site distinguir DUAS coisas que ate 14/08 ele
# confundia: "o computador esta desligado" (normal, ele dorme de proposito) e
# "o computador esta ligado e o WhatsApp caiu" (isso e falha e merece alerta).
#
# Quem carimba e a tarefa da fila, do lado do WINDOWS, de 15 em 15 minutos —
# antes de encostar no WSL. Por isso ela responde mesmo com o WSL caido, que e
# exatamente o caso em que o site se calava dizendo "esta dormindo".
WINDOWS_VISTO_EM = 'windows_visto_em'
# QUANDO A MAQUINA COMECOU A NAO RESPONDER, e QUANDO ELA AVISOU QUE IA PARAR.
#
# Os dois existem para o mesmo fim: nao mandar e-mail sobre coisa normal.
# `MAQUINA_QUEBRADA_DESDE` guarda a primeira medicao ruim — so a segunda, 15
# min depois, vira alerta (reinicio de container nao alcanca isso).
# `MAQUINA_PARANDO_EM` e a parada combinada: o proprio Dell avisa antes de
# desligar, e por 40 minutos porta muda passa a ser "desligada", nao "quebrada".
MAQUINA_QUEBRADA_DESDE = 'maquina_quebrada_desde'
MAQUINA_PARANDO_EM = 'maquina_parando_em'
# O ID DO GRUPO DE WHATSAPP DESTA CIDADE (`...@g.us`), contado pela maquina.
# Sem ele o site nao entrega mensagem de grupo sozinho: "grupo" e uma palavra,
# nao um destino, e o envio ficava esperando o ciclo da automacao.
WHATSAPP_GRUPO = 'whatsapp_grupo_id'
# GRUPO DE TESTE do modo ensaio. Mensagem de grupo mandada para um NUMERO nao
# prova nada: mencao com @ nao marca ninguem e o texto se perde no meio das
# outras mensagens de teste. Com um grupo so de teste, o ensaio fica igual ao
# real. Vazio = tudo cai no numero de teste, como era antes.
MODO_TESTE_GRUPO = 'modo_teste_grupo'
# PEDIDO DE "PUBLICAR O RANKING AGORA" (ver app/rodada.py). Fica no cofre, que
# e por cidade e sobrevive a reinicio: o pedido nao pode se perder enquanto a
# maquina da automacao acorda.
RANKING_PEDIDO = 'ranking_pedido'
# PEDIDO DE "EXECUTAR PEDIDOS AGORA" (17/09/2026, ver app/rodada.py). Mesmo
# molde do de cima: faz andar na hora o que espera a maquina (containers,
# conexao, mensagens), sem publicar ranking.
EXECUCAO_PEDIDA = 'execucao_pedida'

MODO_TESTE = 'modo_teste'
MODO_TESTE_DESTINO = 'modo_teste_destino'
# PEDIDO DE "DESLIGAR A MAQUINA" (17/08/2026, item G do plano do MVP). Guarda
# QUANDO o botao do painel foi clicado; vale 30 min e e cancelavel. Quem
# entrega e `GET /api/fila/pendentes` (a rota que a maquina ja pergunta de 15
# em 15 min) — sem porta nova para dentro do Dell, sem SSH (foi o que barrou
# isto na P-45).
PEDIDO_DESLIGAR_MAQUINA = 'pedido_desligar_maquina'

# MANTER LIGADA (17/08/2026, plano do MVP, item 4.1-b). Alavanca no painel, ao
# lado do modo teste. 'sim' pede para o Dormir.ps1 NAO fazer a parada limpa
# desta janela — serve para quando o dono esta mexendo na maquina ou
# acompanhando um conserto e nao quer que ela desligue por baixo dele. Quem
# le e o `GET /api/maquina/pode-dormir`; a decisao final continua sendo do
# proprio script (regra 21-D: so dorme com o desfecho conhecido).
MANTER_LIGADA = 'manter_ligada'
# CONFERENCIA AUTOMATICA (27/08/2026) — de quantos em quantos dias o sistema
# compara o Strava de cada atleta participante com o ranking e traz o que
# faltou. Valores: '1', '2', '3', '4' ou '0' (desligada). Padrao: 4 dias.
#
# POR QUE NAO E DIARIA POR PADRAO (decisao do dono): se os km sobem sozinhos
# todo dia, o atleta que acompanha o ranking para de postar — e pior, posta um
# link depois e nao ve o numero mudar, porque ja tinha sido contado. Quatro
# dias dao margem para o link postado chegar primeiro. Diaria fica disponivel
# para quando a coleta estiver falhando e a rede precisar ficar apertada.
CONFERENCIA_DIAS = 'conferencia_dias'
# Qual periodo ganha trofeu NESTA cidade (P-137, 20/09/2026):
# 'trimestre', 'semestre', 'ano' — ou vazio, que quer dizer "nao houve
# patrocinador" e por isso aquele ranking nao vai ao grupo.
PERIODO_PREMIADO = 'periodo_premiado'
# Quando a automacao buscou o ranking pela ultima vez nesta cidade
# (P-139, 20/09/2026). E' o que diz se a rodada daquela janela ja
# aconteceu — sem isto, o site pedia uma segunda rodada por cima da
# que o Agendador ja tinha feito.
RANKING_SERVIDO_EM = 'ranking_servido_em'
# Data (AAAA-MM-DD) em que a conferencia rodou pela ultima vez nesta cidade.
# E o que faz o prazo ter MEMORIA: disparar por calendario sem memoria faz a
# checagem sumir quando a maquina nao liga naquele dia (mesma licao da P-78-b).
CONFERENCIA_ULTIMA = 'conferencia_ultima'
# Numero do WhatsApp que o sistema usa para falar com o responsavel: e por ele
# que o codigo de pareamento chega quando o admin de outra cidade pede
# vinculacao e a maquina do WAHA acabou de subir.
WAHA_NUMERO_MASTER = 'waha_numero_master'
# Token do ORQUESTRADOR (a maquina que roda o WAHA). Diferente do token de
# cidade: nao le ranking nem atleta de ninguem — serve apenas para a maquina
# perguntar "tem pedido de vinculacao de WhatsApp na fila?" e responder o que
# fez. Existe porque uma cidade que ainda nao vinculou nada tambem precisa ser
# atendida, e o pedido dela precisa chegar a maquina de alguma forma.
AUTOMATION_TOKEN_GLOBAL = 'automation_api_token_global'
# Endereco fisico (MAC) da placa de rede do computador da automacao e o
# endereco de transmissao da rede dele. Com os dois, o servidor consegue
# ACORDAR aquele computador pela rede — e o que permite o admin de uma cidade
# ligar a maquina sozinho para vincular o WhatsApp dele.
WOL_MAC = 'wol_mac'
WOL_BROADCAST = 'wol_broadcast'
# Microsoft 365 para enviar e-mail (o codigo de pareamento vai por aqui).
#
# NAO E SMTP, E DE PROPOSITO: a Microsoft desativou a autenticacao basica do
# SMTP AUTH (bloqueio gradual desde marco/2026, total em 30/04/2026) e acabou
# com as senhas de aplicativo. O envio usa a Graph API, que sao duas chamadas
# HTTP — ver app/correio.py.
#
# No Entra ID: registrar um app, dar a permissao de APLICATIVO `Mail.Send` com
# consentimento do administrador, e copiar os tres valores abaixo. Permissao
# "delegada" nao serve, porque na hora do envio nao ha usuario logado.
MS_TENANT_ID = 'ms_tenant_id'
MS_CLIENT_ID = 'ms_client_id'
MS_CLIENT_SECRET = 'ms_client_secret'
MS_REMETENTE = 'ms_remetente'
# Host/usuario do SSH de acesso ao servidor de producao (tela
# admin/comandos.html, so supremo_required). Nao e segredo no sentido de
# senha -- e a autenticacao real e do Cloudflare Access -- mas e
# reconhecimento de infraestrutura real, e por isso saiu de hardcoded no
# template para o cofre antes do repositorio poder virar publico
# (docs/planos/2026-08-21_F_github-portfolio_SONNET-medio.md).
SSH_HOSTNAME = 'ssh_hostname'
SSH_USER = 'ssh_user'
# Senha do `sudo` no DS215j (26/08/2026, pedido do dono: "se você não tiver a
# senha, me pergunte... para você salvar ela lá"). Esta SIM é segredo de
# verdade -- nunca exibir em tela nem em `admin/comandos.html`.
SSH_SUDO_PASSWORD = 'ssh_sudo_password'


def _scope_key(scope):
    scope = (scope or '').strip()
    return scope or _GLOBAL_SCOPE


def _load_status():
    """Par (dados, legivel).

    legivel=False significa que o arquivo EXISTE mas nao pode ser lido agora
    (permissao, JSON corrompido, IO). Nesse caso os dados voltam vazios e
    NENHUMA gravacao pode acontecer por cima: sobrescreveria segredos que nao
    conseguimos enxergar. Incidente real em 2026-07-20: um `import wsgi` via
    SSH nao conseguiu ler o cofre (PermissionError), tratou como vazio e o
    regravou so com uma SECRET_KEY nova -- perdendo o cookie do Strava.
    """
    try:
        if not _SECRETS_PATH.exists():
            return {}, True
        # utf-8-SIG, NAO utf-8 (09/09/2026 -- incidente do cofre cego).
        # O `-Encoding UTF8` do Windows PowerShell 5.1 grava uma marca
        # invisivel no comeco do arquivo (BOM). Com `utf-8` puro o
        # `json.load` recusa o arquivo inteiro, e este metodo devolve
        # ({}, False): o site passa a enxergar o cofre VAZIO. Foi assim que
        # em 08/09 a API caiu em 503, o cookie do Strava sumiu (parou de
        # gravar atividade as 15h em ponto), o alerta por e-mail emudeceu e
        # o botao de conectar perdeu a porta -- 971 falhas em duas horas,
        # com TODAS as 31 chaves intactas dentro do arquivo.
        # `utf-8-sig` le com marca e sem marca. A fonte tambem foi
        # corrigida (Movimente-se_Rota_Cloudflare.ps1), mas isto aqui e a
        # rede de seguranca para o proximo script que errar.
        with open(_SECRETS_PATH, 'r', encoding='utf-8-sig') as fh:
            data = json.load(fh)
        return (data if isinstance(data, dict) else {}), True
    except Exception as exc:
        # Nunca inclui o conteudo do arquivo na mensagem.
        logger.warning('Falha ao ler o cofre de segredos: %s', type(exc).__name__)
        return {}, False


def load_status():
    """Versao publica de `_load_status`: (dados, legivel).

    Existe porque quem esta FORA deste modulo precisa distinguir "o cofre esta
    vazio" de "o cofre nao pode ser lido agora" -- e ate 09/09/2026 nao dava.
    Quem so' chamava `get()` recebia None nos dois casos, e foi assim que o
    boot do site trocou a SECRET_KEY por achar que o cofre estava vazio,
    deslogando todo mundo (ver app/__init__.py, _resolver_secret_key).
    """
    return _load_status()


def _load():
    data, _legivel = _load_status()
    return data


class _Travado(object):
    """TRAVA DE GRAVACAO DO COFRE (17/09/2026).

    Toda gravacao aqui e ler-mudar-gravar. O arquivo em si nunca corrompe (a
    gravacao e num temporario e depois `os.replace`, que e atomico), mas sem
    trava duas gravacoes no mesmo instante — dois trabalhadores do uWSGI, ou o
    site e um trabalho longo — leem a mesma versao e a ultima apaga a mudanca
    da primeira. Com um clique por vez isso quase nunca acontecia; com botoes
    que gravam pedido a cada clique, a chance cresce.

    A trava e um arquivo ao lado (`secrets.lock`) travado com `flock`, que o
    proprio sistema solta se o processo morrer — trava esquecida nao prende o
    site. Onde nao houver `flock` (Windows, no banco de provas), ela nao faz
    nada e o comportamento e o de antes: nunca pode impedir a gravacao.
    """

    _CAMINHO = None

    def __enter__(self):
        self._fh = None
        try:
            import fcntl
            if _Travado._CAMINHO is None:
                _Travado._CAMINHO = _SECRETS_PATH.with_suffix('.lock')
            _SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(_Travado._CAMINHO, 'a+')
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        return self

    def __exit__(self, *_erro):
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
        return False


def _save(data):
    try:
        _SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _SECRETS_PATH.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=True, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass  # Sistemas de arquivo sem permissao POSIX (ex.: Windows/SMB).
        os.replace(tmp_path, _SECRETS_PATH)
        return True
    except Exception as exc:
        logger.error('Falha ao gravar o cofre de segredos: %s', type(exc).__name__)
        return False


def fingerprint(value):
    """Impressao digital curta e nao reversivel, segura para exibir e logar."""
    value = (value or '').strip()
    if not value:
        return ''
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]


def get(name, scope=None):
    """Valor do segredo no escopo pedido, com queda para o escopo global."""
    entradas = _load().get(name) or {}
    for chave in (_scope_key(scope), _GLOBAL_SCOPE):
        entrada = entradas.get(chave)
        if isinstance(entrada, dict) and entrada.get('value'):
            return entrada['value']
    return None


def entries(name):
    """Lista (escopo, valor) configurados para o segredo; escopo None = global.

    Uso interno (ex.: mapear token de automacao -> cidade). O valor NUNCA deve
    chegar a templates ou logs a partir daqui.
    """
    resultado = []
    for chave, entrada in (_load().get(name) or {}).items():
        if isinstance(entrada, dict) and entrada.get('value'):
            escopo = None if chave == _GLOBAL_SCOPE else chave
            resultado.append((escopo, entrada['value']))
    return resultado


def set_value(name, value, scope=None, updated_by=None):
    """Grava (ou substitui) o segredo. Retorna True se persistiu."""
    value = (value or '').strip()
    if not value:
        return False
    # LER-MUDAR-GRAVAR DENTRO DA TRAVA (17/09/2026): duas gravacoes no mesmo
    # instante liam a mesma versao e a ultima apagava a mudanca da primeira.
    with _Travado():
        data, legivel = _load_status()
        if not legivel:
            logger.error('Cofre ilegivel: gravacao de %s RECUSADA para nao '
                         'sobrescrever segredos existentes. Corrija a permissao '
                         'de instance/secrets.json antes de gravar.', name)
            return False
        entradas = data.setdefault(name, {})
        entradas[_scope_key(scope)] = {
            'value': value,
            'updated_at': time.time(),
            'updated_by': (updated_by or '')[:120],
            'fingerprint': fingerprint(value),
        }
        ok = _save(data)
    if ok:
        logger.info('Segredo %s atualizado (escopo=%s, fp=%s)',
                    name, _scope_key(scope), fingerprint(value))
    return ok


def rotate_value(name, old_value, new_value, updated_by='auto-rotacao'):
    """Substitui o segredo cujo valor atual e ``old_value`` por ``new_value``.

    Usado para manter a sessao do Strava viva: o Strava renova o cookie a cada
    resposta autenticada; regravar o valor novo estende a validade indefinida-
    mente (estilo WAHA). So mexe na entrada que casa com o valor antigo -- se o
    cofre mudou no meio tempo (outro worker rotacionou antes), nao faz nada.
    Retorna True quando persistiu.
    """
    old_value = (old_value or '').strip()
    new_value = (new_value or '').strip()
    if not old_value or not new_value or old_value == new_value:
        return False
    data, legivel = _load_status()
    if not legivel:
        return False
    entradas = data.get(name) or {}
    for chave, entrada in entradas.items():
        if isinstance(entrada, dict) and entrada.get('value') == old_value:
            entradas[chave] = {
                'value': new_value,
                'updated_at': time.time(),
                'updated_by': (updated_by or '')[:120],
                'fingerprint': fingerprint(new_value),
            }
            ok = _save(data)
            if ok:
                logger.info('Segredo %s rotacionado (escopo=%s, fp=%s)',
                            name, chave, fingerprint(new_value))
            return ok
    return False


def rename_scope(old_scope, new_scope):
    """Move TODOS os segredos do escopo antigo para o novo (usado ao renomear
    cidade: o cofre e um JSON, entao nao e alcancado pelo UPDATE de tabelas).

    Se o novo escopo ja tiver valor para um segredo, o valor novo e mantido e o
    antigo e descartado (nao sobrescreve). Move tambem a saude da sessao.
    Retorna True se algo mudou.
    """
    old_key = _scope_key(old_scope)
    new_key = _scope_key(new_scope)
    if old_key == new_key:
        return False
    data, legivel = _load_status()
    if not legivel:
        logger.error('Cofre ilegivel: renomeacao de escopo %s->%s RECUSADA.',
                     old_key, new_key)
        return False
    mudou = False
    for _nome, entradas in data.items():
        if isinstance(entradas, dict) and old_key in entradas:
            if new_key not in entradas:
                entradas[new_key] = entradas[old_key]
            entradas.pop(old_key, None)
            mudou = True
    if mudou:
        _save(data)
        logger.info('Escopo de segredos renomeado no cofre: %s -> %s', old_key, new_key)
    # Saude da sessao (arquivo separado)
    try:
        if _SESSION_HEALTH_PATH.exists():
            with open(_SESSION_HEALTH_PATH, 'r', encoding='utf-8') as fh:
                saude = json.load(fh)
            if isinstance(saude, dict) and old_key in saude:
                saude.setdefault(new_key, saude[old_key])
                saude.pop(old_key, None)
                tmp = _SESSION_HEALTH_PATH.with_suffix('.tmp')
                with open(tmp, 'w', encoding='utf-8') as fh:
                    json.dump(saude, fh, ensure_ascii=True, indent=2)
                os.replace(tmp, _SESSION_HEALTH_PATH)
    except Exception as exc:
        logger.debug('Saude da sessao nao migrada no rename: %s', type(exc).__name__)
    return mudou


def delete_scope(scope):
    """Apaga TODOS os segredos de um escopo (usado ao excluir uma cidade).

    O cofre e um JSON: o DELETE das tabelas nao o alcanca. Sem isto, apagar
    uma cidade deixaria o token da automacao e o cookie do Strava dela vivos
    no arquivo -- credencial orfa e credencial vazada.
    """
    key = _scope_key(scope)
    data, legivel = _load_status()
    if not legivel:
        logger.error('Cofre ilegivel: exclusao do escopo %s RECUSADA.', key)
        return False
    mudou = False
    for nome in list(data.keys()):
        entradas = data.get(nome)
        if isinstance(entradas, dict) and key in entradas:
            entradas.pop(key, None)
            mudou = True
            if not entradas:
                data.pop(nome, None)
    if mudou:
        _save(data)
        logger.info('Escopo de segredos apagado do cofre: %s', key)
    try:
        if _SESSION_HEALTH_PATH.exists():
            with open(_SESSION_HEALTH_PATH, 'r', encoding='utf-8') as fh:
                saude = json.load(fh)
            if isinstance(saude, dict) and key in saude:
                saude.pop(key, None)
                tmp = _SESSION_HEALTH_PATH.with_suffix('.tmp')
                with open(tmp, 'w', encoding='utf-8') as fh:
                    json.dump(saude, fh, ensure_ascii=True, indent=2)
                os.replace(tmp, _SESSION_HEALTH_PATH)
    except Exception as exc:
        logger.debug('Saude da sessao nao removida: %s', type(exc).__name__)
    return mudou


def delete(name, scope=None):
    """Remove o segredo do escopo. Retorna True se algo foi removido."""
    # Mesma trava do `set_value`: apagar tambem e ler-mudar-gravar.
    with _Travado():
        data = _load()
        entradas = data.get(name) or {}
        if _scope_key(scope) not in entradas:
            return False
        entradas.pop(_scope_key(scope), None)
        if not entradas:
            data.pop(name, None)
        ok = _save(data)
    if ok:
        logger.info('Segredo %s removido (escopo=%s)', name, _scope_key(scope))
    return ok


def record_session_health(scope, ok, message=''):
    """Anota o resultado da ultima verificacao do cookie do Strava.

    Guardado por escopo (cidade). Nao contem o cookie: so ok/mensagem/quando.
    Best-effort -- uma falha aqui nunca interrompe o fluxo que chamou.
    """
    try:
        data = {}
        if _SESSION_HEALTH_PATH.exists():
            with open(_SESSION_HEALTH_PATH, 'r', encoding='utf-8') as fh:
                carregado = json.load(fh)
            if isinstance(carregado, dict):
                data = carregado
        data[_scope_key(scope)] = {
            'ok': bool(ok),
            'message': (message or '')[:300],
            'checked_at': time.time(),
        }
        _SESSION_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _SESSION_HEALTH_PATH.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=True, indent=2)
        os.replace(tmp_path, _SESSION_HEALTH_PATH)
    except Exception as exc:
        logger.debug('Falha ao registrar saude da sessao: %s', type(exc).__name__)


def session_health(scope=None):
    """Ultima saude conhecida do cookie do Strava (ou None se nunca verificado)."""
    try:
        if not _SESSION_HEALTH_PATH.exists():
            return None
        with open(_SESSION_HEALTH_PATH, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        for chave in (_scope_key(scope), _GLOBAL_SCOPE):
            entrada = data.get(chave)
            if isinstance(entrada, dict):
                return entrada
    except Exception:
        return None
    return None


def info(name, scope=None):
    """Metadados do segredo para a tela. NUNCA devolve o valor."""
    entradas = _load().get(name) or {}
    chave_pedida = _scope_key(scope)
    for chave in (chave_pedida, _GLOBAL_SCOPE):
        entrada = entradas.get(chave)
        if isinstance(entrada, dict) and entrada.get('value'):
            return {
                'configurado': True,
                'escopo': chave,
                'herdado': chave != chave_pedida,
                'atualizado_em': entrada.get('updated_at'),
                'atualizado_por': entrada.get('updated_by') or '',
                'impressao': entrada.get('fingerprint') or fingerprint(entrada['value']),
                'tamanho': len(entrada['value']),
            }
    return {
        'configurado': False,
        'escopo': chave_pedida,
        'herdado': False,
        'atualizado_em': None,
        'atualizado_por': '',
        'impressao': '',
        'tamanho': 0,
    }
