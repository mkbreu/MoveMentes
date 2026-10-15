# -*- coding: utf-8 -*-
"""O endereço público do site -- um lugar só (P-102).

POR QUE ISTO EXISTE
O domínio estava escrito à mão em `ranking.py` e em `alertas.py`, e mais duas
vezes em texto de tela. Trocar o domínio virava caça a string, com o risco de
alguém "substituir tudo" e pegar junto o `@exemplo.com` do e-mail de cadastro
-- que derruba o login. Agora quem sabe o endereço é este módulo, e o resto
pergunta.

⚠️ O `SERVER_NAME` do Flask NÃO está em uso (conferido em 28/08/2026): ele
aparece só em `config.py`, e `create_app()` monta a configuração à mão sem
nunca carregar aquela classe. Isso é uma boa notícia para a migração -- o ponto
"se o Host não bater, o site inteiro devolve 404", que era o mais perigoso do
plano, simplesmente não existe aqui. O site responde em qualquer Host que
chegue nele.

⚠️ E É POR ISSO QUE OS DOIS DOMÍNIOS FUNCIONAM AO MESMO TEMPO. Dentro de um
pedido, o endereço sai do Host que a pessoa usou: quem entrou pelo domínio
velho recebe links do velho, quem entrou pelo novo recebe do novo. Ninguém é
jogado de um para o outro no meio da navegação, e a virada não tem instante
crítico.

**Fora de um pedido** (alerta por e-mail, mensagem da automação) não há Host
para consultar -- aí vale a variável de ambiente `SITE_URL`, e o padrão abaixo
como última rede.

MIGRAÇÃO CONCLUÍDA EM 07/09/2026: `PADRAO` virou o domínio novo, a pedido do
dono ("não usar mais o dominio-antigo.exemplo.com"). Reverter é desfazer o mesmo
passo -- não há migração de dado envolvida.
"""
import os

# Domínio ATIVO desde 07/09/2026. O antigo (dominio-antigo.exemplo.com) fica de
# fora daqui de propósito -- não deve mais ser o padrão de nada.
PADRAO = 'https://movementes.engekon.com'

# Domínio anterior, só para referência histórica / comparação em log.
ANTIGO = 'https://dominio-antigo.exemplo.com'


def base_url():
    """O endereço do site, do jeito que quem está lendo o alcança."""
    valor = (os.environ.get('SITE_URL') or '').strip().rstrip('/')

    try:
        from flask import has_request_context, request
        if has_request_context():
            host = (request.host or '').strip()
            if host:
                esquema = request.headers.get('X-Forwarded-Proto') or request.scheme
                # Atrás do Cloudflare o pedido chega em http; o público é https.
                if host not in ('127.0.0.1', 'localhost') and not host.startswith(
                        ('127.0.0.1:', 'localhost:', '192.168.')):
                    esquema = 'https'
                return '%s://%s' % (esquema, host)
    except Exception:
        pass

    return valor or PADRAO


def dominio():
    """Só o nome, sem esquema -- para texto de tela e comando copiável."""
    return base_url().split('://', 1)[-1]
