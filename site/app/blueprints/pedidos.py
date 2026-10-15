# -*- coding: utf-8 -*-
"""Pedidos vindos de FORA do sistema (25/09/2026).

Duas portas públicas, as duas com o mesmo cuidado (limite por IP, campo-isca,
nada de dado sensível na resposta):

1. CADASTRO DE VISUALIZADOR PELO PRÓPRIO ATLETA -- botão na página pública dele.
   Telefone conferido com o do atleta -> e-mail confirmado -> aviso ao master
   supremo e ao master/admin DA CIDADE do atleta -> aprovação. Só nasce
   visualizador, e inativo.
2. PEDIDO DE RELATÓRIO COMPLETO -- botão na página de transparência. O master
   recebe um e-mail já com o link do relatório (últimos 12 meses). O link e os
   comprovantes dentro dele valem 30 dias e depois somem: não fica para sempre
   na internet, nem para os buscadores.

⚠️ Nenhum PDF é gerado no servidor (o NAS tem 490 MB): o relatório é uma página
HTML feita para imprimir, e é o navegador de quem lê que salva em PDF.
"""
from datetime import datetime, date, timedelta
import logging
import re
import secrets
import threading

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    current_app, session,
)
from flask_login import login_required, current_user, login_user

from app import db
from app import caixa as caixa_mod
from app import correio
from app import contas
from app import telefone as tel_mod
from app.models import (
    Atleta, Activity, Usuario, CadastroAtleta, PedidoRelatorio, TentativaPublica,
    LancamentoCaixa, TIPO_SUPREMO, TIPO_MASTER, TIPO_ADMIN,
)
from app.blueprints.auth import admin_required, normalizar_telefone_usuario

pedidos_bp = Blueprint('pedidos', __name__, url_prefix='/pedidos')
logger = logging.getLogger(__name__)

DIAS_DO_LINK = 30
HORAS_CONFIRMAR_EMAIL = 48
_ID_ATLETA = re.compile(r'^\d{1,20}$')
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Testes ligam isto para o envio acontecer na hora, em vez de numa thread.
SINCRONO = False


# ---------------------------------------------------------------------------
# apoio
# ---------------------------------------------------------------------------
def _ip():
    from app.acesso import _de_onde
    return _de_onde(request)


def _passou_do_limite(tipo, chave, max_chave, max_ip, horas):
    """Já houve tentativas demais desta chave ou deste IP na janela?"""
    desde = datetime.utcnow() - timedelta(hours=horas)
    base = TentativaPublica.query.filter(TentativaPublica.tipo == tipo,
                                         TentativaPublica.criado_em >= desde)
    return (base.filter(TentativaPublica.chave == chave).count() >= max_chave
            or base.filter(TentativaPublica.ip == _ip()).count() >= max_ip)


def _contar_tentativa(tipo, chave):
    db.session.add(TentativaPublica(tipo=tipo, chave=chave, ip=_ip()))
    db.session.commit()


def _sem_indexar(resposta):
    resposta.headers['X-Robots-Tag'] = 'noindex, nofollow'
    resposta.headers['Cache-Control'] = 'no-store'
    return resposta


def _mensagem(titulo, texto, status=200, botao=None, destino=None):
    html = render_template('pedidos/mensagem.html', titulo=titulo, texto=texto,
                           botao=botao, destino=destino)
    return _sem_indexar(current_app.response_class(html, status=status))


def _aprovadores(cidade):
    """Quem recebe o aviso: o master supremo e o master e o admin DA CIDADE.
    Nenhuma outra cidade: quem é de fora não saberia quem é a pessoa."""
    return Usuario.query.filter(
        Usuario.ativo.is_(True), Usuario.email.isnot(None), Usuario.email != '',
        db.or_(Usuario.tipo == TIPO_SUPREMO,
               db.and_(Usuario.cidade == cidade,
                       Usuario.tipo.in_((TIPO_MASTER, TIPO_ADMIN))))).all()


def _donos_do_relatorio(cidade):
    """Quem recebe o pedido de relatório: o master supremo e o master da cidade."""
    return Usuario.query.filter(
        Usuario.ativo.is_(True), Usuario.email.isnot(None), Usuario.email != '',
        db.or_(Usuario.tipo == TIPO_SUPREMO,
               db.and_(Usuario.cidade == cidade, Usuario.tipo == TIPO_MASTER))).all()


