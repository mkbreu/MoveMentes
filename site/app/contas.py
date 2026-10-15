# -*- coding: utf-8 -*-
"""Conta do usuario por e-mail: convite, senha esquecida e troca de e-mail.

POR QUE ISTO EXISTE (P-103, pedido do dono em 19-20/09/2026)
Ate aqui a senha se entregava na mao: o admin digitava uma senha ao criar o
usuario e, para resetar, copiava um link da tela e mandava por WhatsApp. Duas
consequencias ruins: alguem alem do dono da conta conhece a senha, e o link
passeia por um canal que nao e' o da pessoa. O dono pediu o caminho normal da
internet -- **o proprio usuario define a senha dele, por um link que chega no
e-mail dele**, e nem o master supremo conhece essa senha.

AS TRES PORTAS, E O QUE CADA UMA EXIGE
  1. **Convite** (admin cria a conta): link de 7 dias para a pessoa escolher a
     primeira senha. A conta ja nasce com `senha_provisoria`, entao mesmo quem
     entrar pela senha temporaria e' obrigado a trocar.
  2. **Esqueci a senha** (a propria pessoa pede): link de 2 horas. A tela
     responde SEMPRE a mesma coisa -- se respondesse "esse e-mail nao existe",
     qualquer um descobriria quem tem conta aqui so testando enderecos.
  3. **Trocar o e-mail**: a confirmacao vai para o e-mail **ATUAL** (regra do
     dono, 20/09). E' ele que prova que o pedido partiu do dono da conta; pedir
     so ao endereco novo deixaria qualquer sessao esquecida aberta trocar o
     dono da conta. Confirmado, o endereco novo recebe o aviso de que passou a
     valer, e o antigo tambem -- avisar os dois e' o que permite reagir quando
     a troca nao foi voce.

O QUE NAO SE FAZ AQUI
Nada de senha por e-mail: o e-mail leva um LINK, nunca uma senha. E nenhuma
destas funcoes levanta excecao -- falha de e-mail vira `(False, motivo)` para a
tela dizer o que houve e oferecer o link para copiar (regra 10: falhou, avisa;
silencio e' o pior desfecho).
"""
import logging
import secrets
from datetime import datetime, timedelta

from app import db

logger = logging.getLogger(__name__)

# Quanto vale cada link. O convite e' longo porque a pessoa pode estar sem ver
# e-mail no dia; o "esqueci" e' curto porque quem pediu esta na tela agora.
HORAS_CONVITE = 24 * 7
HORAS_RESET = 2
HORAS_TROCA_EMAIL = 24

_SCHEMA_PRONTO = False


def ensure_schema():
    """Colunas da troca de e-mail. Idempotente, roda a cada uso.

    `ALTER TABLE` guardado por `PRAGMA table_info` (convencao do projeto) — e
    as mesmas colunas estao declaradas em `Usuario`, senao o SQLAlchemy ignora
    a atribuicao sem erro nenhum (`ERROS.md` §4.35).
    """
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    novas = {
        'email_novo': 'VARCHAR(120)',
        'email_token': 'VARCHAR(64)',
        'email_token_expiry': 'DATETIME',
    }
    try:
        with db.engine.begin() as conn:
            existentes = {linha[1] for linha in
                          conn.execute(db.text('PRAGMA table_info(usuarios)'))}
            for coluna, tipo in novas.items():
                if coluna not in existentes:
                    conn.execute(db.text(
                        'ALTER TABLE usuarios ADD COLUMN %s %s' % (coluna, tipo)))
                    logger.warning('contas: coluna %s criada em usuarios', coluna)
    except Exception as exc:
        logger.error('contas: nao consegui preparar o schema: %s', exc)
        return
    _SCHEMA_PRONTO = True


def _endereco(endpoint, **kw):
    from flask import url_for
    return url_for(endpoint, _external=True, **kw)


