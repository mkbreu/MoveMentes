# -*- coding: utf-8 -*-
""" Aplicacao Flask - Movimente-se """
import os
import logging
import secrets
import sys
import io
import sqlite3
import threading
import time
from pathlib import Path
from flask import Flask, redirect, url_for, render_template, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, login_required
from flask_migrate import Migrate
from datetime import timedelta
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

# Instancias globais
db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()


def _prepare_sqlite_database(db_path, logger):
    """Use a NAS-safe journal mode and fail early if the database is unhealthy."""
    last_error = None
    for attempt in range(10):
        connection = None
        try:
            connection = sqlite3.connect(
                str(db_path), timeout=30, isolation_level=None
            )
            # WAL + NORMAL: seguro em volume ext4 local e reduz drasticamente o
            # custo de fsync por commit no DS215j. Backups usam a API .backup(),
            # que le o conteudo do WAL corretamente.
            mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            connection.execute('PRAGMA synchronous=NORMAL')
            connection.execute('PRAGMA busy_timeout=30000')
            check = connection.execute('PRAGMA quick_check').fetchone()[0]
            if str(mode).lower() != 'wal':
                raise RuntimeError(f'journal_mode inesperado: {mode}')
            if str(check).lower() != 'ok':
                raise RuntimeError(f'quick_check falhou: {check}')
            logger.info('SQLite pronto: journal_mode=%s, quick_check=%s', mode, check)
            return
        except (sqlite3.Error, RuntimeError) as exc:
            last_error = exc
            if attempt < 9:
                time.sleep(1)
        finally:
            if connection is not None:
                connection.close()
    raise RuntimeError(f'Falha ao preparar o SQLite: {last_error}')

# Chave que ficou versionada no git ate 2026-07-20. Continua sendo ACEITA para
# validar tokens ja distribuidos (links publicos de ranking e de revisao), mas
# nunca mais e usada para assinar nada novo. Some daqui quando nao houver mais
# link antigo em circulacao -- na pratica, quando o ranking do mes seguinte ja
# tiver sido publicado com a chave nova.
# O VALOR NAO FICA NO CODIGO (15/10/2026): mora em instance/dados_locais.json
# ("secret_key_legada"). Sem o arquivo ela e' vazia, e `signing_keys()` ja
# ignora chave vazia -- uma instalacao nova simplesmente nao tem link antigo.
from app import dadoslocais  # noqa: E402  (modulo puro: nao importa nada de `app`)
SECRET_KEY_LEGADA = dadoslocais.obter('secret_key_legada')


def _resolver_secret_key(logger):
    """Chave de assinatura da instalacao, em ordem de precedencia.

    1. Variavel de ambiente SECRET_KEY (permite operar por env no futuro).
    2. Cofre em instance/secrets.json (fora do git e fora do backup).
    3. Primeira execucao: gera uma chave forte e persiste no cofre.

    Se o cofre nao puder ser gravado, volta para a chave legada de proposito:
    e melhor manter o comportamento atual do que sortear uma chave nova a cada
    boot -- isso derrubaria a sessao de todo mundo o tempo todo e invalidaria
    os links assinados na rodada anterior.
    """
    from app import secretstore

    do_ambiente = os.environ.get('SECRET_KEY')
    if do_ambiente and do_ambiente != SECRET_KEY_LEGADA:
        return do_ambiente

    do_cofre = secretstore.get(secretstore.FLASK_SECRET)
    if do_cofre:
        return do_cofre

    # COFRE ILEGIVEL NAO TROCA A CHAVE (09/09/2026).
    #
    # Ate aqui, "o cofre nao respondeu" e "o cofre esta vazio" davam no mesmo:
    # os dois caiam na chave legada. Em 08/09 o cofre ficou ilegivel por uma
    # marca invisivel de tres bytes, o site adotou a legada, e quando o cofre
    # foi consertado ele voltou para a chave real -- INVALIDANDO O LOGIN DE
    # TODO MUNDO em silencio. O dono clicou em "Publicar agora" no dia seguinte
    # e nada aconteceu, porque estava deslogado sem saber.
    #
    # As duas situacoes sao diferentes e agora sao tratadas diferente:
    #   - cofre LEGIVEL e sem a chave  -> primeira execucao: gera e guarda.
    #   - cofre ILEGIVEL               -> nao da para saber se ha chave la
    #     dentro; trocar seria derrubar a sessao de todos por um problema que
    #     e' nosso e passageiro. Mantem a legada, mas GRITA, porque enquanto
    #     isso durar o site esta funcionando pela metade.
    #
    # Ler o cofre nao pode depender de login, e login nao pode depender de o
    # cofre estar bom: sao coisas separadas e precisam continuar separadas.
    _dados, legivel = secretstore.load_status()
    if not legivel:
        logger.error(
            'SECRET_KEY: o cofre existe mas NAO PODE SER LIDO agora. Nao vou '
            'trocar a chave (isso deslogaria todo mundo). Seguindo com a chave '
            'legada ate o cofre voltar. CORRIJA: veja se instance/secrets.json '
            'abre como JSON valido e se nao ganhou marca de BOM no comeco.'
        )
        return SECRET_KEY_LEGADA

    nova = secrets.token_urlsafe(64)
    if not secretstore.set_value(secretstore.FLASK_SECRET, nova, updated_by='boot'):
        logger.error(
            'SECRET_KEY: nao foi possivel gravar em instance/secrets.json; '
            'seguindo com a chave legada. Corrija a permissao da pasta instance.'
        )
        return SECRET_KEY_LEGADA
    # Le de volta: se dois workers gerarem ao mesmo tempo, ambos adotam a que
    # de fato ficou gravada, e nao a que cada um sorteou.
    gravada = secretstore.get(secretstore.FLASK_SECRET) or nova
    logger.warning(
        'SECRET_KEY nova gerada e guardada no cofre (fp=%s). Todos os usuarios '
        'precisarao entrar de novo uma unica vez; os links publicos ja enviados '
        'continuam validos pela chave legada.',
        secretstore.fingerprint(gravada),
    )
    return gravada