def _executar(app, tarefa):
    """Roda `tarefa` já, ou numa thread (não faz o visitante esperar o e-mail)."""
    if SINCRONO:
        tarefa()
        return

    def _com_contexto():
        with app.app_context():
            try:
                tarefa()
            except Exception as exc:
                logger.error('pedidos: tarefa em segundo plano falhou: %s', exc, exc_info=True)
    threading.Thread(target=_com_contexto, daemon=True).start()


# ---------------------------------------------------------------------------
# 1. CADASTRO DE VISUALIZADOR PELO ATLETA
# ---------------------------------------------------------------------------
def _atleta_publico(strava_id):
    """(atleta canônico, nome, cidade, telefones cadastrados) ou None.

    Quem pediu reserva não tem página pública -- e portanto não tem esta porta.
    """
    if not _ID_ATLETA.match(str(strava_id or '')):
        return None
    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if atleta is None or atleta.oculto_no_publico:
        return None
    canon = Atleta.resolver_canonico(atleta)
    ids = canon.strava_ids_do_grupo()
    membros = Atleta.query.filter(Atleta.strava_id.in_(ids)).all()
    telefones = [m.telefone_normalizado for m in membros if m.telefone_normalizado]
    ultima = (Activity.query.filter(Activity.id_usuario_strava.in_(ids))
              .order_by(Activity.data_hora.desc()).first())
    if ultima is None or not ultima.cidade:
        return None
    nome = (canon.nome_exibicao or canon.nome_atual or 'Atleta').strip()
    return canon, nome, ultima.cidade, telefones


def _email_na_agenda(strava_id, telefone, email, sobrescrever):
    """Grava o e-mail do cadastro na agenda de contatos (Telefones dos atletas).

    PEDIDO DO DONO (01/10/2026): o e-mail que o atleta informa no cadastro de
    visualizador vai junto do telefone na agenda. Vai para o perfil cujo
    telefone bateu (é a linha da agenda); sem isso, para o canônico.
    No cadastro (telefone provado) só preenche e-mail VAZIO; na confirmação
    pelo link (e-mail provado) o confirmado passa a valer. Falha aqui nunca
    derruba o cadastro.
    """
    try:
        atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
        if atleta is None:
            return
        canon = Atleta.resolver_canonico(atleta)
        membros = Atleta.query.filter(Atleta.strava_id.in_(canon.strava_ids_do_grupo())).all()
        alvo = next((m for m in membros if m.telefone_normalizado
                     and tel_mod.mesmo_numero(telefone, m.telefone_normalizado)), canon)
        atual = (getattr(alvo, 'email', None) or '').strip().lower()
        if atual == email or (atual and not sobrescrever):
            return
        alvo.email = email
        db.session.commit()
        logger.info('pedidos: e-mail %s gravado na agenda do atleta %s (%s)',
                    email, alvo.strava_id, 'confirmado' if sobrescrever else 'cadastro')
    except Exception as exc:
        db.session.rollback()
        logger.warning('pedidos: nao consegui gravar o e-mail na agenda (%s): %s', strava_id, exc)