def _texto(titulo, saudacao, explicacao, botao, link, rodape):
    """Um molde so para os tres e-mails: mesma cara, mesma linguagem."""
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'color:#222;line-height:1.5">'
        '<h2 style="color:#0d6efd;margin:0 0 .6rem">%s</h2>'
        '<p>%s</p><p>%s</p>'
        '<p style="margin:1.4rem 0"><a href="%s" style="background:#0d6efd;'
        'color:#fff;padding:.7rem 1.2rem;border-radius:6px;'
        'text-decoration:none">%s</a></p>'
        '<p style="font-size:13px;color:#666">Se o botão não funcionar, copie '
        'este endereço no navegador:<br><span style="word-break:break-all">%s'
        '</span></p>'
        '<p style="font-size:13px;color:#666">%s</p>'
        '<p style="font-size:13px;color:#666">MoveMentes — mover o corpo para '
        'mover a mente.</p></div>'
    ) % (titulo, saudacao, explicacao, link, botao, link, rodape)
    texto = '%s\n\n%s\n\n%s\n\n%s\n\n%s\n' % (titulo, saudacao, explicacao,
                                              link, rodape)
    return html, texto


def _gravar_token(usuario, horas):
    token = secrets.token_urlsafe(32)
    usuario.reset_token = token
    usuario.reset_token_expiry = datetime.utcnow() + timedelta(hours=horas)
    db.session.commit()
    return token


