#/volume1/web/movimente-se/app/blueprints/auth.py
from flask import (Blueprint, render_template, redirect, url_for, flash, abort,
                   request, current_app, session)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash
from app import db, dadoslocais
from app.models import Usuario, Config, TIPO_SUPREMO, NIVEIS_USUARIO
from app.forms import LoginForm, RegistrationForm
from functools import wraps
import logging
import re
import secrets
from datetime import datetime, timedelta

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
logger = logging.getLogger(__name__)


def normalizar_telefone_usuario(bruto):
    """Telefone no formato do WAHA (55DDD9XXXXXXXX) ou None.

    Mesma regra do modulo de avisos: uma so forma de guardar telefone no
    sistema inteiro, senao o login por telefone e o envio no PV divergem.
    """
    numero = re.sub(r'\D', '', str(bruto or ''))
    if not numero:
        return None
    numero = numero.lstrip('0')
    if not numero.startswith('55'):
        numero = '55' + numero
    if len(numero) == 12:
        ddd, resto = numero[2:4], numero[4:]
        if resto[:1] in ('6', '7', '8', '9'):
            numero = f'55{ddd}9{resto}'
    return numero if len(numero) == 13 else None


def _nivel_minimo(nivel_min, mensagem):
    """Fabrica de decorators por nivel — um lugar so para a regra de acesso."""
    def wrapper(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if current_user.nivel < nivel_min:
                flash(mensagem, 'error')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return wrapper


def viewer_required(f):
    """Permite acesso a qualquer usuario logado, incluindo viewer."""
    return _nivel_minimo(NIVEIS_USUARIO['viewer'], 'Acesso negado.')(f)


def admin_required(f):
    return _nivel_minimo(
        NIVEIS_USUARIO['admin'], 'Acesso negado. Apenas administradores.')(f)


def master_or_aprovador_required(f):
    return _nivel_minimo(
        NIVEIS_USUARIO['aprovador'],
        'Acesso negado. Apenas administradores master ou aprovadores.')(f)


def master_required(f):
    """Master da cidade OU supremo (o supremo pode tudo que o master pode)."""
    return _nivel_minimo(
        NIVEIS_USUARIO['master'], 'Acesso negado. Apenas administrador master.')(f)


def supremo_required(f):
    """Somente o dono da instalacao.

    Guarda o que atravessa a fronteira de cidade e o que mexe na maquina:
    pagina de comandos, exclusao de cidade, criacao de outro supremo.
    """
    return _nivel_minimo(
        NIVEIS_USUARIO[TIPO_SUPREMO],
        'Acesso negado. Apenas o master supremo.')(f)


def cidades_do_usuario(user=None):
    """Cidades que este usuario pode escolher no seletor.

    Supremo e auditor veem todas; os demais so a propria (a lista existe para a
    interface nao precisar saber a regra).
    """
    user = user or current_user
    if not getattr(user, 'is_authenticated', False):
        return []
    if not user.pode_trocar_cidade:
        return [user.cidade]
    return [row[0] for row in db.session.query(Config.cidade).order_by(Config.cidade).all()]

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if session.get('modo_atleta'):
            # Entrou como atleta e agora quer entrar de verdade: sai do modo
            # atleta e mostra a tela de entrar (a última entrada vale).
            logout_user()
            session.pop('modo_atleta', None)
        else:
            destino = request.args.get('next') or ''
            if destino.startswith('/') and not destino.startswith('//'):
                return redirect(destino)
            return redirect(url_for('index'))

    form = LoginForm()

    # PIX de destaque na tela de login: o da cidade do dono da instalacao (o
    # master supremo). Antes era a cidade fixa "Sistema", que deixou de existir
    # na renomeacao de 23/07 -- e desde entao ninguem via o PIX certo.
    cidade_principal = None
    dono = db.session.query(Usuario).filter_by(tipo=TIPO_SUPREMO, ativo=True).first()
    if dono:
        cidade_principal = dono.cidade
    pix_principal = None
    if cidade_principal:
        pix_principal = db.session.query(Config).filter_by(cidade=cidade_principal).first()

    if not pix_principal or not pix_principal.chave_pix:
        pix_principal = Config(
            cidade=cidade_principal or 'MoveMentes',
            chave_pix=dadoslocais.obter('pix_chave_padrao'),
            patrocinador_mes='Projeto MoveMentes',
            qr_pix='qrcode_doacao.png'
        )

    outras_cidades_pix = db.session.query(Config).filter(
        Config.cidade != pix_principal.cidade,
        Config.chave_pix.isnot(None),
        Config.chave_pix != ''
    ).order_by(Config.cidade).all()

    if form.validate_on_submit():
        login_identifier = form.identifier.data.strip().lower()
        password = form.password.data

        try:
            user = db.session.query(Usuario).filter(
                (Usuario.email == login_identifier) | (Usuario.nome.ilike(login_identifier))
            ).first()

            # Nao achou por e-mail/nome: tenta telefone. So entra aqui quando o
            # que foi digitado parece um numero, para nao gastar consulta a toa.
            if not user and any(c.isdigit() for c in login_identifier):
                telefone = normalizar_telefone_usuario(login_identifier)
                if telefone:
                    user = db.session.query(Usuario).filter_by(telefone=telefone).first()

            if not user:
                logger.warning(f"Tentativa de login com identificador inexistente: {login_identifier}")
                flash('Email/Nome de usuário ou senha incorretos.', 'error')
                return render_template('auth/login.html', form=form, pix_principal=pix_principal, outras_cidades_pix=outras_cidades_pix)

            if not user.check_password(password):
                logger.warning(f"Senha incorreta para usuário: {user.email}")
                flash('Email/Nome de usuário ou senha incorretos.', 'error')
                return render_template('auth/login.html', form=form, pix_principal=pix_principal, outras_cidades_pix=outras_cidades_pix)

            if not user.ativo:
                logger.warning(f"Tentativa de login de usuário inativo: {user.email}")
                flash('Sua conta está inativa ou pendente de ativação. Entre em contato com o administrador.', 'error')
                return render_template('auth/login.html', form=form, pix_principal=pix_principal, outras_cidades_pix=outras_cidades_pix)

            login_user(user, remember=form.remember_me.data)
            session.pop('cidade_ativa', None)
            session.pop('modo_atleta', None)   # entrada comum = privilégio de verdade
            logger.info(f"Login bem-sucedido: {user.email} (Tipo: {user.tipo}, Cidade: {user.cidade})")

            if user.senha_provisoria:
                flash('Primeiro acesso: escolha uma senha sua antes de continuar.', 'warning')
                return redirect(url_for('auth.change_password'))

            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                flash(f'Bem-vindo(a), {user.nome}!', 'success')
                return redirect(next_page)

            flash(f'Bem-vindo(a), {user.nome}!', 'success')
            return redirect(url_for('index'))

        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro durante login para {login_identifier}: {str(e)}", exc_info=True)
            flash('Erro ao processar login. Tente novamente ou entre em contato com o administrador.', 'error')
            return render_template('auth/login.html', form=form, pix_principal=pix_principal, outras_cidades_pix=outras_cidades_pix)

    return render_template('auth/login.html', form=form, pix_principal=pix_principal, outras_cidades_pix=outras_cidades_pix)

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        flash('Você já está logado. Faça logout para registrar uma nova conta.', 'info')
        return redirect(url_for('index'))

    form = RegistrationForm()
    cities_from_db = db.session.query(Config).order_by(Config.cidade).all()
    form.cidade.choices = [(c.cidade, c.cidade) for c in cities_from_db]

    if not form.cidade.choices:
        form.cidade.choices = [('', 'Nenhuma cidade configurada. Contate o administrador.')]
        if request.method == 'GET':
            flash('Nenhuma cidade configurada para registro. Por favor, contate o administrador.', 'error')

    if form.validate_on_submit():
        nome = form.nome.data.strip()
        email = form.email.data.strip().lower()
        password = form.password.data
        cidade = form.cidade.data
        telefone = normalizar_telefone_usuario(form.telefone.data)

        # 22/08/2026 — EXIGENCIA DE @exemplo.com REMOVIDA (decisao do dono).
        # Motivo: o tenant Microsoft 365 E5 tem so 16 licencas, entao amarrar
        # o cadastro ao dominio da empresa limitava o projeto a 16 pessoas --
        # e o projeto precisa abrir para a comunidade (porta de entrada
        # publica e requisito do Ministerio do Esporte, ver P-100).
        #
        # POR QUE ISTO NAO ABRE BURACO: o cadastro publico ja cria a conta com
        # `ativo=False` (abaixo) e ela so passa a valer quando um admin
        # ativa. A trava de dominio era uma segunda tranca sobre uma porta que
        # ja estava fechada. A camada de seguranca que ENTRA no lugar e a
        # confirmacao de e-mail (P-103), usando o remetente que ja existe
        # (o remetente guardado no cofre, via Microsoft Graph em `app/correio.py`).
        if not telefone:
            flash('Telefone inválido. Use DDD + número, como (DD) 9XXXX-XXXX.', 'error')
            return render_template('auth/register.html', form=form)

        try:
            existing_user = db.session.query(Usuario).filter_by(email=email).first()
            if existing_user:
                flash('Este e-mail já está cadastrado.', 'error')
                return render_template('auth/register.html', form=form)

            if db.session.query(Usuario).filter_by(telefone=telefone).first():
                flash('Este telefone já está cadastrado para outro usuário.', 'error')
                return render_template('auth/register.html', form=form)

            new_user = Usuario(
                nome=nome,
                email=email,
                cidade=cidade,
                telefone=telefone,
                tipo='viewer',
                ativo=False,
                gerado_por_id=None,
                ult_modificacao=datetime.utcnow()
            )
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()

            logger.info(f"Nova solicitação de registro (viewer inativo): {email} ({cidade})")
            flash('Sua solicitação de registro foi recebida! Ela será revisada pelo administrador e sua conta ativada em breve. Agradecemos sua paciência.', 'success')
            return redirect(url_for('auth.login'))

        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro ao registrar usuário {email}: {str(e)}", exc_info=True)
            flash('Erro ao processar seu registro. Tente novamente ou contate o administrador.', 'error')
            return render_template('auth/register.html', form=form)

    return render_template('auth/register.html', form=form)

@auth_bp.route('/logout')
@login_required
def logout():
    try:
        user_email = current_user.email
        logout_user()
        session.pop('modo_atleta', None)
        logger.info(f"Logout realizado: {user_email}")
        flash('Você saiu do sistema com sucesso.', 'success')
    except Exception as e:
        logger.error(f"Erro durante logout para {current_user.email}: {str(e)}", exc_info=True)
        flash('Erro ao sair do sistema.', 'error')
    return redirect(url_for('auth.login'))

@auth_bp.route('/profile')
@login_required
def profile():
    flash('Funcionalidade de perfil em desenvolvimento.', 'info')
    return redirect(url_for('index'))

@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Alteração de senha para usuário logado."""
    obrigatoria = bool(current_user.senha_provisoria)
    if request.method == 'POST':
        senha_atual   = request.form.get('senha_atual', '')
        nova_senha    = request.form.get('nova_senha', '')
        confirma      = request.form.get('confirma_senha', '')

        if not current_user.check_password(senha_atual):
            flash('Senha atual incorreta.', 'error')
            return render_template('auth/change_password.html', obrigatoria=obrigatoria)
        if len(nova_senha) < 6:
            flash('A nova senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template('auth/change_password.html', obrigatoria=obrigatoria)
        if nova_senha != confirma:
            flash('As senhas não conferem.', 'error')
            return render_template('auth/change_password.html', obrigatoria=obrigatoria)
        if nova_senha == senha_atual:
            flash('A nova senha precisa ser diferente da atual.', 'error')
            return render_template('auth/change_password.html', obrigatoria=obrigatoria)

        current_user.set_password(nova_senha)
        current_user.senha_provisoria = False
        current_user.senha_alterada_em = datetime.utcnow()
        current_user.ult_modificacao = datetime.utcnow()
        db.session.commit()
        logger.info(f"Senha alterada pelo próprio usuário: {current_user.email}")
        flash('Senha alterada com sucesso!', 'success')
        return redirect(url_for('index'))

    return render_template('auth/change_password.html', obrigatoria=obrigatoria)


# Rotas que continuam abertas mesmo com a troca de senha pendente: sem elas o
# usuario ficaria preso num redirecionamento em circulo.
_ROTAS_LIVRES_SENHA = {
    'auth.change_password', 'auth.logout', 'auth.login',
    'auth.reset_password', 'static', 'favicon',
}


@auth_bp.before_app_request
def forcar_troca_de_senha():
    """Senha entregue pelo admin so serve para entrar uma vez.

    Enquanto o usuario nao escolher a propria senha, toda pagina do sistema
    leva para a tela de troca. Paginas publicas (ranking, conferencia, API)
    nao passam por aqui porque nao exigem login.
    """
    if not getattr(current_user, 'is_authenticated', False):
        return None
    if not getattr(current_user, 'senha_provisoria', False):
        return None
    endpoint = request.endpoint or ''
    if endpoint in _ROTAS_LIVRES_SENHA:
        return None
    # Nao sequestra o que e publico: so o que pediria login mesmo.
    if endpoint.startswith(('ranking_short.', 'conferencia.', 'api.')):
        return None
    if request.path.startswith(('/static/', '/r/', '/api/', '/conferencia', '/ath')):
        return None
    flash('Antes de continuar, escolha uma senha sua.', 'warning')
    return redirect(url_for('auth.change_password'))


@auth_bp.route('/cidade-ativa', methods=['POST'])
@login_required
def trocar_cidade_ativa():
    """Troca a cidade em que o master supremo esta trabalhando.

    A escolha fica na SESSAO. Nenhuma tela le cidade de parametro de URL: se
    lesse, bastaria trocar o nome no endereco para cair na cidade vizinha.
    """
    # Supremo (dono) e auditor (patrocinador/analista) sao os dois que
    # atravessam cidade -- por motivos opostos, e por isso a propriedade se
    # chama `pode_trocar_cidade` e nao `is_supremo` (P-91).
    if not current_user.pode_trocar_cidade:
        abort(403)
    destino = (request.form.get('cidade') or '').strip()
    validas = cidades_do_usuario()
    if destino and destino not in validas:
        flash('Cidade desconhecida.', 'error')
        return redirect(request.referrer or url_for('index'))
    if not destino or destino == current_user.cidade:
        session.pop('cidade_ativa', None)
        flash(f'Voltando para {current_user.cidade}.', 'info')
    else:
        session['cidade_ativa'] = destino
        flash(f'Agora você está trabalhando em {destino}.', 'success')
    logger.info('Master supremo %s trocou a cidade ativa para %s',
                current_user.email, destino or current_user.cidade)
    return redirect(request.referrer or url_for('index'))


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Redefinição de senha via token gerado pelo admin."""
    user = Usuario.query.filter_by(reset_token=token).first()

    if not user or not user.reset_token_expiry or user.reset_token_expiry < datetime.utcnow():
        flash('Link de redefinição inválido ou expirado. Solicite um novo ao administrador.', 'error')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        nova_senha = request.form.get('nova_senha', '')
        confirma   = request.form.get('confirma_senha', '')

        if len(nova_senha) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'error')
            return render_template('auth/reset_password.html', token=token)
        if nova_senha != confirma:
            flash('As senhas não conferem.', 'error')
            return render_template('auth/reset_password.html', token=token)

        user.set_password(nova_senha)
        user.reset_token        = None
        user.reset_token_expiry = None
        user.ult_modificacao    = datetime.utcnow()
        db.session.commit()
        logger.info(f"Senha redefinida via token para usuário: {user.email}")
        flash('Senha redefinida com sucesso! Faça login.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', token=token, user=user)