@pedidos_bp.route('/acesso/<strava_id>', methods=['GET', 'POST'])
def acesso_atleta(strava_id):
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    dados = _atleta_publico(strava_id)
    if dados is None:
        abort(404)
    canon, nome, cidade, telefones = dados

    if request.method == 'GET':
        return _sem_indexar(current_app.response_class(
            render_template('pedidos/acesso.html', nome=nome, strava_id=strava_id, form={},
                            abrir_cadastro=False)))

    if request.form.get('site'):          # campo-isca: gente de verdade não preenche
        return redirect(url_for('index'))

    chave = 'atl:%s' % canon.strava_id
    if _passou_do_limite('acesso', chave, max_chave=5, max_ip=10, horas=1):
        return _mensagem('Muitas tentativas', 'Tente de novo daqui a uma hora.', status=429)
    _contar_tentativa('acesso', chave)

    email = (request.form.get('email') or '').strip().lower()
    senha = request.form.get('senha') or ''
    confirma = request.form.get('confirma') or ''
    telefone = normalizar_telefone_usuario(request.form.get('telefone'))
    form = {'email': email, 'telefone': request.form.get('telefone') or ''}

    def _volta(msg, ja_tem_acesso=False):
        flash(msg, 'error')
        return _sem_indexar(current_app.response_class(
            render_template('pedidos/acesso.html', nome=nome, strava_id=strava_id, form=form,
                            ja_tem_acesso=ja_tem_acesso, abrir_cadastro=True)))

    if not _EMAIL.match(email) or len(email) > 120:
        return _volta('Informe um e-mail válido.')
    if len(senha) < 8:
        return _volta('A senha precisa ter pelo menos 8 caracteres.')
    if senha != confirma:
        return _volta('As senhas não são iguais.')
    # O telefone é a prova de que é a própria pessoa. A resposta é a MESMA
    # para "errado" e "atleta sem telefone cadastrado": não dá para usar a tela
    # para descobrir qual telefone pertence a quem.
    if not telefone or not any(tel_mod.mesmo_numero(telefone, t) for t in telefones):
        return _volta('Os dados não conferem.')
    # Só chega aqui quem PROVOU o telefone, então dizer que ele já tem acesso não
    # entrega nada a terceiros. O caso real (25/09): o telefone do atleta já era de
    # um usuário do sistema -- a pessoa não precisa pedir acesso, precisa ENTRAR.
    if Usuario.query.filter_by(telefone=telefone).first():
        return _volta('Este telefone já tem acesso ao sistema.', ja_tem_acesso=True)
    if Usuario.query.filter_by(email=email).first():
        return _volta('Este e-mail já tem acesso ao sistema.', ja_tem_acesso=True)

    usuario = Usuario(nome=nome[:100], email=email, cidade=cidade, telefone=telefone,
                      tipo='viewer', ativo=False, ult_modificacao=datetime.utcnow())
    usuario.set_password(senha)
    db.session.add(usuario)
    db.session.flush()
    cad = CadastroAtleta(usuario_id=usuario.id, strava_id=str(canon.strava_id), cidade=cidade,
                         token_confirma=secrets.token_urlsafe(32),
                         confirma_expira=datetime.utcnow() + timedelta(hours=HORAS_CONFIRMAR_EMAIL))
    db.session.add(cad)
    db.session.commit()

    link = url_for('pedidos.confirmar_acesso', token=cad.token_confirma, _external=True)
    html, texto = contas._texto(
        'Confirme o seu e-mail', 'Olá, %s!' % nome.split(' ')[0],
        'Recebemos o seu pedido de acesso ao MoveMentes. Confirme que este e-mail é seu.',
        'Confirmar meu e-mail', link,
        'O link vale %d horas. Se não foi você, ignore este e-mail.' % HORAS_CONFIRMAR_EMAIL)
    ok, detalhe = correio.enviar(email, 'MoveMentes — confirme o seu e-mail', html, texto)
    if not ok:
        # Rastro nosso e recém-criado: some, para a pessoa poder tentar de novo.
        db.session.delete(cad)
        db.session.delete(usuario)
        db.session.commit()
        logger.warning('pedidos: e-mail de confirmacao para %s NAO saiu: %s', email, detalhe)
        return _volta('Não consegui enviar o e-mail agora. Tente de novo em alguns minutos.')
    logger.warning('pedidos: cadastro de atleta %s (%s) aguardando confirmacao de e-mail',
                   canon.strava_id, cidade)
    _email_na_agenda(canon.strava_id, telefone, email, sobrescrever=False)
    return _mensagem('Confira o seu e-mail',
                     'Enviamos um link para %s. Clique nele para confirmar.' % email)