def signing_keys():
    """Chaves aceitas ao validar tokens assinados, da mais antiga para a mais nova.

    O itsdangerous assina com a ULTIMA da lista e valida contra todas: e assim
    que um link publico gerado antes da troca de chave continua abrindo.
    """
    from flask import current_app

    atual = current_app.config.get('SECRET_KEY')
    ordem = [current_app.config.get('SECRET_KEY_LEGADA'), atual]
    chaves = []
    for chave in ordem:
        if chave and chave not in chaves:
            chaves.append(chave)
    return chaves or [atual]


def create_app(database_uri=None):
    """Factory para criar a aplicacao Flask.

    database_uri: SO para teste (script fora de producao). None (o padrao)
    mantem o caminho de sempre -- instance/movimente_se.db. NUNCA passar um
    caminho de banco por aqui a partir de dentro de Z:\\movimente-se em uso
    real: e o gancho que existe para um teste local nao abrir o banco de
    producao pelo SMB (regra 24 do CLAUDE.md, ERROS.md P-47).
    """
    app = Flask(__name__)

    # ===== CONFIGURACOES BASICAS =====
    # Resolver a chave NUNCA pode derrubar a aplicacao: qualquer imprevisto
    # (permissao, disco cheio, cofre corrompido) cai na chave legada e o site
    # sobe igual. Seguranca que tira o servico do ar nao e seguranca.
    try:
        chave = _resolver_secret_key(logging.getLogger('app'))
    except Exception as _exc:
        logging.getLogger('app').error(
            'Falha ao resolver a SECRET_KEY (%s); seguindo com a chave legada.', _exc
        )
        chave = SECRET_KEY_LEGADA
    app.config['SECRET_KEY'] = chave
    app.config['SECRET_KEY_LEGADA'] = SECRET_KEY_LEGADA

    # Diretorio de instancia (onde fica o banco de dados)
    instance_path = Path(__file__).parent.parent / 'instance'
    instance_path.mkdir(exist_ok=True)

    # Configuracao correta do banco de dados
    db_path = instance_path / 'movimente_se.db'
    app.config['SQLALCHEMY_DATABASE_URI'] = database_uri or f'sqlite:///{db_path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        # timeout=30 vira busy_timeout: em vez de estourar na hora quando outro
        # processo esta escrevendo, a conexao espera ate 30s. E o que segura
        # dois dispositivos usando o sistema ao mesmo tempo.
        # check_same_thread=False porque as tarefas de fundo (jobs de upload,
        # ticker, fechamento) rodam em threads proprias.
        'connect_args': {'timeout': 30, 'check_same_thread': False},
        'pool_pre_ping': True,
        'pool_recycle': 1800,
    }

    # Configuracoes de upload
    app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
    app.config['UPLOAD_FOLDER_ZIP'] = os.path.join(app.config['UPLOAD_FOLDER'], 'zip')
    app.config['UPLOAD_FOLDER_TXT'] = os.path.join(app.config['UPLOAD_FOLDER'], 'txt')
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

    # ===== CONFIGURAR LOGGING UTF-8 =====
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    # O nome do arquivo agora sai do proprio handler, que monta
    # `movimente_se-AAAA-MM.log` (ver `app/logmensal.py`). Aqui basta a pasta.

    # Limpar handlers existentes
    for handler in app.logger.handlers[:]:
        app.logger.removeHandler(handler)

    fmt = logging.Formatter('[%(asctime)s] %(levelname)s in %(module)s: %(message)s')

    # ARQUIVO DE LOG QUE SE DIVIDE SOZINHO (26/08/2026, pedido do dono).
    #
    # Ate hoje era um `FileHandler` simples: um arquivo unico que crescia para
    # sempre. Tinha chegado a 10,9 MB -- pesado para abrir, pesado para ler, e
    # so aumentando. Com 2 MB por arquivo e 10 copias, cada arquivo cobre uns
    # 11 dias de dia movimentado e o historico vai a ~4 meses, sem nenhum
    # arquivo grande.
    #
    # `delay=True` de proposito: o arquivo so e' aberto na primeira linha
    # escrita. Sem isso, os dois workers do uWSGI abrem o mesmo arquivo no boot
    # e podem disputar a hora de dividi-lo.
    #
    # AGORA TAMBEM SE DIVIDE POR MES (07/09/2026, regra 19).
    #
    # Dividir so por tamanho resolvia o arquivo gigante, mas nao respondia a
    # pergunta que a gente faz quando algo quebra: "o que aconteceu no dia 30?".
    # Com onze arquivos rotativos, o dia 30 podia estar em qualquer um, e achar
    # exigia abrir todos -- o oposto da regra 15. Agora a resposta e' o nome do
    # arquivo: `movimente_se-2026-09.log`.
    #
    # O teto de 2 MB continua valendo DENTRO do mes, entao mes movimentado nao
    # gera arquivo pesado. Quem apaga as sobras depois de 60 dias e'
    # `logmensal.expurgar`, chamado pela faxina.
    #
    # O `movimente_se.log` antigo NAO e' apagado nem migrado: ele para de
    # crescer e fica ali como historico ate o dono decidir. Apagar log de
    # producao para "organizar" nao vale o risco (regra 9).
    try:
        from app.logmensal import ArquivoMensal
        file_handler = ArquivoMensal(
            log_dir, 'movimente_se', maxBytes=2 * 1024 * 1024,
            backupCount=10, encoding='utf-8', delay=True,
        )
        file_handler.setFormatter(fmt)
        app.logger.addHandler(file_handler)
    except PermissionError:
        pass  # Log file inacessivel (ex: root-owned); continua apenas com StreamHandler

    # StreamHandler com UTF-8 e errors=replace para nao crashar em ambientes ASCII
    try:
        if hasattr(sys.stderr, 'buffer'):
            utf8_stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        else:
            utf8_stderr = sys.stderr
        stream_handler = logging.StreamHandler(utf8_stderr)
    except Exception:
        stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logging.INFO)
    app.logger.addHandler(stream_handler)

    app.logger.setLevel(logging.INFO)
    # Nao propagar para o root logger (evita handler ASCII do uWSGI)
    app.logger.propagate = False

    # LOG CANONICO SEMANAL (17/09/2026, item D1): AVISO/ERRO de todo o site e
    # os INFO que contam uma acao vao tambem para `logs/canonico_AAAA-Sss.log`,
    # no formato comum a Windows, WSL e site. Detalhe em `app/logcanon.py`.
    try:
        from app import logcanon as _logcanon
        _logcanon.ligar(app.logger)
    except Exception:
        pass

    # Configurar logger raiz dos blueprints para usar os mesmos handlers
    pkg_logger = logging.getLogger('app')
    if pkg_logger is not app.logger:
        for h in list(pkg_logger.handlers):
            pkg_logger.removeHandler(h)
        pkg_logger.addHandler(file_handler if 'file_handler' in dir() else stream_handler)
        pkg_logger.addHandler(stream_handler)
        pkg_logger.setLevel(logging.INFO)
        pkg_logger.propagate = False

    app.logger.info('Movimente-se startup')

    # ===== INICIALIZAR EXTENSOES =====
    # So prepara o arquivo em disco quando o caminho E o de producao -- um
    # database_uri de teste (sqlite:///:memory: etc.) nao tem path de arquivo
    # do NAS para abrir aqui.
    if database_uri is None:
        _prepare_sqlite_database(db_path, app.logger)
    db.init_app(app)

    # PRAGMAs valem POR CONEXAO, nao por banco: sem este gancho, so a conexao
    # do boot ficava ajustada e todas as outras voltavam ao padrao (fsync a
    # cada commit e espera zero por lock). Era isto que transformava dois
    # dispositivos usando o sistema ao mesmo tempo em erro 500.
    from sqlalchemy import event as _sa_event

    @_sa_event.listens_for(Engine, 'connect')
    def _ajustar_conexao_sqlite(dbapi_connection, connection_record):
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('PRAGMA busy_timeout=30000')
            cursor.execute('PRAGMA synchronous=NORMAL')
            # NAO ligar PRAGMA foreign_keys aqui.
            #
            # Em 31/07/2026 eu liguei, e a producao parou de salvar atividade:
            # "no such table: main.processed_files_old_uc". A tabela activities
            # carrega uma referencia ORFA desde uma migracao antiga — o
            # ALTER TABLE ... RENAME do SQLite reescreveu sozinho a chave
            # estrangeira de activities para o nome temporario, e o DROP
            # seguinte apagou o alvo. Com a checagem desligada (o padrao do
            # SQLite) isso nunca incomodou ninguem; ligando, todo INSERT quebra.
            # A aplicacao nunca dependeu dessa checagem: a integridade e feita
            # no codigo. Consertar o schema e uma operacao a parte, com backup.
        except sqlite3.Error as exc:
            app.logger.warning('Nao consegui ajustar a conexao SQLite: %s', exc)
        finally:
            cursor.close()

    # Disponibiliza csrf_token() nos templates sem ativar validacao global
    from flask_wtf.csrf import generate_csrf
    app.jinja_env.globals['csrf_token'] = generate_csrf

    # TEXTO EDITAVEL PELA TELA (09/09/2026). Usa-se assim no template:
    #     {{ texto('home.frase', 'Mover o corpo para mover a mente') }}
    # O segundo argumento e' o texto de fabrica: a pagina nunca fica em branco
    # porque a tabela esta vazia, e o original continua versionado no Git, onde
    # da' para ver o que mudou.
    def _texto_do_site(chave, padrao='', cidade=''):
        try:
            from app import textossite
            return textossite.obter(chave, padrao, cidade)
        except Exception:
            # Pagina publica com o texto de fabrica e' melhor que pagina
            # publica quebrada.
            from markupsafe import Markup, escape
            return Markup(escape(padrao)) if padrao else Markup('')

    app.jinja_env.globals['texto'] = _texto_do_site

    # DADO QUE NAO VAI PARA O GIT (15/10/2026): contato, PIX etc. moram em
    # instance/dados_locais.json. No template: {{ dado_local('contato_email') }}.
    app.jinja_env.globals['dado_local'] = dadoslocais.obter

    # Telefone SEMPRE no mesmo formato na tela, nao "do jeito que foi
    # digitado" — dois cadastros do mesmo mes apareciam diferentes so porque
    # foram digitados por pessoas diferentes.
    from app.telefone import bonito as _tel_bonito
    app.jinja_env.filters['telefone'] = _tel_bonito
    login_manager.init_app(app)
    migrate.init_app(app, db)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Por favor, faca login para acessar esta pagina.'

    # ===== CRIAR DIRETORIOS NECESSARIOS =====
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER_ZIP'], exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER_TXT'], exist_ok=True)
    app.config['TEMP_EXTRACT_FOLDER'] = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp_extract')
    os.makedirs(app.config['TEMP_EXTRACT_FOLDER'], exist_ok=True)

    # ===== APLICAR PROXYFIX PARA CLOUDFLARE TUNNEL =====
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    # ===== REGISTRAR BLUEPRINTS =====
    from app.blueprints.auth import auth_bp
    from app.blueprints.upload import upload_bp, _recover_stuck_uploads, start_resume_ticker
    from app.blueprints.ranking import (
        ranking_bp, ranking_short_bp, rebuild_current_month_backups,
        migrar_patrocinadores_para_periodo,
    )
    from app.blueprints.config import config_bp
    from app.blueprints.admin import admin_bp
    from app.blueprints.athlete import athlete_bp
    from app.blueprints.api import api_bp
    from app.blueprints.conferencia import conferencia_bp
    from app.blueprints.avisos import avisos_bp
    from app.blueprints.adesao import adesao_bp
    from app.blueprints.livrocaixa import caixa_bp
    from app.blueprints.execucao import execucao_bp
    from app.blueprints.relatorios import relatorios_bp
    from app.blueprints.pedidos import pedidos_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(ranking_bp)
    app.register_blueprint(ranking_short_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(athlete_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(conferencia_bp)
    app.register_blueprint(avisos_bp)
    app.register_blueprint(adesao_bp)
    app.register_blueprint(caixa_bp)
    app.register_blueprint(execucao_bp)
    app.register_blueprint(relatorios_bp)
    app.register_blueprint(pedidos_bp)

    # RASTRO DE TODO ACESSO (16/09/2026): uma linha por requisicao em
    # `logs/acesso-AAAA-MM.log`. Existe porque um clique recusado antes da
    # funcao (sessao expirada -> 302 para o login) nao deixava rastro nenhum, e
    # foi isso que impediu de fechar o caso do "Publicar agora" de 15/09.
    try:
        from app import acesso as _acesso
        _acesso.registrar(app)
    except Exception as _e:
        app.logger.warning('NAO consegui ligar o rastro de acesso: %s', _e)

    # A TRAVA DO PATROCINADOR/AUDITOR, depois de todo blueprint registrado
    # (P-91). Allowlist, no servidor, antes da view -- esconder card nunca
    # fechou rota. Detalhe e a lista em `app/auditor.py`.
    try:
        from app import auditor as _auditor
        _auditor.instalar(app)
    except Exception as _e:
        app.logger.error('NAO consegui instalar a trava do auditor: %s', _e)

    # ===== TAREFAS DE STARTUP =====
    # Sincrono: apenas o schema (obrigatorio antes de qualquer request).
    with app.app_context():
        try:
            from app.models import ensure_runtime_schema
            ensure_runtime_schema()
        except Exception as _e:
            app.logger.warning(f'ensure_runtime_schema falhou no startup: {_e}')
        # COLUNAS DA TROCA DE E-MAIL (20/09/2026, P-103) — SINCRONO e ANTES de
        # qualquer request: o modelo `Usuario` ja declara `email_novo`,
        # `email_token` e `email_token_expiry`, e sem as colunas no banco
        # QUALQUER consulta de usuario (o login, inclusive) quebraria.
        try:
            from app import contas
            contas.ensure_schema()
        except Exception as _e:
            app.logger.error('contas.ensure_schema falhou no startup: %s', _e)
        # Patrocinador deixou de ser "da cidade" e passou a ser "do periodo".
        # Roda SINCRONO de proposito: qualquer rota que reconstrua um ranking
        # antes disso encontraria o mes sem patrocinador e apagaria o nome do
        # snapshot. Depois da primeira vez custa duas consultas.
        try:
            migrar_patrocinadores_para_periodo()
        except Exception as _e:
            db.session.rollback()
            app.logger.warning(f'migracao de patrocinadores falhou no startup: {_e}')
        # Cidade que ja tinha WAHA configurado continua na sessao "default";
        # cidade nova nasce com o slug. Trocar sozinho derrubaria a sessao que
        # hoje funciona.
        try:
            from app.whatsapp import preservar_sessao_existente
            preservar_sessao_existente()
        except Exception as _e:
            app.logger.warning(f'preservar_sessao_existente falhou no startup: {_e}')

    # Recuperacao de uploads e reconstrucao de rankings sao manutencao: rodam
    # em segundo plano para nao segurar o primeiro request apos o boot (no
    # DS215j essas tarefas custavam varios segundos no carregamento inicial).
    def _tarefas_pos_boot():
        # CRITICO: dormir ANTES de qualquer acesso a banco. O uWSGI forka os
        # workers milissegundos apos o import do app; se esta thread estiver no
        # meio de uma transacao SQLite nesse instante, os workers herdam o
        # estado do banco corrompido pelo fork e travam no primeiro request
        # (visto em producao em 2026-07-19: 504 em todos os acessos pos-boot).
        # Dormindo, o fork acontece com a thread parada e sem conexao aberta.
        time.sleep(60)
        with app.app_context():
            try:
                _recover_stuck_uploads()
            except Exception as _e:
                app.logger.warning(f'_recover_stuck_uploads falhou no pos-boot: {_e}')
            try:
                rebuild_current_month_backups()
            except Exception as _e:
                app.logger.warning(f'rebuild_current_month_backups falhou no pos-boot: {_e}')
            try:
                from app.fechamento import fechar_meses_pendentes
                fechar_meses_pendentes()
            except Exception as _e:
                db.session.rollback()
                app.logger.warning(f'fechar_meses_pendentes falhou no pos-boot: {_e}')
            try:
                from app.backupauto import rodar_backups_agendados
                rodar_backups_agendados()
            except Exception as _e:
                app.logger.warning(f'backups automaticos falharam no pos-boot: {_e}')
            app.logger.info('Tarefas pos-boot concluidas (recovery + rankings + fechamento)')

    def _rotina_diaria():
        """Fecha mes vencido e roda backup agendado uma vez por dia.

        O NAS fica ligado direto, entao nao existe "cron do usuario" garantido:
        a propria aplicacao cuida. Dorme 6h entre voltas — nada aqui e urgente
        e o DS215j agradece.
        """
        time.sleep(300)
        while True:
            with app.app_context():
                try:
                    from app.fechamento import fechar_meses_pendentes
                    fechar_meses_pendentes()
                except Exception as _e:
                    db.session.rollback()
                    app.logger.warning(f'rotina diaria (fechamento): {_e}')
                try:
                    from app.backupauto import rodar_backups_agendados
                    rodar_backups_agendados()
                except Exception as _e:
                    app.logger.warning(f'rotina diaria (backup): {_e}')
                try:
                    # ATLETA NOVO PRECISA DE CADASTRO (09/09/2026). O sexo nao
                    # vem do Strava e so' o dono sabe; sem aviso, isso so' e'
                    # descoberto meses depois olhando a lista. Um e-mail por
                    # dia, com todos juntos -- e nenhum quando nao ha ninguem.
                    from app.atletanovo import avisar_atletas_novos
                    avisar_atletas_novos()
                except Exception as _e:
                    app.logger.warning(f'rotina diaria (atleta novo): {_e}')
                try:
                    # A foto publica dos numeros do projeto (P-93). Uma vez por
                    # dia, aqui: a pagina inicial e' publica e nao pode pagar 6
                    # agregacoes por visita.
                    from app.consolidado import refazer_foto_do_dia
                    refazer_foto_do_dia()
                except Exception as _e:
                    app.logger.warning(f'rotina diaria (foto do consolidado): {_e}')
                try:
                    # A trilha "acontece sozinho" so acontece sozinha por
                    # causa desta chamada: e aqui que a mensagem do mes entra
                    # na fila sem ninguem apertar botao.
                    from app.agendamensagens import rodar_todas
                    rodar_todas()
                except Exception as _e:
                    db.session.rollback()
                    app.logger.warning(f'rotina diaria (mensagens): {_e}')
                try:
                    # FAXINA DA FILA (26/08/2026). `limpar_antigas` existia
                    # desde sempre e NUNCA foi chamada por ninguem -- medido
                    # no dia: 28,28 MB de imagem ja entregue guardados em 53
                    # linhas, METADE do banco, que dobrou de 25 para 52 MB em
                    # duas semanas. Banco maior e' mais disco lido e mais
                    # memoria gasta num aparelho de 490 MB.
                    from app import filamensagens as _fila
                    _fila.aliviar_imagens_entregues()
                    _fila.limpar_antigas()
                except Exception as _e:
                    db.session.rollback()
                    app.logger.warning(f'rotina diaria (faxina da fila): {_e}')
                try:
                    from app.blueprints.upload import faxina_de_jobs
                    faxina_de_jobs()
                except Exception as _e:
                    app.logger.warning(f'rotina diaria (faxina de jobs): {_e}')
                # A CONFERENCIA SAIU DAQUI (29/08/2026).
                # Esta rotina dorme 6 horas contadas A PARTIR DO BOOT, e o
                # boot anda a cada `.py` salvo (py-autoreload). A conferencia
                # caia em hora aleatoria -- 22:41 em 28/08 -- e o aviso
                # chegava quando nao dava mais para fazer nada com ele.
                # Agora ela mora no vigia, que passa de 15 em 15 min e por
                # isso consegue acertar a janela antes da publicacao.
            time.sleep(6 * 3600)

    def _vigia_da_automacao():
        """Olha a automacao de 15 em 15 min (pendencia P-35).

        POR QUE AQUI E NAO NO DELL: o Dell dorme. Em 02/08 a fila falhou quatro
        vezes seguidas e ninguem soube, porque quem falhou era justamente quem
        deveria avisar. Vigia tem que ser quem fica ligado.

        Dorme 240s antes da primeira passada para nao concorrer com o fork dos
        workers do uWSGI no boot (mesmo motivo comentado no _tarefas_pos_boot).
        """
        time.sleep(240)
        while True:
            try:
                with app.app_context():
                    from app import vigia
                    vigia.rodar()
                    # A CONFERENCIA PEGA CARONA AQUI (29/08/2026): ela
                    # precisa de uma passada frequente para acertar a
                    # janela antes da publicacao, e este relogio ja existe.
                    # Criar um terceiro temporizador para isso seria gastar
                    # a maquina de novo pelo mesmo trabalho.
                    try:
                        from app import conferenciaauto
                        conferenciaauto.rodar_agendadas()
                    except Exception as _ec:
                        db.session.rollback()
                        app.logger.warning(f'conferencia automatica: {_ec}')
                    # ATUALIZACAO DE PERFIS SEM CLIQUE (P-128, 07/09/2026).
                    # Continua de onde o teto interrompeu, e recomeca
                    # sozinha na frequencia definida. So anda com folga de
                    # cota -- ela mesma confere. Pega carona neste relogio
                    # porque ele ja existe e passa de 15 em 15 min.
                    try:
                        from app.blueprints.admin import (
                            tocar_atualizacao_de_perfis)
                        # SO CIDADE ATIVA (17/09/2026, regra do dono). Antes
                        # percorria toda linha de `Config`: Minacu, inativa e
                        # sem cookie do Strava, falhava sempre, nunca ganhava
                        # a marca de "ja rodou" e abria um processo novo no
                        # NAS 4 vezes por hora, o dia inteiro. Cidade inativa
                        # nao tem nada a fazer aqui.
                        from app import consolidado as _cons
                        for _c in _cons.cidades_ativas():
                            if tocar_atualizacao_de_perfis(_c):
                                break      # uma cidade por passada
                    except Exception as _ep:
                        db.session.rollback()
                        app.logger.warning(f'atualizacao de perfis: {_ep}')
            except Exception as _e:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                app.logger.warning(f'vigia da automacao: {_e}')
            time.sleep(15 * 60)

    # PROCESSO DE TRABALHO LONGO NAO E' SERVIDOR (26/08/2026).
    #
    # Desde hoje a importacao de atleta roda num processo separado
    # (`app/jobrunner.py`), que tambem precisa do app para chegar ao banco e aos
    # modelos. Sem esta trava, esse processo subiria um SEGUNDO vigia, uma
    # SEGUNDA rotina diaria e um SEGUNDO ticker de retomada -- e-mail dobrado,
    # backup dobrado e, pior, um ticker reanimando o job que o proprio processo
    # esta executando. Ele quer o app; nao quer as rotinas do servidor.
    if os.environ.get('MOVIMENTE_SO_JOB') == '1':
        app.logger.info('Modo trabalho longo: rotinas de fundo do site nao sobem aqui.')
    else:
        threading.Thread(target=_rotina_diaria, daemon=True, name='rotina-diaria').start()

        threading.Thread(target=_vigia_da_automacao, daemon=True, name='vigia').start()

        threading.Thread(target=_tarefas_pos_boot, daemon=True, name='pos-boot').start()

        try:
            # Retomada autonoma de jobs longos (nao depende do navegador aberto).
            start_resume_ticker(app)
        except Exception as _e:
            app.logger.warning(f'start_resume_ticker falhou no startup: {_e}')
        # RELOGIO DA RODADA (21/09/2026): pede a rodada no MINUTO do horario da
        # tela. Ate aqui quem pedia era a passada de 15 em 15 min do vigia, e o
        # ranking podia sair ate 14 minutos atrasado — medido no teste das
        # 00:25 (pedido as 00:25:51). O vigia continua como rede de seguranca.
        try:
            from app import relogiorodada
            relogiorodada.iniciar(app)
        except Exception as _e:
            app.logger.warning(f'relogio da rodada nao ligou: {_e}')

    # ===== ROTAS PRINCIPAIS =====
    # outro-dominio.exemplo.com aponta para ESTA mesma aplicacao (atras do
    # Cloudflare Zero Trust). Quem entra por la cai direto no painel da
    # automacao da propria cidade, em vez da tela de boas-vindas. Uma
    # aplicacao so: nada de segundo servidor para manter.
    _HOSTS_AUTOMACAO = ('automayk.',)

    def _veio_pelo_automayk():
        host = (request.host or '').lower()
        return host.startswith(_HOSTS_AUTOMACAO)

    def _pix_do_projeto():
        """A chave PIX que a tela de apoio mostra.

        Mesma regra da tela de login: e' a da cidade do dono da instalacao (o
        master supremo). Antes era uma cidade fixa chamada "Sistema", que deixou
        de existir na renomeacao de 23/07 -- e desde entao ninguem via o PIX
        certo. Falhar aqui NAO pode derrubar a porta de entrada: sem PIX, a
        secao de apoio simplesmente nao aparece.
        """
        try:
            from app.models import Usuario, Config as _Config, TIPO_SUPREMO
            dono = db.session.query(Usuario).filter_by(
                tipo=TIPO_SUPREMO, ativo=True).first()
            if not dono or not dono.cidade:
                return None
            cfg = db.session.query(_Config).filter_by(cidade=dono.cidade).first()
            if cfg and cfg.chave_pix:
                return cfg
        except Exception as exc:
            app.logger.warning('nao consegui ler o PIX do projeto: %s', exc)
        return None

    def _slug_da_cidade_do_pix(cfg):
        """Slug da cidade, para o link da transparencia. Vazio nao quebra a tela."""
        try:
            from app.blueprints.ranking import _slugify_city
            return _slugify_city(cfg.cidade) if cfg and cfg.cidade else ''
        except Exception:
            return ''

    @app.route('/login')
    def login_atalho():
        """`/login` existe porque e' o que as pessoas digitam (12/09/2026).

        O login mora em `/auth/login` (o blueprint tem prefixo `/auth`). Quem
        digitava `movementes.engekon.com/login` levava 404 — aconteceu duas
        vezes em setembro, uma delas com o dono tentando entrar pelo celular
        depois de o navegador esquecer o login. Desviar e' mais barato que
        explicar. `next` segue junto para nao perder o destino.
        """
        return redirect(url_for('auth.login', **request.args.to_dict()))

    @app.route('/')
    def index():
        """A PORTA DE ENTRADA DO PROJETO (P-94, 28/08/2026).

        Ate hoje `/` nao era uma pagina: era um desviador. Quem nao tinha conta
        caia direto no login, e o projeto ficava invisivel para quem chegasse de
        fora -- que e' exatamente o "circuito privado" que reprova o projeto no
        Ministerio do Esporte (Decreto 12.861/2026, art. 25, I).

        Agora ela e' uma pagina publica de verdade, com os numeros do projeto na
        primeira tela. Quem esta logado continua com o menu de sempre no
        cabecalho, e o atalho para o painel aparece aqui mesmo.
        """
        try:
            if current_user.is_authenticated and _veio_pelo_automayk()                     and current_user.pode_administrar:
                return redirect(url_for('admin.painel_automayk'))

            from app import consolidado
            foto = consolidado.foto_do_dia()

            # Filtro de periodo nas tabelas de km por pessoa (P-122,
            # 28/08/2026). A foto do dia so' guarda "desde o comeco" (cache
            # de 1x/dia, NAS de 490 MB) -- pedir outro periodo refaz SO' as
            # classificacoes, ao vivo, sem tocar no resto da foto (totais,
            # historia, patrocinadores continuam vindo do cache).
            periodo_pessoas = (request.args.get('periodo_pessoas') or 'tudo').strip()
            if periodo_pessoas not in consolidado.PERIODOS:
                periodo_pessoas = 'tudo'
            if periodo_pessoas != 'tudo':
                foto = dict(foto)
                foto['classificacoes'] = consolidado.classificacoes_da_capa(
                    limite=10, periodo=periodo_pessoas)

            pix = _pix_do_projeto()
            return render_template(
                'publico/home.html', foto=foto, tabela=foto.get('tabela') or {},
                pix=pix, slug_da_cidade=_slug_da_cidade_do_pix(pix),
                periodo_pessoas=periodo_pessoas)
        except Exception as e:
            app.logger.error(f'Erro na rota raiz: {str(e)}', exc_info=True)
            # A porta de entrada nunca devolve tela de erro para visitante: cai
            # no login, que sempre funciona.
            return redirect(url_for('auth.login'))

    @app.route('/automayk')
    def automayk():
        """Atalho curto para o painel da automação (o endereço do domínio)."""
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login', next='/automayk'))
        return redirect(url_for('admin.painel_automayk'))

    @app.route('/welcome', endpoint='welcome_page')
    @login_required
    def welcome_page():
        try:
            return render_template('welcome.html')
        except Exception as e:
            app.logger.error(f'Erro ao renderizar welcome.html: {str(e)}', exc_info=True)
            return f'Erro ao carregar pagina de boas-vindas: {str(e)}', 500

    @app.route('/favicon.ico')
    def favicon():
        return app.send_static_file('images/favicon.svg')

    # ===== CONTEXT PROCESSORS =====
    @app.context_processor
    def inject_endereco():
        """O endereco do site, para qualquer tela (P-102).

        Texto de tela com comando copiavel ficava com o dominio escrito a mao e
        viraria instrucao errada no dia da migracao. Agora todo template tem
        `base_url_do_site` e `dominio_do_site` sem precisar pedir.
        """
        try:
            from app.enderecos import base_url, dominio
            return {'base_url_do_site': base_url(), 'dominio_do_site': dominio()}
        except Exception:
            return {'base_url_do_site': '', 'dominio_do_site': ''}

    # O DECORADOR DESTA FUNCAO TINHA IDO PARAR EM CIMA DA DE CIMA (19/09/2026,
    # regra 4 do CLAUDE.md): `inject_endereco` ficou com dois e esta com
    # nenhum — nunca rodava. Sumiram o seletor de cidade do master supremo e
    # as bolinhas de feedback/revisao do cabecalho, sem erro nenhum.
    @app.context_processor
    def inject_admin_badges():
        """Contadores para a navbar (admin+): feedbacks novos e revisoes de
        anticheat pendentes da cidade. Alimentam as bolinhas vermelhas (que
        somem quando o admin resolve/le os itens) e o alerta ao copiar ranking.
        """
        dados = {'feedback_novas': 0, 'revisoes_pendentes': 0,
                 'cidades_do_supremo': []}
        try:
            if current_user.is_authenticated and current_user.pode_trocar_cidade:
                from app.models import Config as _Config
                dados['cidades_do_supremo'] = [
                    row[0] for row in
                    db.session.query(_Config.cidade).order_by(_Config.cidade).all()
                ]
        except Exception:
            pass
        try:
            if current_user.is_authenticated and current_user.pode_administrar:
                from app.models import Feedback, FlagAnticheat, Activity
                dados['feedback_novas'] = Feedback.query.filter_by(
                    cidade=current_user.cidade_ativa, status='nova'
                ).count()
                # Flags que pedem acao do admin (em revisao) na cidade dele.
                dados['revisoes_pendentes'] = (
                    FlagAnticheat.query
                    .join(Activity, FlagAnticheat.atividade_id == Activity.id)
                    .filter(Activity.cidade == current_user.cidade_ativa,
                            FlagAnticheat.status == 'em_revisao')
                    .count()
                )
        except Exception:
            pass
        return dados

    # ===== USER LOADER =====
    @login_manager.user_loader
    def load_user(user_id):
        from app.models import Usuario
        return Usuario.query.get(int(user_id))

    # ===== ERROR HANDLERS =====
    @app.errorhandler(404)
    def not_found(error):
        app.logger.warning(f'Erro 404: {request.url}')
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_error(error):
        # Sem o rollback, a sessao fica "suja" e TODOS os requests seguintes
        # daquele worker falham em cadeia (PendingRollbackError) — um erro
        # isolado virava o site inteiro fora do ar.
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.error(f'Erro 500: {str(error)}', exc_info=True)
        return render_template('errors/500.html'), 500

    @app.teardown_request
    def _encerrar_sessao(exc=None):
        """Fecha a sessao do banco no fim de cada request.

        Com dois dispositivos no mesmo login, uma sessao deixada aberta segura
        a conexao (e o lock de escrita do SQLite) ate o pool reciclar.
        """
        if exc is not None:
            try:
                db.session.rollback()
            except Exception:
                pass
        try:
            db.session.remove()
        except Exception:
            pass

    return app

__all__ = ['create_app', 'db']
