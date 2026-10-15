# -*- coding: utf-8 -*-
"""Envio de e-mail pelo Microsoft 365 — usado para o codigo de pareamento.

POR QUE NAO E SMTP
O caminho obvio seria smtplib com usuario e senha em smtp.office365.com. Nao
funciona mais: a Microsoft desativou a autenticacao basica do SMTP AUTH (bloqueio
gradual desde marco/2026, aplicacao total em 30/04/2026) e descontinuou tambem
as "senhas de aplicativo". Sobrou OAuth 2.0.

POR QUE GRAPH E NAO n8n
O n8n tem no nativo do Outlook e resolveria o OAuth sozinho — mas ele roda no
Dell, a maquina que passa o dia desligada. O codigo de pareamento precisa chegar
ao admin mesmo que o Dell caia logo depois de gerar o codigo.

Este modulo roda no Synology, que fica ligado sempre, e fala com o Graph em duas
requisicoes HTTP simples. Sem biblioteca de OAuth, sem servidor de e-mail.

O QUE PRECISA ESTAR CADASTRADO (Painel > Automacao, no cofre)
    ms_tenant_id       o "Directory (tenant) ID" do Entra ID
    ms_client_id       o "Application (client) ID" do app registrado
    ms_client_secret   o segredo do app (valor, nao o id)
    ms_remetente       (o e-mail que envia, ex.: remetente@exemplo.com)

No Entra ID o app precisa da permissao de APLICATIVO `Mail.Send` com
consentimento do administrador. Permissao "delegada" nao serve: nao ha usuario
logado na hora do envio.

NADA AQUI LEVANTA EXCECAO PARA CIMA. Falhar ao avisar nao pode virar mais um
erro no meio de um fluxo que ja esta tratando de outro assunto.
"""
import logging

import requests

from app import secretstore

logger = logging.getLogger(__name__)

TIMEOUT = 30
_TOKEN_URL = 'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'
_SENDMAIL_URL = 'https://graph.microsoft.com/v1.0/users/{remetente}/sendMail'
_ESCOPO = 'https://graph.microsoft.com/.default'


def _config():
    """As quatro chaves do cofre, ou None se faltar alguma."""
    dados = {
        'tenant': (secretstore.get(secretstore.MS_TENANT_ID) or '').strip(),
        'client_id': (secretstore.get(secretstore.MS_CLIENT_ID) or '').strip(),
        'secret': (secretstore.get(secretstore.MS_CLIENT_SECRET) or '').strip(),
        'remetente': (secretstore.get(secretstore.MS_REMETENTE) or '').strip(),
    }
    faltando = [k for k, v in dados.items() if not v]
    if faltando:
        return None, faltando
    return dados, []


def configurado():
    """Da para enviar e-mail? Serve para a tela mostrar o estado."""
    dados, _ = _config()
    return dados is not None