@pedidos_bp.route('/entrar/<strava_id>', methods=['POST'])
def entrar_atleta(strava_id):
    """Entrar COMO ATLETA: a mesma conta e a mesma senha de sempre, mas a sessão
    vale só como visualizador (`Usuario.tipo_efetivo`). Serve ao atleta comum e a
    quem administra e também é atleta -- que assim vê o sistema como o atleta vê.

    A conta tem de ser DESTE atleta (o telefone da conta é o do atleta).
    """
    if current_user.is_authenticated and not session.get('modo_atleta'):
        return redirect(url_for('index'))
    dados = _atleta_publico(strava_id)
    if dados is None:
        abort(404)
    canon, nome, cidade, telefones = dados

    chave = 'ent:%s' % canon.strava_id
    if _passou_do_limite('entrar', chave, max_chave=8, max_ip=15, horas=1):
        return _mensagem('Muitas tentativas', 'Tente de novo daqui a uma hora.', status=429)
    _contar_tentativa('entrar', chave)

    identificador = (request.form.get('identificador') or '').strip().lower()
    senha = request.form.get('senha') or ''

    def _volta(msg):
        flash(msg, 'error')
        return _sem_indexar(current_app.response_class(
            render_template('pedidos/acesso.html', nome=nome, strava_id=strava_id,
                            form={'identificador': identificador}, abrir_cadastro=False)))

    usuario = None
    if identificador:
        tel = normalizar_telefone_usuario(identificador) if re.sub(r'\D', '', identificador) else None
        usuario = (Usuario.query.filter(db.func.lower(Usuario.email) == identificador).first()
                   or (Usuario.query.filter_by(telefone=tel).first() if tel else None))
    if usuario is None or not usuario.check_password(senha):
        return _volta('Os dados não conferem.')
    if not any(tel_mod.mesmo_numero(usuario.telefone, t) for t in telefones):
        return _volta('Esta conta não pertence a este atleta.')
    if not usuario.ativo:
        cad = (CadastroAtleta.query.filter_by(usuario_id=usuario.id)
               .order_by(CadastroAtleta.id.desc()).first())
        if cad is not None and not cad.rejeitado_em:
            return _volta('Seu acesso aguarda liberação.' if cad.confirmado_em
                          else 'Confirme o seu e-mail pelo link que enviamos.')
        return _volta('Sua conta está inativa. Fale com o administrador.')

    login_user(usuario, remember=False)
    session.pop('cidade_ativa', None)
    session['modo_atleta'] = True
    logger.info('pedidos: %s entrou COMO ATLETA pela pagina de %s', usuario.email, canon.strava_id)
    if usuario.senha_provisoria:
        flash('Primeiro acesso: escolha uma senha sua antes de continuar.', 'warning')
        return redirect(url_for('auth.change_password'))
    return redirect(url_for('index'))


@pedidos_bp.route('/acesso/confirmar/<token>')
def confirmar_acesso(token):
    cad = CadastroAtleta.query.filter_by(token_confirma=token).first() if len(token) >= 20 else None
    if cad is None:
        return _mensagem('Link inválido', 'Este link não existe.', status=404)
    if cad.confirmado_em:
        return _mensagem('E-mail já confirmado',
                         'Falta só o administrador liberar o seu acesso.')
    if cad.confirma_expira < datetime.utcnow():
        return _mensagem('Link vencido', 'Faça o pedido de novo pela sua página de atleta.', status=410)

    cad.confirmado_em = datetime.utcnow()
    db.session.commit()

    usuario = cad.usuario
    _email_na_agenda(cad.strava_id, usuario.telefone, (usuario.email or '').strip().lower(),
                     sobrescrever=True)
    link = url_for('pedidos.aprovar', cid=cad.id, _external=True)
    html, texto = contas._texto(
        'Novo pedido de acesso',
        'Olá!',
        '<strong>%s</strong> (%s) pediu acesso de <strong>visualizador</strong> em '
        '<strong>%s</strong>. O telefone e o e-mail foram conferidos.'
        % (usuario.nome, usuario.email, cad.cidade),
        'Ver o pedido', link, 'Você precisa estar logado para aprovar ou recusar.')
    enviados = 0
    for destino in _aprovadores(cad.cidade):
        ok, detalhe = correio.enviar(destino.email, 'MoveMentes — pedido de acesso: %s' % usuario.nome,
                                     html, texto)
        enviados += 1 if ok else 0
        if not ok:
            logger.warning('pedidos: aviso de cadastro para %s NAO saiu: %s', destino.email, detalhe)
    if enviados:
        cad.aviso_enviado_em = datetime.utcnow()
        db.session.commit()
    return _mensagem('E-mail confirmado',
                     'Agora peça ao administrador do seu grupo para liberar o acesso.')


