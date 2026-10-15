# -*- coding: utf-8 -*-
"""RASTRO DE TODO ACESSO AO SITE — uma linha por requisicao.

POR QUE ESTE MODULO EXISTE (16/09/2026, regra do dono)

Em 15/09 o dono clicou em "Publicar o ranking agora" por volta das 19:30 e
nada aconteceu; o ranking so saiu depois de um segundo clique, as 21:21. Ao
investigar, nao havia COMO saber se o primeiro clique existiu: o log do site so
registra o que a funcao faz, e um clique recusado antes dela (sessao expirada
vira 302 para o login) nao deixava rastro nenhum. Medido em 15/09 23:47:56 —
um POST em `/admin/publicar-agora` sem sessao respondeu 302 e o log do site
nao ganhou uma linha. Nem o nginx guarda acesso aqui, nem o uWSGI.

Agora toda requisicao escreve uma linha: hora, quem, de onde, o que pediu, o
que recebeu e quanto demorou. E' o instrumento que faltava — sem ele nao da
para provar nem derrubar nada sobre um clique.

CUSTO (o NAS tem 490 MB e 2 nucleos): uma linha de texto por requisicao, no
mesmo formato de arquivo mensal que o resto do site ja usa. Arquivo estatico
(css, js, imagem, favicon) nao entra: e' ruido e e' a maior parte do volume.
"""
import logging
import os
import time

_LOGGER = None
_PULAR = ('/static/', '/favicon.ico', '/apple-touch-icon')
# Rotas GET que MUDAM algo (medido 19/09/2026): contam como clique.
_GET_QUE_E_ACAO = {'admin.toggle_user_status'}


def _logger():
    """O logger do acesso, criado uma vez por processo."""
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    log = logging.getLogger('acesso')
    log.setLevel(logging.INFO)
    log.propagate = False            # nao mistura com o log do site
    if not log.handlers:
        pasta = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
        try:
            os.makedirs(pasta, exist_ok=True)
            from app.logmensal import ArquivoMensal
            h = ArquivoMensal(pasta, 'acesso', maxBytes=2 * 1024 * 1024,
                              backupCount=10, encoding='utf-8', delay=True)
            h.setFormatter(logging.Formatter('[%(asctime)s] %(message)s'))
            log.addHandler(h)
        except Exception:
            # Sem arquivo, o site continua igual: o rastro e' testemunha, nunca
            # pode derrubar quem foi acessar a pagina.
            pass
    _LOGGER = log
    return log


def _quem():
    """O e-mail de quem esta logado, ou 'deslogado'."""
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            return current_user.email or 'logado'
    except Exception:
        pass
    return 'deslogado'


def _de_onde(req):
    """O IP de verdade: atras do tunel da Cloudflare, `remote_addr` e' local."""
    for cabecalho in ('CF-Connecting-IP', 'X-Forwarded-For'):
        valor = req.headers.get(cabecalho)
        if valor:
            return valor.split(',')[0].strip()
    return req.remote_addr or '?'


def registrar(app):
    """Liga o rastro neste app. Chamado uma vez, no `create_app`."""

    @app.before_request
    def _acesso_comecou():
        from flask import g, request
        if request.path.startswith(_PULAR) or request.path in _PULAR:
            return
        # A CONSULTA DE MINUTO EM MINUTO NAO ESCREVE NO DISCO (17/09/2026).
        # `/api/rodada/pendente` e' perguntada pelo Dell 1440 vezes por dia; em
        # linha de acesso seriam ~130 KB/dia de "nada aconteceu" gravados no HD
        # do NAS. Quando o pedido existe, o rastro fica no log canonico (a
        # maquina registra que pegou, o site registra "atendido"); falha da
        # rota (status diferente de 200) continua registrada — ver abaixo.
        if request.path == '/api/rodada/pendente':
            g._acesso_so_falha = True
        g._acesso_t0 = time.monotonic()

    @app.after_request
    def _acesso_terminou(resposta):
        from flask import g, request
        try:
            t0 = getattr(g, '_acesso_t0', None)
            if t0 is None:
                return resposta
            if getattr(g, '_acesso_so_falha', False) and resposta.status_code == 200:
                return resposta
            # O DESTINO DO REDIRECT ENTRA NA LINHA de proposito: e' ele que
            # diz se o clique morreu na porta do login (`/auth/login?next=...`)
            # ou se a acao aconteceu e a tela voltou para o painel.
            destino = ''
            if 300 <= resposta.status_code < 400:
                destino = ' -> %s' % (resposta.headers.get('Location') or '?')
            quem = _quem()
            # QUAL BOTAO (19/09/2026, regra do dono): "POST /admin/whatsapp-
            # sessao" nao dizia se foi Conectar, Apagar ou Faxina — no teste de
            # 18/09 a remocao de uma sessao ficou sem autor ate cruzar logs. So
            # entram `acao` e `sessao` (palavras do sistema), nunca outro campo.
            if request.method == 'POST' and request.form:
                _acao = (request.form.get('acao') or '')[:40]
                _sess = (request.form.get('sessao') or '')[:40]
                if _acao or _sess:
                    destino += ' [%s%s]' % (_acao, (' ' + _sess) if _sess else '')
            _logger().info(
                '%s | %s | %s %s | %s | %dms%s',
                quem, _de_onde(request), request.method,
                request.full_path.rstrip('?'), resposta.status_code,
                int((time.monotonic() - t0) * 1000), destino)
            # CLIQUE DE GENTE VAI TAMBEM PARA O LOG CANONICO (17/09/2026, D1):
            # e' o que costura "o dono clicou" com o que a maquina fez depois,
            # na mesma busca por periodo. Leitura (GET) e chamada da automacao
            # (deslogada, por token) ficam so' no log de acesso.
            # Acao feita por LINK (GET) tambem e clique (19/09/2026, item 70).
            # Medido: so `admin.toggle_user_status` muda algo por GET. Regra
            # para o futuro: botao de acao novo e POST, e entra aqui sozinho.
            if (request.method == 'POST' or request.endpoint in _GET_QUE_E_ACAO) \
                    and quem != 'deslogado':
                from app import logcanon
                logcanon.escrever(
                    'DONO', 'clique', '%s %s %s -> %s%s' % (
                        quem, request.method, request.path,
                        resposta.status_code, destino))
        except Exception:
            pass                      # testemunha nunca derruba a requisicao
        return resposta

    return app