def convidar(usuario, por='', horas=HORAS_CONVITE):
    """Manda o convite para a pessoa criar a PRIMEIRA senha dela.

    Devolve `(ok, detalhe, link)`. O link volta sempre: se o e-mail falhar,
    quem criou a conta ainda consegue entregar o acesso pela tela.
    """
    from app import correio
    token = _gravar_token(usuario, horas)
    link = _endereco('auth.reset_password', token=token)
    if not usuario.email:
        return False, 'usuário sem e-mail cadastrado', link
    html, texto = _texto(
        'Sua conta no MoveMentes está pronta',
        'Olá, %s!' % (usuario.nome or '').split(' ')[0],
        'Você foi cadastrado como <strong>%s</strong> em <strong>%s</strong>. '
        'Clique no botão abaixo para criar a sua senha — ela é só sua, ninguém '
        'do projeto vê ou guarda essa senha.'
        % (usuario.rotulo_tipo, usuario.cidade),
        'Criar minha senha', link,
        'Este link vale por %d dias. Se você não esperava este convite, ignore '
        'este e-mail.' % (horas // 24))
    ok, detalhe = correio.enviar(usuario.email,
                                 'MoveMentes — crie a sua senha de acesso',
                                 html, texto)
    logger.warning('contas: convite para %s por %s — %s (%s)',
                   usuario.email, por or 'sistema', 'enviado' if ok else 'FALHOU',
                   detalhe)
    return ok, detalhe, link


def pedir_nova_senha(usuario, por='', horas=HORAS_RESET):
    """Link de redefinicao de senha, por pedido do proprio dono ou do admin."""
    from app import correio
    token = _gravar_token(usuario, horas)
    link = _endereco('auth.reset_password', token=token)
    if not usuario.email:
        return False, 'usuário sem e-mail cadastrado', link
    html, texto = _texto(
        'Redefinir a sua senha',
        'Olá, %s!' % (usuario.nome or '').split(' ')[0],
        'Recebemos um pedido para redefinir a sua senha do MoveMentes. '
        'Clique no botão abaixo para escolher uma nova.',
        'Escolher nova senha', link,
        'Este link vale por %d horas. <strong>Se não foi você que pediu</strong>, '
        'não clique — a sua senha atual continua valendo.' % horas)
    ok, detalhe = correio.enviar(usuario.email,
                                 'MoveMentes — redefinir a sua senha', html, texto)
    logger.warning('contas: reset de senha para %s pedido por %s — %s (%s)',
                   usuario.email, por or 'o próprio usuário',
                   'enviado' if ok else 'FALHOU', detalhe)
    return ok, detalhe, link


def pedir_troca_de_email(usuario, email_novo):
    """Guarda o endereco novo e manda a confirmacao para o e-mail ATUAL.

    Devolve `(ok, aviso)`. O endereco so' passa a valer depois que alguem com
    acesso a caixa ATUAL clicar no link.
    """
    from app.models import Usuario
    from app import correio

    ensure_schema()
    email_novo = (email_novo or '').strip().lower()
    if '@' not in email_novo or '.' not in email_novo.split('@')[-1]:
        return False, 'Esse e-mail não parece válido.'
    if email_novo == (usuario.email or '').lower():
        return False, 'Esse já é o seu e-mail.'
    if Usuario.query.filter(db.func.lower(Usuario.email) == email_novo).first():
        return False, 'Já existe uma conta com esse e-mail.'
    if not usuario.email:
        return False, ('A sua conta não tem e-mail atual para confirmar o '
                       'pedido. Peça a troca ao master da sua cidade.')

    token = secrets.token_urlsafe(32)
    usuario.email_novo = email_novo
    usuario.email_token = token
    usuario.email_token_expiry = datetime.utcnow() + timedelta(hours=HORAS_TROCA_EMAIL)
    db.session.commit()

    link = _endereco('auth.confirmar_email', token=token)
    html, texto = _texto(
        'Confirme a troca do seu e-mail',
        'Olá, %s!' % (usuario.nome or '').split(' ')[0],
        'Foi pedida a troca do e-mail desta conta para '
        '<strong>%s</strong>. Se foi você, clique no botão para confirmar. '
        'Enquanto você não confirmar, nada muda.' % email_novo,
        'Confirmar a troca', link,
        'Este link vale por %d horas. <strong>Se não foi você</strong>, não '
        'clique e troque a sua senha agora mesmo.' % HORAS_TROCA_EMAIL)
    ok, detalhe = correio.enviar(usuario.email,
                                 'MoveMentes — confirme a troca do seu e-mail',
                                 html, texto)
    logger.warning('contas: troca de e-mail %s -> %s pedida — confirmação %s (%s)',
                   usuario.email, email_novo, 'enviada' if ok else 'FALHOU', detalhe)
    if not ok:
        return False, ('Não consegui enviar a confirmação para o seu e-mail '
                       'atual (%s). Nada foi trocado.' % detalhe)
    return True, ('Enviei a confirmação para %s. A troca só acontece depois '
                  'que você clicar no link de lá.' % usuario.email)


def confirmar_troca_de_email(token):
    """Aplica a troca. Devolve `(ok, aviso, usuario)`."""
    from app.models import Usuario
    from app import correio

    ensure_schema()
    token = (token or '').strip()
    if not token:
        return False, 'Link inválido.', None
    usuario = Usuario.query.filter_by(email_token=token).first()
    if not usuario or not usuario.email_novo:
        return False, 'Este link não vale mais. Peça a troca de novo.', None
    if not usuario.email_token_expiry or usuario.email_token_expiry < datetime.utcnow():
        usuario.email_novo = usuario.email_token = usuario.email_token_expiry = None
        db.session.commit()
        return False, 'Este link expirou. Peça a troca de novo.', usuario

    antigo, novo = usuario.email, usuario.email_novo
    # A CORRIDA QUE ESTRAGARIA TUDO: alguem pode ter criado uma conta com esse
    # endereco entre o pedido e a confirmacao. Conferir de novo, agora.
    if Usuario.query.filter(db.func.lower(Usuario.email) == novo,
                            Usuario.id != usuario.id).first():
        usuario.email_novo = usuario.email_token = usuario.email_token_expiry = None
        db.session.commit()
        return False, ('Enquanto isso, outra conta passou a usar esse e-mail. '
                       'Nada foi trocado.'), usuario

    usuario.email = novo
    usuario.email_novo = usuario.email_token = usuario.email_token_expiry = None
    usuario.ult_modificacao = datetime.utcnow()
    db.session.commit()
    logger.warning('contas: e-mail trocado de %s para %s (usuário %s)',
                   antigo, novo, usuario.id)

    # AVISAR OS DOIS LADOS: o novo, para saber que ja vale; o antigo, para
    # poder reagir se a troca nao foi ele.
    html, texto = _texto(
        'Seu e-mail no MoveMentes foi trocado',
        'Olá, %s!' % (usuario.nome or '').split(' ')[0],
        'A partir de agora a sua conta usa <strong>%s</strong>. O endereço '
        'anterior era %s.' % (novo, antigo),
        'Entrar no MoveMentes', _endereco('auth.login'),
        'Se não foi você, avise o master da sua cidade imediatamente.')
    correio.enviar([novo, antigo], 'MoveMentes — seu e-mail foi trocado',
                   html, texto)
    return True, 'Pronto: a sua conta agora usa %s.' % novo, usuario