@pedidos_bp.route('/aprovar/<int:cid>', methods=['GET', 'POST'])
@login_required
@admin_required
def aprovar(cid):
    cad = CadastroAtleta.query.get_or_404(cid)
    usuario = cad.usuario
    # Só o supremo, ou o master/admin DA CIDADE dele (e de nível acima do visualizador).
    if not current_user.pode_gerir(usuario):
        abort(403)

    if request.method == 'POST':
        if not cad.confirmado_em:
            flash('O e-mail ainda não foi confirmado.', 'error')
        elif cad.aprovado_em or cad.rejeitado_em:
            flash('Este pedido já foi decidido.', 'info')
        elif request.form.get('acao') == 'aprovar':
            usuario.ativo = True
            usuario.ult_modificacao = datetime.utcnow()
            cad.aprovado_em = datetime.utcnow()
            cad.aprovado_por_id = current_user.id
            db.session.commit()
            logger.warning('pedidos: acesso de %s (%s) APROVADO por %s', usuario.email, cad.cidade, current_user.email)
            html, texto = contas._texto(
                'Seu acesso foi liberado', 'Olá, %s!' % usuario.nome.split(' ')[0],
                'Você já pode entrar no MoveMentes com o seu e-mail e a senha que criou.',
                'Entrar', url_for('auth.login', _external=True), 'MoveMentes')
            correio.enviar(usuario.email, 'MoveMentes — acesso liberado', html, texto)
            flash('Acesso liberado.', 'success')
        elif request.form.get('acao') == 'recusar':
            cad.rejeitado_em = datetime.utcnow()
            db.session.commit()
            logger.warning('pedidos: acesso de %s (%s) RECUSADO por %s', usuario.email, cad.cidade, current_user.email)
            flash('Pedido recusado.', 'success')
        return redirect(url_for('pedidos.aprovar', cid=cid))

    return render_template('pedidos/aprovar.html', cad=cad, usuario=usuario)


# ---------------------------------------------------------------------------
# 2. PEDIDO DE RELATÓRIO COMPLETO
# ---------------------------------------------------------------------------
def _ultimos_12_meses(hoje):
    """De (1º dia do mesmo mês do ano passado + 1 mês) até hoje: 12 meses fechando no atual."""
    mes = hoje.month - 11
    ano = hoje.year
    if mes <= 0:
        mes += 12
        ano -= 1
    return date(ano, mes, 1), hoje


@pedidos_bp.route('/relatorio/<city_slug>', methods=['GET', 'POST'])
def solicitar_relatorio(city_slug):
    from app.blueprints.ranking import _resolve_city_by_slug
    cidade = _resolve_city_by_slug(city_slug)
    if not cidade:
        abort(404)

    if request.method == 'GET':
        return _sem_indexar(current_app.response_class(
            render_template('pedidos/solicitar_relatorio.html', cidade=cidade, city_slug=city_slug, form={})))

    if request.form.get('site'):
        return redirect(url_for('ranking.transparencia_public', city_slug=city_slug))

    nome = ' '.join((request.form.get('nome') or '').split())[:100]
    email = (request.form.get('email') or '').strip().lower()
    telefone = (request.form.get('telefone') or '').strip()[:30] or None
    form = {'nome': nome, 'email': email, 'telefone': telefone or ''}

    def _volta(msg, status=200):
        flash(msg, 'error')
        return _sem_indexar(current_app.response_class(
            render_template('pedidos/solicitar_relatorio.html', cidade=cidade, city_slug=city_slug, form=form),
            status=status))

    if _passou_do_limite('relatorio', 'rel:%s' % email, max_chave=2, max_ip=3, horas=24):
        return _volta('Já recebemos pedidos deste e-mail ou deste endereço. Tente amanhã.', 429)
    _contar_tentativa('relatorio', 'rel:%s' % email)

    if len(nome) < 3:
        return _volta('Informe o seu nome.')
    if not _EMAIL.match(email) or len(email) > 150:
        return _volta('Informe um e-mail válido.')

    de, ate = _ultimos_12_meses(date.today())
    pedido = PedidoRelatorio(
        cidade=cidade, nome=nome, email=email, telefone=telefone,
        token=secrets.token_urlsafe(32), periodo_de=de, periodo_ate=ate,
        expira_em=datetime.utcnow() + timedelta(days=DIAS_DO_LINK), ip=_ip())
    db.session.add(pedido)
    db.session.commit()

    # Tudo o que depende do pedido (requisição) é montado AQUI; a thread só envia.
    link = url_for('pedidos.relatorio_publico', token=pedido.token, _external=True)
    validade = pedido.expira_em.strftime('%d/%m/%Y')
    html, texto = contas._texto(
        'Pedido de relatório completo',
        'Olá!',
        '<strong>%s</strong> pediu o relatório completo de <strong>%s</strong> '
        '(%s a %s).<br>E-mail: %s%s<br><br>Encaminhe este e-mail à pessoa. O link '
        'abaixo já é o relatório e vale até <strong>%s</strong>.'
        % (nome, cidade, de.strftime('%d/%m/%Y'), ate.strftime('%d/%m/%Y'), email,
           (' · telefone: %s' % telefone) if telefone else '', validade),
        'Abrir o relatório', link,
        'Ao responder este e-mail, a resposta vai direto para %s.' % email)
    assunto = 'MoveMentes — pedido de relatório: %s' % nome
    destinos = [u.email for u in _donos_do_relatorio(cidade)]
    pid = pedido.id
    app = current_app._get_current_object()

    def _enviar():
        erros = []
        for d in destinos:
            ok, detalhe = correio.enviar(d, assunto, html, texto, responder_para=email)
            if not ok:
                erros.append('%s: %s' % (d, detalhe))
        p = PedidoRelatorio.query.get(pid)
        if p is not None:
            p.email_enviado = bool(destinos) and not erros
            p.email_erro = ('; '.join(erros) or ('ninguém para avisar' if not destinos else None))
            db.session.commit()
        if erros or not destinos:
            logger.warning('pedidos: pedido de relatorio %s — e-mail com problema: %s', pid, erros or 'sem destinatario')

    _executar(app, _enviar)
    return _mensagem('Pedido recebido',
                     'Você vai receber o relatório em %s.' % email,
                     botao='Voltar', destino=url_for('ranking.transparencia_public', city_slug=city_slug))


