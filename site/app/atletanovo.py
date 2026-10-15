# -*- coding: utf-8 -*-
"""Avisa o dono quando entra atleta novo, para ele completar o cadastro.

POR QUE ISTO EXISTE (09/09/2026, pedido do dono)

Atleta entra sozinho: basta alguém postar um link no grupo e o sistema cria o
registro. Só que há informação que o Strava NÃO dá e que só o dono sabe — o
sexo (a documentação oficial diz que a escolha de gênero "will not appear on
your profile page") e, às vezes, o nome pelo qual a pessoa é conhecida na
cidade. Sem um aviso, isso só é descoberto meses depois, olhando a lista.

POR QUE UM AVISO PERIÓDICO, E NÃO UM E-MAIL A CADA ATLETA

O atleta é criado em quatro pontos diferentes do código (conferência,
anticheat, importação, upload). Disparar e-mail em cada um exigiria mexer nos
quatro e ainda arriscaria mandar o mesmo aviso várias vezes na mesma
importação — num dia de upload em massa, seriam dezenas de e-mails. Aqui é uma
passada só, uma vez por dia, com todos os novos numa lista.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import text as sa_text

from app import db

logger = logging.getLogger(__name__)

# Quem entrou nas últimas 24h e ainda está sem sexo preenchido.
_JANELA_HORAS = 24


def _base_url():
    """O endereço público do site, para o link do e-mail abrir de fora."""
    try:
        from flask import current_app
        base = (current_app.config.get('BASE_URL')
                or current_app.config.get('SERVER_NAME') or '')
        base = str(base).strip().rstrip('/')
        if base and not base.startswith('http'):
            base = 'https://' + base
        return base or 'https://movementes.engekon.com'
    except Exception:
        return 'https://movementes.engekon.com'


def novos_sem_cadastro(horas=_JANELA_HORAS):
    """[(strava_id, nome, criado_em)] de quem entrou e está sem sexo."""
    limite = datetime.now() - timedelta(hours=horas)
    try:
        return db.session.execute(sa_text("""
            SELECT strava_id, nome_atual, criado_em
            FROM atletas
            WHERE criado_em >= :desde
              AND (sexo IS NULL OR TRIM(sexo) = '')
              AND (arquivado IS NULL OR arquivado = 0)
            ORDER BY criado_em DESC
        """), {'desde': limite}).fetchall()
    except Exception as exc:
        logger.debug('atleta novo: consulta falhou (%s)', exc)
        return []


def avisar_atletas_novos(horas=_JANELA_HORAS):
    """Manda UM e-mail com todos os atletas novos sem cadastro completo.

    Não levanta erro para cima: é aviso, não é o trabalho. Devolve quantos
    atletas entraram no aviso (0 quando não havia nada a dizer).
    """
    novos = novos_sem_cadastro(horas)
    if not novos:
        return 0

    base = _base_url()
    linhas_html, linhas_txt = [], []
    for sid, nome, criado in novos:
        # O LINK VAI DIRETO PARA A PÁGINA DELE (pedido do dono): abrir o e-mail
        # e já cair no lugar de resolver evita o "depois eu vejo" que nunca vem.
        url = '%s/admin/atletas/%s/ajuste-manual' % (base, sid)
        quando = str(criado)[:16] if criado else ''
        linhas_html.append(
            '<li><a href="%s"><strong>%s</strong></a> '
            '<span style="color:#666">(%s) — entrou em %s</span></li>'
            % (url, nome or 'sem nome', sid, quando))
        linhas_txt.append('- %s (%s) — %s\n  %s'
                          % (nome or 'sem nome', sid, quando, url))

    quantos = len(novos)
    assunto = ('MoveMentes: %d atleta%s novo%s para completar o cadastro'
               % (quantos, '' if quantos == 1 else 's', '' if quantos == 1 else 's'))

    html = (
        '<p>Entrou gente nova no projeto. Falta dizer o <strong>sexo</strong> '
        'de cada um — o Strava não fornece esse dado, e sem ele a página '
        'inicial não consegue mostrar quantas atividades são de homens e '
        'quantas de mulheres.</p>'
        '<p>É rápido: dá para marcar direto na '
        '<a href="%s/admin/atletas">lista de atletas</a>, sem abrir a página de '
        'cada um. Quem está sem preencher aparece destacado.</p>'
        '<ul>%s</ul>'
        '<p style="color:#666;font-size:.9em">Este aviso só chega quando há '
        'alguém para cadastrar.</p>'
        % (base, ''.join(linhas_html))
    )
    texto = ('Entrou gente nova no projeto e falta dizer o sexo de cada um.\n'
             'Dá para marcar direto na lista: %s/admin/atletas\n\n%s\n'
             % (base, '\n'.join(linhas_txt)))

    try:
        from app import alertas, correio
        # QUEM RECEBE JA ESTA DECIDIDO EM UM LUGAR SO' (`alertas.destinatarios`).
        # Inventar uma segunda lista aqui significaria alguem sair do projeto e
        # continuar recebendo aviso por meses, porque ninguem lembraria deste
        # arquivo.
        para = alertas.destinatarios('') or []
        if not para:
            logger.info('atleta novo: sem destinatário configurado.')
            return 0
        ok, detalhe = correio.enviar(para, assunto, html, texto)
        if not ok:
            logger.warning('atleta novo: e-mail não saiu (%s)', detalhe)
            return 0
        logger.info('Aviso de %d atleta(s) novo(s) enviado para %s',
                    quantos, para)
        return quantos
    except Exception as exc:
        logger.warning('atleta novo: não consegui enviar o aviso (%s)', exc)
        return 0
