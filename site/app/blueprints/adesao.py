# -*- coding: utf-8 -*-
"""Adesão pública ao projeto -- a porta de entrada que faltava (P-100).

POR QUE ISTO EXISTE
Até 28/08/2026 só se entrava no MoveMentes sendo adicionado ao grupo de WhatsApp
por alguém. O Decreto 12.861/2026, art. 25, I, chama isso de **circuito
privado** -- público previamente definido, que impede qualquer interessado de
participar -- e é vedação de projeto na Lei de Incentivo ao Esporte. Sem esta
tela o projeto seria reprovado no Ministério.

A mesma tela serve a três frentes de uma vez:
  1. abre o circuito (Ministério do Esporte);
  2. registra consentimento com data, hora, IP e VERSÃO do termo (LGPD);
  3. é onde entra o OAuth do Strava quando a migração de API acontecer (P-89).

⚠️ TRÊS CONSENTIMENTOS SEPARADOS, E SÓ O PRIMEIRO É OBRIGATÓRIO.
Participar do ranking é o aceite geral. Condição de saúde é dado sensível
(LGPD art. 11) e uso de imagem é direito próprio: cada um tem a sua caixa, a
sua data e a sua revogação. "Aceito tudo" numa caixa só é exatamente o que a
lei não admite.

⚠️ A CONTA NASCE INATIVA. Igual ao cadastro que já existia (`auth.register`):
quem entra aqui só passa a ter acesso depois que um admin ativa. Abrir a porta
não é abrir o painel.
"""
from datetime import datetime
import logging
import secrets

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import current_user

from app import db
from app.models import Adesao, Atleta, Config
from app import termos as termos_mod

adesao_bp = Blueprint('adesao', __name__, url_prefix='/participar')
logger = logging.getLogger(__name__)


def _cidades():
    """Cidades que aceitam inscrição -- só as ATIVAS, não todo `Config`.

    `Config` guarda até cidade vazia (ex.: Minaçu) que ainda não tem grupo
    nem administrador rodando de verdade. Oferecer inscrição ali deixaria a
    pessoa "voando", sem grupo para entrar. Mesmo critério da P-81:
    `consolidado.cidades_ativas()` (tem ranking rodando OU foi marcada ativa).
    """
    try:
        from app import consolidado as cons
        return cons.cidades_ativas()
    except Exception:
        return []


def _ip_do_pedido():
    """IP de quem aceitou. Atrás do Cloudflare, o real vem no cabeçalho."""
    for cabecalho in ('CF-Connecting-IP', 'X-Forwarded-For', 'X-Real-IP'):
        valor = (request.headers.get(cabecalho) or '').split(',')[0].strip()
        if valor:
            return valor[:45]
    return (request.remote_addr or '')[:45]


def _normalizar_telefone(bruto):
    """Formato canônico do projeto: 55DDD9XXXXXXXX (regra 22)."""
    try:
        from app.blueprints.auth import normalizar_telefone_usuario
        return normalizar_telefone_usuario(bruto)
    except Exception:
        so_digito = ''.join(c for c in (bruto or '') if c.isdigit())
        return so_digito[:20] or None


def _extrair_strava_id(link):
    """Perfil do Strava -> strava_id, SEM rede (so regex).

    ⚠️ De proposito NAO resolve link de atividade: essa rota e' publica e sem
    login, e buscar cada atividade postada aqui gastaria cota do throttle
    compartilhado do Strava (regra 4-C/4-P) por qualquer visitante que
    quisesse queimar a cota. Só o link de PERFIL, que e' regex pura.
    """
    link = (link or '').strip()
    if not link:
        return None, None
    from app.blueprints.processing import STRAVA_PROFILE_RE, STRAVA_DIRECT_RE
    m = STRAVA_PROFILE_RE.match(link)
    if m:
        return m.group(1), None
    if STRAVA_DIRECT_RE.search(link):
        return None, ('Esse é o link de uma atividade. Cole o link do seu '
                       '**perfil** no Strava (indo em "Eu" e copiando o '
                       'endereço da página) — ou deixe em branco.')
    return None, 'Isso não parece um link do Strava. Deixe em branco se preferir.'