def _pedido_vigente(token):
    if not token or len(token) < 20:
        return None
    pedido = PedidoRelatorio.query.filter_by(token=token).first()
    return pedido if pedido is not None and pedido.vigente else None


@pedidos_bp.route('/r/<token>')
def relatorio_publico(token):
    pedido = _pedido_vigente(token)
    if pedido is None:
        return _mensagem('Link vencido', 'Este relatório não está mais disponível. Peça outro.', status=404)
    anterior, itens = caixa_mod.fluxo(pedido.cidade, pedido.periodo_de, pedido.periodo_ate)
    pedido.visualizacoes = (pedido.visualizacoes or 0) + 1
    db.session.commit()
    html = render_template(
        'pedidos/relatorio_publico.html', pedido=pedido, anterior=anterior, itens=itens,
        periodo=caixa_mod.periodo(pedido.cidade, pedido.periodo_de, pedido.periodo_ate),
        rotulo_categoria=caixa_mod.ROTULO_CATEGORIA, documento=caixa_mod.rotulo_documento,
        gerado_em=datetime.now())
    return _sem_indexar(current_app.response_class(html))


@pedidos_bp.route('/r/<token>/c/<int:lid>')
def comprovante_do_relatorio(token, lid):
    """O comprovante de uma linha DESTE relatório: mesma cidade, dentro do período,
    e só enquanto o pedido vale (30 dias)."""
    pedido = _pedido_vigente(token)
    if pedido is None:
        abort(404)
    linha = LancamentoCaixa.query.get(lid)
    if (linha is None or linha.cidade != pedido.cidade
            or not (pedido.periodo_de <= linha.data <= pedido.periodo_ate)):
        abort(404)
    resposta = caixa_mod.enviar_comprovante(linha)
    if resposta is None:
        abort(404)
    return resposta


# ---------------------------------------------------------------------------
# a lista de quem gere
# ---------------------------------------------------------------------------
@pedidos_bp.route('/lista')
@login_required
@admin_required
def lista():
    cidade = current_user.cidade_ativa
    pedidos = (PedidoRelatorio.query.filter_by(cidade=cidade)
               .order_by(PedidoRelatorio.criado_em.desc()).limit(100).all())
    cadastros = (CadastroAtleta.query.filter_by(cidade=cidade)
                 .order_by(CadastroAtleta.criado_em.desc()).limit(100).all())
    return render_template('pedidos/lista.html', cidade=cidade, pedidos=pedidos, cadastros=cadastros,
                           agora=datetime.utcnow())