def _token(cfg):
    """Token de aplicativo. Sem usuario logado — client credentials."""
    resp = requests.post(
        _TOKEN_URL.format(tenant=cfg['tenant']),
        data={
            'client_id': cfg['client_id'],
            'client_secret': cfg['secret'],
            'scope': _ESCOPO,
            'grant_type': 'client_credentials',
        },
        timeout=TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError('token HTTP %s: %s' % (resp.status_code, resp.text[:300]))
    token = (resp.json() or {}).get('access_token')
    if not token:
        raise RuntimeError('a resposta do token veio sem access_token')
    return token


def enviar(para, assunto, corpo_html, corpo_texto=None, responder_para=None):
    """Manda um e-mail. Devolve (ok, detalhe) — nunca levanta excecao.

    `detalhe` traz o motivo quando falha, para o chamador registrar no log ou
    mostrar na tela sem ter que adivinhar.
    """
    if not para:
        return False, 'sem destinatario'

    cfg, faltando = _config()
    if not cfg:
        return False, ('falta cadastrar no cofre: %s' % ', '.join(faltando))

    try:
        token = _token(cfg)
    except Exception as exc:
        logger.warning('Nao consegui token do Microsoft 365: %s', exc)
        return False, 'autenticacao falhou: %s' % exc

    mensagem = {
        'message': {
            'subject': assunto,
            'body': {'contentType': 'HTML', 'content': corpo_html},
            'toRecipients': [{'emailAddress': {'address': p}}
                             for p in ([para] if isinstance(para, str) else para)],
        },
        # Guardar em "Itens Enviados" da caixa: quando o admin disser que nao
        # recebeu, da para conferir se saiu daqui.
        'saveToSentItems': True,
    }
    if responder_para:
        # "Responder" no e-mail vai para quem pediu (pedido de relatorio), nao
        # para a caixa que envia.
        mensagem['message']['replyTo'] = [{'emailAddress': {'address': responder_para}}]

    try:
        resp = requests.post(
            _SENDMAIL_URL.format(remetente=cfg['remetente']),
            headers={'Authorization': 'Bearer ' + token,
                     'Content-Type': 'application/json'},
            json=mensagem, timeout=TIMEOUT)
    except Exception as exc:
        logger.warning('Falha de rede ao enviar e-mail: %s', exc)
        return False, 'rede: %s' % exc

    # O Graph responde 202 (aceito) quando deu certo.
    if resp.status_code in (200, 202):
        logger.info('E-mail enviado para %s: %s', para, assunto)
        return True, None

    detalhe = 'HTTP %s: %s' % (resp.status_code, resp.text[:300])
    logger.warning('E-mail NAO enviado para %s — %s', para, detalhe)
    return False, detalhe


# ===================== O E-MAIL DO CODIGO DE PAREAMENTO =====================

def enviar_codigo_pareamento(para, cidade, codigo, telefone=None, qr_url=None):
    """O e-mail que o admin da cidade recebe para conectar o WhatsApp.

    O texto e escrito para quem NAO e tecnico: o admin da cidade sabe o numero
    de telefone dele e sabe digitar um codigo. Nada de "sessao", "instancia" ou
    "worker" — isso e problema nosso, nao dele.

    O CODIGO E O CAMINHO PRINCIPAL; o QR vai junto como reforco (regra 17-A).
    Quem recebe esta com o CELULAR na mao, e celular nao escaneia a si mesmo —
    por isso o codigo vem primeiro, grande, no topo. Mas "nunca QR" era forte
    demais (correcao do dono em 11/08): quando o codigo nao esta dando certo, o
    QR salva quem por acaso esta no computador, e nao atrapalha quem nao esta.
    Vai no fim, recolhido, com a instrucao de pedir ajuda a alguem — que e o
    que resolve para quem so tem o proprio aparelho.
    """
    numero = (' (%s)' % telefone) if telefone else ''
    assunto = 'MoveMentes %s — codigo para conectar o WhatsApp' % cidade

    html = """
<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222;">
  <p>Ola!</p>
  <p>Voce pediu para conectar o WhatsApp <strong>{telefone}</strong> ao
     <strong>MoveMentes {cidade}</strong>. O codigo e este:</p>
  <p style="font-size:34px;font-weight:bold;letter-spacing:6px;
            background:#f2f4f7;padding:16px 20px;border-radius:8px;
            display:inline-block;margin:8px 0;">{codigo}</p>
  <p><strong>No celular {telefone}:</strong></p>
  <ol style="line-height:1.7;">
    <li>Abra o WhatsApp</li>
    <li>Toque nos tres pontinhos &rsaquo; <strong>Aparelhos conectados</strong></li>
    <li>Toque em <strong>Conectar um aparelho</strong></li>
    <li>Escolha <strong>Conectar com numero de telefone</strong></li>
    <li>Digite o codigo acima</li>
  </ol>
  <p style="color:#b45309;"><strong>O codigo vale por poucos minutos.</strong>
     Se expirar, peca outro na tela do MoveMentes.</p>
  {qr_bloco}
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:22px 0;">
  <p style="font-size:12px;color:#6b7280;">
     Mensagem automatica do MoveMentes {cidade}.
     Se voce nao pediu isto, ignore este e-mail e avise o responsavel.</p>
</div>
""".format(cidade=cidade, codigo=codigo, telefone=(telefone or 'do responsavel'),
           qr_bloco=_bloco_qr(qr_url))

    texto = ('Codigo para conectar o WhatsApp%s no MoveMentes %s: %s\n\n'
             'No celular: WhatsApp > Aparelhos conectados > Conectar um '
             'aparelho > Conectar com numero de telefone.\n'
             'O codigo vale por poucos minutos.' % (numero, cidade, codigo))
    if qr_url:
        texto += ('\n\nSe o codigo nao funcionar, use o QR Code: %s\n'
                  'Abra em outra tela (computador, tablet, ou peca a alguem '
                  'para abrir) e leia com o SEU celular.' % qr_url)

    return enviar(para, assunto, html, texto)


def _bloco_qr(qr_url):
    """O QR como REFORCO, recolhido. Vazio quando nao ha QR disponivel.

    Recolhido de proposito: aberto, ele compete com o codigo e faz quem esta no
    celular tentar o caminho que nao funciona para ele. Quem precisa, abre.
    """
    if not qr_url:
        return ''
    return """
  <details style="margin:18px 0;">
    <summary style="cursor:pointer;color:#374151;">
      <strong>O codigo nao funcionou?</strong> Toque aqui para usar o QR Code.
    </summary>
    <div style="padding:12px 0;">
      <p style="margin:6px 0;">O QR precisa aparecer em <strong>outra tela</strong> —
         o celular nao consegue ler o proprio visor. Abra no computador, no
         tablet, ou <strong>peca a alguem para abrir e leia com o seu
         celular</strong>.</p>
      <p style="margin:6px 0;"><img src="{url}" alt="QR Code para conectar"
         style="width:260px;height:260px;border:1px solid #e5e7eb;padding:8px;
                border-radius:8px;background:#fff;"></p>
      <p style="margin:6px 0;font-size:13px;color:#6b7280;">
         No celular: WhatsApp &rsaquo; tres pontinhos &rsaquo; Aparelhos
         conectados &rsaquo; Conectar um aparelho &rsaquo; aponte a camera.</p>
    </div>
  </details>""".format(url=qr_url)