@adesao_bp.route('/', methods=['GET', 'POST'])
def inscrever():
    """A inscrição. Sem login, sem convite, sem depender de ninguém."""
    cidades = _cidades()

    if request.method == 'GET':
        return render_template(
            'publico/adesao.html',
            cidades=cidades,
            versao=termos_mod.VERSAO,
            resumo=termos_mod.resumo(),
            idade_minima=termos_mod.IDADE_MINIMA,
            dados={},
        )

    dados = {
        'nome': (request.form.get('nome') or '').strip(),
        'cidade': (request.form.get('cidade') or '').strip(),
        'telefone': (request.form.get('telefone') or '').strip(),
        'link_strava': (request.form.get('link_strava') or '').strip(),
    }
    aceite = request.form.get('aceite_participacao') == 'sim'
    idade_ok = request.form.get('idade_ok') == 'sim'

    erros = []
    if len(dados['nome']) < 3:
        erros.append('Escreva o seu nome completo.')
    if not dados['cidade'] or (cidades and dados['cidade'] not in cidades):
        erros.append('Escolha a cidade onde você mora.')
    telefone = _normalizar_telefone(dados['telefone'])
    if not telefone:
        erros.append('Informe um telefone com DDD — é por ele que avisamos da medalha.')
    if not aceite:
        erros.append('Para participar é preciso aceitar os termos de uso.')
    if not idade_ok:
        erros.append('Confirme que você tem %d anos ou mais, ou fale com a '
                     'organização para inscrever alguém mais novo.'
                     % termos_mod.IDADE_MINIMA)
    strava_id, erro_strava = _extrair_strava_id(dados['link_strava'])
    if erro_strava:
        erros.append(erro_strava)

    if erros:
        for e in erros:
            flash(e, 'error')
        return render_template(
            'publico/adesao.html', cidades=cidades, versao=termos_mod.VERSAO,
            resumo=termos_mod.resumo(), idade_minima=termos_mod.IDADE_MINIMA,
            dados=dados), 400

    # Já se inscreveu antes? atualiza em vez de duplicar -- clicar duas vezes
    # não pode virar duas pessoas (mesma filosofia da fila de importação).
    registro = Adesao.query.filter_by(
        telefone=telefone, cidade=dados['cidade']).first()
    agora = datetime.utcnow()
    if registro is None:
        registro = Adesao(chave=secrets.token_urlsafe(32)[:64])
        db.session.add(registro)

    # "Aparecer nas páginas públicas" não é mais pergunta de entrada (Fase 2):
    # quem se inscreve quer aparecer, por padrão. A saída continua existindo
    # -- pelo caminho de volta, `adesao.meus_dados` -- só não é feita aqui.
    if registro.aparecer_publico is None:
        registro.aparecer_publico = True

    registro.cidade = dados['cidade']
    registro.nome = dados['nome'][:120]
    registro.telefone = telefone
    if strava_id:
        registro.strava_id = strava_id
    registro.versao_termos = termos_mod.VERSAO
    registro.aceite_em = agora
    registro.aceite_ip = _ip_do_pedido()
    registro.aceite_navegador = (request.headers.get('User-Agent') or '')[:250]
    registro.revogado_em = None
    registro.revogado_motivo = None
    # ⚠️ Condição de saúde e autorização de imagem NÃO são mais perguntadas
    # aqui (Fase 2) -- saíram para os termos. O que já tinha sido consentido
    # por um cadastro antigo fica como estava; esta rota não zera nem grava
    # nada nesses dois campos.

    db.session.commit()

    # Já é atleta conhecido? a escolha de aparecer vale para ele também.
    _alinhar_atleta(registro)

    return redirect(url_for('adesao.pronto', chave=registro.chave))


def _alinhar_atleta(registro):
    """Liga a escolha de privacidade ao cadastro de atleta, quando existir.

    Sem isto, a pessoa marcaria "não quero aparecer" na inscrição e continuaria
    aparecendo, porque a página pública lê `Atleta.oculto_no_publico`. Um pedido,
    dois lugares -- e quem concilia é o código, nunca a pessoa.
    """
    if not registro.telefone:
        return
    try:
        atleta = Atleta.query.filter_by(
            telefone_normalizado=registro.telefone).first()
        if atleta:
            atleta.oculto_no_publico = not registro.aparecer_publico
            registro.strava_id = atleta.strava_id
            db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning('adesao: nao consegui alinhar o atleta: %s', exc)


@adesao_bp.route('/pronto/<chave>')
def pronto(chave):
    """Confirmação, com o link do grupo -- é o que fecha o fluxo de verdade."""
    registro = Adesao.query.filter_by(chave=chave).first_or_404()
    config = Config.query.filter_by(cidade=registro.cidade).first()
    link_grupo = (getattr(config, 'link_grupo_whatsapp', None) or '').strip() or None
    return render_template('publico/adesao_pronto.html', a=registro,
                           versao=termos_mod.VERSAO, link_grupo=link_grupo)


@adesao_bp.route('/meus-dados/<chave>', methods=['GET', 'POST'])
def meus_dados(chave):
    """O caminho de volta: ver, mudar de ideia e sair -- sem login.

    A LGPD exige que retirar consentimento seja tão fácil quanto dar. Exigir
    login para isso, num projeto onde a conta nasce inativa, seria trancar a
    porta de saída.
    """
    registro = Adesao.query.filter_by(chave=chave).first_or_404()

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()
        agora = datetime.utcnow()

        if acao == 'privacidade':
            registro.aparecer_publico = request.form.get('aparecer_publico') != 'nao'
            db.session.commit()
            _alinhar_atleta(registro)
            flash('Pronto. A sua escolha sobre aparecer em público foi salva.', 'success')

        elif acao == 'retirar_saude':
            registro.consentimento_saude = False
            registro.consentimento_saude_em = None
            registro.observacao_saude = None
            db.session.commit()
            flash('A informação de saúde foi apagada.', 'success')

        elif acao == 'retirar_imagem':
            registro.consentimento_imagem = False
            registro.consentimento_imagem_em = None
            db.session.commit()
            flash('A autorização de uso de imagem foi retirada.', 'success')

        elif acao == 'sair':
            registro.revogado_em = agora
            registro.revogado_motivo = (request.form.get('motivo') or '')[:300]
            registro.consentimento_saude = False
            registro.observacao_saude = None
            registro.consentimento_imagem = False
            registro.aparecer_publico = False
            db.session.commit()
            _alinhar_atleta(registro)
            flash('Você saiu do projeto. Os seus dados de contato serão '
                  'apagados pela organização.', 'success')

        return redirect(url_for('adesao.meus_dados', chave=chave))

    return render_template('publico/meus_dados.html', a=registro,
                           versao=termos_mod.VERSAO)


@adesao_bp.route('/termos')
def termos():
    """Os termos, na versão em vigor. Público, sem login."""
    return render_template(
        'publico/termos.html',
        versao=termos_mod.VERSAO,
        texto=termos_mod.texto(),
        resumo=termos_mod.resumo(),
    )