# ===================== P-103 (20/09/2026) =====================
# A SENHA E' DE QUEM USA, NAO DE QUEM CADASTRA. Antes, o admin digitava a senha
# do outro e mandava o link por WhatsApp; agora o proprio dono da conta define a
# senha dele por um link que chega no e-mail dele. O que estas rotas fazem de
# concreto esta em `app/contas.py` -- aqui so mora a tela.

@auth_bp.route('/esqueci', methods=['GET', 'POST'])
def esqueci_senha():
    """Esqueci a minha senha — a propria pessoa pede o link.

    NAO TEM TELA PROPRIA (correcao do dono, 20/09): o formulario mora dentro
    da tela de entrar, que e' onde a pessoa trava. Quem chega aqui por GET
    volta para la.
    """
    if request.method == 'POST':
        from app import contas
        quem = (request.form.get('identificador') or '').strip().lower()
        # A RESPOSTA E' SEMPRE A MESMA, ACHANDO OU NAO (regra de sempre em tela
        # publica): dizer "esse e-mail nao existe" entrega a lista de quem tem
        # conta no sistema para qualquer um que fique testando enderecos.
        recado = ('Se essa conta existir, o link para criar uma senha nova já '
                  'está a caminho do e-mail dela. Olhe também o lixo eletrônico.')
        usuario = None
        if quem:
            telefone = normalizar_telefone_usuario(quem)
            usuario = (Usuario.query.filter(db.func.lower(Usuario.email) == quem).first()
                       or (Usuario.query.filter_by(telefone=telefone).first()
                           if telefone else None))
        if usuario and usuario.email:
            if not usuario.ativo:
                logger.warning('esqueci: conta inativa pediu senha (%s)', usuario.email)
            else:
                ok, detalhe, _link = contas.pedir_nova_senha(usuario)
                if not ok:
                    logger.error('esqueci: nao consegui enviar para %s: %s',
                                 usuario.email, detalhe)
        flash(recado, 'info')
        return redirect(url_for('auth.login'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/email/trocar', methods=['GET', 'POST'])
@login_required
def trocar_email():
    """Troca o e-mail da conta — confirmando no endereco ATUAL.

    NAO TEM TELA PROPRIA (correcao do dono, 20/09): o formulario mora dentro de
    "Alterar Senha", que passa a ser a tela da conta da pessoa — senha e
    e-mail no mesmo lugar, que e' onde ela ja vai procurar.
    """
    if request.method == 'POST':
        from app import contas
        # A SENHA ATUAL E' O SEGUNDO PORTAO: sem ela, uma tela deixada aberta
        # num computador emprestado troca o dono da conta.
        if not current_user.check_password(request.form.get('senha_atual') or ''):
            flash('Senha atual incorreta — nada foi trocado.', 'error')
        else:
            ok, aviso = contas.pedir_troca_de_email(
                current_user, request.form.get('email_novo'))
            flash(aviso, 'success' if ok else 'error')
    return redirect(url_for('auth.change_password'))


@auth_bp.route('/email/confirmar/<token>')
def confirmar_email(token):
    """O clique no link que chegou no e-mail ATUAL: aplica a troca."""
    from app import contas
    ok, aviso, _u = contas.confirmar_troca_de_email(token)
    flash(aviso, 'success' if ok else 'error')
    return redirect(url_for('auth.login'))


@auth_bp.errorhandler(404)
def not_found_error(error):
    return render_template('errors/404.html'), 404

@auth_bp.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('errors/500.html'), 500